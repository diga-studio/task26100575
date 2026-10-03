import os
import sqlite3
import subprocess
import sys

import pytest

from audit_store import AuditStore
from audit_store import Credential

BASE = 1700000000
WORKER = os.path.join(os.path.dirname(__file__), "crash_worker.py")


def _run_worker(tmp_path, scenario, crash_point):
    r = subprocess.run(
        [sys.executable, WORKER, str(tmp_path), scenario, crash_point],
        capture_output=True,
    )
    assert r.returncode != 0  # 子进程被“强制终止”


def _confirmed(tmp_path):
    creds = {}
    with open(tmp_path / "confirmed.txt") as f:
        for line in f:
            seq, h = line.strip().split(",")
            creds[int(seq)] = Credential(int(seq), bytes.fromhex(h))
    return creds


def _paths(tmp_path):
    return str(tmp_path / "main.db"), str(tmp_path / "archive.db")


def _count_op(paths, op_id):
    n = 0
    for p in paths:
        db = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        n += db.execute(
            "SELECT COUNT(*) FROM records WHERE op_id = ?", (op_id,)
        ).fetchone()[0]
        db.close()
    return n


@pytest.mark.parametrize("point", ["append.in_tx", "append.committed"])
def test_crash_during_append(tmp_path, point):
    _run_worker(tmp_path, "append", point)
    mp, ap = _paths(tmp_path)
    store = AuditStore(mp, ap)
    creds = _confirmed(tmp_path)
    # 已确认历史不丢
    for c in creds.values():
        store.verify(c)
    # 未返回的追加用原操作号重试：只形成一条记录
    r = store.append("op-11", BASE + 11, b"body-11")
    assert _count_op((mp, ap), "op-11") == 1
    if point == "append.committed":
        assert r.seq == 11  # 提交已成功，重试返回原结果
    else:
        assert r.seq == 11  # 事务回滚，重新追加取得同一序号
    store.verify(r.credential)
    store.close()


@pytest.mark.parametrize(
    "point", ["archive.batch_in_tx", "archive.batch_committed", "archive.done"]
)
def test_crash_during_archive(tmp_path, point):
    _run_worker(tmp_path, "archive", point)
    mp, ap = _paths(tmp_path)
    store = AuditStore(mp, ap)
    creds = _confirmed(tmp_path)
    # 重新打开无需人工修补，已确认历史完整
    store.verify(creds[600])
    # 重复执行同一截止时刻的归档必须最终完成
    res = store.archive(BASE + 1000)
    assert res.total_archived == 600
    again = store.archive(BASE + 1000)
    assert again.migrated == 0
    # 逻辑历史不重复：每个序号全局唯一
    for p, t in ((mp, "records"), (ap, "records")):
        db = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        dup = db.execute(
            f"SELECT seq, COUNT(*) c FROM {t} GROUP BY seq HAVING c > 1"
        ).fetchall()
        db.close()
        assert dup == []
    # 归档完成后历史仍可核验，且能继续追加
    store.verify(creds[600])
    r = store.append("op-601", BASE + 601, b"after-crash")
    assert r.seq == 601
    store.verify(r.credential)
    store.close()
