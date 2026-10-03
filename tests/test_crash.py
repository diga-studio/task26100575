"""崩溃恢复测试：追加与归档在不同写入阶段被终止后重开、重试。"""

import os
import sqlite3

import pytest

from audit_archive import AuditStore


class _Crash(Exception):
    pass


def _kill(store):
    """模拟进程被强杀：直接关闭连接，未提交事务回滚。"""
    store.close()


@pytest.fixture()
def paths(tmp_path):
    return str(tmp_path / "main.db"), str(tmp_path / "archive.db")


def _reopen(paths, hook=None):
    return AuditStore(*paths, crash_hook=hook)


def test_append_crash_before_commit_then_retry_creates_single_record(paths):
    calls = {"n": 0}

    def hook(stage):
        if stage == "append.before_commit":
            calls["n"] += 1
            if calls["n"] == 2:
                raise _Crash

    store = _reopen(paths, hook)
    store.append("op-0", 1, b"a")
    with pytest.raises(_Crash):
        store.append("op-1", 2, b"b")
    _kill(store)

    store = _reopen(paths)
    result = store.append("op-1", 2, b"b")  # 未返回的追加用原操作号重试
    assert result.seq == 2
    assert store.append("op-2", 3, b"c").seq == 3
    assert store.verify(store.append("op-3", 4, b"d").credential) is True
    count = sqlite3.connect(paths[0]).execute("SELECT COUNT(*) FROM records").fetchone()[0]
    assert count == 4  # 每条记录只出现一次
    store.close()


def test_append_crash_after_commit_retry_returns_same_result(paths):
    store = _reopen(paths)
    ok = store.append("op-0", 1, b"a")
    store.close()

    def hook(stage):
        if stage == "append.after_commit":
            raise _Crash

    store = _reopen(paths, hook)
    with pytest.raises(_Crash):
        store.append("op-1", 2, b"b")  # 已提交但结果未返回
    _kill(store)

    store = _reopen(paths)
    retry = store.append("op-1", 2, b"b")
    assert retry.seq == 2  # 重试拿到原序号，不产生重复记录
    count = sqlite3.connect(paths[0]).execute("SELECT COUNT(*) FROM records").fetchone()[0]
    assert count == 2
    assert store.verify(retry.credential) is True
    assert store.verify(ok.credential) is True  # 已确认历史不丢
    store.close()


def _setup_and_crash_archive(paths, crash_stage, record_count=30):
    store = _reopen(paths)
    creds = []
    for i in range(record_count):
        creds.append(store.append(f"op-{i}", i, f"body-{i}".encode()).credential)
    store.close()

    def hook(stage):
        if stage == crash_stage:
            raise _Crash

    store = _reopen(paths, hook)
    with pytest.raises(_Crash):
        store.archive(10_000)
    _kill(store)
    return creds


@pytest.mark.parametrize(
    "stage",
    [
        "archive.before_archive_commit",
        "archive.after_archive_commit",
        "archive.after_main_commit",
    ],
)
def test_archive_crash_at_each_stage_then_rerun_completes(paths, stage):
    creds = _setup_and_crash_archive(paths, stage)
    store = _reopen(paths)
    result = store.archive(10_000)  # 重复执行同一截止时刻，最终完成
    main_path, archive_path = paths
    assert sqlite3.connect(main_path).execute(
        "SELECT COUNT(*) FROM records WHERE body IS NOT NULL"
    ).fetchone()[0] == 0
    assert sqlite3.connect(archive_path).execute(
        "SELECT COUNT(*) FROM records"
    ).fetchone()[0] == 30
    for cred in creds:  # 已确认历史不丢不重
        assert store.verify(cred) is True
    again = store.archive(10_000)
    assert again.migrated == 0
    store.close()


def test_archive_crash_between_chunks_resumes_without_duplicates(paths):
    store = _reopen(paths)
    creds = [store.append(f"op-{i}", i, os.urandom(32)).credential for i in range(1500)]
    store.close()

    calls = {"n": 0}

    def hook(stage):
        if stage == "archive.after_main_commit":
            calls["n"] += 1
            if calls["n"] == 1:
                raise _Crash  # 第一批块完成后崩溃

    store = _reopen(paths, hook)
    with pytest.raises(_Crash):
        store.archive(10_000)
    _kill(store)

    store = _reopen(paths)
    store.archive(10_000)
    main_path, archive_path = paths
    assert sqlite3.connect(archive_path).execute(
        "SELECT COUNT(*) FROM records"
    ).fetchone()[0] == 1500
    assert sqlite3.connect(archive_path).execute(
        "SELECT COUNT(DISTINCT seq) FROM records"
    ).fetchone()[0] == 1500
    assert sqlite3.connect(main_path).execute(
        "SELECT COUNT(*) FROM records WHERE body IS NOT NULL"
    ).fetchone()[0] == 0
    for cred in creds[::100] + [creds[-1]]:
        assert store.verify(cred) is True
    store.close()
