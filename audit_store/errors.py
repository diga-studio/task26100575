"""审计归档库的异常类型定义。"""


class AuditStoreError(Exception):
    """库内所有异常的基类。"""


class ConflictError(AuditStoreError):
    """相同操作号携带不同记录，拒绝追加。"""


class VerificationError(AuditStoreError):
    """核验发现历史被篡改、缺失或与可信凭据不符。"""


class ArchiveConflictError(AuditStoreError):
    """归档过程中发现归档库已存在同序号但内容冲突的记录。"""
