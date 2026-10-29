"""
云端骨架的 SQLite 数据层。

表结构对应 docs/PROTOCOL.md：
  users / invites / agents / datasets / tasks / artifacts

用法：
    db = Database(path)
    db.execute(...) / db.query_one(...) / db.query_all(...)

每次调用开一条新连接（sqlite3 默认不准跨线程共享连接），
Flask 多线程下这比维护连接池简单且够用——云端本来就是轻负载。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL,
    pw_hash       TEXT NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    artifact_mode TEXT NOT NULL DEFAULT 'cloud',
    -- Phase 4b：计划模型的偏好（空 = 跟随推荐默认）、每日计划调用上限（NULL = 跟随全局默认）
    llm_model          TEXT NOT NULL DEFAULT '',
    quota_daily_calls  INTEGER,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invites (
    code       TEXT PRIMARY KEY,
    created_by TEXT,
    used_by    TEXT,
    used_at    TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id),
    name       TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    last_seen  TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS datasets (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id),
    agent_id    TEXT NOT NULL REFERENCES agents(id),
    name        TEXT NOT NULL,
    schema_json TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id                TEXT PRIMARY KEY,
    user_id           TEXT NOT NULL REFERENCES users(id),
    agent_id          TEXT NOT NULL REFERENCES agents(id),
    dataset_id        TEXT NOT NULL REFERENCES datasets(id),
    query             TEXT,
    selected_template TEXT NOT NULL,
    mapping_json      TEXT NOT NULL,
    do_script         TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'queued',
    result_json       TEXT,
    log               TEXT,
    audit_json        TEXT,
    artifact_upload   INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    id         TEXT PRIMARY KEY,
    task_id    TEXT NOT NULL REFERENCES tasks(id),
    filename   TEXT NOT NULL,
    path       TEXT NOT NULL,
    size       INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

-- Phase 4b：大模型调用计量。BYOK 也记，只是不计入配额与站点预算。
-- cost_cny 为 NULL 表示没配单价、算不出钱。
CREATE TABLE IF NOT EXISTS usage (
    id                TEXT PRIMARY KEY,
    user_id           TEXT NOT NULL REFERENCES users(id),
    kind              TEXT NOT NULL DEFAULT 'plan',
    key_mode          TEXT NOT NULL,
    model             TEXT,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost_cny          REAL,
    created_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_user   ON tasks(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_agent  ON tasks(agent_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_datasets_user ON datasets(user_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(task_id);
CREATE INDEX IF NOT EXISTS idx_usage_user_time ON usage(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_mode_time ON usage(key_mode, created_at);
"""

# 老库加列：CREATE TABLE IF NOT EXISTS 不会补列，只能显式加。
# 每张表一次，重复执行安全（加之前先查 pragma）。
_MIGRATIONS = [
    ("users", "llm_model", "ALTER TABLE users ADD COLUMN llm_model TEXT NOT NULL DEFAULT ''"),
    ("users", "quota_daily_calls", "ALTER TABLE users ADD COLUMN quota_daily_calls INTEGER"),
]


def migrate(conn) -> None:
    """给建库早于 Phase 4b 的数据文件补列。"""
    for table, column, ddl in _MIGRATIONS:
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(ddl)

# 任务的终态：重复回报这些状态要拒掉（409）
TERMINAL_STATES = ("succeeded", "failed", "error")


def utcnow() -> str:
    """ISO 8601 UTC 时间戳，全库统一用这个格式。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path):
        # ":memory:" 走共享缓存内存库（测试用）：
        # 每条连接各自独立，但只要锚定连接不断开，它们看到的是同一份数据。
        self._memory = str(path) == ":memory:"
        if self._memory:
            self._uri = f"file:t2s_{uuid.uuid4().hex[:12]}?mode=memory&cache=shared"
            self._anchor = self._connect()  # 锚定连接：它一关，内存库就蒸发
        else:
            self._uri = None
            self._anchor = None
            self.path = Path(path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # DDL 只需要跑一次；用锁防多线程同时初始化
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
            migrate(conn)
        finally:
            conn.close()
        self._write_lock = threading.Lock()

    def _connect(self):
        if self._memory:
            conn = sqlite3.connect(self._uri, uri=True, timeout=10)
        else:
            conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    # ------------------------------------------------------------------
    # 基础读写
    # ------------------------------------------------------------------
    def execute(self, sql, params=()):
        """写操作（INSERT/UPDATE/DELETE）。返回受影响行数。"""
        with self._write_lock:
            conn = self._connect()
            try:
                with conn:  # 提交/回滚
                    cur = conn.execute(sql, params)
                    return cur.rowcount
            finally:
                conn.close()  # with 只管事务不管关闭，不 close 在 Windows 上会留文件锁

    def query_one(self, sql, params=()):
        conn = self._connect()
        try:
            row = conn.execute(sql, params).fetchone()
        finally:
            conn.close()
        return dict(row) if row else None

    def query_all(self, sql, params=()):
        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # 序列化辅助：JSON 列的存取
    # ------------------------------------------------------------------
    @staticmethod
    def dumps(value) -> str:
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def loads(text, default=None):
        if not text:
            return default
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return default
