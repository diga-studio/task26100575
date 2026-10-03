"""对外暴露的数据类型。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Credential:
    """完整性凭据：覆盖截至 seq 的全部历史的链式哈希。

    调用方在模块外可信保存，用于后续核验。
    """

    seq: int
    chain_hash: bytes


@dataclass(frozen=True)
class AppendResult:
    """追加成功（或幂等重试）的返回结果。"""

    seq: int
    credential: Credential


@dataclass(frozen=True)
class ArchiveResult:
    """一次归档执行的统计结果。"""

    cutoff: float
    migrated: int
    total_archived: int
