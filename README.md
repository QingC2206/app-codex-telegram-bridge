# App Codex Telegram Bridge

Control Codex Desktop from Telegram on Windows — notifications, task management, and messaging.

在 Telegram 中接收 Codex Desktop 的任务通知、回复原会话、查看正在运行的任务，并按需查看进度、修改和截图。

当前版本：**[0.3.17](VERSION)** · **Windows** · **MIT License**

## 功能

- **双向消息**：任务完成后发送通知；回复任务卡片即可继续对应的 Desktop 会话，也可以通过 `/send` 选择任务。
- **运行任务列表**：`/running` 实时核验当前环境中的运行任务，显示名称、工作区和等待状态，支持刷新、分页、详情和回复。
- **任务查看与控制**：查看最近任务、进度、Diff、Token 用量；向运行中的任务追加指令，或在具备中止能力时停止当前 Turn。
- **提问与审批**：转发支持的审批、选项和文字输入请求，包括异步提问；复杂认证和不支持的交互会提示回到 Desktop 处理。
- **完整回复**：展示 Markdown、代码和表格；长内容支持分页与下载，本地图片可上传到 Telegram。
- **附件与截图**：向任务发送文字、图片或文件；随包截图插件提供 `/screenshot`。
- **侧边聊天**：通过 `/side` 对任务发起独立的只读提问，保留主任务的执行过程。
- **额度与提醒**：查询额度、接收周额度提醒；账户及接口支持时可使用重置卡相关功能。
- **后台服务**：Windows 登录时启动，进程退出后由任务计划程序定期尝试恢复。

## 运行要求

- Windows 10 / 11，使用 **64 位 Python 3.10+**；建议使用 Python 3.11 或更新版本。
- 已安装、登录并运行 **Codex Desktop**。机器人选择 Desktop 配套的 App Server，不使用 VS Code 扩展或 PATH 中的其他 Codex CLI。
- 可以访问 Telegram Bot API，并拥有一个由自己管理的 Telegram Bot。
- 截图功能需要当前 Windows 用户的可用交互桌面。

Python 部分仅使用标准库，无需运行 `pip install`。本项目依赖 Desktop 的本地接口；Desktop 更新后可能需要相应适配。

## 快速开始

### 1. 创建 Telegram Bot

在 Telegram 中打开官方 [@BotFather](https://t.me/BotFather)，发送 `/newbot` 并按提示创建机器人。保存 Token，然后打开新机器人的私聊并发送 `/start`。

详细操作见 [BotFather 配置指南](BOTFATHER_GUIDE.zh-CN.md)。Token 在安装向导中本机输入，不需要填写到源代码中。

### 2. 获取源码

在 PowerShell 中执行：

```powershell
git clone https://github.com/QingC2206/app-codex-telegram-bridge.git
Set-Location app-codex-telegram-bridge
```

也可以从 GitHub 的 **Code → Download ZIP** 下载源码并解压。如果 [Releases](https://github.com/QingC2206/app-codex-telegram-bridge/releases) 中已有安装包，也可直接下载使用。

### 3. 安装

保持 Codex Desktop 正在运行，在仓库目录双击 `install.cmd`，或者执行：

```powershell
.\install.cmd
```

按向导输入自己的 Bot Token，选择接收消息的聊天，并完成通知设置。安装后完全退出并重新打开 Codex Desktop，以加载新的通知和 Hook 配置；如应用要求审核 Hook，请核对安装路径后确认。

默认安装目录：

```text
%USERPROFILE%\.codex\hooks\codex-telegram-bot
```

安装器会复制发布白名单中的文件，在本机加密保存 Token，备份并更新 `config.toml` 和 `hooks.json`，并配置名为 **Codex Telegram Bot** 的任务计划。已有的其他 `notify` 配置会要求确认后才替换；其他 Hook 会保留。

### 4. 开始使用

在 Telegram 中发送 `/help` 查看菜单，发送 `/running` 查看运行任务。收到任务通知后，使用 Telegram 的“回复”功能输入消息，即可回复对应任务。

## 常用命令

| 命令 | 功能 |
| --- | --- |
| `/running` | 查看当前运行中的任务、工作区和等待状态 |
| `/tasks` | 浏览最近任务，筛选运行中、待处理和最近完成 |
| `/task [ID]` | 查看任务详情和最近输出 |
| `/send [ID] [消息]` | 选择并继续任务 |
| `/steer [ID] [消息]` | 向运行中的任务追加指令 |
| `/stop [ID]` | 中止可由当前 Desktop 或机器人控制的任务 |
| `/watch [ID]` | 打开任务进度卡片 |
| `/diff [ID]` | 查看最近一轮文件修改 |
| `/usage [ID]` | 查看 Token 用量和耗时 |
| `/new <需求>` | 在默认工作目录新建机器人任务 |
| `/cwd [绝对路径]` | 查看或设置新任务的工作目录 |
| `/side [ID] <问题>` | 对任务发起独立的只读侧边提问 |
| `/screenshot` | 通过随包插件获取 Codex 窗口和显示器截图 |
| `/quota` | 查看额度、提醒及账户支持的重置卡操作 |
| `/plugins` | 查看本地插件、命令和加载状态 |
| `/mute`、`/unmute` | 关闭或恢复完成通知 |
| `/subagents [on\|off]` | 设置子代理完成通知，默认关闭 |
| `/logs` | 查看服务日志 |
| `/help` | 查看帮助并清除待回复输入状态 |

多数带任务 ID 的命令可省略 ID，通过按钮选择任务。回复任务卡片时可以附带图片或文件，单附件上限为 20 MB；多附件请分次发送。

## 任务归属与兼容边界

- 默认面向当前 Windows 用户的 `%USERPROFILE%\.codex` 环境，查询和提交时验证 Desktop 实例及本机连接。其他订阅的隔离环境不会自动成为发送目标。
- 已有 Desktop 任务经其当前持有者提交用户消息，保留原任务设置。找不到持有者时，先在 Desktop 中打开该任务后再发送。
- 机器人不会用独立后台恢复并接管外部原任务；`/new` 创建的机器人任务使用它自己的后台。
- 发送中断、缺少确认或结果不明时不会自动重发。先核对任务最新消息，避免手动重复提交。
- `/running` 每次重新核验状态。查询有时间和分页上限；核验不完整时会显示可能遗漏的提示。等待审批或输入但尚未结束的任务也计入运行中。
- 中止能力取决于当前持有者和 Desktop 接口。不可用时需回 Desktop 操作。

## 额度与重置卡

`/quota` 展示账户返回的额度数据。机器人提供周额度的低额度提醒，并在账户及本地接口支持时提供重置卡功能。

预约可选择 6 / 12 / 24 小时；它绑定当前账户、当前额度周期和指定卡，周剩余严格低于 3% 时才尝试使用。立即重置要求周剩余不高于 5%，并需要二次确认。预约会在到期、额度恢复、周期或账户变化、服务重启等情况下失效。结果不明的兑换不会自动重试或换卡。

## 更新与卸载

更新源码后，重新运行相同安装目录的安装器即可保留原有本机凭据和状态。默认安装的用户可以执行：

```powershell
git pull --ff-only
.\install.cmd
```

如果之前使用了自定义安装目录，更新时仍需指定它：

```powershell
.\install.ps1 -InstallRoot '<原安装目录>'
```

更换机器人或重新配置 Token：

```powershell
.\install.ps1 -Reconfigure
```

在已安装的目录运行 `uninstall.cmd` 可停止服务并移除本工具管理的配置。默认会删除本机凭据、状态和缓存；需要保留时执行：

```powershell
.\uninstall.ps1 -KeepData
```

## 数据与隐私

Token 使用 Windows DPAPI 加密，保存在安装目录的 `telegram_notify.credentials.json` 中，由同一 Windows 用户解密。加密文件仍属于凭据，不应公开。

任务回复、主动请求的截图和文件会经 Telegram 传输。截图功能会包含相应显示器上的画面；插件以当前 Windows 用户权限执行，安装前应审核其代码。

日志、聊天关联、状态数据库、附件、回复缓存和配置备份只应留在本机。完整回复缓存默认保留 7 天；下载到本机的附件不会随回复缓存一起自动清理。

仓库的 `.gitignore` 排除了这些文件以及本地验证报告、虚拟环境和构建产物。构建脚本使用文件白名单生成安装包。报告问题时请隐藏 Token、Chat ID、会话内容及个人路径，详见 [安全说明](SECURITY.md)。

## 检查与测试

### 离线检查

以下检查不需要真实 Bot Token，也不代表已验证手机到 Desktop 的整条通信链路：

```powershell
.\run_tests.ps1 -Suite Communication,Interactions,Feedback
```

检查安装包文件与全部离线回归：

```powershell
.\install.ps1 -CheckOnly -TestSuite Full
```

可选测试组包括 `Communication`、`Notifications`、`Quota`、`Interactions`、`Screenshots`、`Plugins`、`SideChat`、`Security`、`Images`、`Feedback`，或 `Full`。

### 已安装环境的只读检查

在安装目录运行：

```powershell
py -3 .\telegram_bot.py --check
```

它检查 Telegram API 和真实 Desktop 本机接口，不向任务发送消息，也不消费 Telegram 更新。

### 构建安装包

构建需要先完成安装和配置，并显式指定一个正在运行、允许接收测试消息的任务：

```powershell
.\build_release.ps1 -ThreadId '<正在运行的任务完整 ID>' -TestSuite Communication,Interactions,Feedback
```

该命令会向指定任务发送一条带标记的自检消息，核对实际用户消息接收记录，再发送 Telegram 验收消息并运行所选离线回归；验收失败即停止打包。输出位于 `dist/`。本地 `.validation/` 和 `*.validation.json` 包含任务关联信息，不应提交到 Git。

## 常见问题

**通知没有到达**：确认机器人私聊已发送 `/start`，检查网络、`/mute` 状态及安装目录中的日志；配置 Hook 后需要重新启动 Codex Desktop。

**找不到任务持有者／任务由另一个应用打开**：在正确的 Desktop 环境中打开目标任务，检查是否存在共用同一环境的重复实例。不要同时用其他后台恢复原任务。

**Desktop 更新后消息提交失败**：先运行只读连接检查并查看错误，安装本项目的兼容更新。结果未知时先检查原任务，确认后再决定是否重发。

**机器人进程退出**：默认安装会创建登录触发及每分钟健康触发的计划任务，可在 Windows 任务计划程序中检查 **Codex Telegram Bot**。未启用自启动的安装需要自行运行服务。

**截图失败**：确认当前会话有可用交互桌面；锁屏、断开的远程桌面或窗口停止绘制会影响捕获。

## 源码与贡献

核心入口是 `telegram_bot.py` 和 `telegram_notify.py`；Desktop 连接位于 `desktop_bridge.py`，运行任务核验位于 `running_tasks.py`。插件文档见 [PLUGIN_API.zh-CN.md](PLUGIN_API.zh-CN.md)，版本记录见 [CHANGELOG.md](CHANGELOG.md)。

提交修改前运行相关测试组。请只提交源码、文档和测试，不提交个人配置、凭据、日志、会话、数据库、截图或验证报告。

## 许可证

本项目采用 [MIT License](LICENSE)，允许使用、修改、分发和商用；分发副本或实质部分时保留版权及许可声明。
