"""异常类型定义。"""


class AuditError(Exception):
    """库抛出的所有异常的基类。"""


class OperationConflictError(AuditError):
    """相同操作号携带了与历史不同的记录。"""


class CutoffRegressionError(AuditError):
    """归档截止时刻小于已使用过的截止时刻。"""


class ContentConflictError(AuditError):
    """归档过程中发现两个库中同序号内容冲突。"""


class VerificationError(AuditError):
    """核验发现历史与可信凭据不一致。"""
