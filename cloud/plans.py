"""
云端的"计划 → .do 脚本"装配。

Phase 1 不接大模型：调用方直接给 selected_template + variable_mapping（假计划），
这里负责校验模板契约并装配出可下发给代理的脚本。

与本地版 app.py 的关键差异：数据文件在用户自己的机器上，
脚本里的数据路径写成占位符 __T2S_DATA_PATH__（见 docs/PROTOCOL.md 4.1），
由代理在执行前替换成它本机的真实路径。云端从始至终不知道数据在哪。

复用 backend/core 的模板引擎与头部生成——两个版本共用同一套分析内核，
这里不复制实现，只做路径引导。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 让 cloud/ 能直接 import backend/core 下的模块（开发模式）。
# Docker 部署时把 backend/core 一并拷进镜像即可，导入路径不变。
_BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from core.logic_center import generate_stata_header  # noqa: E402
from core.template_manager import TemplateManager  # noqa: E402

# 代理执行前必须替换的本机数据路径占位符（协议 4.1）
DATA_PATH_PLACEHOLDER = "__T2S_DATA_PATH__"

template_mgr = TemplateManager()


def _normalize_controls(value):
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [item for item in value if item]
    return [item.strip() for item in str(value).split() if item.strip()]


def _missing_required_globals(template_info, variable_mapping):
    """与本地版同一条契约：required_globals 缺一个都不放行。"""
    missing = []
    for key in template_info.get("required_globals") or []:
        value = variable_mapping.get(key)
        if key == "controls":
            ok = bool(_normalize_controls(value))
        else:
            ok = bool(str(value).strip()) if value is not None else False
        if not ok:
            missing.append(key)
    return missing


def build_task_script(selected_template, variable_mapping):
    """
    校验 + 装配，返回 {do_script, data_structure, template_info}。
    校验失败抛 ValueError（消息面向用户）。
    """
    registry = template_mgr.get_all_templates()
    if selected_template not in registry:
        raise ValueError(f"未知模板: {selected_template}")
    template_info = registry[selected_template]

    mapping = dict(variable_mapping or {})
    mapping.setdefault("controls", [])
    for key in ("y", "x", "id", "time"):
        mapping.setdefault(key, "")

    missing = _missing_required_globals(template_info, mapping)
    if missing:
        label = template_info.get("name", selected_template)
        raise ValueError(
            f"模板「{label}」缺少必需变量：{'、'.join('$' + m for m in missing)}。"
        )

    logic_res = generate_stata_header(mapping)
    header = f'use "{DATA_PATH_PLACEHOLDER}", clear\n' + logic_res["header_code"]
    do_script = template_mgr.assemble_script(selected_template, header)

    return {
        "do_script": do_script,
        "data_structure": logic_res["data_structure"],
        "template_info": template_info,
        "variable_mapping": mapping,
    }
