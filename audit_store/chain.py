"""全局审计链的哈希计算。

链式哈希：H(0) = 32 字节零值，
H(i) = SHA256(H(i-1) || seq || op_id || occurred_at || body)，
各字段带长度前缀，编码规范且确定，与字段在磁盘上的存储无关。

occurred_at 由调用方提供，允许 int 或 float，二者区分编码；
SQLite 列使用 BLOB 亲和性（ANY），保证写入与读回的类型一致，
从而追加时算出的哈希与核验时重算的哈希逐字节相同。
"""

from __future__ import annotations

import hashlib
import struct

GENESIS = b"\x00" * 32

_INT_TAG = b"\x01"
_FLOAT_TAG = b"\x02"


def encode_occurred_at(value: int | float) -> bytes:
    """把发生时刻编码为确定性的字节串。"""
    if isinstance(value, bool):
        raise TypeError("occurred_at 必须是 int 或 float，不接受 bool")
    if isinstance(value, int):
        return _INT_TAG + struct.pack(">q", value)
    if isinstance(value, float):
        return _FLOAT_TAG + struct.pack(">d", value)
    raise TypeError(f"occurred_at 必须是 int 或 float，收到 {type(value).__name__}")


def body_hash(body: bytes) -> bytes:
    """正文的 SHA-256，用于归档后幂等重试的内容比对。"""
    return hashlib.sha256(body).digest()


def chain_hash(
    prev: bytes,
    seq: int,
    op_id: str,
    occurred_at: int | float,
    body: bytes,
) -> bytes:
    """计算第 seq 条记录的链式哈希。"""
    op = op_id.encode("utf-8")
    ts = encode_occurred_at(occurred_at)
    h = hashlib.sha256()
    h.update(prev)
    h.update(struct.pack(">q", seq))
    h.update(struct.pack(">I", len(op)))
    h.update(op)
    h.update(struct.pack(">I", len(ts)))
    h.update(ts)
    h.update(struct.pack(">Q", len(body)))
    h.update(body)
    return h.digest()


def decode_occurred_at(data: bytes) -> int | float:
    """encode_occurred_at 的逆变换，用于读取持久化的归档截止时刻。"""
    tag, raw = data[:1], data[1:]
    if tag == _INT_TAG:
        return struct.unpack(">q", raw)[0]
    if tag == _FLOAT_TAG:
        return struct.unpack(">d", raw)[0]
    raise ValueError("未知的 occurred_at 编码")
