"""
运行路径解析。

开发模式与打包模式（PyInstaller）的目录布局不同，这里统一收口：

                      开发模式                    打包模式
只读资源（代码/模板/前端）  <项目根>                    sys._MEIPASS
可写数据（上传/工作区/配置）  <项目根>/backend            %APPDATA%\\Text2Stata

打包后数据放 APPDATA 而不是 exe 旁边，有两个原因：
  1. 用户可能把程序解压到 Program Files 之类没有写权限的地方；
  2. 覆盖升级或挪动目录时，上传过的数据与配置不会跟着丢。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "Text2Stata"


def is_frozen() -> bool:
    """PyInstaller 打包后 sys.frozen 会被置位。"""
    return bool(getattr(sys, "frozen", False))


def _resource_root() -> Path:
    """只读资源根目录。"""
    if is_frozen():
        # PyInstaller 把 --add-data 的内容解到这里
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parents[2]


def _data_root() -> Path:
    """可写数据根目录。"""
    if is_frozen():
        base = os.getenv("TEXT2STATA_DATA_DIR") or os.getenv("APPDATA") or str(Path.home())
        return Path(base) / APP_NAME
    # 开发模式沿用原有布局，历史数据与 .gitignore 都不用改
    return Path(__file__).resolve().parents[1]


RESOURCE_ROOT = _resource_root()
DATA_ROOT = _data_root()

# 只读资源
FRONTEND_DIR = RESOURCE_ROOT / "frontend"
TEMPLATES_DIR = (
    RESOURCE_ROOT / "core" / "templates" if is_frozen() else RESOURCE_ROOT / "backend" / "core" / "templates"
)

# 可写数据
UPLOAD_DIR = DATA_ROOT / "uploads"
WORKSPACE_DIR = DATA_ROOT / "workspace"
CONFIG_FILE = DATA_ROOT / "config.json"


def ensure_dirs() -> None:
    """启动时调用，确保可写目录存在。"""
    for directory in (UPLOAD_DIR, WORKSPACE_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def describe() -> str:
    """给启动日志用的一行说明。"""
    mode = "打包模式" if is_frozen() else "开发模式"
    return f"{mode}｜资源 {RESOURCE_ROOT}｜数据 {DATA_ROOT}"
