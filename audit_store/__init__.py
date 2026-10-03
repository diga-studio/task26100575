"""维修门店审计归档库。

当前记录与归档记录分别保存在两个本地 SQLite 文件中，
由上层直接调用：追加记录、执行归档、核验历史。
"""

from .errors import (
    ArchiveConflictError,
    AuditStoreError,
    ConflictError,
    VerificationError,
)
from .store import AuditStore
from .types import AppendResult, ArchiveResult, Credential

__all__ = [
    "AuditStore",
    "AppendResult",
    "ArchiveResult",
    "Credential",
    "AuditStoreError",
    "ConflictError",
    "VerificationError",
    "ArchiveConflictError",
]
