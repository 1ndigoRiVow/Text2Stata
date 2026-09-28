import copy
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pyreadstat
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from werkzeug.utils import secure_filename

from core.config import CORS_ORIGINS, FLASK_DEBUG, MAX_UPLOAD_MB, STATA_PATH
from core.llm_agent import LLMAgent
from core.logic_center import generate_stata_header
from core.stata_worker import StataWorker
from core.template_manager import TemplateManager


BASE_DIR = Path(__file__).resolve().parent
UPLOAD_FOLDER = BASE_DIR / "uploads"
WORKSPACE_DIR = BASE_DIR / "workspace"
UPLOAD_FOLDER.mkdir(exist_ok=True)
WORKSPACE_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
CORS(app, resources={r"/api/*": {"origins": "*"}})

template_mgr = TemplateManager()
worker = StataWorker(stata_path=STATA_PATH)

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


def _build_script(file_id, selected_template, variable_mapping):
    file_path = UPLOAD_FOLDER / secure_filename(file_id)
    if not file_path.exists():
        raise FileNotFoundError("找不到指定的数据文件，请重新上传。")

    template_info = _validate_template(selected_template)
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
        _set_task(task_id, status=execution_result.get("status", "finished"), result=execution_result)
    except Exception as exc:
        _set_task(
            task_id,
            status="error",
            result={"status": "error", "message": f"任务执行失败: {exc}"},
        )


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


if __name__ == "__main__":
    print("Text2Stata backend is running at http://127.0.0.1:5000")
    app.run(host="0.0.0.0", port=5000, debug=FLASK_DEBUG)
