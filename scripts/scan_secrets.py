"""扫描工作区里的密钥明文。提交前跑一遍。

只报「文件 + 条数 + 掩码」，绝不打印密钥本身 —— 打印出来就等于再泄露一次。
已两次把 key 带进对话（一次 grep .env，一次回显请求报错），这个脚本就是为此存在的。

用法：
    ./.venv/Scripts/python.exe scripts/scan_secrets.py            # 扫工作区
    ./.venv/Scripts/python.exe scripts/scan_secrets.py --staged   # 只扫将要提交的文件
    ./.venv/Scripts/python.exe scripts/scan_secrets.py --history  # 扫已推送的 git 历史

退出码 0 = 干净，1 = 有命中。
"""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 放宽起点：`sk-` 之外的常见前缀将来可能需要补，先覆盖 OpenAI 兼容格式
PATTERNS = [
    ("openai 风格", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")),
    ("Bearer 头", re.compile(r"Bearer\s+[A-Za-z0-9._-]{16,}")),
]

# 这些文件本来就是拿来放密钥的，命中不算问题，但要确认它们没被 git 跟踪
EXPECTED = {"apikey.txt", ".env", ".env.local"}

# 测试里构造的假值：名字里带这些词的一律不算真密钥
FAKE_HINTS = ("byok", "site-", "test", "dummy", "fake", "placeholder", "supersecret", "secretvalue")

SKIP_DIRS = {".git", ".venv", "node_modules", "dist", "build", "__pycache__"}
MAX_SIZE = 5_000_000


def mask(token: str) -> str:
    t = token.split()[-1] if token.startswith("Bearer") else token
    return f"{t[:4]}…{t[-3:]}({len(t)})"


def scan_text(source: str, text: str) -> list:
    out = []
    for label, pat in PATTERNS:
        for m in pat.finditer(text):
            out.append((source, label, mask(m.group(0))))
    return out


def scan_workspace() -> list:
    hits = []
    for p in ROOT.rglob("*"):
        if not p.is_file() or any(s in p.parts for s in SKIP_DIRS):
            continue
        if p.stat().st_size > MAX_SIZE:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        rel = str(p.relative_to(ROOT)).replace("\\", "/")
        hits += scan_text(rel, text)
    return hits


def scan_git(refs: list) -> list:
    hits = []
    for ref in refs:
        try:
            text = subprocess.run(
                ["git", "log", "-p", "--all", ref],
                cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="ignore",
            ).stdout
        except Exception:
            continue
        hits += scan_text(f"git:{ref}", text)
    return hits


def staged_files() -> list:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout.splitlines()
    return [f for f in out if f.strip()]


def main() -> int:
    args = set(sys.argv[1:])
    hits = []

    if "--history" in args:
        hits = scan_git(["*.py", "*.md", "*.json", "*.html", "*.txt", "*.yaml", "*.yml"])
    elif "--staged" in args:
        for rel in staged_files():
            p = ROOT / rel
            if not p.is_file():
                continue
            hits += scan_text(rel, p.read_text(encoding="utf-8", errors="ignore"))
    else:
        hits = scan_workspace()

    tracked = set(subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True
    ).stdout.splitlines())

    real, expected, fake, tracked_bad = [], [], [], []
    for source, label, m in hits:
        name = source.split("/")[-1] if "git:" not in source else source
        if name in EXPECTED or name.startswith(".env"):
            expected.append((source, label, m))
            if source in tracked:
                tracked_bad.append(source)
        elif "tests/" in source or any(h in m.lower() for h in FAKE_HINTS):
            fake.append((source, label, m))
        else:
            real.append((source, label, m))

    print(f"扫描范围：{'git 历史' if '--history' in args else ('暂存区' if '--staged' in args else '工作区')}")
    print(f"命中 {len(hits)} 处，其中非预期位置 {len(real)} 处。\n")

    if real:
        print("!! 这些文件不该出现密钥明文：")
        for source, label, m in real:
            print(f"   {source}  [{label}] {m}")
        print()

    if expected:
        print("· 预期存放点（本身没问题，但必须确认未被 git 跟踪）：")
        for source, label, m in expected:
            flag = "  ❌ 已被 git 跟踪！" if source in tracked else "  ✓ 未入库"
            print(f"   {source}{flag}")
        print()

    if fake:
        print(f"· 测试假值 {len(fake)} 处，忽略：")
        for source, label, m in fake:
            print(f"   {source}  {m}")
        print()

    if not hits:
        print("干净，没有命中。")
        return 0
    return 1 if (real or tracked_bad) else 0


if __name__ == "__main__":
    sys.exit(main())
