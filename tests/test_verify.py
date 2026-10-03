import sqlite3

import pytest

from audit_store import AuditStore, Credential, VerificationError

BASE = 1700000000


@pytest.fixture()
def built(paths):
    """20 条记录（乱序时刻），前 10 条归档，返回凭据列表。"""
    s = AuditStore(*paths)
    creds = []
    for i in range(1, 21):
        ts = BASE + (i * 13) % 25
        creds.append(s.append(f"op-{i}", ts, f"body-{i}".encode()).credential)
    s.archive(BASE + 25)  # 全部迁空
    for i in range(21, 26):  # 迁空后继续追加
        creds.append(s.append(f"op-{i}", BASE + 100 + i, f"n-{i}".encode()).credential)
    s.close()
    return creds


def _sql(path, stmt, args=()):
    db = sqlite3.connect(path)
    db.execute(stmt, args)
    db.commit()
    db.close()


def _reopen(paths):
    return AuditStore(*paths)


def test_clean_history_passes(built, paths):
    s = _reopen(paths)
    for c in built:
        s.verify(c)
    s.close()


def test_tamper_body_in_main_detected(built, paths):
    _sql(paths[0], "UPDATE records SET body = ? WHERE seq = 22", (b"forged",))
    s = _reopen(paths)
    with pytest.raises(VerificationError):
        s.verify(built[-1])
    s.close()


def test_tamper_body_in_archive_detected(built, paths):
    _sql(paths[1], "UPDATE records SET body = ? WHERE seq = 3", (b"forged",))
    s = _reopen(paths)
    with pytest.raises(VerificationError):
        s.verify(built[9])
    s.close()


def test_tamper_op_id_detected(built, paths):
    _sql(paths[1], "UPDATE records SET op_id = 'op-x' WHERE seq = 5")
    s = _reopen(paths)
    with pytest.raises(VerificationError):
        s.verify(built[9])
    s.close()


def test_tamper_occurred_at_detected(built, paths):
    _sql(paths[0], "UPDATE records SET occurred_at = occurred_at + 1 WHERE seq = 23")
    s = _reopen(paths)
    with pytest.raises(VerificationError):
        s.verify(built[-1])
    s.close()


def test_delete_middle_record_detected(built, paths):
    _sql(paths[1], "DELETE FROM records WHERE seq = 7")
    s = _reopen(paths)
    with pytest.raises(VerificationError):
        s.verify(built[9])
    s.close()


def test_delete_whole_tail_detected(built, paths):
    # 整段链尾被删除：归档库 11..20 与主库全部记录
    _sql(paths[1], "DELETE FROM records WHERE seq >= 11")
    _sql(paths[0], "DELETE FROM records")
    s = _reopen(paths)
    with pytest.raises(VerificationError):
        s.verify(built[19])  # 针对 seq 20 的凭据绝不能通过
    s.verify(built[9])       # 未受损的前缀仍然有效
    s.close()


def test_reorder_detected(built, paths):
    _sql(paths[1], "UPDATE records SET seq = seq + 1000 WHERE seq IN (4, 6)")
    _sql(paths[1], "UPDATE records SET seq = 6 WHERE seq = 1004")
    _sql(paths[1], "UPDATE records SET seq = 4 WHERE seq = 1006")
    s = _reopen(paths)
    with pytest.raises(VerificationError):
        s.verify(built[9])
    s.close()


def test_cross_db_conflict_detected(built, paths):
    # 人为制造两库同序号但内容冲突（如中断留下的副本被篡改）
    row = sqlite3.connect(paths[1]).execute(
        "SELECT seq, op_id, occurred_at, body, body_sha256, chain_hash"
        " FROM records WHERE seq = 8"
    ).fetchone()
    _sql(
        paths[0],
        "UPDATE records SET body = ?, body_sha256 = ?, chain_hash = ?"
        " WHERE seq = 8",
        (row[3], row[4], b"\x00" * 32),  # 主库副本的链哈希被改
    )
    _sql(paths[0], "UPDATE records SET body = NULL WHERE seq = 8")
    # 恢复主库正文以制造“迁移途中”状态，但链哈希与归档库冲突
    _sql(paths[0], "UPDATE records SET body = ? WHERE seq = 8", (row[3],))
    s = _reopen(paths)
    with pytest.raises(VerificationError):
        s.verify(built[9])
    s.close()


def test_transient_duplicate_identical_passes(built, paths):
    # 归档中断留下的合法状态：两库同序号、内容完全一致
    row = sqlite3.connect(paths[1]).execute(
        "SELECT body FROM records WHERE seq = 2"
    ).fetchone()
    _sql(paths[0], "UPDATE records SET body = ? WHERE seq = 2", (row[0],))
    s = _reopen(paths)
    s.verify(built[9])
    s.close()


def test_later_appends_do_not_invalidate_old_credential(built, paths):
    s = _reopen(paths)
    old = built[9]
    for i in range(26, 31):
        s.append(f"op-{i}", BASE + 200 + i, b"later")
    s.verify(old)
    s.close()


def test_verify_does_not_modify_records(built, paths):
    def dump(path, table):
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        rows = db.execute(f"SELECT * FROM {table} ORDER BY seq").fetchall()
        db.close()
        return rows

    before = (dump(paths[0], "records"), dump(paths[1], "records"))
    s = _reopen(paths)
    s.verify(built[-1])
    s.close()
    after = (dump(paths[0], "records"), dump(paths[1], "records"))
    assert before == after


def test_invalid_credential_rejected(store):
    with pytest.raises(VerificationError):
        store.verify(Credential(0, b"\x00" * 32))
    with pytest.raises(VerificationError):
        store.verify(Credential(1, b"short"))
