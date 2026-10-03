"""20 万条 × 1 KiB 规模下，归档与核验的额外峰值内存不得超过 32 MiB。"""

import json
import os
import sqlite3
import subprocess
import sys

import pytest

from audit_store import AuditStore, Credential

WORKER = os.path.join(os.path.dirname(__file__), "memory_worker.py")
N = 200_000
LIMIT_MIB = 32


def _run(*args):
    r = subprocess.run(
        [sys.executable, WORKER, *map(str, args)], capture_output=True, text=True
    )
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    d = tmp_path_factory.mktemp("scale")
    _run("build", d, N)
    return d


def test_archive_memory_and_completeness(built):
    stats = json.loads(_run("archive", built))
    assert stats["migrated"] == N
    assert stats["total_archived"] == N
    assert stats["extra_mib"] < LIMIT_MIB, stats
    # 主库正文全部迁空
    db = sqlite3.connect(f"file:{built}/main.db?mode=ro", uri=True)
    left = db.execute(
        "SELECT COUNT(*) FROM records WHERE body IS NOT NULL"
    ).fetchone()[0]
    db.close()
    assert left == 0


def test_verify_memory_and_continued_append(built):
    stats = json.loads(_run("verify", built))
    assert stats["extra_mib"] < LIMIT_MIB, stats
    # 主库迁空后仍能继续追加，且 20 万条历史凭据依然有效
    with open(built / "cred.txt") as f:
        seq, h = f.read().split(",")
    store = AuditStore(str(built / "main.db"), str(built / "archive.db"))
    old = Credential(int(seq), bytes.fromhex(h))
    r = store.append("op-final", 1800000000, b"after-full-archive")
    assert r.seq == N + 1
    store.verify(old)
    store.verify(r.credential)
    store.close()
