# 问答主提示词

本目录只保留 `prompt.md` 和本说明。手机问答只有一套实现：`app/lib/core/question_agent.dart`；没有旧 Python Agent、第二套工具分发器或服务器。

## 修改位置

| 要改的内容 | 唯一维护位置 |
| --- | --- |
| 问答语气、任务和注意事项 | `agent/prompt.md` |
| 问题、历史、北京时间、档案日期、工具和校验 | `app/lib/core/question_agent.dart` |
| 检索 SQL、附近语境、引用与计数 | `app/lib/core/memory_pack.dart` |
| 每日短句生成 / 独立审核提示词 | `maintenance/prompts/daily_memories.md` / `daily_memories_review.md` |

主提示词在发布时原样复制到 `app/assets/agent/prompt.md`。该副本是生成文件，不提交、不单独修改。用户对主提示词的改动必须保留；代码整理不顺手改文风。

旧 PC 构建支持已经归入 `maintenance/`：档案与权威映射在 `archive/`，PC 模型通信在 `model_client.py`，共享消息规则在 `core/`。它们不构成第二套手机问答。

## 调用与边界

入口是 `Api.askQuestion(question, history: completedTurns)`，只接收当前会话已完成轮次。模型决定搜什么、搜多少、怎么搜和何时结束；程序限制只读范围、隐私、技术预算并核验引用。

六个工具：`search_chats`、`get_context`、`get_references`、`get_context_coverage`、`get_profile`、`count_chats`。

首问与追问独立获得最多 8 轮检索、32 次工具调用、24 万字符工具内容及 12 次模型调用；历史正文和重新核验的历史依据分别受长度预算约束。完整会话仍保存在手机，不设置用户次数额度。

App 中 `self=她/收礼人`，`partner=我/赠送者`。PC 工作库是维护者视角，导出只交换一次，详见 [人物映射](../docs/architecture.md)。逐字引文和 speaker 标签可严格检查，正文代词和推断仍需评测。技术失败应明确报错并可重试，不能伪装成资料不足的正常答案。

每日短句是独立 PC 构建步骤，不进入手机问答循环；手机通知读取最终池，不每日调用模型。

## 验证

从仓库根目录：

```powershell
uv run python -m unittest discover -s tests
cd app
flutter analyze
flutter test
```

新克隆若尚无 App 资产，先按 [根 README](../README.md) 准备合成资产。普通回归不调用真实模型。

只有明确需要真实连续问答验收时，回到根目录运行：

```powershell
uv run python -m tools.qa.check_live_followup --reverse
```

这会消耗模型费用，并读取本机安全配置。完整输入输出仅保存在被 Git 忽略的 `data/`；不能纳入提示词、测试 fixture 或提交记录。修改提示词后须构建并安装新版 APK 才生效，不能认为已有安装包自动包含当前文件。
