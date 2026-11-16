# QQ 消息解析器来源

- 上游：<https://github.com/QQBackup/nt_msg_db_util>
- 本地来源版本：`317024ce2c5e3482fdf9f310a5a7159ab4927e34`
- 原许可证保留在 [LICENSE](LICENSE)。第三方代码的分发须遵守其许可证。

仅保留 C2C 消息模型、解析器、Protobuf 定义及 wire 辅助函数。
删去独立解密/导出命令、群聊解析和导出 schema；协议入口仅导出 C2C。
Python 引用调整为仓库内命名空间，避免修改全局 sys.path 或依赖另一个本机仓库。

业务层不依赖上游 direction 推测人物：按原始 sender_qq 确定身份，按外层 msg_type=6 标识语音。
parser 返回 dataclass，通过 to_db_row() 序列化。导入策略及角色转换属于
maintenance/ingest/ 和 maintenance/archive/，不应添加进此第三方目录。
