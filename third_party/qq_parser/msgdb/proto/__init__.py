"""NTQQ 消息 BLOB 的按字段编号协议定义入口。"""

from . import c2c_40800_pb2 as msg_40800
from .c2c_40800_parser import parse_40800

__all__ = [
    "msg_40800",
    "parse_40800",
]
