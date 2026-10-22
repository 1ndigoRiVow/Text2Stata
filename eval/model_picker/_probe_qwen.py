"""确认 qwen3.7-flash 是渠道真的没了，还是临时抖动。

只在内存里用 key，不打印、不落盘。
"""
import pathlib
import re

import requests

raw = pathlib.Path("apikey.txt").read_text(encoding="utf-8")
url = re.findall(r"https?://[^\s\"']+", raw)[0]
keys = [t for t in re.findall(r"[A-Za-z0-9_\-\.]{20,}", raw) if not t.startswith("http")]
base = url.replace("/chat/completions", "")

CHECK = ["qwen3.7-flash", "qwen3.8-flash", "deepseek-v4-flash", "glm-5.3-flash", "gpt-6.1-sol"]

print("=== /v1/models 里有没有 ===")
try:
    r = requests.get(base + "/models", headers={"Authorization": "Bearer " + keys[0]}, timeout=30)
    ids = sorted(d.get("id", "") for d in r.json().get("data", []))
    print("HTTP", r.status_code, "| 模型总数:", len(ids))
    for m in CHECK:
        same = [x for x in ids if x == m]
        near = [x for x in ids if x.startswith(m.split("-")[0])][:6]
        print(f"  {m:<20} 精确存在={bool(same)}  同族={near}")
except Exception as e:
    print("  /v1/models 失败:", type(e).__name__)

print()
print("=== 各试一次真实对话 ===")
payload = {
    "model": None,
    "messages": [{"role": "user", "content": "只回一个数字：1"}],
    "max_tokens": 8,
}
for m in CHECK:
    payload["model"] = m
    try:
        r = requests.post(base + "/chat/completions",
                          headers={"Authorization": "Bearer " + keys[0]},
                          json=payload, timeout=45)
        if r.status_code == 200:
            txt = r.json()["choices"][0]["message"]["content"]
            print(f"  {m:<20} HTTP 200  回复={txt.strip()[:20]!r}")
        else:
            print(f"  {m:<20} HTTP {r.status_code}  {r.text[:110]}")
    except Exception as e:
        print(f"  {m:<20} 异常 {type(e).__name__}: {str(e)[:80]}")
