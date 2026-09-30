"""
Text2Stata 启动器。

这是"傻瓜版"的入口。双击之后按顺序做五件事：

    1. 让数据目录就绪（%APPDATA%\\Text2Stata）
    2. 检查端口：已经跑着一个就直接开浏览器，不重复启动
    3. 后台起 Flask
    4. 等健康检查通过，再打开浏览器
    5. 主线程挂住，Ctrl+C 或关窗口退出

刻意不做的事：不静默安装任何东西、不写注册表、不改系统设置。
关掉这个窗口，一切痕迹只剩数据目录里的文件。
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path


def _prepare_import_path() -> Path:
    """开发模式下把 backend/ 加进模块搜索路径；打包后由 PyInstaller 负责。"""
    project_root = Path(__file__).resolve().parent
    if not getattr(sys, "frozen", False):
        backend = project_root / "backend"
        if str(backend) not in sys.path:
            sys.path.insert(0, str(backend))
    return project_root


def _relax_streams() -> None:
    """控制台编码兜底：编码不下的字符替换掉，不要让启动器因为一个中文句号崩掉。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def _url(host: str, port: int, path: str = "/") -> str:
    display = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    return f"http://{display}:{port}{path}"


def _port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def _find_free_port(host: str, start: int, attempts: int = 20) -> int:
    for offset in range(attempts):
        candidate = start + offset
        if _port_available(host, candidate):
            return candidate
    raise RuntimeError(f"从 {start} 开始连续 {attempts} 个端口都被占用了。")


def _already_running(host: str, port: int) -> bool:
    """端口上是不是已经跑着一个 Text2Stata —— 是的话没必要再起一个。"""
    try:
        with urllib.request.urlopen(f"{_url(host, port, '/api/health')}", timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return False
    return "Text2Stata" in str(payload.get("message", ""))


def _wait_until_ready(host: str, port: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{_url(host, port, '/api/health')}", timeout=1):
                return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.25)
    return False


def _banner(host: str, port: int, backend) -> None:
    from core import paths  # 放这里导入，让 _prepare_import_path 先生效

    ok, stata_message = backend.worker.check_environment()
    line = "=" * 64
    print(line)
    print("  Text2Stata  ——  用中文描述需求，自动跑 Stata 实证分析")
    print(line)
    print(f"  访问地址：{_url(host, port)}")
    print(f"  数据目录：{paths.DATA_ROOT}")
    print(f"  Stata  ：{stata_message}")
    if not ok:
        print("          → 打开页面后点右上角「设置」，手动指定 Stata 的 exe 路径")
    if not backend.config.has_usable_llm():
        print("  模型   ：尚未配置密钥")
        print("          → 打开页面后点右上角「设置」，填入 API Key")
    print(line)
    print("  关闭本窗口即退出程序。")
    print(line)


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="Text2Stata",
        description="启动本地服务并打开浏览器。",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="只启动服务，不自动打开浏览器（服务器环境或自动化测试用）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="指定端口，覆盖配置文件里的设置",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)

    _relax_streams()
    _prepare_import_path()

    from core import config  # noqa: E402

    import app as backend  # noqa: E402

    host = config.HOST or "127.0.0.1"
    port = args.port or config.PORT or 5000

    if _already_running(host, port):
        print(f"Text2Stata 已经在运行，直接打开：{_url(host, port)}")
        if not args.no_browser:
            webbrowser.open(_url(host, port))
        return 0

    if not _port_available(host, port):
        fallback = _find_free_port(host, port + 1)
        print(f"端口 {port} 被占用，改用 {fallback}。")
        port = fallback

    backend.apply_runtime_config()
    _banner(host, port, backend)

    server = threading.Thread(
        target=lambda: backend.app.run(
            host=host,
            port=port,
            debug=False,
            use_reloader=False,
            threaded=True,
        ),
        daemon=True,
        name="text2stata-server",
    )
    server.start()

    if _wait_until_ready(host, port):
        if not args.no_browser:
            # 没配模型的话直接把设置面板打开，省掉用户自己找入口
            first_run = not backend.config.has_usable_llm()
            webbrowser.open(_url(host, port, "/?settings=1" if first_run else "/"))
    else:
        print("服务启动超时，请把上面的日志发出来看看。")
        return 1

    try:
        while server.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n已退出。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
