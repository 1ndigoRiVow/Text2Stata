# -*- coding: utf-8 -*-
"""
云端版全链路端到端验证（需要本机 Stata）。

真实链路：云端（临时目录 SQLite + 本机端口）
  → 注册/邀请/建代理 → 代理上传 testdata.dta 的 schema
  → 用户建任务（假计划：fe 模板 + 手工映射）→ 代理轮询领取
  → 本机 Stata 真跑 → 产物回传 → 云端体检 → 用户取回结果与产物

运行（项目根目录）：
    .venv/Scripts/python.exe scripts/e2e_cloud.py
"""

import sys
import tempfile
import threading
import time
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for extra in (PROJECT_ROOT, PROJECT_ROOT / "backend"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from cloud.server import create_app  # noqa: E402
from agent.agent import AgentConfig, DatasetRegistry, LocalAgent  # noqa: E402
from core.stata_worker import StataWorker  # noqa: E402

CLOUD_PORT = 8123
BASE = f"http://127.0.0.1:{CLOUD_PORT}"

# testdata.dta 里真实存在的变量（93 企业 × 14 年面板）
FE_MAPPING = {
    "y": "tfp", "x": "rnd",
    "id": "id", "time": "year",
    "controls": ["lev", "size", "roa"],
}


def step(msg):
    print(f"\n=== {msg} ===")


def main():
    tmp_cloud = tempfile.TemporaryDirectory(prefix="t2s_e2e_cloud_")
    tmp_agent = tempfile.TemporaryDirectory(prefix="t2s_e2e_agent_")

    # ---------- 起云端 ----------
    step("启动云端骨架（临时数据目录）")
    app = create_app(data_dir=tmp_cloud.name)
    from werkzeug.serving import make_server
    server = make_server("127.0.0.1", CLOUD_PORT, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    for _ in range(50):
        try:
            requests.get(f"{BASE}/api/health", timeout=2)
            break
        except requests.RequestException:
            time.sleep(0.1)
    print("云端已就绪:", BASE)

    db = app.extensions["t2s_db"]
    invite = db.query_one("SELECT code FROM invites WHERE used_by IS NULL LIMIT 1")["code"]

    # ---------- 用户侧 ----------
    step("注册首个用户 + 创建执行代理")
    sess = requests.Session()
    r = sess.post(f"{BASE}/api/auth/register",
                  json={"username": "hao", "password": "password123", "invite_code": invite})
    assert r.status_code == 201, r.text
    r = sess.post(f"{BASE}/api/agents", json={"name": "e2e-agent"})
    agent_token = r.json()["token"]
    print("代理令牌已签发（t2sa_… 只显示这一次）")

    # ---------- 代理侧 ----------
    step("配置本地代理 + 注册数据集（只上传 schema）")
    agent_dir = Path(tmp_agent.name)
    config = AgentConfig(agent_dir)
    config.values["cloud_url"] = BASE
    config.values["token"] = agent_token
    config.save()
    registry = DatasetRegistry(agent_dir)
    worker = StataWorker(stata_path=None, workspace=agent_dir / "workspace")
    ok, msg = worker.check_environment()
    assert ok, msg
    print("Stata:", worker.stata_path)

    agent = LocalAgent(config, registry, worker)
    dataset_id, schema = agent.register_local_dataset(PROJECT_ROOT / "testdata.dta")
    print(f"数据集已注册：{dataset_id}，{len(schema)} 个变量（数据本体留在本机）")

    step("用户创建任务（假计划：fe 模板）")
    r = sess.post(f"{BASE}/api/tasks", json={
        "dataset_id": dataset_id,
        "selected_template": "fe",
        "variable_mapping": FE_MAPPING,
        "query": "研发对全要素生产率的影响（企业固定效应）",
    })
    assert r.status_code == 202, r.text
    task_id = r.json()["task_id"]
    print("任务已入队:", task_id)

    step("代理领取并执行（真实 Stata，约十几秒）")
    assert agent.run_once(), "代理没领到任务"

    step("校验云端归档")
    detail = sess.get(f"{BASE}/api/tasks/{task_id}").json()["task"]
    print("任务状态:", detail["status"])
    assert detail["status"] == "succeeded", detail.get("result")
    audit = detail.get("audit") or {}
    print("体检:", audit.get("status"), f"({audit.get('warning_count', 0)} 条提醒)")
    for warning in audit.get("warnings", []):
        print("  -", warning.get("code"), ":", warning.get("message"))

    log_text = sess.get(f"{BASE}/api/tasks/{task_id}/log").text
    assert "end of do-file" in log_text
    assert "__T2S_DATA_PATH__" not in log_text  # 占位符必须已被替换成本机路径

    artifacts = detail.get("artifacts") or []
    print("产物:", [a["filename"] for a in artifacts] or "（无）")
    for artifact in artifacts:
        dl = sess.get(f"{BASE}/api/tasks/{task_id}/artifacts/{artifact['filename']}")
        assert dl.status_code == 200, artifact["filename"]
        print(f"  下载 {artifact['filename']} → {len(dl.content)} 字节 OK")

    server.shutdown()
    print("\n端到端全链路通过 ✔")
    print(f"（临时目录可删：{tmp_cloud.name} / {tmp_agent.name}）")


if __name__ == "__main__":
    main()
