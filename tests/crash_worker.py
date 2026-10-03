"""崩溃注入子进程：在指定写入阶段 os._exit 模拟进程被强制终止。

用法：python tests/crash_worker.py <dir> <scenario> <crash_point>
已确认返回的凭据写入 <dir>/confirmed.txt（每行 seq,chain_hash_hex）。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from audit_store import AuditStore  # noqa: E402

BASE = 1700000000


def main() -> None:
    workdir, scenario, crash_point = sys.argv[1], sys.argv[2], sys.argv[3]
    store = AuditStore(os.path.join(workdir, "main.db"),
                       os.path.join(workdir, "archive.db"))
    confirmed = open(os.path.join(workdir, "confirmed.txt"), "a")

    def save(cred):
        confirmed.write(f"{cred.seq},{cred.chain_hash.hex()}\n")
        confirmed.flush()
        os.fsync(confirmed.fileno())

    if scenario == "append":
        # 先确认 10 条，第 11 条在 crash_point 处崩溃（结果未返回）
        for i in range(1, 11):
            save(store.append(f"op-{i}", BASE + i, f"body-{i}".encode()).credential)
        store.crash_hook = lambda p: os._exit(1) if p == crash_point else None
        store.append("op-11", BASE + 11, b"body-11")
    elif scenario == "archive":
        # 600 条（> 一个迁移批次），归档在 crash_point 处崩溃
        last = None
        for i in range(1, 601):
            last = store.append(f"op-{i}", BASE + i, f"body-{i}".encode())
        save(last.credential)
        store.crash_hook = lambda p: os._exit(1) if p == crash_point else None
        store.archive(BASE + 1000)
    else:
        raise SystemExit(f"unknown scenario {scenario}")
    store.close()


if __name__ == "__main__":
    main()
