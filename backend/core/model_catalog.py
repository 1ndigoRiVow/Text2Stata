"""
模型目录。

分两层：

1. **策展层（CURATED）**——人工挑过、在本项目任务上验证过的模型，带推荐标记和
   一句话说明。**推荐默认值只从这一层产生**，不参与任何自动排序。

2. **发现层（remote）**——拿当前 key 去中转站打 `/v1/models`，把策展层没列到、
   但确实能用的模型补进"其他可用"。

这么分是因为：中转站今天上架什么、下架什么，不由我们控制。如果让默认值跟着
远程列表飘，用户的体验会随供应商的库存变化；策展层把"推荐什么"这件事钉死，
发现层只负责"还有什么能选"。

本项目 LLM 的任务形状（决定了选型标准）：
    输入：中文需求 + 数据 schema + 模板注册表（约 3~6k token）
    输出：{selected_template, variable_mapping, reasoning}（约 0.3k token）
本质是 closed-set 分类 + 槽位填充，不吃推理能力。所以按
**JSON 稳定性 > 中文 > 速度 > 价格** 排序，而不是按综合 benchmark 排名。
"""

from __future__ import annotations

import hashlib
import re
import time

import requests


# --------------------------------------------------------------------------
# 策展层
# --------------------------------------------------------------------------
# 每个模型的 id 都已在 globalai.vip 的 /v1/models 中确认存在（2026-10-15 实测）。
# 改这个列表 = 改用户看到的推荐，动之前先在 eval/model_picker 里跑一遍。
#
# 下面是两轮实测（模板路由题），推荐默认值由此产生，不靠综合榜单猜：
#
#   2026-10-15（13 模板，12 题）      准确率      延迟    JSON 失败
#   qwen3.7-flash                   11/12 91.7%  22.1s    0
#   gpt-6.1-sol                     11/12 91.7%  36.0s    0
#   glm-5.3-flash                    7/12 58.3%  31.4s    2
#   deepseek-v4-flash                5/12 41.7%  10.7s    7
#
#   2026-10-22（28 模板，18 题）      准确率      延迟    JSON 失败
#   gpt-6.1-sol                     17/18 94.4%  17.7s    0     ← 选它
#   deepseek-v4-flash               16/18 88.9%   8.8s    1
#   glm-5.3-flash                   15/18 83.3%  12.0s    0
#   qwen3.7-flash                    0/18  0.0%   —       18    渠道已下架
#
# 换推荐值的直接原因：qwen3.7-flash 在 10-22 全部 18 次调用都返回
# HTTP 500「分组 auto 下模型 qwen3.7-flash 的可用渠道不存在」。
# 它**仍然挂在 /v1/models 列表里** —— 列表存在 ≠ 可调用，见 probe_callable。
#
# gpt-6.1-sol 是两轮里唯一两次都拿第一的。deepseek 从 41.7% 跳到 88.9%，
# 波动全来自中转站的渠道分配，所以宁可选两次都稳的那个：
# 默认模型的第一指标是"每次都给得出能用的答案"，不是"跑通时有多快"。
RECOMMENDED_MODEL = "gpt-6.1-sol"

CURATED_MODELS = [
    {
        "id": "gpt-6.1-sol",
        "label": "GPT-6.1-Sol",
        "tier": "recommended",
        "note": "两轮实测都是第一，JSON 零失败。需求表述含糊时最稳，代价是慢一些、贵一些。",
    },
    {
        "id": "deepseek-v4-flash",
        "label": "DeepSeek-V4-Flash",
        "tier": "recommended",
        "note": "快，中文好。但渠道可用性随中转站调度大幅波动（实测 41.7% → 88.9%），别押它做唯一依赖。",
    },
    {
        "id": "glm-5.3-flash",
        "label": "GLM-5.3-Flash",
        "tier": "cheap",
        "note": "便宜且快。有往高维固定效应上乱选的倾向，只适合简单的、指向明确的需求。",
    },
    {
        "id": "deepseek-v4-pro",
        "label": "DeepSeek-V4-Pro",
        "tier": "high",
        "note": "pro 档。变量一堆、需求绕的时候比 flash 稳，价格约为 flash 的 3 倍。",
    },
    {
        "id": "qwen3.7-flash",
        "label": "Qwen3.7-Flash",
        "tier": "high",
        "note": "2026-10-15 时是首选，10-22 起该渠道在中转站下线（列表里还在，但调不通）。留着观察，恢复后它依然是最优解。",
    },
]

TIER_LABELS = {
    "recommended": "推荐",
    "high": "高精度（更贵）",
    "cheap": "省钱档",
}

# 只保留对话文本模型：中转站目录里还混着 embedding / TTS / 图像 / 重排等
_NON_TEXT = re.compile(
    r"(embedding|bge-|tts|whisper|dall-e|image|moderation|rerank|speech|audio|video|clip)",
    re.IGNORECASE,
)
# 日期快照类噪音（claude-opus-4-5-20251101 这类），展示价值低于同族带名字的版本
_SNAPSHOT = re.compile(r"-(19|20)\d{6}$")

_FAMILIES = [
    ("OpenAI", ("gpt-", "chatgpt")),
    ("Anthropic Claude", ("claude-",)),
    ("Google Gemini", ("gemini-",)),
    ("DeepSeek", ("deepseek",)),
    ("阿里通义千问", ("qwen",)),
    ("智谱 GLM", ("glm-",)),
    ("月之暗面 Kimi", ("kimi", "moonshot")),
    ("MiniMax", ("minimax",)),
    ("xAI Grok", ("grok",)),
    ("字节豆包", ("doubao",)),
]


def _family_of(model_id: str) -> str:
    lowered = model_id.lower()
    for name, prefixes in _FAMILIES:
        if lowered.startswith(prefixes):
            return name
    return "其他"


def is_text_model(model_id: str) -> bool:
    """过滤掉非对话文本模型。"""
    return not _NON_TEXT.search(model_id)


# --------------------------------------------------------------------------
# 探活：列表存在 ≠ 可调用
# --------------------------------------------------------------------------
# 2026-10-22 实测：qwen3.7-flash 明明在 /v1/models 里，实际调用却返回
# HTTP 500「分组 auto 下模型 qwen3.7-flash 的可用渠道不存在」。
# 中转站把模型从渠道撤了，但没从目录里摘掉。只看列表会把下架的模型标成"可用"，
# 用户选了才发现调不通。所以对策展层（就那几个，值得）真打一次最小对话。
_PROBE_CACHE: dict[tuple[str, str], tuple[float, bool | None]] = {}


def probe_callable(api_url: str, api_key: str, model_id: str, timeout: int = 12) -> bool | None:
    """
    真打一次最小对话（8 token），确认这个模型能不能调通。

    返回 True / False；网络异常等"没问出结果"的情况返回 None——
    None 表示不确定，调用方不应据此把模型标成不可用。
    """
    if not api_url or not api_key:
        return None

    base = api_url.rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]

    try:
        response = requests.post(
            base + "/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model_id,
                "messages": [{"role": "user", "content": "1"}],
                "max_tokens": 8,
            },
            timeout=timeout,
        )
        return response.status_code == 200
    except Exception:
        return None


def _probe_cached(api_url: str, api_key: str, model_id: str) -> bool | None:
    """带缓存的探活，TTL 与远程列表一致。"""
    cache_key = (_key_fingerprint(api_key), model_id)
    now = time.time()
    hit = _PROBE_CACHE.get(cache_key)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]

    result = probe_callable(api_url, api_key, model_id)
    _PROBE_CACHE[cache_key] = (now, result)
    return result


def _annotate_callable(curated: list[dict], api_url: str, api_key: str) -> None:
    """并发探活策展层，就地写回 callable 与 available。"""
    if not api_url or not api_key or not curated:
        return

    from concurrent.futures import ThreadPoolExecutor

    ids = [item["id"] for item in curated]
    with ThreadPoolExecutor(max_workers=len(ids)) as pool:
        results = list(pool.map(lambda m: _probe_cached(api_url, api_key, m), ids))

    for item, result in zip(curated, results):
        item["callable"] = result
        # 只有明确调不通才标不可用；探活本身失败（None）时不改判
        if result is False:
            item["available"] = False


def fetch_remote_models(api_url: str, api_key: str, timeout: int = 15) -> list[str]:
    """
    向中转站查询可用模型列表。失败时抛异常，由调用方降级处理。

    api_url 可能是 .../v1/chat/completions，统一换成 .../v1/models。
    """
    if not api_url or not api_key:
        raise ValueError("缺少 api_url 或 api_key")

    base = api_url.rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]

    response = requests.get(
        base + "/models",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=timeout,
    )
    response.raise_for_status()

    data = response.json().get("data") or []
    return sorted({item.get("id", "") for item in data if item.get("id")})


# --------------------------------------------------------------------------
# 缓存：避免同一 key 反复打 /v1/models
# --------------------------------------------------------------------------
_CACHE: dict[tuple[str, str], tuple[float, list[str]]] = {}
_CACHE_TTL = 300  # 秒


def _key_fingerprint(api_key: str) -> str:
    """只留哈希，绝不明文缓存密钥。"""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]


def get_remote_models(api_url: str, api_key: str, timeout: int = 15) -> tuple[list[str], str]:
    """
    带缓存地取远程模型列表。

    返回 (模型 id 列表, 状态)：状态为 ok / empty_key / error。
    """
    if not api_url or not api_key:
        return [], "empty_key"

    cache_key = (_key_fingerprint(api_key), api_url)
    now = time.time()
    hit = _CACHE.get(cache_key)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1], "ok"

    try:
        models = fetch_remote_models(api_url, api_key, timeout=timeout)
    except Exception:
        # 远程拿不到就退回旧缓存（哪怕过期），再没有就空列表
        if hit:
            return hit[1], "ok"
        return [], "error"

    _CACHE[cache_key] = (now, models)
    return models, "ok"


def build_catalog(api_url: str, api_key: str, timeout: int = 15,
                  probe: bool = True) -> dict:
    """
    组装前端要的模型目录。

    结构：
        {
          "recommended": "gpt-6.1-sol",
          "curated": [ {id, label, tier, note, available, callable}, ... ],
          "others": [ {family, models:[id, ...]}, ... ],
          "source": "remote" | "cached" | "curated_only",
        }

    curated 里的 available 表示这个模型在当前中转站是否真的能用：
    既要在 /v1/models 列表里，也要探活调得通（列表存在 ≠ 可调用）。
    没挂出来也照样列（方便用户看清"我配的这个站没有它"），只是标灰。

    probe=False 可跳过探活，测试里用它避免真发网络请求。
    """
    remote, status = get_remote_models(api_url, api_key, timeout=timeout)
    remote_set = set(remote)

    curated = []
    for item in CURATED_MODELS:
        tier = item["tier"]
        curated.append(
            dict(
                item,
                available=item["id"] in remote_set,
                callable=None,
                # 分组名由后端给，前端不要再抄一份，否则两边迟早不一致
                tier_label=TIER_LABELS.get(tier, tier),
            )
        )

    if probe:
        _annotate_callable(curated, api_url, api_key)

    curated_ids = {item["id"] for item in CURATED_MODELS}
    buckets: dict[str, list[str]] = {}
    for model_id in remote:
        if model_id in curated_ids or not is_text_model(model_id) or _SNAPSHOT.search(model_id):
            continue
        buckets.setdefault(_family_of(model_id), []).append(model_id)

    others = [
        {"family": family, "models": sorted(models)}
        for family, models in sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    ]

    source = "remote" if remote else ("empty_key" if status == "empty_key" else "curated_only")
    return {
        "recommended": RECOMMENDED_MODEL,
        "curated": curated,
        "others": others,
        "source": source,
    }
