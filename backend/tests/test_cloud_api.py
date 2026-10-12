# -*- coding: utf-8 -*-
"""
云端骨架（cloud/）接口测试。

不依赖真实 Stata 与大模型：用 Flask test client + 临时目录 SQLite，
覆盖 docs/PROTOCOL.md 的关键契约——鉴权、任务生命周期、产物策略、属主隔离。

运行（在 backend 目录下）：
    ../.venv/Scripts/python.exe tests/test_cloud_api.py
"""

import io
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
for extra in (PROJECT_ROOT, PROJECT_ROOT / "backend"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from cloud.server import create_app  # noqa: E402

FE_MAPPING = {"y": "tfp_lp", "x": "rd", "id": "code", "time": "year", "controls": ["lev"]}


def make_client():
    tmp = tempfile.TemporaryDirectory(prefix="t2s_cloud_test_")
    # 内存库：沙箱环境下新建 SQLite 文件要几秒，24 个用例各建一次会拖垮整套测试
    app = create_app(data_dir=tmp.name, db_path=":memory:")
    app.config["TESTING"] = True
    client = app.test_client()
    db = app.extensions["t2s_db"]
    invite = db.query_one("SELECT code FROM invites WHERE used_by IS NULL LIMIT 1")["code"]
    return tmp, app, client, db, invite


def register(client, invite, username="hao", password="password123"):
    return client.post("/api/auth/register", json={
        "username": username, "password": password, "invite_code": invite,
    })


def make_agent(client, name="test-agent"):
    resp = client.post("/api/agents", json={"name": name})
    assert resp.status_code == 201, resp.get_json()
    body = resp.get_json()
    return body["agent_id"], body["token"]


def agent_headers(token):
    return {"Authorization": f"Bearer {token}"}


def register_dataset(client, token, name="testdata.dta", n_vars=3):
    schema = [{"name": f"v{i}", "label": f"变量{i}"} for i in range(n_vars)]
    resp = client.post("/api/agent/datasets",
                       json={"name": name, "schema": schema}, headers=agent_headers(token))
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()["dataset_id"]


def create_task(client, dataset_id, template="fe", mapping=None):
    return client.post("/api/tasks", json={
        "dataset_id": dataset_id,
        "selected_template": template,
        "variable_mapping": mapping if mapping is not None else FE_MAPPING,
        "query": "测试任务",
    })


class TestAuth(unittest.TestCase):
    def setUp(self):
        self.tmp, self.app, self.client, self.db, self.invite = make_client()

    def tearDown(self):
        self.tmp.cleanup()

    def test_register_requires_valid_invite(self):
        resp = register(self.client, "wrong-code")
        self.assertEqual(resp.status_code, 403)

    def test_first_user_is_admin_and_invite_consumed(self):
        resp = register(self.client, self.invite)
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.get_json()["user"]["is_admin"])
        # 同一个邀请码不能再用
        resp2 = register(self.client, self.invite, username="someone_else")
        self.assertEqual(resp2.status_code, 403)

    def test_password_min_length(self):
        resp = register(self.client, self.invite, password="short")
        self.assertEqual(resp.status_code, 400)

    def test_login_logout_me(self):
        register(self.client, self.invite)
        self.client.post("/api/auth/logout")
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

        bad = self.client.post("/api/auth/login",
                               json={"username": "hao", "password": "wrong-password"})
        self.assertEqual(bad.status_code, 401)

        ok = self.client.post("/api/auth/login",
                              json={"username": "hao", "password": "password123"})
        self.assertEqual(ok.status_code, 200)
        me = self.client.get("/api/auth/me").get_json()
        self.assertEqual(me["user"]["username"], "hao")
        self.assertEqual(me["user"]["artifact_mode"], "cloud")  # 默认回传

    def test_settings_artifact_mode_validation(self):
        register(self.client, self.invite)
        bad = self.client.post("/api/auth/settings", json={"artifact_mode": "everywhere"})
        self.assertEqual(bad.status_code, 400)
        ok = self.client.post("/api/auth/settings", json={"artifact_mode": "local"})
        self.assertEqual(ok.status_code, 200)


class TestAgentAuth(unittest.TestCase):
    def setUp(self):
        self.tmp, self.app, self.client, self.db, self.invite = make_client()
        register(self.client, self.invite)

    def tearDown(self):
        self.tmp.cleanup()

    def test_agent_endpoints_require_token(self):
        self.assertEqual(self.client.post("/api/agent/poll", json={}).status_code, 401)
        resp = self.client.post("/api/agent/poll", json={},
                                headers=agent_headers("t2sa_wrong"))
        self.assertEqual(resp.status_code, 401)

    def test_agent_token_shown_once_and_usable(self):
        _, token = make_agent(self.client)
        resp = self.client.post("/api/agent/poll", json={}, headers=agent_headers(token))
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.get_json()["task"])
        # 列表里不回显 token
        agents = self.client.get("/api/agents").get_json()["agents"]
        self.assertEqual(len(agents), 1)
        self.assertNotIn("token", agents[1 - 1])


class TestTasks(unittest.TestCase):
    def setUp(self):
        self.tmp, self.app, self.client, self.db, self.invite = make_client()
        register(self.client, self.invite)
        self.agent_id, self.token = make_agent(self.client)
        self.dataset_id = register_dataset(self.client, self.token)

    def tearDown(self):
        self.tmp.cleanup()

    def test_unknown_template_rejected(self):
        resp = create_task(self.client, self.dataset_id, template="no_such_template")
        self.assertEqual(resp.status_code, 400)

    def test_missing_required_globals_rejected(self):
        resp = create_task(self.client, self.dataset_id,
                           mapping={"y": "tfp_lp", "x": "rd"})  # fe 还要 id/time
        self.assertEqual(resp.status_code, 400)
        self.assertIn("$id", resp.get_json()["message"])

    def test_task_of_other_users_dataset_rejected(self):
        # 第二个用户拿第一个用户的 dataset_id 建任务 → 404
        invite = self.client.post("/api/auth/invites", json={}).get_json()["code"]
        other = self.app.test_client()
        register(other, invite, username="other", password="password123")
        resp = create_task(other, self.dataset_id)
        self.assertEqual(resp.status_code, 404)

    def test_full_dispatch_cycle(self):
        resp = create_task(self.client, self.dataset_id)
        self.assertEqual(resp.status_code, 202)
        task_id = resp.get_json()["task_id"]

        # 领取：do_script 必须带占位符，云端绝不知道真实数据路径
        poll = self.client.post("/api/agent/poll", json={}, headers=agent_headers(self.token))
        task = poll.get_json()["task"]
        self.assertEqual(task["task_id"], task_id)
        self.assertIn("__T2S_DATA_PATH__", task["do_script"])
        self.assertTrue(task["artifact_upload"])

        # 重复 poll 不再发同一任务
        again = self.client.post("/api/agent/poll", json={}, headers=agent_headers(self.token))
        self.assertIsNone(again.get_json()["task"])

        # 先传产物，再回报结果（协议顺序）
        up = self.client.post(
            f"/api/agent/tasks/{task_id}/artifacts",
            data={"file": (io.BytesIO(b"{\\rtf1 fake}"), "fe_result.rtf")},
            headers=agent_headers(self.token),
            content_type="multipart/form-data",
        )
        self.assertEqual(up.status_code, 201)

        fake_log = ". regress tfp_lp rd\n... 略 ...\nend of do-file\n"
        done = self.client.post(f"/api/agent/tasks/{task_id}/result", json={
            "status": "success", "message": "执行成功",
            "time_cost": 1.2, "log": fake_log, "files": ["fe_result.rtf"],
        }, headers=agent_headers(self.token))
        self.assertEqual(done.status_code, 200)
        self.assertIn("audit", done.get_json())

        detail = self.client.get(f"/api/tasks/{task_id}").get_json()["task"]
        self.assertEqual(detail["status"], "succeeded")
        self.assertEqual(detail["result"]["files"], ["fe_result.rtf"])
        self.assertEqual(detail["artifacts"][0]["filename"], "fe_result.rtf")

        # 日志与产物都能取回
        log_resp = self.client.get(f"/api/tasks/{task_id}/log")
        self.assertEqual(log_resp.status_code, 200)
        self.assertIn("end of do-file", log_resp.get_data(as_text=True))
        dl = self.client.get(f"/api/tasks/{task_id}/artifacts/fe_result.rtf")
        self.assertEqual(dl.status_code, 200)

        # 终态后拒绝重复回报与继续传产物
        dup = self.client.post(f"/api/agent/tasks/{task_id}/result", json={
            "status": "success", "log": "", "files": [],
        }, headers=agent_headers(self.token))
        self.assertEqual(dup.status_code, 409)
        late = self.client.post(
            f"/api/agent/tasks/{task_id}/artifacts",
            data={"file": (io.BytesIO(b"x"), "late.rtf")},
            headers=agent_headers(self.token),
            content_type="multipart/form-data",
        )
        self.assertEqual(late.status_code, 409)

    def test_failed_result_keeps_error_info(self):
        task_id = create_task(self.client, self.dataset_id).get_json()["task_id"]
        self.client.post("/api/agent/poll", json={}, headers=agent_headers(self.token))
        self.client.post(f"/api/agent/tasks/{task_id}/result", json={
            "status": "failed", "error_code": "r(198)",
            "message": "Stata 执行报错", "log": "r(198);", "files": [],
        }, headers=agent_headers(self.token))
        detail = self.client.get(f"/api/tasks/{task_id}").get_json()["task"]
        self.assertEqual(detail["status"], "failed")
        self.assertEqual(detail["result"]["error_code"], "r(198)")

    def test_report_rejects_bad_status(self):
        task_id = create_task(self.client, self.dataset_id).get_json()["task_id"]
        self.client.post("/api/agent/poll", json={}, headers=agent_headers(self.token))
        resp = self.client.post(f"/api/agent/tasks/{task_id}/result", json={
            "status": "maybe", "log": "", "files": [],
        }, headers=agent_headers(self.token))
        self.assertEqual(resp.status_code, 400)

    def test_artifact_upload_policy_follows_user_setting(self):
        self.client.post("/api/auth/settings", json={"artifact_mode": "local"})
        create_task(self.client, self.dataset_id)
        poll = self.client.post("/api/agent/poll", json={}, headers=agent_headers(self.token))
        self.assertFalse(poll.get_json()["task"]["artifact_upload"])

    def test_artifact_suffix_rejected(self):
        task_id = create_task(self.client, self.dataset_id).get_json()["task_id"]
        self.client.post("/api/agent/poll", json={}, headers=agent_headers(self.token))
        resp = self.client.post(
            f"/api/agent/tasks/{task_id}/artifacts",
            data={"file": (io.BytesIO(b"MZ"), "evil.exe")},
            headers=agent_headers(self.token),
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 400)

    def test_requeue_stale_task(self):
        task_id = create_task(self.client, self.dataset_id).get_json()["task_id"]
        self.client.post("/api/agent/poll", json={}, headers=agent_headers(self.token))
        # 人为把任务时间戳改成 1 小时前，模拟代理领了活就死掉
        self.db.execute("UPDATE tasks SET updated_at = ? WHERE id = ?",
                        ("2000-01-01T00:00:00+00:00", task_id))
        poll = self.client.post("/api/agent/poll", json={}, headers=agent_headers(self.token))
        self.assertEqual(poll.get_json()["task"]["task_id"], task_id)

    def test_delete_agent_terminates_pending_tasks(self):
        task_id = create_task(self.client, self.dataset_id).get_json()["task_id"]
        self.client.delete(f"/api/agents/{self.agent_id}")
        detail = self.client.get(f"/api/tasks/{task_id}").get_json()["task"]
        self.assertEqual(detail["status"], "error")

    def test_ownership_isolation(self):
        task_id = create_task(self.client, self.dataset_id).get_json()["task_id"]
        invite = self.client.post("/api/auth/invites", json={}).get_json()["code"]
        other = self.app.test_client()
        register(other, invite, username="other", password="password123")
        self.assertEqual(other.get(f"/api/tasks/{task_id}").status_code, 404)
        self.assertEqual(other.get(f"/api/tasks/{task_id}/log").status_code, 404)
        self.assertEqual(
            other.get(f"/api/tasks/{task_id}/artifacts/fe_result.rtf").status_code, 404)

    def test_schema_size_limit(self):
        big_schema = [{"name": f"v{i}", "label": ""} for i in range(2001)]
        resp = self.client.post("/api/agent/datasets",
                                json={"name": "big.dta", "schema": big_schema},
                                headers=agent_headers(self.token))
        self.assertEqual(resp.status_code, 400)

    def test_dataset_register_validation(self):
        resp = self.client.post("/api/agent/datasets",
                                json={"name": "x.dta", "schema": []},
                                headers=agent_headers(self.token))
        self.assertEqual(resp.status_code, 400)


class TestFrontendAndTemplates(unittest.TestCase):
    def setUp(self):
        self.tmp, self.app, self.client, self.db, self.invite = make_client()

    def tearDown(self):
        self.tmp.cleanup()

    def test_frontend_served(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Text2Stata", resp.get_data(as_text=True))

    def test_frontend_traversal_blocked(self):
        resp = self.client.get("/../server.py")
        self.assertIn(resp.status_code, (403, 404))

    def test_templates_requires_login(self):
        self.assertEqual(self.client.get("/api/templates").status_code, 401)

    def test_templates_registry_shape(self):
        register(self.client, self.invite)
        body = self.client.get("/api/templates").get_json()
        self.assertIn("fe", body["templates"])
        fe = body["templates"]["fe"]
        self.assertEqual(fe["required_globals"], ["y", "x", "id", "time"])
        self.assertIn("controls", fe["optional_globals"])
        self.assertTrue(fe["description"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
