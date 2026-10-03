import pytest

from audit_store import AuditStore, ConflictError, Credential
from tests.conftest import Ledger


def test_seq_starts_at_1_and_is_consecutive(store):
    for expect in range(1, 6):
        r = store.append(f"op-{expect}", 1700000000 + expect, b"x")
        assert r.seq == expect
        assert r.credential.seq == expect
        assert len(r.credential.chain_hash) == 32


def test_out_of_order_timestamps_do_not_affect_seq(store):
    r1 = store.append("a", 1700000900, b"1")
    r2 = store.append("b", 1700000001, b"2")  # 时刻更早，序号仍随后
    r3 = store.append("c", 1699999999, b"3")
    assert (r1.seq, r2.seq, r3.seq) == (1, 2, 3)
    assert r1.credential.chain_hash != r2.credential.chain_hash
    store.verify(r3.credential)


def test_credential_covers_full_history(store):
    creds = [store.append(f"op-{i}", i, b"b").credential for i in range(1, 8)]
    for c in creds:  # 每个历史凭据都独立有效
        store.verify(c)


def test_idempotent_retry_returns_original(store):
    first = store.append("op-1", 123, b"payload")
    for _ in range(3):
        again = store.append("op-1", 123, b"payload")
        assert again == first
    nxt = store.append("op-2", 124, b"other")
    assert nxt.seq == 2  # 重试不产生新记录


def test_conflicting_op_id_rejected_and_history_unchanged(store):
    r1 = store.append("op-1", 123, b"payload")
    with pytest.raises(ConflictError):
        store.append("op-1", 123, b"different-body")
    with pytest.raises(ConflictError):
        store.append("op-1", 999, b"payload")  # 时刻不同也算不同记录
    r2 = store.append("op-2", 124, b"next")
    assert r2.seq == 2  # 历史未被改变
    store.verify(r1.credential)
    store.verify(r2.credential)


def test_invalid_arguments(store):
    with pytest.raises(ValueError):
        store.append("", 1, b"x")
    with pytest.raises(ValueError):
        store.append(123, 1, b"x")
    with pytest.raises(TypeError):
        store.append("op", "not-a-time", b"x")
    with pytest.raises(TypeError):
        store.append("op", True, b"x")


def test_reopen_continues_chain(paths):
    s = AuditStore(*paths)
    c1 = s.append("op-1", 1, b"a").credential
    s.close()
    s = AuditStore(*paths)
    c2 = s.append("op-2", 2, b"b").credential
    assert c2.seq == 2
    s.verify(c1)
    s.verify(c2)
    s.close()


def test_append_matches_independent_ledger(store, paths):
    ledger = Ledger()
    body = bytes(range(256)) * 4  # 1 KiB 不透明正文
    last = None
    for i in range(1, 51):
        ts = 1700000000 + (i * 7919) % 100  # 乱序时刻
        last = ledger.record(f"op-{i}", ts, body, store.append(f"op-{i}", ts, body))
    store.verify(last.credential)
    ledger.check_against(*paths)
