"""审计归档库主入口：追加记录、执行归档、核验历史。

所有调用串行执行。写路径使用 BEGIN IMMEDIATE 显式事务；
归档的跨库迁移通过 ATTACH 把两个库挂在同一连接上，
每个迁移批次都在单个事务内完成（回滚日志模式下 SQLite 保证
跨库事务的原子性），进程在任意写入阶段被终止后，
重新打开文件由 SQLite 自动恢复，无需人工修补。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import contextmanager
from typing import Iterator

from .chain import (
    GENESIS,
    body_hash,
    chain_hash,
    decode_occurred_at,
    encode_occurred_at,
)
from .errors import ArchiveConflictError, ConflictError, VerificationError
from .schema import connect_archive, connect_main
from .types import AppendResult, ArchiveResult, Credential

_BATCH_SIZE = 512
_META_CUTOFF = "archive_cutoff"


@contextmanager
def _tx(conn: sqlite3.Connection) -> Iterator[None]:
    """显式事务：提交或回滚，配合 autocommit 连接使用。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


class AuditStore:
    """跨越主库与归档库两个 SQLite 文件的全局审计链。"""

    def __init__(self, main_path: str, archive_path: str) -> None:
        if main_path == archive_path:
            raise ValueError("主库与归档库必须是两个不同的文件")
        # 先确保归档库文件与表结构存在，再 ATTACH 到主连接上，
        # 之后所有跨库读写都在同一连接、同一事务里完成。
        connect_archive(archive_path).close()
        self._db = connect_main(main_path)
        self._db.execute("ATTACH DATABASE ? AS archive", (archive_path,))
        # 故障注入钩子（仅测试使用）：在关键写入阶段被调用，
        # 测试可在钩子中 os._exit() 模拟进程被强制终止。
        self.crash_hook: Callable[[str], None] | None = None

    def _fire(self, point: str) -> None:
        if self.crash_hook is not None:
            self.crash_hook(point)

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "AuditStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- append

    def append(self, op_id: str, occurred_at: float, body: bytes) -> AppendResult:
        """追加一条记录，返回序号与覆盖全部历史的完整性凭据。

        相同操作号、相同记录的重试返回原结果（即使记录已归档）；
        相同操作号携带不同记录则拒绝且不改变历史。
        """
        if not isinstance(op_id, str) or not op_id:
            raise ValueError("op_id 必须是非空字符串")
        encode_occurred_at(occurred_at)  # 类型校验
        body = bytes(body)
        digest = body_hash(body)
        with _tx(self._db):
            row = self._db.execute(
                "SELECT seq, occurred_at, body_sha256, chain_hash"
                " FROM records WHERE op_id = ?",
                (op_id,),
            ).fetchone()
            if row is not None:
                seq, ts, stored_digest, stored_chain = row
                if ts == occurred_at and stored_digest == digest:
                    return AppendResult(seq, Credential(seq, stored_chain))
                raise ConflictError(f"操作号 {op_id!r} 已存在且记录内容不同")
            seq = self._db.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM records"
            ).fetchone()[0]
            prev = (
                self._db.execute(
                    "SELECT chain_hash FROM records WHERE seq = ?", (seq - 1,)
                ).fetchone()[0]
                if seq > 1
                else GENESIS
            )
            chain = chain_hash(prev, seq, op_id, occurred_at, body)
            self._db.execute(
                "INSERT INTO records"
                " (seq, op_id, occurred_at, body, body_sha256, chain_hash)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (seq, op_id, occurred_at, body, digest, chain),
            )
            self._fire("append.in_tx")
        self._fire("append.committed")
        return AppendResult(seq, Credential(seq, chain))

    # --------------------------------------------------------------- archive

    def archive(self, cutoff: float) -> ArchiveResult:
        """把发生时刻严格早于 cutoff 的正文迁入归档库。

        截止时刻只会前进：若曾应用过更晚的截止时刻，则沿用该值。
        每个批次一个跨库事务；中断后重复执行同一截止时刻可最终完成。
        归档库中已存在同序号但内容冲突的记录会报错而不是被掩盖。
        """
        encode_occurred_at(cutoff)  # 类型校验
        effective = self._effective_cutoff(cutoff)
        migrated = 0
        while True:
            batch = self._db.execute(
                "SELECT seq, op_id, occurred_at, body, body_sha256, chain_hash"
                " FROM records"
                " WHERE occurred_at < ? AND body IS NOT NULL"
                " ORDER BY seq LIMIT ?",
                (effective, _BATCH_SIZE),
            ).fetchall()
            if not batch:
                break
            with _tx(self._db):
                for row in batch:
                    self._migrate_one(row)
                self._fire("archive.batch_in_tx")
            self._fire("archive.batch_committed")
            migrated += len(batch)
        with _tx(self._db):
            self._db.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_META_CUTOFF, encode_occurred_at(effective)),
            )
        total = self._db.execute(
            "SELECT COUNT(*) FROM archive.records"
        ).fetchone()[0]
        self._fire("archive.done")
        return ArchiveResult(cutoff=effective, migrated=migrated, total_archived=total)

    def _effective_cutoff(self, cutoff: float) -> float:
        row = self._db.execute(
            "SELECT value FROM meta WHERE key = ?", (_META_CUTOFF,)
        ).fetchone()
        if row is None:
            return cutoff
        stored = decode_occurred_at(row[0])
        return stored if stored > cutoff else cutoff

    def _migrate_one(self, row: tuple) -> None:
        seq, op_id, occurred_at, body, digest, chain = row
        existing = self._db.execute(
            "SELECT op_id, occurred_at, body, body_sha256, chain_hash"
            " FROM archive.records WHERE seq = ?",
            (seq,),
        ).fetchone()
        if existing is None:
            self._db.execute(
                "INSERT INTO archive.records"
                " (seq, op_id, occurred_at, body, body_sha256, chain_hash)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (seq, op_id, occurred_at, body, digest, chain),
            )
        elif tuple(existing) != (op_id, occurred_at, body, digest, chain):
            raise ArchiveConflictError(
                f"序号 {seq} 在归档库中已存在且内容冲突，拒绝覆盖"
            )
        self._db.execute(
            "UPDATE records SET body = NULL WHERE seq = ?", (seq,)
        )

    # ---------------------------------------------------------------- verify

    _PROBE = (
        "SELECT seq, op_id, occurred_at, body, body_sha256, chain_hash"
        " FROM {table} ORDER BY seq"
    )

    def verify(self, credential: Credential) -> None:
        """用可信凭据核验从第 1 条到 credential.seq 的完整历史。

        只读检查，不修改任何记录。发现正文改动、记录缺失、顺序改动、
        两库同序号内容冲突或链尾被删除时抛出 VerificationError。
        """
        if (
            not isinstance(credential, Credential)
            or credential.seq < 1
            or len(credential.chain_hash) != 32
        ):
            raise VerificationError("凭据格式非法")
        target = credential.seq
        main_iter = self._stream(self._PROBE.format(table="records"))
        arch_iter = self._stream(self._PROBE.format(table="archive.records"))
        m = next(main_iter, None)
        a = next(arch_iter, None)
        prev = GENESIS
        for seq in range(1, target + 1):
            mrow = m if m is not None and m[0] == seq else None
            arow = a if a is not None and a[0] == seq else None
            if mrow is not None:
                m = next(main_iter, None)
            if arow is not None:
                a = next(arch_iter, None)
            row = self._resolve(seq, mrow, arow)
            _, op_id, occurred_at, body, digest, stored_chain = row
            if body_hash(body) != digest:
                raise VerificationError(f"序号 {seq} 正文与其摘要不符")
            prev = chain_hash(prev, seq, op_id, occurred_at, body)
            if prev != stored_chain:
                raise VerificationError(f"序号 {seq} 链式哈希不符，历史被改动")
        if prev != credential.chain_hash:
            raise VerificationError(
                f"截至序号 {target} 的整体历史与可信凭据不符"
            )

    def _stream(self, sql: str) -> Iterator[tuple]:
        cur = self._db.execute(sql)
        cur.arraysize = 1024  # 批量取行，峰值内存与历史总量无关
        return iter(cur)

    @staticmethod
    def _resolve(seq: int, mrow: tuple | None, arow: tuple | None) -> tuple:
        """合并两库中同一序号的视图，检出缺失与跨库冲突。"""
        if mrow is not None and arow is not None:
            # 元数据必须一致；主库仍持有正文时正文也必须一致
            if mrow[1:3] != arow[1:3] or mrow[4:6] != arow[4:6]:
                raise VerificationError(f"序号 {seq} 在两库中内容冲突")
            if mrow[3] is not None and mrow[3] != arow[3]:
                raise VerificationError(f"序号 {seq} 在两库中正文冲突")
            return arow
        if arow is not None:
            return arow
        if mrow is not None:
            if mrow[3] is None:
                raise VerificationError(f"序号 {seq} 的正文缺失")
            return mrow
        raise VerificationError(f"序号 {seq} 的记录缺失（链尾可能被删除）")
