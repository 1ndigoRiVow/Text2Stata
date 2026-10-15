# -*- coding: utf-8 -*-
"""
模型选型评测：在 Text2Stata 自己的路由任务上横向比几个候选模型。

用法（项目根目录）：
    .venv/Scripts/python.exe eval/model_picker/run_pick.py

凭据从 apikey.txt 读（该文件已在 .gitignore 内，永不入库）。
输出：准确率 / JSON 失败率 / 平均延迟，按准确率排序。
"""

from __future__ import annotations

import json
import os
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT))

import pyreadstat  # noqa: E402

from core import config  # noqa: E402
from core.llm_agent import LLMAgent  # noqa: E402

from test_cases import CASES  # noqa: E402

# 候选模型：四个都已在 globalai.vip 的 /v1/models 里确认存在
CANDIDATES = [
    "deepseek-v4-flash",
    "qwen3.7-flash",
    "glm-5.3-flash",
    "gpt-6.1-sol",
]


def load_credentials() -> tuple[str, list[str]]:
    """从 apikey.txt 解析 (api_url, keys)。不打印任何密钥内容。"""
    path = ROOT / "apikey.txt"
    if not path.exists():
        raise SystemExit("找不到 apikey.txt")

    raw = path.read_text(encoding="utf-8")
    urls = re.findall(r"https?://[^\s\"']+", raw)
    keys = [t for t in re.findall(r"[A-Za-z0-9_\-.]{20,}", raw) if not t.startswith("http")]
    if not urls or not keys:
        raise SystemExit("apikey.txt 解析失败：没找到 URL 或密钥")
    return urls[0], keys


def build_schema() -> list[dict]:
    """用演示数据构造与 /api/upload 完全一致的 schema 结构。"""
    _, meta = pyreadstat.read_dta(str(ROOT / "testdata.dta"))
    return [{"name": c, "label": meta.column_names_to_labels.get(c, "")} for c in meta.column_names]


def run_one(model: str, query: str, schema: list[dict], registry: dict) -> dict:
    agent = LLMAgent(current_line="line1", current_model=model)
    started = time.time()
    result = agent.parse_intent(query, schema, registry)
    elapsed_ms = int((time.time() - started) * 1000)

    if result.get("status") == "error":
        return {"ok": False, "error": result.get("message", ""), "ms": elapsed_ms}

    got = (result.get("selected_template") or "").strip()
    return {"ok": True, "template": got, "ms": elapsed_ms}


def evaluate(model: str, schema: list[dict], registry: dict) -> dict:
    rows = []
    for query, expected in CASES:
        outcome = run_one(model, query, schema, registry)
        outcome["query"] = query
        outcome["expected"] = expected
        outcome["hit"] = bool(outcome.get("ok")) and outcome.get("template") == expected
        rows.append(outcome)

    finished = [r for r in rows if r["ok"]]
    return {
        "model": model,
        "correct": sum(1 for r in rows if r["hit"]),
        "total": len(rows),
        "accuracy": sum(1 for r in rows if r["hit"]) / len(rows),
        "json_fail": sum(1 for r in rows if not r["ok"]),
        "avg_ms": int(statistics.mean([r["ms"] for r in finished])) if finished else None,
        "rows": rows,
    }


def main() -> None:
    api_url, keys = load_credentials()
    # 走环境变量注入，prompt 与生产链路完全一致（不魔改 LLMAgent）
    os.environ["TEXT2STATA_API_URL"] = api_url
    os.environ["TEXT2STATA_API_KEY_LINE1"] = keys[0]
    config.refresh()

    schema = build_schema()
    registry = json.loads(
        (ROOT / "backend" / "core" / "templates" / "registry.json").read_text(encoding="utf-8")
    )

    print(f"凭据：{len(keys)} 把 key（已掩码）  schema：{len(schema)} 个变量  用例：{len(CASES)} 条")
    print(flush=True)

    out_dir = ROOT / "eval" / "model_picker" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_file = out_dir / f"{stamp}.json"

    results = []
    with ThreadPoolExecutor(max_workers=len(CANDIDATES)) as pool:
        futures = {pool.submit(evaluate, m, schema, registry): m for m in CANDIDATES}
        for future in as_completed(futures):
            model = futures[future]
            try:
                outcome = future.result()
            except Exception as exc:  # 单个模型炸了不要拖垮整轮
                outcome = {
                    "model": model, "correct": 0, "total": len(CASES),
                    "accuracy": 0.0, "json_fail": len(CASES), "avg_ms": None,
                    "rows": [{"query": q, "expected": e, "ok": False, "error": str(exc),
                              "hit": False} for q, e in CASES],
                }
            results.append(outcome)
            print(f"  完成 {outcome['model']}: "
                  f"{outcome['correct']}/{outcome['total']} "
                  f"({outcome['accuracy']:.1%})", flush=True)
            # 实时落盘，即使后面被中断也不丢进度
            out_file.write_text(
                json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    results.sort(key=lambda r: (-r["accuracy"], r["avg_ms"] or 10**9))

    print(f"{'模型':<22}{'准确率':<12}{'延迟':<10}{'JSON失败'}")
    print("-" * 56)
    for r in results:
        acc = f"{r['correct']}/{r['total']} ({r['accuracy']:.1%})"
        ms = f"{r['avg_ms']}ms" if r["avg_ms"] else "—"
        print(f"{r['model']:<22}{acc:<12}{ms:<10}{r['json_fail']}")

    print("\n错题明细：")
    for r in results:
        for row in r["rows"]:
            if not row["hit"]:
                got = row.get("template") or f"ERROR: {row.get('error', '')[:60]}"
                print(f"  [{r['model']}] “{row['query']}”")
                print(f"      期望 {row['expected']} / 得到 {got}")

    print(f"\n已保存：{out_file.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
