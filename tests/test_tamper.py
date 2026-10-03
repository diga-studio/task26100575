"""篡改检测测试：用事先保存的可信凭据核验，各类破坏均须检出。"""

import sqlite3

import pytest

from audit_archive import AuditStore, ContentConflictError, VerificationError


@pytest.fixture()
def paths(tmp_path):
    return str(tmp_path / "main.db"), str(tmp_path / "archive.db")


def _build(paths, count=20, cutoff=None):
    """建立历史并返回最终凭据；cutoff 给定则先归档。"""
    store = AuditStore(*paths)
    cred = None
    for i in range(count):
        cred = store.append(f"op-{i}", i * 10, f"body-{i}".encode()).credential
    if cutoff is not None:
        store.archive(cutoff)
    store.close()
    return cred


def _exec(path, sql, args=()):
    conn = sqlite3.connect(path)
    conn.execute(sql, args)
    conn.commit()
    conn.close()


def test_normal_archive_passes_verification(paths):
    cred = _build(paths, 50, cutoff=10_000)
    store = AuditStore(*paths)
    assert store.verify(cred) is True
    store.close()


def test_tampered_body_in_main_detected(paths):
    cred = _build(paths, 20)
    _exec(paths[0], "UPDATE records SET body = ? WHERE seq = 5", (b"forged",))
    store = AuditStore(*paths)
    with pytest.raises(VerificationError):
        store.verify(cred)
    store.close()


def test_tampered_body_in_archive_detected(paths):
    cred = _build(paths, 20, cutoff=10_000)
    _exec(paths[1], "UPDATE records SET body = ? WHERE seq = 5", (b"forged",))
    store = AuditStore(*paths)
    with pytest.raises(VerificationError):
        store.verify(cred)
    store.close()


def test_missing_record_detected(paths):
    cred = _build(paths, 20)
    _exec(paths[0], "DELETE FROM records WHERE seq = 7")
    store = AuditStore(*paths)
    with pytest.raises(VerificationError):
        store.verify(cred)
    store.close()


def test_order_swap_detected(paths):
    cred = _build(paths, 20)
    main = paths[0]
    b3 = sqlite3.connect(main).execute(
        "SELECT body FROM records WHERE seq = 3"
    ).fetchone()[0]
    b4 = sqlite3.connect(main).execute(
        "SELECT body FROM records WHERE seq = 4"
    ).fetchone()[0]
    _exec(main, "UPDATE records SET body = ? WHERE seq = 3", (b4,))
    _exec(main, "UPDATE records SET body = ? WHERE seq = 4", (b3,))
    store = AuditStore(*paths)
    with pytest.raises(VerificationError):
        store.verify(cred)
    store.close()


def test_whole_chain_tail_deletion_detected(paths):
    cred = _build(paths, 20, cutoff=10_000)
    _exec(paths[0], "DELETE FROM records WHERE seq > 10")
    _exec(paths[1], "DELETE FROM records WHERE seq > 10")
    store = AuditStore(*paths)
    with pytest.raises(VerificationError):
        store.verify(cred)
    store.close()


def test_cross_database_conflict_detected_by_verify(paths):
    """归档中断留下的副本与主库内容冲突时，核验必须检出。"""
    cred = _build(paths, 20)
    # 模拟中断留下的副本：归档库出现 seq=5，但正文与主库不一致
    row = sqlite3.connect(paths[0]).execute(
        "SELECT seq, op_id, occurred_at, chain_hash FROM records WHERE seq = 5"
    ).fetchone()
    _exec(
        paths[1],
        "INSERT INTO records (seq, op_id, occurred_at, chain_hash, body)"
        " VALUES (?, ?, ?, ?, ?)",
        (row[0], row[1], row[2], row[3], b"conflicting-copy"),
    )
    store = AuditStore(*paths)
    with pytest.raises(VerificationError):
        store.verify(cred)
    store.close()


def test_cross_database_conflict_detected_by_archive(paths):
    """归档过程中发现归档库已有同序号不同内容，必须报冲突而不是覆盖。"""
    cred = _build(paths, 20)
    row = sqlite3.connect(paths[0]).execute(
        "SELECT seq, op_id, occurred_at, chain_hash FROM records WHERE seq = 3"
    ).fetchone()
    _exec(
        paths[1],
        "INSERT INTO records (seq, op_id, occurred_at, chain_hash, body)"
        " VALUES (?, ?, ?, ?, ?)",
        (row[0], row[1], row[2], row[3], b"stale-copy"),
    )
    store = AuditStore(*paths)
    with pytest.raises(ContentConflictError):
        store.archive(10_000)
    store.close()


def test_later_appends_do_not_invalidate_old_credentials(paths):
    store = AuditStore(*paths)
    creds = [store.append(f"op-{i}", i, f"b-{i}".encode()).credential for i in range(10)]
    store.archive(10_000)
    for i in range(10, 30):
        store.append(f"op-{i}", i, f"b-{i}".encode())
    for cred in creds:
        assert store.verify(cred) is True
    store.close()


def test_verification_does_not_modify_records(paths):
    cred = _build(paths, 20, cutoff=10_000)
    snapshots = []
    for path in paths:
        conn = sqlite3.connect(path)
        snapshots.append(conn.execute("SELECT * FROM records ORDER BY seq").fetchall())
        conn.close()
    store = AuditStore(*paths)
    assert store.verify(cred) is True
    store.close()
    for path, snap in zip(paths, snapshots):
        conn = sqlite3.connect(path)
        assert conn.execute("SELECT * FROM records ORDER BY seq").fetchall() == snap
        conn.close()
