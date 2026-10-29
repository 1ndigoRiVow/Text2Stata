"""
配额、突发限流与站点预算（Phase 4b）。

三道闸，管的对象不一样，别混为一谈：

    突发限流   按分钟 + 按用户。保护的是**服务器**，两种密钥模式都受它管。
    每日配额   按天 + 按用户，只对**站点密钥**生效（BYOK 花的是用户自己的钱）。
    站点预算   按月 + 全站，只对**站点密钥**生效。用尽即熔断，提示改用 BYOK。

计量单位先说死：一次计划 = 一次调用；token 取大模型响应里的 `usage`；
金额 = token × 单价。**单价没配时金额是 None，预算退化成按 token 算**——
宁可用一个粗糙的 token 上限，也不留"算不出钱所以干脆不设限"的口子：
挂在公网上的接口没有配额就是敞开的钱包。

环境变量（都支持热改，运行时每次读）：
    T2S_QUOTA_DAILY_CALLS    每人每天的计划调用上限，默认 50
    T2S_BURST_PER_MIN        每分钟突发上限，默认 10
    T2S_SITE_BUDGET_CNY      站点月度预算（元），需配单价才生效
    T2S_LLM_PRICE_PER_MTOK   单价，元 / 百万 token
    T2S_SITE_TOKEN_BUDGET    兜底的月度 token 预算（没配单价时用）
"""

from __future__ import annotations

import os
import threading
import time
import uuid

from .db import Database, utcnow

# 突发窗口：user_id → 最近 60 秒内的请求时间戳
_BURST: dict[str, list[float]] = {}
_BURST_LOCK = threading.Lock()
_BURST_WINDOW = 60


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def _env_float(name: str):
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


class Quota:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------
    # 配置（每次读，允许运行时改环境变量生效）
    # ------------------------------------------------------------------
    @property
    def global_daily_calls(self) -> int:
        return _env_int("T2S_QUOTA_DAILY_CALLS", 50)

    @property
    def burst_per_min(self) -> int:
        return _env_int("T2S_BURST_PER_MIN", 10)

    @property
    def budget_cny(self):
        return _env_float("T2S_SITE_BUDGET_CNY")

    @property
    def price_per_mtok(self):
        return _env_float("T2S_LLM_PRICE_PER_MTOK")

    @property
    def token_budget(self):
        return _env_int("T2S_SITE_TOKEN_BUDGET", 0) or None

    def daily_limit(self, user) -> int:
        """用户自己的配额优先；没设就跟随全局默认。"""
        own = user.get("quota_daily_calls") if user else None
        return int(own) if own else self.global_daily_calls

    # ------------------------------------------------------------------
    # 计量
    # ------------------------------------------------------------------
    def used_today(self, user_id: str) -> int:
        """今日已用（只数站点密钥的调用，BYOK 不占配额）。"""
        row = self.db.query_one(
            "SELECT COUNT(*) AS c FROM usage WHERE user_id = ? "
            "AND key_mode = 'site' AND created_at LIKE ?",
            (user_id, utcnow()[:10] + "%"),
        )
        return int(row["c"]) if row else 0

    def user_quota_state(self, user) -> dict:
        limit = self.daily_limit(user)
        used = self.used_today(user["id"])
        return {
            "daily_limit": limit,
            "used_today": used,
            "remaining": max(0, limit - used),
            # 管理员给这个人单独设过配额时标出来，免得他以为是全局默认
            "customized": bool(user.get("quota_daily_calls")),
        }

    def burst_state(self, user_id: str) -> dict:
        now = time.time()
        with _BURST_LOCK:
            used = len([t for t in _BURST.get(user_id, []) if now - t < _BURST_WINDOW])
        return {"per_minute": self.burst_per_min, "used_last_minute": used}

    def _month_site_totals(self):
        row = self.db.query_one(
            "SELECT COALESCE(SUM(prompt_tokens),0) AS pt, "
            "COALESCE(SUM(completion_tokens),0) AS ct, "
            "COALESCE(SUM(cost_cny),0) AS cost "
            "FROM usage WHERE key_mode = 'site' AND created_at LIKE ?",
            (utcnow()[:7] + "%",),
        )
        row = row or {"pt": 0, "ct": 0, "cost": 0}
        return int(row["pt"]) + int(row["ct"]), float(row["cost"] or 0)

    def site_budget(self) -> dict:
        """站点月度预算的当前状态。没配预算时 enabled=False（不拦）。"""
        used_tokens, used_cost = self._month_site_totals()
        budget, price = self.budget_cny, self.price_per_mtok

        if budget is not None and price is not None:
            return {
                "enabled": True,
                "basis": "cny",
                "limit_cny": round(budget, 4),
                "used_cny": round(used_cost, 4),
                "remaining_cny": round(max(0.0, budget - used_cost), 4),
                "exhausted": used_cost >= budget,
            }

        tokens = self.token_budget
        if tokens is not None:
            return {
                "enabled": True,
                "basis": "token",
                "limit_tokens": tokens,
                "used_tokens": used_tokens,
                "remaining_tokens": max(0, tokens - used_tokens),
                "exhausted": used_tokens >= tokens,
            }

        return {
            "enabled": False,
            "reason": "未配置 T2S_SITE_BUDGET_CNY（需同时配 T2S_LLM_PRICE_PER_MTOK）"
                      "或 T2S_SITE_TOKEN_BUDGET，站点密钥目前不受预算约束。",
        }

    def cost_of(self, tokens: int):
        """按单价算钱；没配单价返回 None（不是 0——0 会让人以为真的没花钱）。"""
        price = self.price_per_mtok
        if price is None:
            return None
        return round(tokens / 1_000_000 * price, 6)

    # ------------------------------------------------------------------
    # 闸门
    # ------------------------------------------------------------------
    def _burst_hit(self, user_id: str) -> bool:
        limit = self.burst_per_min
        now = time.time()
        with _BURST_LOCK:
            stamps = [t for t in _BURST.get(user_id, []) if now - t < _BURST_WINDOW]
            if len(stamps) >= limit:
                _BURST[user_id] = stamps
                return False
            stamps.append(now)
            _BURST[user_id] = stamps
            return True

    def check(self, user, key_mode: str):
        """
        调用前检查。返回 (ok, message, http_code)。
        ok 时 message 与 code 均为 None。
        """
        if not self._burst_hit(user["id"]):
            return False, (
                f"请求太密集：每分钟最多 {self.burst_per_min} 次，稍等一下再试。"
            ), 429

        if key_mode != "site":
            # BYOK：花的是用户自己的钱，只受突发限流约束
            return True, None, None

        limit = self.daily_limit(user)
        used = self.used_today(user["id"])
        if used >= limit:
            return False, (
                f"今日站点密钥额度已用完（{used}/{limit} 次）。"
                "想继续用就在设置里填自己的 API Key —— 自带密钥不计配额。"
            ), 429

        budget = self.site_budget()
        if budget["enabled"] and budget["exhausted"]:
            unit = "元" if budget["basis"] == "cny" else "token"
            return False, (
                f"站点密钥的月度预算已用尽（{unit}），暂不可用。"
                "请改用自带密钥，或等下个月重置。"
            ), 503

        return True, None, None

    # ------------------------------------------------------------------
    # 记账
    # ------------------------------------------------------------------
    def record(self, user_id: str, key_mode: str, model: str, usage, kind: str = "plan") -> dict:
        """把一次调用写进 usage。BYOK 也记，但不进配额与预算的分子。"""
        usage = usage or {}
        pt = int(usage.get("prompt_tokens") or 0)
        ct = int(usage.get("completion_tokens") or 0)
        cost = self.cost_of(pt + ct) if key_mode == "site" else None

        row = {
            "id": f"use_{uuid.uuid4().hex[:8]}",
            "user_id": user_id,
            "kind": kind,
            "key_mode": key_mode,
            "model": model or "",
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "cost_cny": cost,
            "created_at": utcnow(),
        }
        self.db.execute(
            "INSERT INTO usage (id, user_id, kind, key_mode, model, prompt_tokens, "
            "completion_tokens, cost_cny, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (row["id"], row["user_id"], row["kind"], row["key_mode"], row["model"],
             row["prompt_tokens"], row["completion_tokens"], row["cost_cny"], row["created_at"]),
        )
        return row
