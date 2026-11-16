# 7u Android App

当前生产目标只有 Android。客户端提供连续问答、会话与草稿保存、逐条依据、答案复制、后台回答通知和每日短句；不包含日历、配对、账号或服务器。

## 目录与职责

| 位置 | 职责 |
| --- | --- |
| `lib/main.dart` | 组装应用、主题、开屏与应用级控制器 |
| `lib/screens/` | 问答页面；不直接访问数据库或网络 |
| `lib/widgets/` | 共用视觉组件、字标、每日通知详情和设置 |
| `lib/core/conversation_controller.dart` | 会话切换、草稿、发送/重试、并发任务与通知导航 |
| `lib/core/conversations.dart`、`question_history.dart` | 本机会话/答案模型、串行存盘和旧历史兼容 |
| `lib/core/api.dart` | 问答调用入口；连接应用本地存储与问答引擎 |
| `lib/core/question_agent.dart` | 每轮上下文、模型工具循环、引用与人物校验 |
| `lib/core/memory_pack.dart`、`model_client.dart` | 只读检索与模型网络请求；彼此不承担 UI 状态 |
| `lib/core/background_answers.dart`、`daily_memories.dart` | Android 通道契约及无 Android 时的安全实现 |
| `android/` | 缓存 FlutterEngine、前台回答服务、权限和每日通知 |
| `assets/branding/` | 原创 SVG 字标与维护说明 |
| `test/`、`integration_test/` | 离线回归；单独显式开启的真实设备/模型回归 |
| `tool/` | 不依赖私有数据的开发准备工具 |

会话存储与 APK 自带记忆包是两份数据库。升级替换记忆包，不替换用户的会话；失败问题保留供重试。模型看到的角色始终是 `self=她`、`partner=赠送者`，PC 端导出时转换一次。

控制器跟随应用/缓存引擎存活，不跟随页面或 Android Activity 销毁。正常后台请求由前台服务维持；强停、进程回收或系统时限仍可能中断，不会自动重发。每日短句由原生本地闹钟读取随包资产，不启动模型或服务器；闹钟不是精确时刻保证。

## 新克隆：无私有数据也能测试

需要匹配 `pubspec.yaml` 的 Flutter/Dart SDK、仓库锁定的 Python 环境和 Android SDK（Android 构建需要 compileSdk 37、Java 17）。`pubspec.lock` 随仓库保存，日常验证不要顺手升级依赖。

先在仓库根目录准备，再进入 App 目录运行 Flutter：

```powershell
uv sync --locked
uv run python app/tool/prepare_test_assets.py
cd app
flutter pub get
flutter analyze
flutter test
```

准备脚本只生成 15 条明确标记的合成消息、空人物档案和合成短句，并从根目录 `agent/prompt.md` 复制提示词。它不读聊天源库、不读凭据、不联网。如果本地任一记忆资产已经存在，脚本立即拒绝，不覆盖、不替换；正常私有维护电脑无需运行它。

`flutter test` 默认不调用真实模型；测试自行创建临时数据库和 mock 网络/平台通道。声明在 pubspec 的合成资产只用于让干净克隆能够打包测试资源，不把真实聊天纳入 Git。

无 Key 也可以构建仅供开发检查的调试 APK：

```powershell
flutter build apk --debug
```

此包含合成数据，问答没有 Key 会明确报未配置；不是可发给使用者的正式包。合成数据带 `is_synthetic` 标记，不可用于正式发布。正式 APK 必须走根目录维护工具的完整构建流程，提供本机私有资料、已保存模型配置和原有永久签名。

准备脚本自身的安全测试（从仓库根目录运行）：

```powershell
uv run python -m unittest discover -s app/tool -p 'test_*.py'
```

## 维护约定

- 主提示词只编辑根 `agent/prompt.md`；App 中的副本由准备/发布流程同步，不维护第二套文案。
- `assets/memory/`、提示词副本、构建目录、IDE 缓存、Key、签名文件均不提交。保留 pubspec 版本和 Android 包名，内部构建号由发布流程递增。
- 图标只编辑 `assets/branding/7u-mark.svg`；重新生成方式见该目录 README。日常 Flutter 构建不需要图标设计依赖。
- 不在 UI 中加入第二条发送、存盘或检索链路。用户消息发出前先保存 pending，答案保存成功后才发送完成通知。
- 真模型测试只在明确开启 `LIVE_FOLLOWUP_TEST` / `LIVE_QUESTION_TEST` 并由安全启动器提供配置时运行；会消耗 API，诊断输出留在本机忽略目录。
