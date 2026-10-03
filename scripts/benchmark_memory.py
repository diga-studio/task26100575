"""内存基准：20 万条 x 1 KiB 正文，验证归档与核验的额外峰值内存 <= 32 MiB。

用法：python3 scripts/benchmark_memory.py [记录数]
归档与核验各自在全新子进程中执行，分别测量：
  - tracemalloc 的 Python 侧峰值
  - 进程 RSS 峰值相对基线的增量（含 SQLite 页缓存等 C 侧分配）
"""

import os
import resource
import subprocess
import sys
import tempfile
import tracemalloc

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

N_DEFAULT = 200_000
BODY_SIZE = 1024
LIMIT = 32 * 1024 * 1024


def _peak_rss() -> int:
    # macOS 上 ru_maxrss 单位为字节（Linux 为 KiB）
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform == "darwin" else value * 1024


def _paths(directory):
    return os.path.join(directory, "main.db"), os.path.join(directory, "archive.db")


def build(directory: str, n: int) -> None:
    from audit_archive import AuditStore

    store = AuditStore(*_paths(directory), durable=False)
    body = bytes(BODY_SIZE)
    for i in range(n):
        store.append(f"op-{i}", i, body)
    store.close()


def run_phase(phase: str, directory: str) -> None:
    from audit_archive import AuditStore, Credential

    store = AuditStore(*_paths(directory), durable=False)
    baseline = _peak_rss()
    tracemalloc.start()
    if phase == "archive":
        store.archive(2**62)
    else:
        seq, chain = store._main.execute(
            "SELECT seq, chain_hash FROM records ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        assert store.verify(Credential(seq, chain)) is True
    py_peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    store.close()
    rss_extra = max(0, _peak_rss() - baseline)
    print(
        f"[{phase}] Python 峰值 {py_peak / 2**20:.2f} MiB, "
        f"RSS 额外峰值 {rss_extra / 2**20:.2f} MiB, 上限 32.00 MiB"
    )
    ok = py_peak < LIMIT and rss_extra < LIMIT
    print(f"[{phase}] {'通过' if ok else '超标'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    if len(sys.argv) == 3:
        run_phase(sys.argv[1], sys.argv[2])
        sys.exit(0)
    n = int(sys.argv[1]) if len(sys.argv) > 1 else N_DEFAULT
    directory = tempfile.mkdtemp(prefix="audit-bench-")
    print(f"构建 {n} 条 x {BODY_SIZE} B 记录 ...")
    build(directory, n)
    size = sum(
        os.path.getsize(os.path.join(directory, f))
        for f in os.listdir(directory)
    )
    print(f"数据库文件共 {size / 2**20:.1f} MiB，开始分相测量 ...")
    rc = 0
    for phase in ("archive", "verify"):
        rc |= subprocess.run(
            [sys.executable, os.path.abspath(__file__), phase, directory]
        ).returncode
    sys.exit(rc)
