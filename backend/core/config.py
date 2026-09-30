"""
配置分层。

优先级由高到低：

    1. 进程环境变量（含 .env，仅开发模式加载）
    2. %APPDATA%\\Text2Stata\\config.json（打包后由前端设置面板写入）
    3. 内置默认值

打包后的程序里没有 .env，config.json 就是唯一的配置来源；
开发时仍以 .env 为准，保持原有习惯不变。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

try:
    from . import paths
except ImportError:  # 允许直接运行本文件
    import paths  # type: ignore


# --------------------------------------------------------------------------
# 默认值
# --------------------------------------------------------------------------
DEFAULTS = {
    "api_url": "https://globalai.vip/v1/chat/completions",
    "api_keys": {"line1": "", "line2": "", "line3": ""},
    "default_model": "gpt-4o",
    "stata_path": "",
    "max_upload_mb": 100,
    "host": "127.0.0.1",
    "port": 5000,
}


def load_dotenv(dotenv_path=None):
    """Load simple KEY=VALUE pairs from .env without adding a dependency."""
    path = Path(dotenv_path or paths.RESOURCE_ROOT / ".env")
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _read_file_config() -> dict:
    """读 config.json；损坏时当作空配置，不要让程序起不来。"""
    if not paths.CONFIG_FILE.exists():
        return {}
    try:
        data = json.loads(paths.CONFIG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_file_config(payload: dict) -> None:
    paths.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    paths.CONFIG_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def get_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def get_int(name, default):
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def get_csv(name, default=None):
    value = os.getenv(name)
    if not value:
        return default or []
    return [item.strip() for item in value.split(",") if item.strip()]


# --------------------------------------------------------------------------
# 生效配置：环境变量 > config.json > 默认值
# --------------------------------------------------------------------------
def _pick(env_name, file_value, default):
    """三选一。环境变量存在（即使是空串）就以它为准。"""
    if env_name in os.environ:
        return os.environ[env_name]
    if file_value not in (None, ""):
        return file_value
    return default


# 下面这批模块级常量由 refresh() 重算。
# app.py 请用 `config.STATA_PATH` 这种属性访问，不要 `from core.config import STATA_PATH`，
# 否则设置面板改完值之后拿到的还是导入时那份旧快照。
API_URL = DEFAULTS["api_url"]
DEFAULT_MODEL = DEFAULTS["default_model"]
STATA_PATH = DEFAULTS["stata_path"]
MAX_UPLOAD_MB = DEFAULTS["max_upload_mb"]
HOST = DEFAULTS["host"]
PORT = DEFAULTS["port"]
CORS_ORIGINS = ["http://127.0.0.1:5000", "http://localhost:5000", "null"]
FLASK_DEBUG = False
API_LINES = dict(DEFAULTS["api_keys"])


def refresh() -> None:
    """重新计算生效配置。改完 config.json 或环境变量后调用。"""
    global API_URL, DEFAULT_MODEL, STATA_PATH, MAX_UPLOAD_MB, HOST, PORT
    global CORS_ORIGINS, FLASK_DEBUG, API_LINES

    file_cfg = _read_file_config()
    file_keys = file_cfg.get("api_keys") or {}

    API_URL = _pick("TEXT2STATA_API_URL", file_cfg.get("api_url"), DEFAULTS["api_url"])
    DEFAULT_MODEL = _pick(
        "TEXT2STATA_DEFAULT_MODEL", file_cfg.get("default_model"), DEFAULTS["default_model"]
    )
    STATA_PATH = _pick("TEXT2STATA_STATA_PATH", file_cfg.get("stata_path"), DEFAULTS["stata_path"])
    MAX_UPLOAD_MB = get_int(
        "TEXT2STATA_MAX_UPLOAD_MB", int(file_cfg.get("max_upload_mb") or DEFAULTS["max_upload_mb"])
    )
    HOST = _pick("TEXT2STATA_HOST", file_cfg.get("host"), DEFAULTS["host"])
    PORT = get_int("TEXT2STATA_PORT", int(file_cfg.get("port") or DEFAULTS["port"]))

    # 打包后前端由本服务托管，同源请求不需要 CORS 放行；
    # 这里只为开发时 file:// 直开保留 "null"。
    CORS_ORIGINS = get_csv(
        "TEXT2STATA_CORS_ORIGINS",
        ["http://127.0.0.1:5000", "http://localhost:5000", "null"],
    )
    FLASK_DEBUG = get_bool("TEXT2STATA_FLASK_DEBUG", False)

    API_LINES = {
        "line1": os.getenv("TEXT2STATA_API_KEY_LINE1") or file_keys.get("line1", ""),
        "line2": os.getenv("TEXT2STATA_API_KEY_LINE2") or file_keys.get("line2", ""),
        "line3": os.getenv("TEXT2STATA_API_KEY_LINE3") or file_keys.get("line3", ""),
    }


def _mask(secret: str) -> str:
    """只露头尾，够用户认出是哪把 key 就行。"""
    if not secret:
        return ""
    if len(secret) <= 10:
        return "*" * len(secret)
    return f"{secret[:6]}…{secret[-4:]}"


def public_config() -> dict:
    """给前端设置面板用的配置快照 —— 绝不回传完整 key。"""
    return {
        "api_url": API_URL,
        "default_model": DEFAULT_MODEL,
        "stata_path": STATA_PATH,
        "max_upload_mb": MAX_UPLOAD_MB,
        "keys": [
            {"line": name, "set": bool(API_LINES.get(name)), "hint": _mask(API_LINES.get(name, ""))}
            for name in ("line1", "line2", "line3")
        ],
        "config_file": str(paths.CONFIG_FILE),
    }


def save_config(patch: dict) -> None:
    """
    把改动合并进 config.json 并立即生效。

    api_keys 是部分更新：只覆盖本次真正传了新值的行，
    空字符串表示"不改"，避免前端把已保存的 key 覆盖成空。
    """
    current = _read_file_config()
    keys = dict(current.get("api_keys") or {})

    for field in ("api_url", "default_model", "stata_path", "max_upload_mb", "host", "port"):
        if field in patch and patch[field] not in (None, ""):
            value = patch[field]
            if field in ("max_upload_mb", "port"):
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    continue
            current[field] = value

    incoming_keys = patch.get("api_keys") or {}
    for name in ("line1", "line2", "line3"):
        if name in incoming_keys and str(incoming_keys[name]).strip():
            keys[name] = str(incoming_keys[name]).strip()

    if keys:
        current["api_keys"] = keys

    _write_file_config(current)
    refresh()


def bootstrap() -> None:
    """进程启动时调一次：加载 .env（仅开发模式）并算出首份生效配置。"""
    if not paths.is_frozen():
        load_dotenv()
    refresh()


def has_usable_llm() -> bool:
    """是否至少配了一把 key —— 没配的话前端要引导去设置。"""
    return any(bool(v) for v in API_LINES.values())
