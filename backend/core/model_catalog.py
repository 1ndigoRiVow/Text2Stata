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
# 下面是 2026-10-15 在 12 道模板路由题上的实测结果，
# 推荐默认值由此产生，不靠综合榜单猜：
#
#   模型                  准确率       平均延迟   JSON 失败
#   qwen3.7-flash        11/12 91.7%   22.1s      0     ← 选它
#   gpt-6.1-sol          11/12 91.7%   36.0s      0     准确但慢一倍
#   glm-5.3-flash         7/12 58.3%   31.4s      2     爱往 hdfe 上凑
#   deepseek-v4-flash     5/12 41.7%   10.7s      7     渠道不稳，大量超时
#
# deepseek 的两条数据要合看：成功那 5 次确实快（10.7s），
# 但 12 次里 7 次根本没回来。这是中转站给它的渠道问题，不是模型能力问题。
RECOMMENDED_MODEL = "qwen3.7-flash"

CURATED_MODELS = [
    {
        "id": "qwen3.7-flash",
        "label": "Qwen3.7-Flash",
        "tier": "recommended",
        "note": "实测准确率最高且零失败，严格 JSON 输出支持好。日常首选。",
    },
    {
        "id": "gpt-6.1-sol",
        "label": "GPT-6.1-Sol",
        "tier": "recommended",
        "note": "准确率与推荐款持平，但慢一倍、更贵。指令遵循强，文献式表述更准。",
    },
    {
        "id": "deepseek-v4-pro",
        "label": "DeepSeek-V4-Pro",
        "tier": "high",
        "note": "需求表述含糊、变量一堆时更稳，价格约为 flash 的 3 倍。",
    },
    {
        "id": "deepseek-v4-flash",
        "label": "DeepSeek-V4-Flash",
        "tier": "high",
        "note": "成功时很快，但实测 12 次里 7 次超时或返回非 JSON，渠道不稳定。",
    },
    {
        "id": "glm-5.3-flash",
        "label": "GLM-5.3-Flash",
        "tier": "cheap",
        "note": "便宜档。实测有往 hdfe 上乱选的倾向，只适合简单需求。",
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


def build_catalog(api_url: str, api_key: str, timeout: int = 15) -> dict:
    """
    组装前端要的模型目录。

    结构：
        {
          "recommended": "deepseek-v4-flash",
          "curated": [ {id, label, tier, note, available}, ... ],
          "others": [ {family, models:[id, ...]}, ... ],
          "source": "remote" | "cached" | "curated_only",
        }

    curated 里的 available 表示这个模型在当前中转站是否真的挂出来了；
    没挂出来也照样列（方便用户看清"我配的这个站没有它"），只是标灰。
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
                # 分组名由后端给，前端不要再抄一份，否则两边迟早不一致
                tier_label=TIER_LABELS.get(tier, tier),
            )
        )

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
