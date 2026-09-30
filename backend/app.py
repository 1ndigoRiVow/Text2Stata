import copy
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pyreadstat
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from werkzeug.utils import secure_filename

from core import config, paths, stata_detect
from core.llm_agent import LLMAgent
from core.logic_center import generate_stata_header
from core.result_auditor import ResultAuditor
from core.stata_worker import StataWorker
from core.template_manager import TemplateManager


config.bootstrap()
paths.ensure_dirs()

UPLOAD_FOLDER = paths.UPLOAD_DIR
WORKSPACE_DIR = paths.WORKSPACE_DIR
FRONTEND_DIR = paths.FRONTEND_DIR

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = config.MAX_UPLOAD_MB * 1024 * 1024
CORS(app, resources={r"/api/*": {"origins": "*"}})

template_mgr = TemplateManager()
worker = StataWorker(stata_path=config.STATA_PATH or None, workspace=WORKSPACE_DIR)
auditor = ResultAuditor()


def apply_runtime_config():
    """设置面板改完配置后，把变动同步到已实例化的组件上。"""
    app.config["MAX_CONTENT_LENGTH"] = config.MAX_UPLOAD_MB * 1024 * 1024
    worker.stata_path = config.STATA_PATH or worker._detect_default_stata()


executor = ThreadPoolExecutor(max_workers=2)
tasks = {}
tasks_lock = threading.Lock()


def _set_task(task_id, **updates):
    with tasks_lock:
        current = tasks.setdefault(task_id, {})
        current.update(updates)
        return copy.deepcopy(current)


def _get_task(task_id):
    with tasks_lock:
        task = tasks.get(task_id)
        return copy.deepcopy(task) if task else None


def _schema_from_dta(file_path):
    _, meta = pyreadstat.read_dta(str(file_path))
    schema_info = []
    for col_name in meta.column_names:
        col_label = meta.column_names_to_labels.get(col_name, "")
        schema_info.append({"name": col_name, "label": col_label})
    return schema_info


def _normalize_controls(value):
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [item for item in value if item]
    return [item.strip() for item in str(value).split() if item.strip()]


def _merge_manual_mappings(ai_mapping, manual_mappings):
    final_mapping = dict(ai_mapping or {})
    for key, value in (manual_mappings or {}).items():
        if key == "controls":
            controls = _normalize_controls(value)
            if controls:
                final_mapping[key] = controls
        elif value:
            final_mapping[key] = value
    final_mapping.setdefault("controls", [])
    for key in ("y", "x", "id", "time"):
        final_mapping.setdefault(key, "")
    return final_mapping


def _validate_template(template_name):
    registry = template_mgr.get_all_templates()
    if template_name not in registry:
        raise ValueError(f"未知模板: {template_name}")
    return registry[template_name]


def _missing_required_globals(template_info, variable_mapping):
    """
    检查模板声明的 required_globals 是否都有值。
    registry.json 里每个模板都声明了 required_globals，但此前没有任何代码校验它，
    结果是变量映射为空时照样生成 $y / $x 全未定义的脚本，跑出无意义的结果。
    """
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


def _build_script(file_id, selected_template, variable_mapping):
    file_path = UPLOAD_FOLDER / secure_filename(file_id)
    if not file_path.exists():
        raise FileNotFoundError("找不到指定的数据文件，请重新上传。")

    template_info = _validate_template(selected_template)

    missing = _missing_required_globals(template_info, variable_mapping)
    if missing:
        template_label = template_info.get("name", selected_template)
        raise ValueError(
            f"模板「{template_label}」缺少必需变量：{'、'.join('$' + m for m in missing)}。"
            f"请在需求中指明变量名，或手动补充变量映射后重试。"
        )

    logic_res = generate_stata_header(variable_mapping)
    load_data_cmd = f'use "{file_path}", clear\n'
    full_header = load_data_cmd + logic_res["header_code"]
    final_do_script = template_mgr.assemble_script(selected_template, full_header)

    return {
        "template_info": template_info,
        "data_structure": logic_res["data_structure"],
        "do_script": final_do_script,
    }


def _create_plan(payload):
    user_query = (payload.get("query") or "").strip()
    file_id = payload.get("file_id")
    schema_info = payload.get("schema", [])
    manual_mappings = payload.get("manual_mappings", {})
    ai_settings = payload.get("ai_settings", {})

    if not user_query or not file_id:
        raise ValueError("缺少必要参数 query 或 file_id。")

    file_path = UPLOAD_FOLDER / secure_filename(file_id)
    if not file_path.exists():
        raise FileNotFoundError("找不到指定的数据文件，请重新上传。")

    agent = LLMAgent(
        current_line=ai_settings.get("line", "line1"),
        current_model=ai_settings.get("model"),
    )
    intent_result = agent.parse_intent(user_query, schema_info, template_mgr.get_all_templates())
    if intent_result.get("status") == "error":
        raise RuntimeError(intent_result.get("message", "意图解析失败。"))

    selected_template = intent_result.get("selected_template")
    variable_mapping = _merge_manual_mappings(intent_result.get("variable_mapping", {}), manual_mappings)
    build = _build_script(file_id, selected_template, variable_mapping)

    return {
        "status": "success",
        "plan_id": f"plan_{uuid.uuid4().hex[:8]}",
        "query": user_query,
        "file_id": file_id,
        "selected_template": selected_template,
        "template_info": build["template_info"],
        "variable_mapping": variable_mapping,
        "data_structure": build["data_structure"],
        "ai_reasoning": intent_result.get("reasoning", "已完成分析设计。"),
        "do_preview": build["do_script"],
    }


def _execute_task(task_id, plan):
    _set_task(task_id, status="running", message="Stata 正在执行分析任务。")
    try:
        build = _build_script(plan["file_id"], plan["selected_template"], plan["variable_mapping"])
        execution_result = worker.execute_script(build["do_script"], task_id=task_id)
        execution_result["ai_reasoning"] = plan.get("ai_reasoning")
        execution_result["selected_template"] = plan.get("selected_template")
        execution_result["variable_mapping"] = plan.get("variable_mapping")
        execution_result["data_structure"] = build["data_structure"]

        # 执行层（跑没跑通）与校验层（结果像不像话）是两件事，这里是二者的交汇点。
        # 只有执行成功时才做结果体检 —— 失败时日志本身就不完整，体检会产生噪声警告。
        execution_result["audit"] = _audit_result(
            plan.get("selected_template"), execution_result
        )

        _set_task(task_id, status=execution_result.get("status", "finished"), result=execution_result)
    except Exception as exc:
        _set_task(
            task_id,
            status="error",
            result={"status": "error", "message": f"任务执行失败: {exc}"},
        )


def _audit_result(selected_template, execution_result):
    """
    对执行结果做合理性体检。**只提醒，不改变任务成败** —— 结果照常可下载。

    边界说明：本层只做客观自相矛盾检查（样本量为 0、系数无量纲、区间倒挂、
    聚类过少等），不做计量学推理。凡是需要领域判断的检查都不属于这里。
    """
    if execution_result.get("status") != "success":
        return {"status": "skipped", "warning_count": 0, "warnings": []}

    try:
        report = auditor.audit(selected_template, execution_result.get("log", ""))
        # 磁盘级复核：日志里的数字是转述，产物文件才是原始事实
        extra = auditor.audit_artifacts(execution_result.get("files"), worker.workspace)
        if extra:
            merged = {item["code"]: item for item in report["warnings"]}
            for item in extra:
                merged.setdefault(item["code"], item)
            report["warnings"] = list(merged.values())
            report["warning_count"] = len(report["warnings"])
            report["status"] = "warning"
        return report
    except Exception as exc:
        # 体检本身出错绝不能连累主流程 —— 用户该拿到结果还是要拿到
        return {
            "status": "error",
            "warning_count": 0,
            "warnings": [],
            "message": f"结果体检未能完成（不影响本次执行结果）：{exc}",
        }


@app.route("/api/upload", methods=["POST"])
def upload_file():
    if "file" not in request.files:
        return jsonify({"status": "error", "message": "没有找到上传文件。"}), 400

    uploaded = request.files["file"]
    if uploaded.filename == "":
        return jsonify({"status": "error", "message": "文件名为空。"}), 400

    if not uploaded.filename.lower().endswith(".dta"):
        return jsonify({"status": "error", "message": "目前仅支持上传 .dta 文件。"}), 400

    try:
        filename = secure_filename(uploaded.filename)
        unique_filename = f"{uuid.uuid4().hex[:8]}_{filename}"
        file_path = UPLOAD_FOLDER / unique_filename
        uploaded.save(file_path)
        schema_info = _schema_from_dta(file_path)
        return jsonify(
            {
                "status": "success",
                "message": "文件解析成功。",
                "file_id": unique_filename,
                "schema": schema_info,
            }
        )
    except Exception as exc:
        return jsonify({"status": "error", "message": f"解析文件失败: {exc}"}), 500


@app.route("/api/health", methods=["GET"])
def health_check():
    return jsonify({"status": "ok", "message": "Text2Stata backend is running."})


@app.route("/api/plan", methods=["POST"])
def create_plan():
    try:
        plan = _create_plan(request.get_json(silent=True) or {})
        return jsonify(plan)
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route("/api/analyze", methods=["POST"])
def enqueue_analysis():
    payload = request.get_json(silent=True) or {}
    plan = payload.get("plan")
    try:
        if not plan:
            plan = _create_plan(payload)

        task_id = f"task_{uuid.uuid4().hex[:8]}"
        _set_task(
            task_id,
            status="queued",
            message="任务已进入队列。",
            plan={
                "plan_id": plan.get("plan_id"),
                "query": plan.get("query"),
                "selected_template": plan.get("selected_template"),
                "variable_mapping": plan.get("variable_mapping"),
            },
        )
        executor.submit(_execute_task, task_id, plan)
        return jsonify({"status": "queued", "task_id": task_id, "message": "任务已提交。"}), 202
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route("/api/task/<task_id>", methods=["GET"])
def get_task(task_id):
    task = _get_task(task_id)
    if not task:
        return jsonify({"status": "error", "message": "任务不存在或已过期。"}), 404
    return jsonify(task)


@app.route("/api/download/<filename>", methods=["GET"])
def download_result(filename):
    safe_name = os.path.basename(filename)
    file_path = WORKSPACE_DIR / safe_name
    if not file_path.exists():
        return "文件不存在或已过期。", 404
    return send_from_directory(WORKSPACE_DIR, safe_name, as_attachment=True)


# --------------------------------------------------------------------------
# 配置：读取 / 保存 / Stata 探测
#
# 打包版没有 .env 可以手改，设置面板就是唯一入口，
# 所以这几个接口必须能拿到当前生效值、也能写回磁盘。
# --------------------------------------------------------------------------
def _stata_status():
    ok, message = worker.check_environment()
    return {"ok": ok, "path": worker.stata_path, "message": message}


def _config_payload():
    return {
        "status": "success",
        "config": config.public_config(),
        "llm_ready": config.has_usable_llm(),
        "stata": _stata_status(),
    }


@app.route("/api/config", methods=["GET"])
def read_config():
    return jsonify(_config_payload())


@app.route("/api/config", methods=["POST"])
def update_config():
    patch = request.get_json(silent=True)
    if not isinstance(patch, dict):
        return jsonify({"status": "error", "message": "配置格式不正确。"}), 400
    try:
        config.save_config(patch)
        apply_runtime_config()
    except Exception as exc:
        return jsonify({"status": "error", "message": f"保存配置失败：{exc}"}), 500

    payload = _config_payload()
    payload["message"] = "配置已保存并生效。"
    return jsonify(payload)


@app.route("/api/stata/detect", methods=["GET"])
def detect_stata():
    return jsonify({"status": "success", "candidates": stata_detect.detect()})


# --------------------------------------------------------------------------
# 前端托管
#
# 打包版的目标是"双击就打开浏览器"，不能再让用户自己去磁盘里找 index.html。
# --------------------------------------------------------------------------
@app.route("/")
def index_page():
    if not (FRONTEND_DIR / "index.html").exists():
        return (
            jsonify({"status": "error", "message": f"找不到前端文件：{FRONTEND_DIR / 'index.html'}"}),
            500,
        )
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.route("/<path:filename>")
def frontend_asset(filename):
    if filename.startswith("api/"):
        return jsonify({"status": "error", "message": "接口不存在。"}), 404

    root = FRONTEND_DIR.resolve()
    candidate = (root / filename).resolve()
    # 防目录穿越：解析后的路径必须仍在 frontend 目录内
    if root not in candidate.parents:
        return jsonify({"status": "error", "message": "非法路径。"}), 403
    if not candidate.is_file():
        return jsonify({"status": "error", "message": "资源不存在。"}), 404
    return send_from_directory(FRONTEND_DIR, filename)


if __name__ == "__main__":
    apply_runtime_config()
    print("=" * 62)
    print(f"  Text2Stata 已启动 → http://{config.HOST}:{config.PORT}")
    print(f"  {paths.describe()}")
    ok, message = worker.check_environment()
    print(f"  Stata：{message}")
    if not config.has_usable_llm():
        print("  提示：还没有配置大模型密钥，请在网页右上角「设置」中填写。")
    print("=" * 62)
    app.run(host=config.HOST, port=config.PORT, debug=config.FLASK_DEBUG)

