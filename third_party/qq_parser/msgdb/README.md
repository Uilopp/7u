# C2C 消息解析
此处保留上游的 C2C 模型和解析器。来源与修改范围见 [PROVENANCE](../PROVENANCE.md)。

- `c2c/models.py`：消息内容模型及 `to_db_row()`。
- `c2c/parser.py`：从 QQ 的 C2C 行读取消息。
- `proto/`：40800 消息体定义、生成代码和 wire fallback。

7u 使用 `third_party.qq_parser.msgdb` 命名空间，不注入全局搜索路径。
群聊解析与独立导出程序不在本仓库维护；角色、语音状态与隐私策略由 maintenance 层负责。
