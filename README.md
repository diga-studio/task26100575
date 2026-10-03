# 维修门店审计归档库（audit_store）

纯 Python + SQLite 的本地审计归档库，由上层直接调用。当前记录与归档记录
分别保存在**两个本地 SQLite 文件**中，共同构成一条从序号 1 开始、
按追加顺序连续编号的全局审计链。无第三方依赖，不监听任何端口。

## 能力总览

只提供三类能力：

| 方法 | 说明 |
| --- | --- |
| `append(op_id, occurred_at, body)` | 追加一条记录，返回序号与完整性凭据 |
| `archive(cutoff)` | 把发生时刻严格早于截止时刻的正文迁入归档库 |
| `verify(credential)` | 用可信凭据核验从第 1 条到凭据序号的完整历史 |

记录只包含：唯一操作号 `op_id`（非空字符串）、发生时刻
`occurred_at`（`int` 或 `float`，由调用方提供、允许乱序）、
不透明正文 `body`（`bytes`，上层已生成的修改明细）。
库内不含客户、工单或权限系统。

## 快速开始

```python
from audit_store import AuditStore, Credential

store = AuditStore("main.db", "archive.db")

# 1. 追加：返回序号与覆盖全部历史的完整性凭据
result = store.append("op-20261004-0001", 1759593600, b"...修改明细...")
save_safely(result.credential)          # 调用方在模块外可信保存凭据

# 相同操作号 + 相同记录的重试返回原结果（即使记录已归档）；
# 相同操作号携带不同记录会抛出 ConflictError 且不改变历史。

# 2. 归档：24 个月保留期，截止时刻只会前进
cutoff = 1759593600 - 24 * 31 * 86400
store.archive(cutoff)   # 严格早于 cutoff 的正文迁出；恰好等于的不迁移

# 3. 核验：用事先保存的可信凭据检查完整历史
store.verify(result.credential)         # 被篡改/缺失/删尾时抛 VerificationError

store.close()
```

## 行为保证

- **全局审计链**：序号从 1 开始按成功追加顺序连续分配，跨越两个文件；
  发生时刻允许乱序，不影响序号顺序。每条记录的链式哈希
  `H(i) = SHA256(H(i-1) ‖ seq ‖ op_id ‖ occurred_at ‖ body)`，
  追加返回的 `Credential(seq, chain_hash)` 即截至该序号的完整性凭据。
- **幂等追加**：`op_id` 唯一。归档后主库保留
  `(seq, op_id, occurred_at, body_sha256, chain_hash)` 元数据，
  因此已归档记录的重试仍能返回原凭据；携带不同内容则拒绝。
- **归档**：`archive()` 返回时，主库中 `occurred_at < cutoff` 的正文
  全部位于归档库；截止时刻只前进（传入更小的值时沿用历史最大值）。
  归档不改变既有序号、正文与凭据，归档记录仍受链式哈希保护。
- **核验**：只读，不修改被检查的记录。可检出：正文改动、记录缺失、
  顺序改动、两库同序号内容冲突、整段链尾被删除；后来追加的记录
  不影响旧凭据的有效性。

## 崩溃恢复

- 所有写路径使用显式事务；归档迁移通过 `ATTACH` 把两个库挂在同一
  连接上，每个批次一个跨库事务（回滚日志模式保证其原子性），
  `synchronous=FULL` 保证已返回成功的追加掉电不丢。
- 进程在任意写入阶段被强制终止后，重新打开两个文件即可继续使用，
  无需人工修补：未提交的批次由 SQLite 自动回滚，已提交的不会丢失。
- 归档被打断后，重复执行同一截止时刻的归档会最终完成；归档库中
  已存在同序号但内容冲突的记录会抛 `ArchiveConflictError`，
  中断留下的副本不会掩盖真实冲突。

## 资源占用

归档按 512 条一批迁移，核验按 1024 行一批流式读取，额外峰值内存
与历史正文总量无关。20 万条 × 1 KiB 正文规模下的实测见
`tests/test_memory.py`（上限 32 MiB）。

## 运行测试

```bash
python3 -m pytest tests/ -q          # 全部用例（含约 3 分钟的规模/内存测试）
python3 -m pytest tests/ -q --ignore=tests/test_memory.py   # 跳过规模测试
```

## 文件结构

- `audit_store/store.py` — `AuditStore`：追加、归档、核验
- `audit_store/chain.py` — 链式哈希与时刻编码
- `audit_store/schema.py` — 两个 SQLite 文件的表结构与连接参数
- `audit_store/types.py` / `audit_store/errors.py` — 对外类型与异常
- `tests/` — 追加、归档、核验、崩溃恢复、规模内存五组自动化测试
