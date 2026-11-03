"""本地开发起云端：自动注入站点密钥 + 启动 cloud.server。

为什么不直接 python -m cloud.server：站点密钥来自 apikey.txt（带备注的多行文本），
手工 export 麻烦且容易把密钥留在终端历史里。这里按 sk- 前缀正则抽，只进环境变量。

双击 run_cloud_dev.bat，或在仓库根目录跑：
    ./.venv/Scripts/python.exe scripts/run_cloud_dev.py

端口/地址可用环境变量覆盖：T2S_CLOUD_HOST / T2S_CLOUD_PORT。
"""
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if not os.environ.get("T2S_LLM_API_KEY"):
    raw = (ROOT / "apikey.txt").read_text(encoding="utf-8")
    found = re.findall(r"sk-[A-Za-z0-9_\-]{16,}", raw)
    if found:
        os.environ["T2S_LLM_API_KEY"] = found[0]
    else:
        print("注意：apikey.txt 里没抽出密钥，站点密钥模式不可用（用户仍可 BYOK）")

from cloud.server import main  # noqa: E402

if __name__ == "__main__":
    main()
