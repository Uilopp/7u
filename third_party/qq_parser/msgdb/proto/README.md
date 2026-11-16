# NTQQ 40800 协议
保留 C2C 解析所需的 `c2c_40800.proto`、对应 `*_pb2.py`、`*_parser.py` 及 `wire.py`。
生成的 pb2 代码由 Protobuf 7.34.1 生成，运行时下限在根 pyproject.toml 声明；不要手改生成代码。

保留的上游解析约定：

- 40800 是消息体；段级 45001 不等于消息表外层 40001。
- 45101 按 UTF-8 应用层文本处理。
- raw hash、CDN 二进制值和未知 length-delimited 字段保留为 bytes。

群聊独立 schema 已从本仓库移除。完整来源与修改范围见 [PROVENANCE](../../PROVENANCE.md)。
