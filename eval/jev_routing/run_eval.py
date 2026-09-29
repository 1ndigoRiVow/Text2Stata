# -*- coding: utf-8 -*-
"""
Jev 路由能力旁路评测器

做一件事：拿同一批测试题，分别喂给两个方案，比较路由质量。

  方案 A（baseline）: Text2Stata 现有的一体化 LLM 调用（backend/core/llm_agent.py）
  方案 B（jev）     : Jev 的 choice 原语，标签空间 = 13 个模板 + 两个拒答标签

**本脚本完全旁路**：只读项目文件，不写任何东西回项目，不修改 .env，不启动 Flask。

用法：
    # 先看两个方案各自会被问什么，不花任何 API 钱
    python run_eval.py --dry-run

    # 正式跑（需要 key，见下方配置）
    python run_eval.py --arms baseline,jev
    python run_eval.py --arms jev --limit 5          # 先拿 5 题试水

配置（任选其一）：
  1. 环境变量：
       JEVMODEL_API_KEY=<你的 key>            # jevmodel.org（有免费额度，适合评估）
       JEV_BASE_URL=https://jevmodel.org/v1/systemone
     TypeSafe 官方直连则设：
       JEV_BASE_URL=https://jevmodel.net/v1/systemone
     或经 OpenRouter / Vercel AI Gateway 中转（价格同官方）。
  2. 或写进 eval/jev_routing/.env.local（本目录已被 .gitignore 覆盖）

  方案 A 复用项目根目录 .env 里的 TEXT2STATA_API_KEY_LINE1 / TEXT2STATA_API_URL。
  模型默认用较贵的一档（--baseline-model 可改），确保 baseline 不因模型弱而吃亏。

输出：
  results/<时间戳>/
      raw_jev.jsonl        方案 B 每题原始返回
      raw_baseline.jsonl   方案 A 每题原始返回
      report.md            对比报告（含分类维度、成本与延迟）
"""

import argparse
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent
RESULTS_DIR = HERE / "results"

sys.path.insert(0, str(HERE))
from test_cases import TEMPLATE_CATALOG, ASK_USER, NO_TEMPLATE, all_cases  # noqa: E402

# ----------------------------------------------------------------------
# 配置读取
# ----------------------------------------------------------------------
TEMPLATE_KEYS = list(TEMPLATE_CATALOG.keys())
ROUTE_OPTIONS = TEMPLATE_KEYS + [ASK_USER, NO_TEMPLATE]

# 标签的中文说明，作为 choice criteria 与 baseline prompt 的共用素材
LABEL_CRITERIA = {
    k: f"{v['name']}：{v['when']}" for k, v in TEMPLATE_CATALOG.items()
}
LABEL_CRITERIA[ASK_USER] = "需求信息不足（没说清变量或目的），应该先回问用户而不是猜"
LABEL_CRITERIA[NO_TEMPLATE] = "不属于任何模板能力范围（导数据、画图、写文字、域外问题等）"


def load_kv_env(path: Path):
    """极简 .env 读取，避免引入 python-dotenv 依赖。"""
    data = {}
    if not path.exists():
        return data
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        data[k.strip()] = v.strip().strip('"').strip("'")
    return data


def resolve_config(args):
    """把三个来源合并：项目 .env → 本目录 .env.local → 进程环境变量（优先级递增）。"""
    cfg = {}
    cfg.update(load_kv_env(PROJECT_ROOT / ".env"))
    cfg.update(load_kv_env(HERE / ".env.local"))
    for k, v in os.environ.items():
        if k.startswith(("TEXT2STATA_", "JEV", "JEVMODEL")):
            cfg[k] = v
    return cfg


# ----------------------------------------------------------------------
# 方案 A：项目现有的一体化 LLM 调用（复刻 llm_agent 的 prompt 结构）
# ----------------------------------------------------------------------
def call_baseline(cfg, question, schema_text, model, timeout):
    api_url = cfg.get("TEXT2STATA_API_URL", "https://globalai.vip/v1/chat/completions")
    api_key = cfg.get("TEXT2STATA_API_KEY_LINE1", "")
    # 在原有 prompt 上补一句"信息不足时拒答"，否则 baseline 永远不可能输出拒答标签，
    # 对比会失真。这是为了让两个方案在同等前提下比赛。
    system_prompt = f"""
You are a professional econometrics assistant and the planning engine of Text2Stata.
Your job is to convert a user's natural language analysis request into strict JSON.

Available dataset variables:
{schema_text}

Available Stata template registry:
{json.dumps({k: {"name": v["name"], "description": v["when"]} for k, v in TEMPLATE_CATALOG.items()}, ensure_ascii=False, indent=2)}

Output requirements:
Return valid JSON only. Do not include markdown fences or explanatory text outside JSON.
The JSON must contain:
1. "selected_template": string. Must be one key from the registry, or exactly "{ASK_USER}" when the
   request lacks the information needed to choose (e.g. variables not specified), or exactly
   "{NO_TEMPLATE}" when the request is outside the scope of every template.
2. "variable_mapping": object. Include "y", "x", "controls", "id", "time".
   Use an empty string or empty array when a field is not mentioned.
3. "reasoning": string. Briefly explain the choice.
"""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"用户需求: {question}"},
        ],
        "temperature": 0.1,
        "max_tokens": 2000,
        "response_format": {"type": "json_object"},
    }
    t0 = time.time()
    try:
        r = requests.post(
            api_url,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            json=payload,
            timeout=timeout,
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"].strip()
        content = re.sub(r"^```(?:json)?|```$", "", content, flags=re.MULTILINE).strip()
        data = json.loads(content)
        return {
            "ok": True,
            "route": data.get("selected_template"),
            "reasoning": data.get("reasoning", ""),
            "mapping": data.get("variable_mapping", {}),
            "latency_ms": round((time.time() - t0) * 1000),
            "raw": data,
        }
    except Exception as exc:
        return {
            "ok": False,
            "route": None,
            "error": f"{type(exc).__name__}: {exc}",
            "latency_ms": round((time.time() - t0) * 1000),
        }


# ----------------------------------------------------------------------
# 方案 B：Jev choice 原语
# ----------------------------------------------------------------------
def call_jev(cfg, question, schema_text, model, timeout):
    base_url = cfg.get("JEV_BASE_URL", "https://jevmodel.org/v1/systemone")
    api_key = cfg.get("JEVMODEL_API_KEY", "") or cfg.get("JEV_API_KEY", "")
    if not api_key:
        return {"ok": False, "route": None, "error": "缺少 JEVMODEL_API_KEY", "latency_ms": 0}

    # state 上限 8000 字符，装载 schema + 需求
    state = f"数据集可用变量：\n{schema_text}\n\n用户的分析需求：\n{question}"
    state = state[:7900]

    payload = {
        "model": model,
        "state": state,
        "questions": {
            "route": {
                "type": "choice",
                "instructions": (
                    "该用户的统计分析需求，应该交给哪个 Stata 分析模板处理？"
                    "如果需求信息不足或超出能力范围，就选对应的拒答项。"
                ),
                "criteria": LABEL_CRITERIA,
            }
        },
    }
    t0 = time.time()
    try:
        r = requests.post(
            base_url,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            json=payload,
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
        block = data.get("route") or data.get("answers", {}).get("route") or {}
        picked = block.get("choice") or block.get("selected") or block.get("value")
        dist = block.get("probabilities") or block.get("distribution") or block.get("criteria") or {}
        return {
            "ok": True,
            "route": picked,
            "confidence": block.get("confidence"),
            "distribution": dist,
            "latency_ms": round((time.time() - t0) * 1000),
            "raw": data,
        }
    except Exception as exc:
        return {
            "ok": False,
            "route": None,
            "error": f"{type(exc).__name__}: {exc}",
            "latency_ms": round((time.time() - t0) * 1000),
        }


# ----------------------------------------------------------------------
# 打分
# ----------------------------------------------------------------------
def score_case(case, route):
    """
    返回 (判定, 说明)。判定取值：
      correct      命中 expected 集合
      over_route   题目期望拒答，但路由到了某个模板（幻觉路由）
      under_route  题目期望模板，但给出了拒答
      wrong_route  路由到了别的模板
      no_output    没有产出（调用失败/解析失败）
    """
    if route is None:
        return "no_output", "无输出"
    expected = set(case["expected"])
    if route in expected:
        return "correct", "命中"
    if route == ASK_USER and ASK_USER not in expected:
        return "over_route", f"不该拒绝却拒绝（期望 {sorted(expected)}）"
    if route == NO_TEMPLATE and NO_TEMPLATE not in expected:
        return "over_route", f"不该拒绝却拒绝（期望 {sorted(expected)}）"
    if route in (ASK_USER, NO_TEMPLATE) and expected & set(TEMPLATE_KEYS):
        return "under_route", f"该路由却拒答（期望 {sorted(expected)}）"
    return "wrong_route", f"选中 {route}，期望 {sorted(expected)}"


def schema_text_from_project():
    """优先从项目里取真实 schema，取不到就用占位说明。"""
    try:
        import pyreadstat
        dta = PROJECT_ROOT / "testdata.dta"
        _, meta = pyreadstat.read_dta(str(dta))
        cols = list(meta.column_names)
        labels = meta.column_names_to_labels
        lines = [f"- {c}（{labels.get(c, '')}）" if labels.get(c) else f"- {c}" for c in cols]
        return f"共 {len(cols)} 个变量：\n" + "\n".join(lines)
    except Exception:
        return "（未能读取 testdata.dta，使用占位）tfp, focus, lev, size, growth, age, rnd, id, year, soe ..."


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------
def run_arm(arm_name, cases, cfg, schema_text, args):
    raw_path = args.outdir / f"raw_{arm_name}.jsonl"
    records = []
    with open(raw_path, "w", encoding="utf-8") as fh:
        for i, case in enumerate(cases, 1):
            if arm_name == "jev":
                res = call_jev(cfg, case["question"], schema_text, args.jev_model, args.timeout)
            else:
                res = call_baseline(cfg, case["question"], schema_text, args.baseline_model, args.timeout)
            verdict, note = score_case(case, res.get("route"))
            rec = {
                "id": case["id"],
                "category": case["category"],
                "tags": case["tags"],
                "question": case["question"],
                "expected": case["expected"],
                "got": res.get("route"),
                "verdict": verdict,
                "note": note,
                "confidence": res.get("confidence"),
                "latency_ms": res.get("latency_ms"),
                "error": res.get("error"),
                "raw": res.get("raw"),
            }
            records.append(rec)
            flag = {"correct": "OK  ", "no_output": "ERR "}.get(verdict, "MISS")
            print(f"  [{i:>2}/{len(cases)}] {flag} {case['id']} 期望={case['expected']} 实得={res.get('route')}"
                  f"  ({res.get('latency_ms')}ms)")
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            time.sleep(args.sleep)
    return records


def summarize(records):
    total = len(records)
    verdicts = Counter(r["verdict"] for r in records)
    by_cat = defaultdict(Counter)
    for r in records:
        by_cat[r["category"]][r["verdict"]] += 1
    lat = [r["latency_ms"] for r in records if r.get("latency_ms")]
    return {
        "total": total,
        "accuracy": verdicts["correct"] / total if total else 0,
        "verdicts": dict(verdicts),
        "by_category": {k: dict(v) for k, v in by_cat.items()},
        "avg_latency_ms": round(sum(lat) / len(lat)) if lat else None,
    }


def write_report(outdir, cases, results, cfg, args):
    lines = []
    lines.append("# Jev 路由能力旁路评测报告\n")
    lines.append(f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
    lines.append(f"- 题量：{len(cases)}（routing "
                 f"{sum(1 for c in cases if c['category']=='routing')} / ambiguous "
                 f"{sum(1 for c in cases if c['category']=='ambiguous')} / non_analysis "
                 f"{sum(1 for c in cases if c['category']=='non_analysis')}）")
    lines.append(f"- 标签空间：{len(TEMPLATE_KEYS)} 个模板 + ASK_USER + NO_TEMPLATE")
    lines.append(f"- baseline 模型：`{args.baseline_model}`")
    lines.append(f"- jev 模型：`{args.jev_model}`\n")

    lines.append("## 总览\n")
    lines.append("| 方案 | 准确率 | 正确 | 错路由 | 幻觉路由 | 该路由却拒答 | 无输出 | 平均延迟 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for arm, s in results.items():
        v = s["verdicts"]
        lines.append(
            f"| {arm} | **{s['accuracy']*100:.1f}%** | {v.get('correct',0)} | "
            f"{v.get('wrong_route',0)} | {v.get('over_route',0)} | {v.get('under_route',0)} | "
            f"{v.get('no_output',0)} | {s['avg_latency_ms']} ms |"
        )

    lines.append("\n## 分类维度\n")
    cats = ["routing", "ambiguous", "non_analysis"]
    labels = {"routing": "正常路由", "ambiguous": "歧义（应回问）", "non_analysis": "非分析请求（应拒绝）"}
    lines.append("| 方案 | " + " | ".join(labels[c] for c in cats) + " |")
    lines.append("|---" * (len(cats) + 1) + "|")
    for arm, s in results.items():
        cells = []
        for c in cats:
            r = s["by_category"].get(c, {})
            n = sum(r.values())
            cells.append(f"{r.get('correct',0)}/{n}" if n else "—")
        lines.append(f"| {arm} | " + " | ".join(cells) + " |")

    lines.append("\n## 关键失败案例\n")
    for arm, s in results.items():
        recs = [r for r in s["records"] if r["verdict"] != "correct"]
        lines.append(f"### {arm} —— 共 {len(recs)} 题未命中\n")
        if not recs:
            lines.append("全部命中。\n")
            continue
        lines.append("| id | 类别 | 期望 | 实得 | 判定 | 说明 |")
        lines.append("|---|---|---|---|---|---|")
        for r in recs[:25]:
            lines.append(f"| {r['id']} | {r['category']} | {r['expected']} | {r['got']} | "
                         f"{r['verdict']} | {r['note']} |")
        lines.append("")

    lines.append("## 怎么读这份报告\n")
    lines.append("- **幻觉路由（over_route）最重要**：题目本身信息不足或不在能力范围内，方案却照样给了模板。")
    lines.append("  这类错误在真实使用中等于\"静默产出错误结果\"，比选错模板更危险。")
    lines.append("- **该路由却拒答（under_route）**：过度保守，用户体验受损但不会产出错结果，危害较低。")
    lines.append("- **正常路由类**若两者差距很小，说明现有方案在这步本来就够用，")
    lines.append("  此时 Jev 的价值主要在延迟/成本，而不在准确率。")
    lines.append("- 采样规模有限（此骨架 36 题），结论只作方向判断；正式结论需扩到 100+ 题并固定模型版本。\n")

    (outdir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return outdir / "report.md"


def main():
    ap = argparse.ArgumentParser(description="Jev 路由能力旁路评测")
    ap.add_argument("--arms", default="baseline,jev", help="要跑哪几个方案，逗号分隔")
    ap.add_argument("--baseline-model", default="gpt-4o", help="方案 A 用的模型（默认取较贵的一档）")
    ap.add_argument("--jev-model", default="jev-1.13.0", help="Jev 模型串，默认 pin 到具体版本")
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 题，用于试水")
    ap.add_argument("--sleep", type=float, default=0.4, help="每题间隔秒数，避开限流")
    ap.add_argument("--dry-run", action="store_true", help="只打印将发送的内容，不调用任何 API")
    ap.add_argument("--outdir", default=None)
    args = ap.parse_args()

    cases = all_cases()
    if args.limit:
        cases = cases[: args.limit]

    cfg = resolve_config(args)
    schema_text = schema_text_from_project()

    if args.dry_run:
        print(f"题量: {len(cases)}")
        print(f"标签空间: {ROUTE_OPTIONS}")
        print(f"\nbaseline 模型: {args.baseline_model}")
        print(f"baseline 端点: {cfg.get('TEXT2STATA_API_URL', '(未配置)')}")
        print(f"jev 模型: {args.jev_model}")
        print(f"jev 端点: {cfg.get('JEV_BASE_URL', 'https://jevmodel.org/v1/systemone')}")
        print(f"jev key: {'已配置' if (cfg.get('JEVMODEL_API_KEY') or cfg.get('JEV_API_KEY')) else '未配置'}")
        print(f"\nschema 摘要（前 200 字符）:\n{schema_text[:200]}")
        print("\n--- 示例：第一题将发送给 Jev 的内容 ---")
        demo = cases[0]
        print(json.dumps({
            "model": args.jev_model,
            "state": f"数据集可用变量：\n{schema_text[:400]}...\n\n用户的分析需求：\n{demo['question']}",
            "questions": {"route": {"type": "choice",
                                    "instructions": "该用户的统计分析需求，应该交给哪个 Stata 分析模板处理？",
                                    "criteria": f"（共 {len(LABEL_CRITERIA)} 个选项）"}},
        }, ensure_ascii=False, indent=2))
        print("\n（以上为预览，未发起任何请求，未产生任何费用）")
        return 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    args.outdir = Path(args.outdir) if args.outdir else (RESULTS_DIR / stamp)
    args.outdir.mkdir(parents=True, exist_ok=True)
    print(f"输出目录: {args.outdir}\n")

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    results = {}
    for arm in arms:
        print(f"=== 跑方案: {arm} ===")
        recs = run_arm(arm, cases, cfg, schema_text, args)
        s = summarize(recs)
        s["records"] = recs
        results[arm] = s
        print(f"  → 准确率 {s['accuracy']*100:.1f}%  平均延迟 {s['avg_latency_ms']}ms\n")
        if len(results) < len(arms):
            time.sleep(2)

    report = write_report(args.outdir, cases, results, cfg, args)
    print(f"报告已生成: {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
