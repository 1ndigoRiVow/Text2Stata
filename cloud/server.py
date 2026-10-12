"""
Text2Stata 云端骨架（Phase 1）。

职责边界（docs/DEPLOYMENT.md 第三节）：只做控制平面——
账号、模板装配、任务派发、结果归档与体检。不碰数据本体，不跑 Stata。

接口契约见 docs/PROTOCOL.md，本文件按它实现，改协议先改文档。

运行：
    python -m cloud.server                 # 开发模式，监听 127.0.0.1:8000
    T2S_CLOUD_PORT=8000 python -m cloud.server

环境变量：
    T2S_CLOUD_DATA_DIR   数据目录（SQLite、产物、密钥），默认 ./cloud_data
    T2S_CLOUD_HOST       监听地址，默认 127.0.0.1
    T2S_CLOUD_PORT       端口，默认 8000
    T2S_CLOUD_SECURE_COOKIE=1  会话 Cookie 加 Secure（HTTPS 部署时开）
"""

from __future__ import annotations

import functools
import hashlib
import os
import secrets
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, g, jsonify, request, send_file, session
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from . import plans
from .db import TERMINAL_STATES, Database, utcnow

# plans 已把 backend/ 加进 sys.path，结果体检层与本地版共用一份实现
from core.result_auditor import ResultAuditor  # noqa: E402

# --------------------------------------------------------------------------
# 常量（协议第五、六节的数字都在这里）
# --------------------------------------------------------------------------
REQUEUE_AFTER_SECONDS = 600       # dispatched/running 超过这么久没回报 → 回收重投
MAX_LOG_CHARS = 2 * 1024 * 1024   # 日志上限 2 MB
MAX_SCHEMA_VARS = 2000            # schema 变量数上限
MAX_ARTIFACT_BYTES = 50 * 1024 * 1024
ARTIFACT_SUFFIXES = (".rtf", ".doc", ".docx", ".csv", ".png", ".gph")

# 代理回报的状态 → 云端任务状态
_STATUS_MAP = {"success": "succeeded", "failed": "failed", "error": "error"}


def _new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_app(data_dir=None, db_path=None) -> Flask:
    data_dir = Path(
        data_dir
        or os.getenv("T2S_CLOUD_DATA_DIR")
        or (Path(__file__).resolve().parents[1] / "cloud_data")
    )
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "artifacts").mkdir(exist_ok=True)

    app = Flask(__name__)
    app.secret_key = _load_or_create_secret(data_dir)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = os.getenv("T2S_CLOUD_SECURE_COOKIE") == "1"
    app.config["MAX_CONTENT_LENGTH"] = MAX_ARTIFACT_BYTES

    db = Database(db_path or (data_dir / "cloud.db"))
    auditor = ResultAuditor()
    app.extensions["t2s_db"] = db
    app.extensions["t2s_data_dir"] = data_dir

    bootstrap_invite = _ensure_initial_invite(db)

    # ------------------------------------------------------------------
    # 鉴权辅助
    # ------------------------------------------------------------------
    def current_user():
        user_id = session.get("user_id")
        if not user_id:
            return None
        return db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))

    def login_required(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            user = current_user()
            if not user:
                return jsonify({"status": "error", "message": "未登录。"}), 401
            g.user = user
            return view(*args, **kwargs)

        return wrapper

    def agent_required(view):
        """Bearer 令牌 → g.agent。令牌只存哈希，比对用哈希。"""
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            header = request.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                return jsonify({"status": "error", "message": "缺少代理令牌。"}), 401
            token_hash = _hash_token(header[len("Bearer "):].strip())
            agent = db.query_one("SELECT * FROM agents WHERE token_hash = ?", (token_hash,))
            if not agent:
                return jsonify({"status": "error", "message": "代理令牌无效。"}), 401
            g.agent = agent
            return view(*args, **kwargs)

        return wrapper

    def get_owned_task(task_id, owner_field="user_id", owner_id=None):
        return db.query_one(
            f"SELECT * FROM tasks WHERE id = ? AND {owner_field} = ?",
            (task_id, owner_id),
        )

    # ------------------------------------------------------------------
    # 3.1 认证
    # ------------------------------------------------------------------
    @app.post("/api/auth/register")
    def register():
        payload = request.get_json(silent=True) or {}
        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")
        invite_code = str(payload.get("invite_code") or "").strip()
        if not username or not password or not invite_code:
            return jsonify({"status": "error", "message": "用户名、密码、邀请码均为必填。"}), 400
        if len(password) < 8:
            return jsonify({"status": "error", "message": "密码至少 8 位。"}), 400

        invite = db.query_one("SELECT * FROM invites WHERE code = ?", (invite_code,))
        if not invite or invite.get("used_by"):
            return jsonify({"status": "error", "message": "邀请码无效或已被使用。"}), 403
        if db.query_one("SELECT id FROM users WHERE username = ?", (username,)):
            return jsonify({"status": "error", "message": "用户名已被占用。"}), 409

        is_first = not db.query_one("SELECT id FROM users LIMIT 1")
        user_id = _new_id("u")
        db.execute(
            "INSERT INTO users (id, username, pw_hash, is_admin, created_at) VALUES (?,?,?,?,?)",
            (user_id, username, generate_password_hash(password), 1 if is_first else 0, utcnow()),
        )
        db.execute(
            "UPDATE invites SET used_by = ?, used_at = ? WHERE code = ?",
            (user_id, utcnow(), invite_code),
        )
        session["user_id"] = user_id
        return jsonify({"status": "success", "user": _public_user(db.query_one(
            "SELECT * FROM users WHERE id = ?", (user_id,)))}), 201

    @app.post("/api/auth/login")
    def login():
        payload = request.get_json(silent=True) or {}
        user = db.query_one(
            "SELECT * FROM users WHERE username = ?",
            (str(payload.get("username") or "").strip(),),
        )
        if not user or not check_password_hash(user["pw_hash"], str(payload.get("password") or "")):
            return jsonify({"status": "error", "message": "用户名或密码不正确。"}), 401
        session["user_id"] = user["id"]
        return jsonify({"status": "success", "user": _public_user(user)})

    @app.post("/api/auth/logout")
    def logout():
        session.clear()
        return jsonify({"status": "success"})

    @app.get("/api/auth/me")
    @login_required
    def me():
        return jsonify({"status": "success", "user": _public_user(g.user)})

    @app.post("/api/auth/invites")
    @login_required
    def create_invite():
        code = secrets.token_urlsafe(8)
        db.execute(
            "INSERT INTO invites (code, created_by, created_at) VALUES (?,?,?)",
            (code, g.user["id"], utcnow()),
        )
        return jsonify({"status": "success", "code": code}), 201

    @app.post("/api/auth/settings")
    @login_required
    def update_settings():
        payload = request.get_json(silent=True) or {}
        mode = payload.get("artifact_mode")
        if mode not in ("cloud", "local"):
            return jsonify({"status": "error", "message": "artifact_mode 只能是 cloud 或 local。"}), 400
        db.execute("UPDATE users SET artifact_mode = ? WHERE id = ?", (mode, g.user["id"]))
        return jsonify({"status": "success", "artifact_mode": mode})

    # ------------------------------------------------------------------
    # 3.2 代理管理（用户侧）
    # ------------------------------------------------------------------
    @app.post("/api/agents")
    @login_required
    def create_agent():
        payload = request.get_json(silent=True) or {}
        name = str(payload.get("name") or "").strip() or "未命名代理"
        agent_id = _new_id("ag")
        token = f"t2sa_{secrets.token_hex(20)}"
        db.execute(
            "INSERT INTO agents (id, user_id, name, token_hash, created_at) VALUES (?,?,?,?,?)",
            (agent_id, g.user["id"], name, _hash_token(token), utcnow()),
        )
        # 令牌只在这一次完整返回，之后云端也无法再告诉你（只存了哈希）
        return jsonify({"status": "success", "agent_id": agent_id, "token": token}), 201

    @app.get("/api/agents")
    @login_required
    def list_agents():
        rows = db.query_all(
            "SELECT id, name, last_seen, created_at FROM agents "
            "WHERE user_id = ? AND token_hash != '' ORDER BY created_at",
            (g.user["id"],),
        )
        return jsonify({"status": "success", "agents": rows})

    @app.delete("/api/agents/<agent_id>")
    @login_required
    def delete_agent(agent_id):
        agent = db.query_one(
            "SELECT * FROM agents WHERE id = ? AND user_id = ?", (agent_id, g.user["id"])
        )
        if not agent:
            return jsonify({"status": "error", "message": "代理不存在。"}), 404
        # 进行中的任务不可能再被完成：直接置 error，不留悬挂任务
        db.execute(
            "UPDATE tasks SET status = 'error', result_json = ?, updated_at = ? "
            "WHERE agent_id = ? AND status IN ('queued','dispatched','running')",
            (Database.dumps({"message": "执行代理已被吊销，任务终止。"}), utcnow(), agent_id),
        )
        # 不物理删除（datasets/tasks 有外键引用）：清空令牌哈希即永久吊销，
        # 任何 Bearer token 的 sha256 都不可能等于空串
        db.execute("UPDATE agents SET token_hash = '' WHERE id = ?", (agent_id,))
        return jsonify({"status": "success"})

    # ------------------------------------------------------------------
    # 3.3 数据集（用户侧只读；注册走代理侧 4.3）
    # ------------------------------------------------------------------
    @app.get("/api/datasets")
    @login_required
    def list_datasets():
        rows = db.query_all(
            "SELECT id, agent_id, name, schema_json, created_at FROM datasets "
            "WHERE user_id = ? ORDER BY created_at DESC",
            (g.user["id"],),
        )
        for row in rows:
            row["schema"] = Database.loads(row.pop("schema_json"), [])
        return jsonify({"status": "success", "datasets": rows})

    # ------------------------------------------------------------------
    # 3.4 任务（用户侧）
    # ------------------------------------------------------------------
    @app.post("/api/tasks")
    @login_required
    def create_task():
        payload = request.get_json(silent=True) or {}
        dataset_id = payload.get("dataset_id")
        selected_template = payload.get("selected_template")
        variable_mapping = payload.get("variable_mapping")
        if not dataset_id or not selected_template or not isinstance(variable_mapping, dict):
            return jsonify({"status": "error",
                            "message": "缺少 dataset_id / selected_template / variable_mapping。"}), 400

        dataset = db.query_one(
            "SELECT * FROM datasets WHERE id = ? AND user_id = ?", (dataset_id, g.user["id"])
        )
        if not dataset:
            return jsonify({"status": "error", "message": "数据集不存在。"}), 404

        try:
            build = plans.build_task_script(selected_template, variable_mapping)
        except ValueError as exc:
            return jsonify({"status": "error", "message": str(exc)}), 400

        task_id = _new_id("task")
        now = utcnow()
        db.execute(
            "INSERT INTO tasks (id, user_id, agent_id, dataset_id, query, selected_template, "
            "mapping_json, do_script, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,'queued',?,?)",
            (
                task_id, g.user["id"], dataset["agent_id"], dataset_id,
                str(payload.get("query") or ""), selected_template,
                Database.dumps(build["variable_mapping"]), build["do_script"], now, now,
            ),
        )
        return jsonify({"status": "queued", "task_id": task_id}), 202

    @app.get("/api/tasks")
    @login_required
    def list_tasks():
        rows = db.query_all(
            "SELECT id, dataset_id, query, selected_template, status, created_at, updated_at "
            "FROM tasks WHERE user_id = ? ORDER BY created_at DESC LIMIT 100",
            (g.user["id"],),
        )
        return jsonify({"status": "success", "tasks": rows})

    @app.get("/api/tasks/<task_id>")
    @login_required
    def get_task(task_id):
        task = get_owned_task(task_id, "user_id", g.user["id"])
        if not task:
            return jsonify({"status": "error", "message": "任务不存在。"}), 404
        return jsonify({"status": "success", "task": _public_task(db, task)})

    @app.get("/api/tasks/<task_id>/log")
    @login_required
    def get_task_log(task_id):
        task = get_owned_task(task_id, "user_id", g.user["id"])
        if not task:
            return jsonify({"status": "error", "message": "任务不存在。"}), 404
        return app.response_class(task.get("log") or "", mimetype="text/plain")

    @app.get("/api/tasks/<task_id>/artifacts/<filename>")
    @login_required
    def download_artifact(task_id, filename):
        task = get_owned_task(task_id, "user_id", g.user["id"])
        if not task:
            return jsonify({"status": "error", "message": "任务不存在。"}), 404
        safe = os.path.basename(filename)
        row = db.query_one(
            "SELECT * FROM artifacts WHERE task_id = ? AND filename = ?", (task_id, safe)
        )
        if not row or not Path(row["path"]).exists():
            return jsonify({"status": "error", "message": "产物不存在（或本任务产物只保留在本机）。"}), 404
        return send_file(row["path"], as_attachment=True, download_name=safe)

    # ------------------------------------------------------------------
    # 4.x 代理侧
    # ------------------------------------------------------------------
    @app.post("/api/agent/datasets")
    @agent_required
    def register_dataset():
        payload = request.get_json(silent=True) or {}
        name = str(payload.get("name") or "").strip()
        schema = payload.get("schema")
        if not name or not isinstance(schema, list) or not schema:
            return jsonify({"status": "error", "message": "缺少 name 或 schema。"}), 400
        if len(schema) > MAX_SCHEMA_VARS:
            return jsonify({"status": "error",
                            "message": f"变量数超过上限（{MAX_SCHEMA_VARS}）。"}), 400

        dataset_id = _new_id("ds")
        db.execute(
            "INSERT INTO datasets (id, user_id, agent_id, name, schema_json, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (dataset_id, g.agent["user_id"], g.agent["id"], name,
             Database.dumps(schema), utcnow()),
        )
        return jsonify({"status": "success", "dataset_id": dataset_id}), 201

    @app.post("/api/agent/poll")
    @agent_required
    def agent_poll():
        _requeue_stale(db)
        db.execute("UPDATE agents SET last_seen = ? WHERE id = ?", (utcnow(), g.agent["id"]))

        task = db.query_one(
            "SELECT * FROM tasks WHERE agent_id = ? AND status = 'queued' "
            "ORDER BY created_at LIMIT 1",
            (g.agent["id"],),
        )
        if not task:
            return jsonify({"task": None})

        db.execute("UPDATE tasks SET status = 'dispatched', updated_at = ? WHERE id = ?",
                   (utcnow(), task["id"]))
        owner = db.query_one("SELECT artifact_mode FROM users WHERE id = ?", (task["user_id"],))
        artifact_upload = bool(owner and owner.get("artifact_mode") == "cloud")
        db.execute("UPDATE tasks SET artifact_upload = ? WHERE id = ?",
                   (1 if artifact_upload else 0, task["id"]))

        return jsonify({"task": {
            "task_id": task["id"],
            "dataset_id": task["dataset_id"],
            "do_script": task["do_script"],
            "artifact_upload": artifact_upload,
        }})

    @app.post("/api/agent/tasks/<task_id>/artifacts")
    @agent_required
    def upload_artifact(task_id):
        task = get_owned_task(task_id, "agent_id", g.agent["id"])
        if not task:
            return jsonify({"status": "error", "message": "任务不存在。"}), 404
        if task["status"] in TERMINAL_STATES:
            return jsonify({"status": "error", "message": "任务已进入终态，不再接收产物。"}), 409
        if "file" not in request.files:
            return jsonify({"status": "error", "message": "没有找到上传文件。"}), 400

        uploaded = request.files["file"]
        safe = secure_filename(uploaded.filename or "")
        if not safe or not safe.lower().endswith(ARTIFACT_SUFFIXES):
            return jsonify({"status": "error", "message": "不支持的产物类型。"}), 400

        task_dir = data_dir / "artifacts" / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        target = task_dir / safe
        uploaded.save(target)
        size = target.stat().st_size

        db.execute(
            "INSERT INTO artifacts (id, task_id, filename, path, size, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (_new_id("art"), task_id, safe, str(target), size, utcnow()),
        )
        return jsonify({"status": "success", "stored": safe, "size": size}), 201

    @app.post("/api/agent/tasks/<task_id>/result")
    @agent_required
    def report_result(task_id):
        task = get_owned_task(task_id, "agent_id", g.agent["id"])
        if not task:
            return jsonify({"status": "error", "message": "任务不存在。"}), 404
        if task["status"] in TERMINAL_STATES:
            return jsonify({"status": "error", "message": "任务已进入终态，重复回报被拒绝。"}), 409

        payload = request.get_json(silent=True) or {}
        status = _STATUS_MAP.get(payload.get("status"))
        if not status:
            return jsonify({"status": "error",
                            "message": "status 必须是 success / failed / error。"}), 400

        log_text = str(payload.get("log") or "")
        if len(log_text) > MAX_LOG_CHARS:
            log_text = log_text[:MAX_LOG_CHARS] + "\n... [日志过长，已截断]"

        result_summary = {
            "status": payload.get("status"),
            "message": str(payload.get("message") or ""),
            "error_code": payload.get("error_code"),
            "diagnosis": payload.get("diagnosis"),
            "time_cost": payload.get("time_cost"),
            "files": payload.get("files") or [],
        }

        # 体检层与本地版同一个边界：只对跑通的结果做体检，只提醒不改成败
        audit = {"status": "skipped", "warning_count": 0, "warnings": []}
        if status == "succeeded":
            try:
                audit = auditor.audit(task["selected_template"], log_text)
                extra = auditor.audit_artifacts(
                    result_summary["files"], data_dir / "artifacts" / task_id
                )
                if extra:
                    merged = {item["code"]: item for item in audit["warnings"]}
                    for item in extra:
                        merged.setdefault(item["code"], item)
                    audit["warnings"] = list(merged.values())
                    audit["warning_count"] = len(audit["warnings"])
                    audit["status"] = "warning"
            except Exception as exc:
                audit = {"status": "error", "warning_count": 0, "warnings": [],
                         "message": f"结果体检未能完成（不影响本次执行结果）：{exc}"}

        db.execute(
            "UPDATE tasks SET status = ?, result_json = ?, log = ?, audit_json = ?, updated_at = ? "
            "WHERE id = ?",
            (status, Database.dumps(result_summary), log_text,
             Database.dumps(audit), utcnow(), task_id),
        )
        return jsonify({"status": "ok", "audit": audit})

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok", "service": "text2stata-cloud"})

    if bootstrap_invite:
        print("=" * 62)
        print("  数据库为空，已生成初始邀请码（仅此一处可见，用完即废）：")
        print(f"    {bootstrap_invite}")
        print("  第一个注册的用户自动成为管理员。")
        print("=" * 62)

    return app


# --------------------------------------------------------------------------
# 模块级辅助
# --------------------------------------------------------------------------
def _load_or_create_secret(data_dir: Path) -> str:
    """会话签名密钥持久化——重启不挤掉已登录用户。"""
    secret_file = data_dir / "secret_key"
    if secret_file.exists():
        return secret_file.read_text(encoding="utf-8").strip()
    secret = secrets.token_hex(32)
    secret_file.write_text(secret, encoding="utf-8")
    return secret


def _ensure_initial_invite(db: Database):
    """空库时生成初始邀请码，返回邀请码字符串；否则返回 None。"""
    if db.query_one("SELECT id FROM users LIMIT 1"):
        return None
    if db.query_one("SELECT code FROM invites WHERE used_by IS NULL LIMIT 1"):
        return None
    code = secrets.token_urlsafe(8)
    db.execute("INSERT INTO invites (code, created_at) VALUES (?,?)", (code, utcnow()))
    return code


def _public_user(user) -> dict:
    return {
        "id": user["id"],
        "username": user["username"],
        "is_admin": bool(user["is_admin"]),
        "artifact_mode": user["artifact_mode"],
    }


def _public_task(db: Database, task) -> dict:
    artifacts = db.query_all(
        "SELECT filename, size, created_at FROM artifacts WHERE task_id = ? ORDER BY filename",
        (task["id"],),
    )
    return {
        "task_id": task["id"],
        "dataset_id": task["dataset_id"],
        "query": task["query"],
        "selected_template": task["selected_template"],
        "variable_mapping": Database.loads(task["mapping_json"], {}),
        "do_script": task["do_script"],
        "status": task["status"],
        "artifact_upload": bool(task["artifact_upload"]),
        "result": Database.loads(task["result_json"]),
        "audit": Database.loads(task["audit_json"]),
        "artifacts": artifacts,
        "created_at": task["created_at"],
        "updated_at": task["updated_at"],
    }


def _requeue_stale(db: Database) -> None:
    """回收超时任务：dispatched/running 太久没动静 → 回到 queued 重新排队。"""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=REQUEUE_AFTER_SECONDS)).isoformat(
        timespec="seconds"
    )
    db.execute(
        "UPDATE tasks SET status = 'queued', updated_at = ? "
        "WHERE status IN ('dispatched','running') AND updated_at < ?",
        (utcnow(), cutoff),
    )


def main():
    app = create_app()
    host = os.getenv("T2S_CLOUD_HOST", "127.0.0.1")
    port = int(os.getenv("T2S_CLOUD_PORT", "8000"))
    print(f"Text2Stata 云端骨架 → http://{host}:{port}")
    app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
