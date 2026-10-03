"""两个 SQLite 文件的表结构定义与连接辅助。

设计要点：
- 主库 records.body 允许为 NULL：归档后正文迁出，但保留
  (seq, op_id, occurred_at, body_sha256, chain_hash) 元数据，
  用于续链、幂等重试与核验。
- 归档库 records.body 不允许为 NULL：归档记录永远携带正文。
- 两库都使用回滚日志（非 WAL），保证 ATTACH 后的跨库事务
  在掉电时也能由 SQLite 自动恢复，无需人工修补。
- synchronous=FULL：已返回成功的追加在掉电后不丢失。
"""

from __future__ import annotations

import sqlite3

MAIN_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    seq          INTEGER PRIMARY KEY,
    op_id        TEXT    NOT NULL UNIQUE,
    occurred_at  ANY     NOT NULL,
    body         BLOB,
    body_sha256  BLOB    NOT NULL,
    chain_hash   BLOB    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_main_pending
    ON records (occurred_at, seq) WHERE body IS NOT NULL;
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value BLOB NOT NULL
);
"""

ARCHIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    seq          INTEGER PRIMARY KEY,
    op_id        TEXT    NOT NULL UNIQUE,
    occurred_at  ANY     NOT NULL,
    body         BLOB    NOT NULL,
    body_sha256  BLOB    NOT NULL,
    chain_hash   BLOB    NOT NULL
);
"""


def _connect(path: str, ddl: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(ddl)
    return conn


def connect_main(path: str) -> sqlite3.Connection:
    """打开（必要时创建）主库并确保表结构存在。"""
    return _connect(path, MAIN_SCHEMA)


def connect_archive(path: str) -> sqlite3.Connection:
    """打开（必要时创建）归档库并确保表结构存在。"""
    return _connect(path, ARCHIVE_SCHEMA)
