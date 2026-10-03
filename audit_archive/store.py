"""维修门店审计归档库：追加、归档、核验。"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from typing import Callable, Optional

from .errors import (
    ContentConflictError,
    CutoffRegressionError,
    OperationConflictError,
    VerificationError,
)

GENESIS_HASH = b"\x00" * 32
_CHUNK_SIZE = 512

_MAIN_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    seq INTEGER PRIMARY KEY,
    op_id TEXT NOT NULL UNIQUE,
    occurred_at INTEGER NOT NULL,
    body_hash BLOB NOT NULL,
    chain_hash BLOB NOT NULL,
    body BLOB
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""

_ARCHIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    seq INTEGER PRIMARY KEY,
    op_id TEXT NOT NULL,
    occurred_at INTEGER NOT NULL,
    chain_hash BLOB NOT NULL,
    body BLOB NOT NULL
);
"""


@dataclass(frozen=True)
class Credential:
    """完整性凭据：覆盖截至 seq 的全部历史。"""

    seq: int
    chain_hash: bytes


@dataclass(frozen=True)
class AppendResult:
    """追加成功返回：序号 + 截至该序号的完整性凭据。"""

    seq: int
    credential: Credential


@dataclass(frozen=True)
class ArchiveResult:
    """归档完成返回：本次迁移的记录条数与当前归档截止时刻。"""

    migrated: int
    cutoff: int


def _body_hash(body: bytes) -> bytes:
    return hashlib.sha256(body).digest()


def _chain_hash(prev: bytes, seq: int, op_id: str, occurred_at: int, body: bytes) -> bytes:
    op = op_id.encode("utf-8")
    h = hashlib.sha256()
    h.update(b"AA1")
    h.update(prev)
    h.update(seq.to_bytes(8, "big", signed=True))
    h.update(len(op).to_bytes(4, "big"))
    h.update(op)
    h.update(occurred_at.to_bytes(8, "big", signed=True))
    h.update(len(body).to_bytes(8, "big"))
    h.update(body)
    return h.digest()


class AuditStore:
    """由上层直接调用的审计归档库。

    当前记录与归档记录分别保存在两个本地 SQLite 文件中。
    所有调用串行执行，不要求并发支持。
    """

    def __init__(
        self,
        main_path: str,
        archive_path: str,
        *,
        durable: bool = True,
        crash_hook: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._main_path = main_path
        self._archive_path = archive_path
        self._hook = crash_hook or (lambda stage: None)
        self._main = self._open(main_path, durable, _MAIN_SCHEMA)
        self._archive = self._open(archive_path, durable, _ARCHIVE_SCHEMA)

    @staticmethod
    def _open(path: str, durable: bool, schema: str) -> sqlite3.Connection:
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=" + ("FULL" if durable else "NORMAL"))
        conn.executescript(schema)
        conn.commit()
        return conn

    def close(self) -> None:
        self._main.close()
        self._archive.close()

    def __enter__(self) -> "AuditStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def append(self, op_id: str, occurred_at: int, body: bytes) -> AppendResult:
        """追加一条记录；相同操作号重试返回原结果，携带不同记录则拒绝。"""
        body = bytes(body)
        digest = _body_hash(body)
        row = self._main.execute(
            "SELECT seq, occurred_at, body_hash, chain_hash FROM records WHERE op_id = ?",
            (op_id,),
        ).fetchone()
        if row is not None:
            seq, occ, stored_digest, chain = row
            if occ != occurred_at or stored_digest != digest:
                raise OperationConflictError(op_id)
            return AppendResult(seq, Credential(seq, chain))
        last = self._main.execute(
            "SELECT seq, chain_hash FROM records ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        if last is None:
            seq, prev_chain = 1, GENESIS_HASH
        else:
            seq, prev_chain = last[0] + 1, last[1]
        chain = _chain_hash(prev_chain, seq, op_id, occurred_at, body)
        self._hook("append.before_commit")
        with self._main:
            self._main.execute(
                "INSERT INTO records (seq, op_id, occurred_at, body_hash, chain_hash, body)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (seq, op_id, occurred_at, digest, chain, body),
            )
        self._hook("append.after_commit")
        return AppendResult(seq, Credential(seq, chain))

    def archive(self, cutoff: int) -> ArchiveResult:
        """把发生时刻严格早于 cutoff 的正文迁入归档库。"""
        row = self._main.execute(
            "SELECT value FROM meta WHERE key = 'last_cutoff'"
        ).fetchone()
        if row is not None and cutoff < row[0]:
            raise CutoffRegressionError(f"cutoff {cutoff} < 已使用的 {row[0]}")
        migrated = 0
        while True:
            rows = self._main.execute(
                "SELECT seq, op_id, occurred_at, body_hash, chain_hash, body"
                " FROM records WHERE occurred_at < ? AND body IS NOT NULL"
                " ORDER BY seq LIMIT ?",
                (cutoff, _CHUNK_SIZE),
            ).fetchall()
            if not rows:
                break
            self._hook("archive.before_archive_commit")
            with self._archive:
                for seq, op_id, occ, digest, chain, body in rows:
                    existing = self._archive.execute(
                        "SELECT op_id, occurred_at, chain_hash, body"
                        " FROM records WHERE seq = ?",
                        (seq,),
                    ).fetchone()
                    if existing is None:
                        self._archive.execute(
                            "INSERT INTO records (seq, op_id, occurred_at, chain_hash, body)"
                            " VALUES (?, ?, ?, ?, ?)",
                            (seq, op_id, occ, chain, body),
                        )
                    elif (
                        existing[0] != op_id
                        or existing[1] != occ
                        or existing[2] != chain
                        or _body_hash(existing[3]) != digest
                    ):
                        raise ContentConflictError(f"seq {seq} 两库内容冲突")
            self._hook("archive.after_archive_commit")
            with self._main:
                self._main.executemany(
                    "UPDATE records SET body = NULL WHERE seq = ?",
                    [(r[0],) for r in rows],
                )
            self._hook("archive.after_main_commit")
            migrated += len(rows)
        with self._main:
            self._main.execute(
                "INSERT INTO meta (key, value) VALUES ('last_cutoff', ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (cutoff,),
            )
        return ArchiveResult(migrated, cutoff)

    def verify(self, credential: Credential) -> bool:
        """用可信凭据核验从第一条到 credential.seq 的完整历史。"""
        if credential.seq < 1:
            raise VerificationError("凭据序号必须 >= 1")
        main = _connect_ro(self._main_path)
        archive = _connect_ro(self._archive_path)
        try:
            prev = GENESIS_HASH
            for seq in range(1, credential.seq + 1):
                m = main.execute(
                    "SELECT op_id, occurred_at, body_hash, chain_hash, body"
                    " FROM records WHERE seq = ?",
                    (seq,),
                ).fetchone()
                a = archive.execute(
                    "SELECT op_id, occurred_at, chain_hash, body"
                    " FROM records WHERE seq = ?",
                    (seq,),
                ).fetchone()
                if m is None:
                    raise VerificationError(f"seq {seq} 在主库中缺失")
                op_id, occ, digest, chain, body = m
                if a is not None:
                    if a[0] != op_id or a[1] != occ or a[2] != chain:
                        raise VerificationError(f"seq {seq} 两库元数据冲突")
                    if body is not None and _body_hash(a[3]) != digest:
                        raise VerificationError(f"seq {seq} 两库正文冲突")
                if body is None:
                    if a is None:
                        raise VerificationError(f"seq {seq} 正文缺失")
                    body = a[3]
                if _body_hash(body) != digest:
                    raise VerificationError(f"seq {seq} 正文与摘要不符")
                prev = _chain_hash(prev, seq, op_id, occ, body)
                if prev != chain:
                    raise VerificationError(f"seq {seq} 链哈希不匹配")
            if prev != credential.chain_hash:
                raise VerificationError("链尾与可信凭据不符")
            return True
        finally:
            main.close()
            archive.close()


def _connect_ro(path: str) -> sqlite3.Connection:
    """以只读方式打开；若 WAL 恢复需要写权限则退回普通连接（仍只执行 SELECT）。"""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        return conn
    except sqlite3.OperationalError:
        conn = sqlite3.connect(path)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        return conn
