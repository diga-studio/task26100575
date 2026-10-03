import sqlite3

import pytest

from audit_store import AuditStore
from tests.conftest import Ledger

BASE = 1700000000


def _bodies(path, table="records"):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    rows = dict(db.execute(f"SELECT seq, body FROM {table}").fetchall())
    db.close()
    return rows


def test_boundary_exact_cutoff_not_migrated(store, paths):
    for i, ts in enumerate([BASE - 1, BASE, BASE + 1], start=1):
        store.append(f"op-{i}", ts, f"body-{i}".encode())
    r = store.archive(BASE)
    assert r.migrated == 1 and r.total_archived == 1
    main_bodies = _bodies(paths[0])
    assert main_bodies[1] is None          # 严格早于 cutoff：已迁出
    assert main_bodies[2] == b"body-2"     # 恰好等于 cutoff：保留
    assert main_bodies[3] == b"body-3"


def test_cutoff_only_moves_forward(store):
    store.append("op-1", BASE + 50, b"a")
    store.append("op-2", BASE + 150, b"b")
    r1 = store.archive(BASE + 100)
    assert r1.migrated == 1
    r2 = store.archive(BASE + 10)  # 更小的 cutoff：沿用更晚的
    assert r2.cutoff == BASE + 100 and r2.migrated == 0


def test_repeat_same_cutoff_completes(store):
    for i in range(1, 30):
        store.append(f"op-{i}", BASE + i, b"x" * 100)
    r1 = store.archive(BASE + 15)
    r2 = store.archive(BASE + 15)
    assert r1.migrated == 14
    assert r2.migrated == 0 and r2.total_archived == 14


def test_archive_empty_store(store):
    r = store.archive(BASE)
    assert r.migrated == 0 and r.total_archived == 0


def test_full_migration_then_continue_appending(store, paths):
    ledger = Ledger()
    creds = {}
    for i in range(1, 41):
        ts = BASE + (i * 37) % 50  # 乱序时刻
        creds[i] = ledger.record(
            f"op-{i}", ts, f"body-{i}".encode(), store.append(f"op-{i}", ts, f"body-{i}".encode())
        ).credential
    r = store.archive(BASE + 100)  # 全部迁空
    assert r.migrated == 40
    assert all(b is None for b in _bodies(paths[0]).values())
    # 主库迁空后继续追加，序号与链延续
    for i in range(41, 51):
        ts = BASE + 200 + i
        creds[i] = ledger.record(
            f"op-{i}", ts, f"new-{i}".encode(), store.append(f"op-{i}", ts, f"new-{i}".encode())
        ).credential
    assert creds[41].seq == 41
    # 归档前的旧凭据与新凭据全部有效
    for c in creds.values():
        store.verify(c)
    ledger.check_against(*paths)


def test_archived_op_retry_returns_original_credential(store):
    first = store.append("op-1", BASE - 10, b"payload")
    store.archive(BASE)
    again = store.append("op-1", BASE - 10, b"payload")
    assert again == first  # 已归档仍幂等
    with pytest.raises(Exception):
        store.append("op-1", BASE - 10, b"tampered-retry")


def test_archive_does_not_change_credentials(store):
    creds = {}
    for i in range(1, 11):
        creds[i] = store.append(f"op-{i}", BASE + i, f"b-{i}".encode()).credential
    store.archive(BASE + 5)
    for i, c in creds.items():
        store.verify(c)  # 归档不改变既有的完整性凭据
