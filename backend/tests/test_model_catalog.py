# -*- coding: utf-8 -*-
"""模型目录的回归测试。"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import model_catalog


class TestTextModelFilter(unittest.TestCase):
    """中转站目录里混着非对话模型，必须滤掉。"""

    def test_text_models_pass(self):
        for model_id in ("deepseek-v4-flash", "qwen3.7-flash", "glm-5.3-flash", "gpt-6.1-sol"):
            self.assertTrue(model_catalog.is_text_model(model_id), model_id)

    def test_non_text_models_rejected(self):
        for model_id in (
            "text-embedding-3-large",
            "bge-large-zh",
            "tts-1-hd",
            "whisper-1",
            "dall-e-3",
            "gpt-image-2",
            "omni-moderation-latest",
        ):
            self.assertFalse(model_catalog.is_text_model(model_id), model_id)


class TestCatalogBuild(unittest.TestCase):
    def test_recommended_is_curated(self):
        """推荐默认值必须来自策展清单，不能是远程列表自动排出来的。"""
        recommended_ids = {
            item["id"] for item in model_catalog.CURATED_MODELS if item["tier"] == "recommended"
        }
        self.assertIn(model_catalog.RECOMMENDED_MODEL, recommended_ids)

    def test_no_key_degrades_to_curated_only(self):
        """没配 key 时不能报错，也要给出可选内容。"""
        catalog = model_catalog.build_catalog("https://example.invalid/v1", "")
        self.assertEqual(catalog["source"], "empty_key")
        self.assertTrue(catalog["curated"])
        # 没查到远程清单，无从判断可用性，全部标为不可用但仍列出
        self.assertTrue(all(not item["available"] for item in catalog["curated"]))

    def test_unreachable_endpoint_degrades(self):
        catalog = model_catalog.build_catalog("https://example.invalid/v1/chat/completions", "x" * 32)
        self.assertEqual(catalog["source"], "curated_only")
        self.assertTrue(catalog["curated"])

    def test_remote_models_are_merged(self):
        """远程列表里的模型要能选，且策展过的不要重复出现。"""
        api_url = "https://example.invalid/v1/chat/completions"
        api_key = "sk-unit-test-key"

        original = model_catalog.fetch_remote_models

        def fake_fetch(url, key, timeout=15):
            self.assertEqual(key, api_key)
            return [
                "deepseek-v4-flash",      # 策展里有，应归入推荐组、不重复进 others
                "qwen3.7-flash",          # 同上
                "glm-5.3-flash",
                "some-random-model",      # 不在策展里，进 others
                "text-embedding-3-large", # 非对话模型，应被过滤
                "claude-opus-4-5-20251101",  # 日期快照噪音，应被过滤
            ]

        model_catalog.fetch_remote_models = fake_fetch
        try:
            catalog = model_catalog.build_catalog(api_url, api_key)
        finally:
            model_catalog.fetch_remote_models = original

        self.assertEqual(catalog["source"], "remote")
        # 不要在这里写死模型名：推荐值换了，这条断言会假摔，
        # 真正该守的不变量（推荐值必须来自策展层）由 test_recommended_is_curated 守着。
        self.assertEqual(catalog["recommended"], model_catalog.RECOMMENDED_MODEL)

        # 策展模型带 available 标记
        by_id = {item["id"]: item for item in catalog["curated"]}
        self.assertTrue(by_id["deepseek-v4-flash"]["available"])
        self.assertTrue(by_id["qwen3.7-flash"]["available"])

        # others 里不该出现策展过的、非文本的、日期快照的
        others = [m for group in catalog["others"] for m in group["models"]]
        self.assertIn("some-random-model", others)
        self.assertNotIn("deepseek-v4-flash", others)
        self.assertNotIn("text-embedding-3-large", others)
        self.assertNotIn("claude-opus-4-5-20251101", others)

    def test_key_never_cached_in_plaintext(self):
        """缓存键里只留哈希，防止密钥被顺手写进内存快照或日志。"""
        api_key = "sk-super-secret-key-value"
        fingerprint = model_catalog._key_fingerprint(api_key)
        self.assertNotIn(api_key, str(fingerprint))
        self.assertEqual(len(fingerprint), 12)


class TestModelsEndpoint(unittest.TestCase):
    """`/api/models` 接口。"""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        import app as flask_app

        cls.client = flask_app.app.test_client()

    def test_endpoint_shape(self):
        response = self.client.get("/api/models")
        self.assertEqual(response.status_code, 200)

        payload = response.get_json()
        self.assertEqual(payload["status"], "success")
        self.assertIn("recommended", payload)
        self.assertIn("curated", payload)
        self.assertIn("others", payload)
        self.assertIn("source", payload)

    def test_never_returns_empty_curated(self):
        """哪怕中转站完全连不上，也要有策展清单兜底，下拉框不能空。"""
        payload = self.client.get("/api/models").get_json()
        self.assertTrue(payload["curated"])
        self.assertEqual(payload["recommended"], model_catalog.RECOMMENDED_MODEL)

    def test_carries_tier_labels(self):
        """分组名由后端给，前端不用自己维护一份。"""
        payload = self.client.get("/api/models").get_json()
        for item in payload["curated"]:
            self.assertTrue(item["tier_label"], item["id"])
            self.assertTrue(item["note"], item["id"])

    def test_accepts_line_parameter(self):
        self.assertEqual(self.client.get("/api/models?line=line2").status_code, 200)
        self.assertEqual(self.client.get("/api/models?line=nonsense").status_code, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
