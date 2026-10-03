"""核心行为测试：乱序时刻、幂等、归档迁空后续追加、账本逐字节核对。"""

import os
import sqlite3

import pytest

from audit_archive import (
    AuditStore,
    Credential,
    CutoffRegressionError,
    OperationConflictError,
)


@pytest.fixture()
def paths(tmp_path):
    return str(tmp_path / "main.db"), str(tmp_path / "archive.db")


def _bodies(paths):
    """直接从两个库读出 {seq: body}，用于与输入账本逐字节核对。"""
    main_path, archive_path = paths
    out = {}
    for seq, body in sqlite3.connect(archive_path).execute(
        "SELECT seq, body FROM records"
    ):
        out[seq] = body
    for seq, body in sqlite3.connect(main_path).execute(
        "SELECT seq, body FROM records WHERE body IS NOT NULL"
    ):
        assert seq not in out
        out[seq] = body
    return out


def test_append_returns_sequential_seq_and_credentials(paths):
    with AuditStore(*paths) as store:
        results = [store.append(f"op-{i}", i * 10, f"body-{i}".encode()) for i in range(5)]
    assert [r.seq for r in results] == [1, 2, 3, 4, 5]
    assert all(r.credential.seq == r.seq for r in results)
    assert len({r.credential.chain_hash for r in results}) == 5


def test_out_of_order_timestamps_do_not_affect_seq(paths):
    with AuditStore(*paths) as store:
        r1 = store.append("late", 9999, b"x")
        r2 = store.append("early", 1, b"y")
    assert (r1.seq, r2.seq) == (1, 2)


def test_retry_same_operation_returns_original_result(paths):
    with AuditStore(*paths) as store:
        first = store.append("op", 100, b"data")
        assert store.append("op", 100, b"data") == first
    # 重开后重试仍返回原结果
    with AuditStore(*paths) as store:
        assert store.append("op", 100, b"data") == first


def test_same_op_id_with_different_record_rejected(paths):
    with AuditStore(*paths) as store:
        store.append("op", 100, b"data")
        with pytest.raises(OperationConflictError):
            store.append("op", 100, b"other")
        with pytest.raises(OperationConflictError):
            store.append("op", 101, b"data")
        # 拒绝不改变历史
        again = store.append("op", 100, b"data")
        assert again.seq == 1
        assert store.append("next", 200, b"n").seq == 2


def test_archive_moves_only_strictly_older_bodies(paths):
    main_path, _ = paths
    with AuditStore(*paths) as store:
        store.append("a", 99, b"body-a")
        store.append("b", 100, b"body-b")  # 恰好等于截止时刻，不迁移
        store.append("c", 101, b"body-c")
        res = store.archive(100)
        assert res.migrated == 1
        rows = dict(
            sqlite3.connect(main_path).execute(
                "SELECT seq, body IS NOT NULL FROM records"
            ).fetchall()
        )
        assert rows == {1: 0, 2: 1, 3: 1}


def test_cutoff_never_goes_backwards(paths):
    with AuditStore(*paths) as store:
        store.append("a", 1, b"x")
        store.archive(100)
        with pytest.raises(CutoffRegressionError):
            store.archive(99)
        store.archive(100)  # 相同截止时刻允许重复执行


def test_archive_empty_main_then_continue_append_and_check_ledger(paths):
    """乱序时刻 + 主库正文全部迁空后继续追加，与输入账本逐字节核对。"""
    ledger = []  # (op_id, occurred_at, body)
    credentials = []
    with AuditStore(*paths) as store:
        for i in range(200):
            occurred_at = (i * 37 % 200) * 1000  # 乱序
            body = os.urandom(64) + f"rec-{i}".encode()
            ledger.append((f"op-{i}", occurred_at, body))
            credentials.append(store.append(f"op-{i}", occurred_at, body).credential)
        # 截止时刻覆盖全部已有时刻 -> 主库正文迁空
        res = store.archive(200 * 1000)
        assert res.migrated == 200
        main_path, archive_path = paths
        assert sqlite3.connect(main_path).execute(
            "SELECT COUNT(*) FROM records WHERE body IS NOT NULL"
        ).fetchone()[0] == 0
        assert sqlite3.connect(archive_path).execute(
            "SELECT COUNT(*) FROM records"
        ).fetchone()[0] == 200
        # 迁空后继续追加
        for i in range(200, 220):
            body = os.urandom(64) + f"rec-{i}".encode()
            ledger.append((f"op-{i}", 500_000 + i, body))
            credentials.append(store.append(f"op-{i}", 500_000 + i, body).credential)
        assert credentials[-1].seq == 220
        # 旧凭据在归档与后续追加后仍然有效
        for cred in credentials:
            assert store.verify(cred) is True
        # 归档后重试已归档操作号仍返回原结果
        retry = store.append("op-0", ledger[0][1], ledger[0][2])
        assert retry.seq == 1 and retry.credential == credentials[0]
    # 与独立账本逐字节核对
    bodies = _bodies(paths)
    assert len(bodies) == 220
    for seq, (_, _, body) in enumerate(ledger, start=1):
        assert bodies[seq] == body
