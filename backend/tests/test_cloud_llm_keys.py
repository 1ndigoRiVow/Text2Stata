"""Phase 4b：双密钥模式 + 配额/限流/预算。

单独一个文件而不是塞进 test_cloud_api.py：那边的用例围绕任务与代理生命周期，
这一组围绕"谁付钱、用多少次"，混在一起反而看不清边界。

关键点：所有用例都不发真实网络请求。LLMAgent 在 cloud.server 里被替换成假实现，
用量由假实现自己报——我们要测的是闸门逻辑，不是模型回不回得来。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cloud import server as srv  # noqa: E402

USER = "quota_tester"
PASS = "password123"
FE_MAPPING = {"y": "tfp", "x": "focus", "controls": ["size"], "id": "id", "time": "year"}
SCHEMA = [{"name": name} for name in ("id", "year", "tfp", "focus", "size")]

# 一个"用户自己的"密钥，只用于验证它不会出现在数据库或响应里
BYOK_KEY = "sk-byok-AAAABBBBCCCCDDDD1234"
SITE_KEY = "sk-site-EEEEFFFFGGGGHHHH5678"


class FakeAgent:
    """假的大模型：固定返回一个 fe 计划，并报一份用量。"""

    calls: list = []

    def __init__(self, api_url=None, api_key=None, current_model=None):
        self.api_url = api_url
        self.api_key = api_key
        self.model = current_model
        self.last_usage = None
        FakeAgent.calls.append({"url": api_url, "key": api_key, "model": current_model})

    def parse_intent(self, query, schema, registry):
        self.last_usage = {"prompt_tokens": 1200, "completion_tokens": 80}
        return {
            "selected_template": "fe",
            "variable_mapping": dict(FE_MAPPING),
            "reasoning": "测试用固定返回",
        }


def make_client():
    tmp = tempfile.mkdtemp(prefix="t2s_cloud_llm_")
    app = srv.create_app(data_dir=os.path.join(tmp, "data"))
    app.config.update(TESTING=True)
    client = app.test_client()
    db = app.extensions["t2s_db"]
    invite = db.query_one("SELECT code FROM invites WHERE used_by IS NULL LIMIT 1")["code"]
    return client, db, invite


def register(client, invite, username=USER, password=PASS):
    resp = client.post("/api/auth/register", json={
        "username": username, "password": password, "invite_code": invite,
    })
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()["user"]


def make_dataset(client):
    """建一个代理 + 一份数据集，返回 dataset_id。/api/plan 需要它。"""
    token = client.post("/api/agents", json={"name": "测试代理"}).get_json()["token"]
    resp = client.post(
        "/api/agent/datasets",
        json={"name": "测试数据集", "schema": SCHEMA},
        headers={"Authorization": f"Bearer {token}"},
    )
    return resp.get_json()["dataset_id"]


def db_contains(db, needle):
    """全库文本搜索：确认某个字符串一个字节都没落进数据库。"""
    tables = [row["name"] for row in db.query_all(
        "SELECT name FROM sqlite_master WHERE type = 'table'")]
    for table in tables:
        for row in db.query_all(f"SELECT * FROM {table}"):
            if needle in repr(row):
                return True
    return False


CLEAN_ENV = {
    "T2S_LLM_API_KEY": "",
    "T2S_LLM_API_URL": "",
    "T2S_ALLOW_BYOK": "1",
    "T2S_QUOTA_DAILY_CALLS": "50",
    "T2S_BURST_PER_MIN": "50",
    "T2S_SITE_BUDGET_CNY": "",
    "T2S_LLM_PRICE_PER_MTOK": "",
    "T2S_SITE_TOKEN_BUDGET": "",
}


class TestDoubleKeyMode(unittest.TestCase):
    def setUp(self):
        self.client, self.db, self.invite = make_client()
        self.user = register(self.client, self.invite)
        self.dataset_id = make_dataset(self.client)
        FakeAgent.calls = []
        self.patcher = mock.patch.object(srv, "LLMAgent", FakeAgent)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def plan(self, **extra):
        body = {"dataset_id": self.dataset_id, "query": "检验 focus 对 tfp 的影响"}
        body.update(extra)
        return self.client.post("/api/plan", json=body)

    # ------------------------------------------------------------------
    def test_status_without_site_key(self):
        with mock.patch.dict(os.environ, CLEAN_ENV, clear=False):
            body = self.client.post("/api/llm/status", json={}).get_json()
        self.assertFalse(body["site_key_configured"])
        self.assertTrue(body["byok_allowed"])
        self.assertEqual(body["model"], body["recommended_model"])
        self.assertEqual(body["quota"]["used_today"], 0)
        self.assertFalse(body["budget"]["enabled"])

    def test_status_masks_site_key(self):
        env = dict(CLEAN_ENV, T2S_LLM_API_KEY=SITE_KEY)
        with mock.patch.dict(os.environ, env, clear=False):
            resp = self.client.post("/api/llm/status", json={})
        text = resp.get_data(as_text=True)
        self.assertNotIn(SITE_KEY, text)
        body = resp.get_json()
        self.assertTrue(body["site_key_configured"])
        self.assertTrue(body["site_key_hint"].startswith("sk-s"))
        self.assertIn("...", body["site_key_hint"])

    def test_plan_requires_key(self):
        """一个密钥都没有 → 402，并告诉用户可以自带。"""
        with mock.patch.dict(os.environ, CLEAN_ENV, clear=False):
            resp = self.plan()
        self.assertEqual(resp.status_code, 402)
        self.assertTrue(resp.get_json()["byok_allowed"])

    def test_byok_works_and_is_never_persisted(self):
        """BYOK 能跑通，且密钥不进数据库、不进响应、不进日志表。"""
        with mock.patch.dict(os.environ, CLEAN_ENV, clear=False):
            resp = self.plan(api_key=BYOK_KEY)
        self.assertEqual(resp.status_code, 200, resp.get_json())
        body = resp.get_json()
        self.assertEqual(body["usage"]["key_mode"], "byok")
        self.assertNotIn(BYOK_KEY, resp.get_data(as_text=True))

        self.assertFalse(db_contains(self.db, BYOK_KEY),
                         "BYOK 密钥被写进了数据库——这是 Phase 4b 的底线")

        row = self.db.query_one("SELECT * FROM usage ORDER BY created_at DESC LIMIT 1")
        self.assertEqual(row["key_mode"], "byok")
        self.assertEqual(row["prompt_tokens"], 1200)
        # BYOK 不花钱（花的是用户的），金额就该是 NULL 而不是 0
        self.assertIsNone(row["cost_cny"])

    def test_byok_does_not_consume_daily_quota(self):
        """配额只对站点密钥生效：BYOK 连打也不掉额度。"""
        env = dict(CLEAN_ENV, T2S_QUOTA_DAILY_CALLS="1")
        with mock.patch.dict(os.environ, env, clear=False):
            for _ in range(3):
                self.assertEqual(self.plan(api_key=BYOK_KEY).status_code, 200)
            state = self.client.post("/api/llm/status", json={}).get_json()
        self.assertEqual(state["quota"]["used_today"], 0)
        self.assertEqual(state["quota"]["daily_limit"], 1)
        self.assertEqual(len(self.db.query_all("SELECT * FROM usage")), 3)


class TestQuotaGates(unittest.TestCase):
    def setUp(self):
        self.client, self.db, self.invite = make_client()
        self.user = register(self.client, self.invite)
        self.dataset_id = make_dataset(self.client)
        FakeAgent.calls = []
        self.patcher = mock.patch.object(srv, "LLMAgent", FakeAgent)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def plan(self, **extra):
        body = {"dataset_id": self.dataset_id, "query": "检验 focus 对 tfp 的影响"}
        body.update(extra)
        return self.client.post("/api/plan", json=body)

    # ------------------------------------------------------------------
    def test_site_key_daily_quota_blocks(self):
        env = dict(CLEAN_ENV, T2S_LLM_API_KEY=SITE_KEY, T2S_QUOTA_DAILY_CALLS="2")
        with mock.patch.dict(os.environ, env, clear=False):
            self.assertEqual(self.plan().status_code, 200)
            self.assertEqual(self.plan().status_code, 200)
            third = self.plan()
            self.assertEqual(third.status_code, 429)
            self.assertIn("额度已用完", third.get_json()["message"])
            # 被拦下的这一次没有真的发出调用
            self.assertEqual(len(FakeAgent.calls), 2)

    def test_burst_limit_applies_to_both_modes(self):
        """突发限流保护的是服务器，BYOK 也照拦。"""
        env = dict(CLEAN_ENV, T2S_BURST_PER_MIN="2")
        with mock.patch.dict(os.environ, env, clear=False):
            self.assertEqual(self.plan(api_key=BYOK_KEY).status_code, 200)
            self.assertEqual(self.plan(api_key=BYOK_KEY).status_code, 200)
            third = self.plan(api_key=BYOK_KEY)
            self.assertEqual(third.status_code, 429)
            self.assertIn("请求太密集", third.get_json()["message"])

    def test_site_budget_circuit_breaker(self):
        """预算用尽 → 站点密钥熔断（503），BYOK 不受影响。"""
        env = dict(CLEAN_ENV, T2S_LLM_API_KEY=SITE_KEY,
                   T2S_SITE_BUDGET_CNY="1", T2S_LLM_PRICE_PER_MTOK="1000")
        with mock.patch.dict(os.environ, env, clear=False):
            first = self.plan()
            self.assertEqual(first.status_code, 200)
            # 1280 token × 1000元/百万 = 1.28 元 > 1 元预算 → 下一次熔断
            second = self.plan()
            self.assertEqual(second.status_code, 503)
            self.assertIn("预算已用尽", second.get_json()["message"])
            # 熔断只针对站点密钥
            self.assertEqual(self.plan(api_key=BYOK_KEY).status_code, 200)

            budget = self.client.post("/api/llm/status", json={}).get_json()["budget"]
        self.assertEqual(budget["basis"], "cny")
        self.assertTrue(budget["exhausted"])
        self.assertAlmostEqual(budget["used_cny"], 1.28, places=2)

    def test_token_budget_fallback_when_price_missing(self):
        """没配单价时按 token 预算兜底，不允许"算不出钱所以不设限"。"""
        env = dict(CLEAN_ENV, T2S_LLM_API_KEY=SITE_KEY, T2S_SITE_TOKEN_BUDGET="1000")
        with mock.patch.dict(os.environ, env, clear=False):
            self.assertEqual(self.plan().status_code, 200)   # 0 → 允许
            self.assertEqual(self.plan().status_code, 503)   # 1280 ≥ 1000 → 熔断
            budget = self.client.post("/api/llm/status", json={}).get_json()["budget"]
        self.assertEqual(budget["basis"], "token")
        self.assertTrue(budget["exhausted"])


class TestQuotaAdmin(unittest.TestCase):
    def setUp(self):
        self.client, self.db, self.invite = make_client()
        self.admin = register(self.client, self.invite)
        self.dataset_id = make_dataset(self.client)

    def test_admin_sets_quota_and_plain_user_cannot(self):
        invite2 = self.client.post("/api/auth/invites", json={}).get_json()["code"]
        plain = register(self.client, invite2, username="plain_user")

        # 普通用户改配额 → 403
        resp = self.client.post(f"/api/admin/users/{self.admin['id']}/quota",
                                json={"daily_calls": 5})
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(self.db.query_one(
            "SELECT quota_daily_calls AS q FROM users WHERE id = ?",
            (self.admin["id"],))["q"])

        # 管理员设配额 → 生效；传 null 表示跟随全局默认
        self.client.post("/api/auth/login", json={"username": USER, "password": PASS})
        ok = self.client.post(f"/api/admin/users/{plain['id']}/quota",
                              json={"daily_calls": 5})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(self.db.query_one(
            "SELECT quota_daily_calls AS q FROM users WHERE id = ?", (plain["id"],))["q"], 5)

        cleared = self.client.post(f"/api/admin/users/{plain['id']}/quota",
                                   json={"daily_calls": None})
        self.assertEqual(cleared.status_code, 200)
        self.assertIsNone(self.db.query_one(
            "SELECT quota_daily_calls AS q FROM users WHERE id = ?", (plain["id"],))["q"])

    def test_llm_model_preference_round_trip(self):
        resp = self.client.post("/api/auth/settings", json={"llm_model": "deepseek-v4-flash"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["user"]["llm_model"], "deepseek-v4-flash")
        me = self.client.get("/api/auth/me").get_json()
        self.assertEqual(me["user"]["llm_model"], "deepseek-v4-flash")

        # 留空 = 跟随推荐默认
        self.client.post("/api/auth/settings", json={"llm_model": ""})
        self.assertEqual(self.client.get("/api/auth/me").get_json()["user"]["llm_model"], "")

    def test_settings_rejects_unknown_field(self):
        resp = self.client.post("/api/auth/settings", json={"nonsense": 1})
        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
