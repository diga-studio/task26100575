import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from audit_store import AuditStore  # noqa: E402


@pytest.fixture()
def paths(tmp_path):
    return str(tmp_path / "main.db"), str(tmp_path / "archive.db")


@pytest.fixture()
def store(paths):
    s = AuditStore(*paths)
    yield s
    s.close()


class Ledger:
    """独立保留的输入账本：记录每次追加的原始输入与返回凭据。"""

    def __init__(self):
        self.entries = []  # (seq, op_id, occurred_at, body, chain_hash)

    def record(self, op_id, occurred_at, body, result):
        self.entries.append(
            (result.seq, op_id, occurred_at, bytes(body), result.credential.chain_hash)
        )
        return result

    def check_against(self, main_path, archive_path):
        """与两个 SQLite 文件逐字节核对：正文、时刻、操作号、链哈希。"""
        merged = {}
        for path, table in ((main_path, "records"), (archive_path, "records")):
            db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            for seq, op_id, ts, body, chain in db.execute(
                f"SELECT seq, op_id, occurred_at, body, chain_hash FROM {table}"
            ):
                if seq in merged and merged[seq][2] is None:
                    merged[seq] = (op_id, ts, body, chain)
                elif seq not in merged:
                    merged[seq] = (op_id, ts, body, chain)
            db.close()
        assert len(merged) == len(self.entries)
        for seq, op_id, ts, body, chain in self.entries:
            got = merged[seq]
            assert got[0] == op_id
            assert got[1] == ts
            assert got[2] == body  # 逐字节
            assert got[3] == chain
