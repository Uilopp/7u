<div align="center">
  <img src="app/assets/branding/7u-icon.png" width="96" alt="7u 图标" />
  <h1>7u</h1>
  <p>把两个人的聊天，做成可以追问的私人记忆。</p>
  <p>Flutter Android App · Windows 维护工具 · 本地记忆检索</p>
</div>

## 这是什么

7u 是一款私人问答 App：维护者在 PC 上整理聊天资料，生成只读记忆包并打进 APK；使用者在手机上检索、提问和连续追问。已有会话、草稿、历史答案和每日短句可以离线查看，生成新回答需要联网调用模型服务。

项目适合少量可信收件人的私发场景。它包含完整的 App、资料整理、语音转写、质量检查和 Android 发布流程，不包含账号系统、云同步或长期运行的 7u 服务器。

## 核心卖点

- **有依据的连续问答**：模型可以自主检索文字、上下文、引用关系和人物档案；回答保留消息 ID、说话人和逐字引文，程序会核验依据。
- **本地优先的记忆体验**：记忆包、会话、草稿和历史答案保存在手机本地；覆盖安装新版资料时保留用户会话。
- **从聊天到 APK 的一条链路**：PC 工具支持 QQ 增量导入、语音自动转写、按消息 ID 修正、记忆包净化、每日短句生成和签名构建。
- **为手机使用而设计**：支持失败重试、答案复制、后台回答通知和每日短句提醒；每日提醒读取随包资产，不会每天重新请求模型。
- **隐私边界清楚**：原始聊天、原音、工作库和过程审计留在维护电脑；公开仓库只包含通用代码、合成测试数据和文档。

## 快速开始：先跑起来

### 环境要求

- Windows PC（维护工具）。
- Python `>=3.14` 与 [uv](https://docs.astral.sh/uv/)。
- 与 `app/pubspec.yaml` 匹配的 Flutter/Dart SDK。
- 构建 Android 时需要 Android SDK（compileSdk 37）和 Java 17。

### 1. 安装依赖并准备演示资产

在仓库根目录执行：

```powershell
uv sync --locked
uv run python app/tool/prepare_test_assets.py
```

准备脚本只生成明确标记的合成消息、空人物档案和合成短句，并复制通用提示词。它不读取聊天资料、不读取凭据、不联网；检测到已有记忆资产时会拒绝覆盖。

### 2. 运行 App 检查

```powershell
cd app
flutter pub get
flutter analyze
flutter test
flutter run
```

没有模型 Key 时，界面和离线功能仍可用于开发检查，新问答会明确提示未配置密钥。合成数据只能用于测试和调试，不能用于正式发布。

### 3. 构建调试 APK（可选）

```powershell
flutter build apk --debug
```

调试包不包含真实私人资料，也不作为正式发布包分发。

## 使用真实资料

真实资料维护只在维护者自己的 Windows 电脑上进行：

1. 首次使用时，将 `config/default.json` 复制为 `config/local.json`，填写本机数据库、语音目录和双方映射。真实路径、账号和映射不要写入默认模板。
2. 双击 `tools/start_7u_maintenance.cmd`，在维护窗口中配置模型服务和腾讯语音识别服务，然后运行完整构建流程。
3. 按 [Android 发布手册](docs/android-release.md) 准备永久签名、检查版本和验收结果。成功后只分发 `releases/` 中本次生成的 APK，使用相同包名和签名覆盖安装。

命令行入口与窗口使用同一条完整构建链：

```powershell
uv run python -m maintenance.build --version-name 0.1.YYMMDD
```

DeepSeek API Key、腾讯语音识别 `SecretId` / `SecretKey` 只在维护窗口输入，由本机加密配置保存；不要把真实值写入源码、README、命令参数、Issue 或提交记录。模型 Key 会编入正式 APK，理论上可能被逆向提取，Git 忽略无法保护已经分发的 APK。

## 项目结构

```text
app/                    Flutter App、Android 原生能力与离线测试
  lib/core/             会话、检索、模型调用和问答编排
  lib/screens/          问答页面
  lib/widgets/          视觉组件、开屏和通知界面
  android/              后台服务、通知、权限和签名配置
  tool/                 合成资产准备工具
agent/                  通用问答主提示词
maintenance/            PC 维护窗口与完整构建流程
  ingest/               QQ 快照、增量导入和引用解析
  voice/                语音转写、播放和按 ID 修正
  archive/              人物档案与净化记忆包
  core/                 隐私、消息、引用和进程锁
config/default.json     可公开的配置模板
third_party/qq_parser/  隔离的 QQ 解析器及许可证
tests/                  Python 离线回归
docs/                   架构、维护、通知和发布手册
tools/                  开发检查、发布审计和启动入口
```

## 常用检查

在仓库根目录执行：

```powershell
uv run ruff check maintenance tools tests
uv run ruff format --check maintenance tools tests
uv run python -m unittest discover -s tests
uv run python -m unittest discover -s app/tool -p "test_*.py"
uv run python -m tools.check_repository
```

Flutter 检查见上文。真实模型、QQ 提取和腾讯语音服务不会在普通离线测试中自动调用；显式联网测试会消耗服务额度，详情见 [App 说明](app/README.md)。

## 发布与隐私清单

以下内容始终留在本机，已由 `.gitignore` 排除：

- `data/`：原始聊天、工作库、备份、模型缓存、语音和过程审计。
- `releases/`：历史及本次 APK。
- `config/local.json`、真实 App 记忆资产和生成的提示词副本。
- DeepSeek／腾讯语音凭据、DPAPI 文件、Android 签名和本机 SDK 配置。

公开前请再运行 `uv run python -m tools.check_repository`，并人工检查候选文件。扫描规则不能替代隐私审查，尤其不要提交姓名、联系方式、具体事件、聊天原文或真实生成的每日短句。第三方解析器的来源和许可证见 `third_party/qq_parser/`。

## 文档导航

- [App 使用与开发](app/README.md)
- [架构与数据流](docs/architecture.md)
- [PC 维护手册](docs/maintenance.md)
- [Android 发布手册](docs/android-release.md)
- [后台与通知说明](docs/background-notifications.md)
- [问答提示词维护](agent/README.md)
