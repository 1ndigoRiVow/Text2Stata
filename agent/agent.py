"""
Text2Stata 本地执行代理（Phase 2）。

跑在用户自己的机器上，做三件事：
  1. 本机小服务（127.0.0.1）：接收 .dta 上传 → 提取 schema → 注册到云端（协议 4.3 / 五）
  2. 轮询云端领任务（协议 4.1）：拿到 .do 脚本，把 __T2S_DATA_PATH__ 换成本机路径
  3. 调本机 Stata 执行（复用 StataWorker），先传产物（4.4）再回报结果（4.2）

数据边界：.dta 全程不离开这台机器，发往云端的只有变量名+标签、日志与（可选的）产物。

用法：
    python -m agent.agent init --cloud http://127.0.0.1:8000 --token t2sa_xxx
    python -m agent.agent run                 # 常驻：轮询 + 本机小服务
    python -m agent.agent once                # 只跑一个轮询周期（调试用）
    python -m agent.agent upload data.dta     # 注册一份数据集到云端

配置：<数据目录>/agent.json（init 生成，也可手改）
数据目录优先级：--data-dir > 环境变量 T2S_AGENT_DATA_DIR > ./agent_data
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

import requests

# 复用 backend/core 的内核（StataWorker / 自动探测），不复制实现
_BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from core.stata_worker import StataWorker  # noqa: E402

DEFAULT_LOCAL_PORT = 5100
REQUEST_TIMEOUT = 30


# --------------------------------------------------------------------------
# 配置与本地数据集登记
# --------------------------------------------------------------------------
class AgentConfig:
    """agent.json 的读写。字段：cloud_url / token / stata_path / poll_interval / local_port"""

    FIELDS = ("cloud_url", "token", "stata_path", "poll_interval", "local_port")

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.path = data_dir / "agent.json"
        self.values = {
            "cloud_url": "",
            "token": "",
            "stata_path": "",
            "poll_interval": 2,
            "local_port": DEFAULT_LOCAL_PORT,
        }
        if self.path.exists():
            try:
                stored = json.loads(self.path.read_text(encoding="utf-8"))
                for key in self.FIELDS:
                    if key in stored and stored[key] not in (None, ""):
                        self.values[key] = stored[key]
            except (json.JSONDecodeError, OSError):
                pass

    def save(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.values, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @property
    def cloud_url(self):
        return str(self.values["cloud_url"]).rstrip("/")


class DatasetRegistry:
    """本地数据集台账：dataset_id → 本机 .dta 路径。云端只知道 schema。"""

    def __init__(self, data_dir: Path):
        self.path = data_dir / "datasets.json"
        self.store_dir = data_dir / "datasets"
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self._datasets = {}
        if self.path.exists():
            try:
                self._datasets = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._datasets = {}

    def _save(self):
        self.path.write_text(
            json.dumps(self._datasets, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def add(self, dataset_id, name, file_path):
        self._datasets[dataset_id] = {"name": name, "path": str(file_path)}
        self._save()

    def get(self, dataset_id):
        return self._datasets.get(dataset_id)

    def all(self):
        return dict(self._datasets)


# --------------------------------------------------------------------------
# 云端通信
# --------------------------------------------------------------------------
class CloudClient:
    def __init__(self, config: AgentConfig):
        self.config = config
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {config.values['token']}"

    def _url(self, path):
        return f"{self.config.cloud_url}{path}"

    def ping(self):
        resp = self.session.get(self._url("/api/health"), timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    def register_dataset(self, name, schema):
        resp = self.session.post(
            self._url("/api/agent/datasets"),
            json={"name": name, "schema": schema},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()["dataset_id"]

    def poll(self):
        resp = self.session.post(self._url("/api/agent/poll"), json={}, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json().get("task")

    def upload_artifact(self, task_id, file_path: Path):
        with open(file_path, "rb") as fh:
            resp = self.session.post(
                self._url(f"/api/agent/tasks/{task_id}/artifacts"),
                files={"file": (file_path.name, fh)},
                timeout=max(REQUEST_TIMEOUT, 120),
            )
        resp.raise_for_status()
        return resp.json()

    def report_result(self, task_id, payload):
        resp = self.session.post(
            self._url(f"/api/agent/tasks/{task_id}/result"),
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()


# --------------------------------------------------------------------------
# 代理本体
# --------------------------------------------------------------------------
class LocalAgent:
    def __init__(self, config: AgentConfig, registry: DatasetRegistry, worker: StataWorker):
        self.config = config
        self.registry = registry
        self.worker = worker
        self.cloud = CloudClient(config)
        self.last_task = None  # 给 /status 看的最近一条任务记录

    # ---------------- 数据集注册 ----------------
    def register_local_dataset(self, dta_path: Path):
        """存档 .dta → 提取 schema → 注册到云端。返回 (dataset_id, schema)。"""
        import pyreadstat

        if not dta_path.exists():
            raise FileNotFoundError(f"找不到文件：{dta_path}")
        if dta_path.suffix.lower() != ".dta":
            raise ValueError("目前仅支持 .dta 文件。")

        archived = self.registry.store_dir / f"{uuid.uuid4().hex[:8]}_{dta_path.name}"
        archived.write_bytes(dta_path.read_bytes())

        _, meta = pyreadstat.read_dta(str(archived))
        schema = [
            {"name": col, "label": meta.column_names_to_labels.get(col, "") or ""}
            for col in meta.column_names
        ]

        dataset_id = self.cloud.register_dataset(dta_path.name, schema)
        self.registry.add(dataset_id, dta_path.name, archived)
        return dataset_id, schema

    # ---------------- 任务执行 ----------------
    def run_once(self):
        """跑一个轮询周期。返回 True 表示这次领到了活。"""
        task = self.cloud.poll()
        if not task:
            return False

        task_id = task["task_id"]
        print(f"[agent] 领到任务 {task_id}")
        try:
            self._execute(task)
        except Exception as exc:
            # 代理自身的异常（网络断、磁盘满…）也要让云端知道，别留悬挂任务
            try:
                self.cloud.report_result(task_id, {
                    "status": "error",
                    "message": f"代理执行异常：{exc}",
                    "log": "", "files": [],
                })
            except Exception as report_exc:
                print(f"[agent] 回报失败（任务 {task_id} 云端将超时回收）：{report_exc}")
        return True

    def _execute(self, task):
        task_id = task["task_id"]
        dataset = self.registry.get(task["dataset_id"])
        if not dataset or not Path(dataset["path"]).exists():
            self.cloud.report_result(task_id, {
                "status": "error",
                "error_code": "dataset_missing",
                "message": "本机找不到该数据集（可能换过机器或重装了代理），请重新上传数据。",
                "log": "", "files": [],
            })
            self.last_task = {"task_id": task_id, "status": "error",
                              "message": "dataset_missing"}
            return

        local_path = Path(dataset["path"]).resolve().as_posix()
        do_script = task["do_script"].replace("__T2S_DATA_PATH__", local_path)

        result = self.worker.execute_script(do_script, task_id=task_id)

        # 顺序按协议：先传产物，再回报结果（云端收到结果时就要做体检）
        if task.get("artifact_upload") and result.get("files"):
            for name in result["files"]:
                file_path = self.worker.workspace / name
                if file_path.exists():
                    self.cloud.upload_artifact(task_id, file_path)
                    print(f"[agent] 产物已回传：{name}")

        response = self.cloud.report_result(task_id, {
            "status": result.get("status"),
            "message": result.get("message"),
            "error_code": result.get("error_code"),
            "diagnosis": result.get("diagnosis"),
            "time_cost": result.get("time_cost"),
            "log": result.get("log") or "",
            "files": result.get("files") or [],
        })

        audit = response.get("audit") or {}
        warning_count = audit.get("warning_count", 0)
        print(f"[agent] 任务 {task_id} 完成：{result.get('status')}"
              + (f"，体检 {warning_count} 条提醒" if warning_count else ""))
        self.last_task = {
            "task_id": task_id,
            "status": result.get("status"),
            "message": result.get("message"),
            "audit_warnings": warning_count,
        }

    # ---------------- 常驻循环 ----------------
    def serve_forever(self):
        interval = max(1, int(self.config.values.get("poll_interval") or 2))
        print(f"[agent] 开始轮询 {self.config.cloud_url}（每 {interval} 秒）")
        while True:
            try:
                if not self.run_once():
                    time.sleep(interval)
            except requests.RequestException as exc:
                print(f"[agent] 云端不可达（{exc}），{interval * 5} 秒后重试")
                time.sleep(interval * 5)


# --------------------------------------------------------------------------
# 本机小服务（协议第五节）：只监听 127.0.0.1
# --------------------------------------------------------------------------
def create_local_app(agent: LocalAgent, cloud_origin: str):
    from flask import Flask, jsonify, request
    from flask_cors import CORS

    app = Flask("text2stata-agent")
    # 未来云端前端会从浏览器直连这里，CORS 只放行云端站点域名
    CORS(app, resources={r"/*": {"origins": [cloud_origin] if cloud_origin else []}})

    @app.post("/datasets")
    def upload_dataset():
        if "file" not in request.files:
            return jsonify({"status": "error", "message": "没有找到上传文件。"}), 400
        uploaded = request.files["file"]
        if not (uploaded.filename or "").lower().endswith(".dta"):
            return jsonify({"status": "error", "message": "目前仅支持 .dta 文件。"}), 400
        tmp = agent.registry.store_dir / f"incoming_{uuid.uuid4().hex[:8]}.dta"
        uploaded.save(tmp)
        try:
            dataset_id, schema = agent.register_local_dataset(tmp)
        except Exception as exc:
            return jsonify({"status": "error", "message": f"注册数据集失败：{exc}"}), 502
        finally:
            tmp.unlink(missing_ok=True)
        return jsonify({"status": "success", "dataset_id": dataset_id, "schema": schema}), 201

    @app.get("/datasets")
    def list_datasets():
        rows = [
            {"dataset_id": ds_id, "name": info["name"], "path": info["path"]}
            for ds_id, info in agent.registry.all().items()
        ]
        return jsonify({"status": "success", "datasets": rows})

    @app.get("/status")
    def status():
        try:
            agent.cloud.ping()
            cloud_ok, cloud_msg = True, "云端连接正常。"
        except Exception as exc:
            cloud_ok, cloud_msg = False, f"云端不可达：{exc}"
        stata_ok, stata_msg = agent.worker.check_environment()
        return jsonify({
            "status": "success",
            "cloud": {"ok": cloud_ok, "message": cloud_msg, "url": agent.config.cloud_url},
            "stata": {"ok": stata_ok, "message": stata_msg, "path": agent.worker.stata_path},
            "last_task": agent.last_task,
        })

    return app


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _resolve_data_dir(args) -> Path:
    raw = (
        getattr(args, "data_dir", None)
        or os.getenv("T2S_AGENT_DATA_DIR")
        or (Path(__file__).resolve().parents[1] / "agent_data")
    )
    data_dir = Path(raw)
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def _build_agent(data_dir: Path) -> LocalAgent:
    config = AgentConfig(data_dir)
    if not config.values["cloud_url"] or not config.values["token"]:
        raise SystemExit("还没配置云端地址或令牌，先跑：python -m agent.agent init --cloud <url> --token <t2sa_...>")
    registry = DatasetRegistry(data_dir)
    worker = StataWorker(
        stata_path=config.values["stata_path"] or None,
        workspace=data_dir / "workspace",
    )
    return LocalAgent(config, registry, worker)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="text2stata-agent", description="Text2Stata 本地执行代理")
    parser.add_argument("--data-dir", help="代理数据目录（默认 ./agent_data）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="写入云端地址与代理令牌")
    p_init.add_argument("--cloud", required=True, help="云端地址，如 http://127.0.0.1:8000")
    p_init.add_argument("--token", required=True, help="云端创建的代理令牌 t2sa_...")
    p_init.add_argument("--stata-path", default="", help="Stata 可执行文件路径（留空自动探测）")

    p_run = sub.add_parser("run", help="常驻运行：轮询 + 本机小服务")
    p_run.add_argument("--no-local-api", action="store_true", help="不开本机小服务")

    sub.add_parser("once", help="只跑一个轮询周期")
    sub.add_parser("status", help="打印云端连通性与 Stata 自检")

    p_upload = sub.add_parser("upload", help="上传一份 .dta 并注册到云端")
    p_upload.add_argument("file", help=".dta 文件路径")

    args = parser.parse_args(argv)
    data_dir = _resolve_data_dir(args)

    if args.command == "init":
        config = AgentConfig(data_dir)
        config.values["cloud_url"] = args.cloud.rstrip("/")
        config.values["token"] = args.token.strip()
        if args.stata_path:
            config.values["stata_path"] = args.stata_path
        config.save()
        print(f"配置已写入 {config.path}")
        return

    agent = _build_agent(data_dir)

    if args.command == "once":
        got = agent.run_once()
        print("[agent] 本周期" + ("处理了 1 个任务。" if got else "没有任务。"))
    elif args.command == "status":
        app = create_local_app(agent, agent.config.cloud_url)
        with app.test_client() as client:
            print(client.get("/status").get_data(as_text=True))
    elif args.command == "upload":
        dataset_id, schema = agent.register_local_dataset(Path(args.file))
        print(f"已注册：dataset_id={dataset_id}，共 {len(schema)} 个变量")
    elif args.command == "run":
        if args.no_local_api:
            agent.serve_forever()
        else:
            local_app = create_local_app(agent, agent.config.cloud_url)
            port = int(agent.config.values.get("local_port") or DEFAULT_LOCAL_PORT)
            threading.Thread(target=agent.serve_forever, daemon=True).start()
            print(f"[agent] 本机服务 → http://127.0.0.1:{port}（上传数据走这里）")
            local_app.run(host="127.0.0.1", port=port, threaded=True)


if __name__ == "__main__":
    main()
