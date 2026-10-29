"""
大模型密钥的两种来源（双密钥模式）。

    站点密钥（site）  服务器环境变量 / 数据目录里的配置文件。全站共用，计入配额与站点预算。
    自带密钥（byok）  用户浏览器本地存着，随请求体带上。服务端不落库，用户自担费用。

「服务端不保存 BYOK」不是一句承诺，是这个模块的结构决定的：
解析出来的 byok 密钥只作为返回值活在调用栈里——没有任何一行代码把它写进
数据库、日志或响应体。要回显一律走 mask()，全文只留首尾各 4 位。

环境变量：
    T2S_LLM_API_KEY        站点密钥（不配就只能用 BYOK）
    T2S_LLM_API_URL        站点接口地址，默认与本地版一致
    T2S_ALLOW_BYOK         是否允许自带密钥，默认 1
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_API_URL = "https://globalai.vip/v1/chat/completions"

SITE_KEY_ENV = "T2S_LLM_API_KEY"
SITE_URL_ENV = "T2S_LLM_API_URL"
ALLOW_BYOK_ENV = "T2S_ALLOW_BYOK"


def site_url() -> str:
    """站点接口地址。"""
    return (os.getenv(SITE_URL_ENV) or "").strip() or DEFAULT_API_URL


def site_key(data_dir=None) -> str:
    """
    站点密钥：环境变量优先；没有则退到数据目录里的 llm_key 文件。

    之所以也支持文件：环境变量在 systemd / Docker 里都好配，但在本机随手起服务时
    写文件更省事，而且不会在 `ps` 里把密钥露出来。
    """
    key = (os.getenv(SITE_KEY_ENV) or "").strip()
    if key:
        return key
    if data_dir:
        path = Path(data_dir) / "llm_key"
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return ""


def byok_allowed() -> bool:
    return os.getenv(ALLOW_BYOK_ENV, "1") != "0"


def mask(key: str) -> str:
    """只留首尾各 4 位。短密钥一律打成 ****，避免长度本身泄露信息。"""
    if not key:
        return ""
    if len(key) <= 10:
        return "*" * 8
    return f"{key[:4]}...{key[-4:]}"


def resolve(payload: dict, data_dir=None):
    """
    从请求体里解析出 (api_url, api_key, mode)。

    mode 为 "byok" / "site"；一个都拿不到时返回 (url, "", None)，
    由调用方决定报 402 还是别的。

    BYOK 的 api_url 可由请求体带（自建兼容端点），缺省用站点地址——
    这样只换 key 不换站的用户不用多填一个字段。
    """
    byok = str((payload or {}).get("api_key") or "").strip()
    if byok and byok_allowed():
        url = str((payload or {}).get("api_url") or "").strip() or site_url()
        return url, byok, "byok"

    key = site_key(data_dir)
    if key:
        return site_url(), key, "site"

    return site_url(), "", None
