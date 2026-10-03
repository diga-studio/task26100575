"""规模与内存验证子进程。

用法：
  python tests/memory_worker.py build  <dir> <n>   # 建库并保存最终凭据
  python tests/memory_worker.py archive <dir>      # 输出归档阶段额外峰值内存
  python tests/memory_worker.py verify  <dir>      # 输出核验阶段额外峰值内存
"""

import gc
import hashlib
import json
import os
import resource
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from audit_store import AuditStore, Credential  # noqa: E402

BASE = 1700000000
BODY = hashlib.sha256(b"repair-audit").digest() * 32  # 1 KiB 确定性正文


def peak_mib() -> float:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS 单位为字节，Linux 为 KiB
    return rss / 1024 / 1024 if sys.platform == "darwin" else rss / 1024


def paths(d):
    return os.path.join(d, "main.db"), os.path.join(d, "archive.db")


def build(d, n):
    store = AuditStore(*paths(d))
    cred = None
    for i in range(1, n + 1):
        ts = BASE + (i * 7919) % n  # 乱序时刻
        cred = store.append(f"op-{i}", ts, BODY).credential
    with open(os.path.join(d, "cred.txt"), "w") as f:
        f.write(f"{cred.seq},{cred.chain_hash.hex()}")
    store.close()


def load_cred(d):
    with open(os.path.join(d, "cred.txt")) as f:
        seq, h = f.read().split(",")
    return Credential(int(seq), bytes.fromhex(h))


def measure(d, phase):
    gc.collect()
    baseline = peak_mib()
    store = AuditStore(*paths(d))
    gc.collect()
    opened = peak_mib()
    if phase == "archive":
        res = store.archive(BASE + 10**12)  # 截止时刻远超全部时刻：主库迁空
        extra = {"migrated": res.migrated, "total_archived": res.total_archived}
    else:
        store.verify(load_cred(d))
        extra = {}
    store.close()
    gc.collect()
    print(json.dumps({
        "baseline_mib": round(baseline, 2),
        "opened_mib": round(opened, 2),
        "peak_mib": round(peak_mib(), 2),
        "extra_mib": round(peak_mib() - opened, 2),
        **extra,
    }))


if __name__ == "__main__":
    cmd, d = sys.argv[1], sys.argv[2]
    if cmd == "build":
        build(d, int(sys.argv[3]))
    else:
        measure(d, cmd)
