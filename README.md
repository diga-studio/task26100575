# 维修门店审计归档库

纯 Python + SQLite 实现的追加式审计库，由上层直接调用。当前记录与归档记录
分别保存在两个本地 SQLite 文件中，通过全局哈希链保证从第一条到最新一条
历史的完整性。

## 能力概览

只提供三类能力：

- `append(op_id, occurred_at, body)` — 追加记录
- `archive(cutoff)` — 执行归档
- `verify(credential)` — 核验历史

记录包含唯一操作号 `op_id`、发生时刻 `occurred_at`（整数，由调用方提供，
允许乱序）和不透明正文 `body`（bytes）。记录按成功追加顺序取得从 1 开始
的连续序号，序号与发生时刻无关。

## 用法

```python
from audit_archive import AuditStore, Credential

store = AuditStore("main.db", "archive.db")

# 追加：返回序号与覆盖截至该序号全部历史的完整性凭据
result = store.append("op-0001", occurred_at=1_700_000_000, body=b"...")
result.seq                  # 1
result.credential           # Credential(seq=1, chain_hash=b"...32 字节")
# 调用方需在模块外可信保存 credential

# 幂等：相同操作号 + 相同记录重试，返回原结果（即使记录已归档）
again = store.append("op-0001", 1_700_000_000, b"...")
assert again == result
# 相同操作号携带不同记录 -> 抛出 OperationConflictError，历史不变

# 归档：发生时刻严格早于 cutoff 的正文迁入归档库；等于 cutoff 的不迁移
store.archive(cutoff=1_720_000_000)   # cutoff 只允许前进

# 核验：用可信凭据检查从第 1 条到 credential.seq 的完整历史
store.verify(result.credential)       # 通过返回 True，否则抛 VerificationError

store.close()
```

`AuditStore(..., durable=False)` 可关闭 `synchronous=FULL`（仅建议用于
基准测试）；默认模式下面向进程强杀与断电保证：已返回成功的追加不丢失，
未返回的追加用原操作号重试只会形成一条记录。

## 设计要点

- **哈希链**：每条记录存储 `chain_hash = SHA256(前一条 chain_hash ‖ seq ‖
  op_id ‖ occurred_at ‖ body)`，跨越两个文件形成全局审计链；凭据即
  `(seq, chain_hash)`。
- **归档两阶段提交**：先把一批记录写入归档库并提交，再清除主库对应正文
  （保留 `seq/op_id/occurred_at/body_hash/chain_hash` 元数据用于续链与
  幂等）。中断后重跑时，归档库已存在的同序号记录必须内容一致才跳过，
  否则抛 `ContentConflictError` —— 中断留下的副本不会掩盖真实冲突。
- **核验**：只读打开两个文件，逐条重放序号 1..credential.seq，重算链哈希
  并与凭据比对。能检出正文改动、记录缺失、顺序改动、两库同序号冲突与
  整段链尾删除；credential.seq 之后追加的记录不影响旧凭据。核验不修改
  任何记录。
- **内存**：归档按 512 条一批流式迁移，核验逐条流式重放，额外峰值内存
  不随历史正文总量增长。

## 测试

```bash
python3 -m pytest tests/ -q
```

- `tests/test_audit_archive.py` — 乱序时刻、幂等重试、冲突拒绝、归档边界
  （严格小于）、主库迁空后继续追加、与独立输入账本逐字节核对
- `tests/test_crash.py` — 追加/归档在各写入阶段被终止后重开、重试：
  已确认历史不丢不重，归档最终完成
- `tests/test_tamper.py` — 正文篡改（主库/归档库）、记录缺失、顺序调换、
  跨库冲突、整段链尾删除均检出；正常归档通过；核验不修改记录

## 内存验证（20 万条 × 1 KiB）

```bash
python3 scripts/benchmark_memory.py          # 默认 200000 条
```

本机实测（macOS, Python 3.14, SQLite 3.50）：

| 阶段 | Python 峰值 | RSS 额外峰值 | 上限 |
| ---- | ----------- | ------------ | ---- |
| 归档 | 1.43 MiB    | 7.08 MiB     | 32 MiB |
| 核验 | 0.04 MiB    | 4.98 MiB     | 32 MiB |

2 万条与 20 万条结果基本持平（7.02 → 7.08 MiB），额外峰值内存不随历史
正文总量线性增长。
