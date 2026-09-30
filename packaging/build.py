"""
打包脚本（Windows）。

    python packaging/build.py

产出两份东西：

    dist/Text2Stata/Text2Stata.exe     双击即用
    dist/Text2Stata-windows.zip        发给别人的那个文件

前提：Stata 不在打包范围内。用户得自己有 Stata 授权，
本包只把 Python 运行时和代码装好，省掉"配环境"这一步。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SPEC_FILE = Path(__file__).resolve().parent / "text2stata.spec"
DIST_DIR = PROJECT_ROOT / "dist"
APP_DIR = DIST_DIR / "Text2Stata"
ZIP_BASE = DIST_DIR / "Text2Stata-windows"


def _require_pyinstaller() -> bool:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "PyInstaller", "--version"],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    print(f"PyInstaller {result.stdout.strip()}")
    return True


def _run_pyinstaller() -> None:
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(DIST_DIR),
        "--workpath",
        str(PROJECT_ROOT / "build"),
        str(SPEC_FILE),
    ]
    print("执行：" + " ".join(command))
    started = time.time()
    result = subprocess.run(command, cwd=str(PROJECT_ROOT))
    if result.returncode != 0:
        raise SystemExit(f"PyInstaller 失败，退出码 {result.returncode}")
    print(f"打包耗时 {time.time() - started:.1f} 秒")


def _zip_app() -> Path:
    if not APP_DIR.exists():
        raise SystemExit(f"没有找到打包产物目录：{APP_DIR}")
    # 把整个 Text2Stata 文件夹塞进压缩包 —— 解压后是一个干净的目录，
    # 而不是把 exe 和一堆 dll 直接撒在用户的下载文件夹里。
    archive = shutil.make_archive(str(ZIP_BASE), "zip", root_dir=str(DIST_DIR), base_dir="Text2Stata")
    return Path(archive)


USAGE_NOTE = """Text2Stata —— 使用说明
========================================

一、怎么启动
   双击 Text2Stata.exe。
   会弹出一个黑色窗口，等几秒浏览器会自动打开。
   —— 那个黑窗口别关，关掉程序就退出了。

二、第一次用要配两样东西
   1) Stata
      通常不用管，程序会自动找你电脑上装的 Stata。
      找不到就点「自动探测」；还找不到就手动填 exe 的完整路径，
      例如：C:\\Program Files\\StataNow19\\StataSE-64.exe

   2) 大模型密钥（API Key）
      填一个兼容 OpenAI 接口的密钥就行。
      密钥只存在你自己电脑上，不会上传到任何地方。

三、Windows 提示"已保护你的电脑"
   本程序没有买数字签名证书，Windows 对所有这类程序都会弹这个提示。
   点「更多信息」→「仍要运行」。不是病毒。

四、你的数据在哪
   .dta 数据全程只在你自己的电脑上，不会被上传。
   程序产生的文件都在：%APPDATA%\\Text2Stata
   （在文件资源管理器地址栏粘贴这一行就能打开）

五、怎么卸载
   删掉这个文件夹，再删掉上面的 %APPDATA%\\Text2Stata 就行。
   不写注册表，不留后台服务。

六、前提
   需要你这台电脑已经装好 Stata 并有有效授权。
   本程序不包含 Stata。
"""


def _write_usage_note() -> Path:
    """把说明书写进压缩包 —— 收到 zip 的人不用去翻 GitHub 也能上手。

    用 utf-8-sig：带 BOM 的 UTF-8 才能在 Windows 记事本里正常显示中文。
    """
    note = APP_DIR / "使用说明.txt"
    note.write_text(USAGE_NOTE, encoding="utf-8-sig")
    return note


def _folder_size(path: Path) -> float:
    total = sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    return total / (1024 * 1024)


def main() -> int:
    if not SPEC_FILE.exists():
        raise SystemExit(f"找不到 spec 文件：{SPEC_FILE}")

    print("=" * 64)
    print("  Text2Stata 打包")
    print("=" * 64)

    if sys.platform != "win32":
        print("提示：当前不是 Windows。产出的可执行文件只能在 Windows 上跑。")

    if not _require_pyinstaller():
        print()
        print("没装 PyInstaller。先执行：")
        print(f"  {sys.executable} -m pip install -r requirements-build.txt")
        return 1

    _run_pyinstaller()

    note = _write_usage_note()
    archive = _zip_app()
    print()
    print("=" * 64)
    print(f"  可执行文件：{APP_DIR / 'Text2Stata.exe'}")
    print(f"  使用说明  ：{note.name}")
    print(f"  压缩包    ：{archive}")
    print(f"  目录体积  ：{_folder_size(APP_DIR):.0f} MB")
    print("=" * 64)
    print("  提醒：压缩包不含 Stata，使用者需自备授权。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
