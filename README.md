# App Codex Telegram Bridge

**简体中文** | [English](README.en.md)

通过 Telegram 管理 Codex Desktop：接收通知、管理任务、双向发送消息。

在 Telegram 接收 Codex Desktop 回复、继续原任务、回答提问、查看运行任务和截图。**0.6.0 · Python 3.10+ · Windows / macOS / Linux · MIT**。

文档和 Telegram 界面均支持中英文，默认中文。发送 `/settings` → **语言** 通过按钮选择，或用 `/language en` / `/language zh` 切换；重启和升级后保留。任务正文、模型答复与用户输入不自动翻译。

[安装指南](docs/setup.zh-CN.md) · [使用指南](docs/usage.zh-CN.md) · [插件 API](docs/plugins.zh-CN.md) · [文档目录](docs/README.md)

本项目只连接 **Codex Desktop** 及其配套后端，禁止回退到 VS Code 扩展或 PATH 中的独立 Codex CLI。原 Desktop 任务始终经当前持有者接收用户消息，不由机器人另起后端接管。

## 平台与验收范围

| 平台 | 实现 | 实机验收 |
| --- | --- | --- |
| Windows | 命名管道、DPAPI、任务计划、原生窗口/屏幕截图 | 已实测通知、手机直接回复、`/send`、提问卡片回答及用户来源回执 |
| macOS | Unix socket、Keychain、LaunchAgent、窗口/屏幕截图 | 自动测试覆盖；真实 Desktop/Telegram 闭环待 Mac 用户验收 |
| Linux | Unix socket、Secret Service、systemd 用户服务、X11/Wayland 截图 | 自动测试覆盖；真实 Desktop/Telegram 闭环待 Linux 桌面验收 |

跨平台 CI 在三个系统上分别运行 Python 3.10、3.11、3.13，覆盖业务回归、安装配置及各系统原生进程/连接测试。**CI 通过不代表真实 Desktop 或手机通信已经通过。** macOS/Linux 在完成对应实机验收前按实验性支持发布。

Desktop 的本地接口可能随版本变化。连接验证失败时停止提交，不改用其他客户端，也不关闭系统的进程/截图权限检查。详情见 [架构与兼容边界](docs/architecture.zh-CN.md) 和 [验收步骤](docs/testing.zh-CN.md)。

## 功能

- **通知和双向消息**：完成通知、完整回复、Markdown/表格、分页和下载；回复任务卡片或使用 `/send` 继续原任务。
- **任务管理**：`/running` 实时核验运行任务；列表、详情、进度、Diff、Token 用量、追加指令及能力允许时中止 Turn。
- **提问与审批**：支持选项、文字、异步提问和兼容的审批；过期或已回答按钮失效，结果未知的提交不自动重试。
- **附件和插件**：上传图片/文件、显示本地图片，随包 `/screenshot` 插件与自定义 Python 插件。
- **侧边聊天与额度**：只读侧边提问、额度查询、低额度提醒，以及账户和接口支持的重置卡操作。
- **后台服务**：随当前用户桌面会话启动；Windows 任务计划、macOS LaunchAgent、Linux systemd 用户服务。

## 安装

需要已登录并运行的 Codex Desktop、64 位 Python 3.10+ 和可访问的 Telegram Bot API。推荐 Python 3.11 或更新版本。

1. 在 Telegram 的官方 [@BotFather](https://t.me/BotFather) 创建机器人，并对新机器人发送 `/start`。详见 [配置指南](docs/setup.zh-CN.md)。
2. 克隆项目：

```text
git clone https://github.com/QingC2206/app-codex-telegram-bridge.git
cd app-codex-telegram-bridge
```

3. Windows 执行：

```powershell
py -3 install.py
```

macOS / Linux 执行：

```sh
python3 install.py
```

安装器会在当前 Codex 环境下创建独立 Python 虚拟环境 `telegram-bridge-runtime/`，安装依赖，配置 Telegram 凭据、通知 Hook 和用户服务。无需修改全局 Python，也不需要 PowerShell 安装脚本。安装失败会返回错误；不要用管理员/root 用户重跑。

4. 完全退出并重新打开 Codex Desktop，按应用要求审核新增 Hook。在 Telegram 发送 `/help` 或 `/running`。

需要英文配置向导和初始界面时，在安装命令后加 `--language en`；不传参数时首次安装为中文，升级保留现有语言。

首次安装需要从 PyPI 下载依赖。macOS 应使用可运行 `venv` 的 Python；Linux 需安装发行版的 Python venv 支持、Secret Service 密钥环、systemd 用户会话。无桌面会话的服务器不属于本项目 Desktop 模式的运行目标。

### 平台权限

- **Windows**：凭据继续使用当前用户的 DPAPI，后台服务不要求管理员权限。
- **macOS**：首次访问 Keychain 可能要求授权。截图需要为实际运行机器人的 Python 程序允许“屏幕录制”；未授权时返回明确错误。
- **Linux**：登录后应有解锁的 Secret Service 密钥环；缺失或锁定时不降级为明文存储。X11 的窗口截图是可见区域，可能包含遮挡。Wayland 使用桌面截图 Portal，需在电脑上确认，无法承诺无人值守的逐窗口截图。

### 隔离环境和自定义目录

`CODEX_HOME` 或 `--codex-home` 指定要连接的 Desktop 环境；机器人仍会核验实际运行实例的环境归属。

```text
python install.py --codex-home <Codex环境绝对路径> --data-dir <机器人数据绝对路径>
```

`--no-service` 只配置 Hook，供手动启动；已有其他通知命令时，安装器会停止，确认确实需要替换后才使用 `--replace-notify`。

## 更新、管理与卸载

```text
git pull --ff-only
```

然后重新运行本平台的 `install.py`。检测到唯一的旧版凭据目录时，会继续使用该目录里的凭据、状态、回复关联和附件；有多个旧目录时要求明确指定 `--data-dir`。不要把 Windows DPAPI 凭据复制到其他系统；在新电脑运行配置向导。

多台电脑同时测试时，请各用一个 Telegram Bot；同一个 Bot Token 不能交给多个正在运行的长轮询服务。这样也能避免把 Mac 测试消息交给 Windows 任务。

管理命令使用安装器创建的 Python。macOS/Linux 示例：

```sh
~/.codex/telegram-bridge-runtime/bin/python -m codex_telegram_bridge doctor
~/.codex/telegram-bridge-runtime/bin/python -m codex_telegram_bridge service status
~/.codex/telegram-bridge-runtime/bin/python -m codex_telegram_bridge run
~/.codex/telegram-bridge-runtime/bin/python -m codex_telegram_bridge uninstall
```

Windows 示例：

```powershell
& "$env:USERPROFILE\.codex\telegram-bridge-runtime\Scripts\python.exe" -m codex_telegram_bridge doctor
```

其他子命令：`setup`、`service install/start/stop/status/uninstall`、`doctor --offline`。也可在自己管理的虚拟环境中 `python -m pip install .`，再使用 `codex-telegram-bridge` 命令。

自定义环境的管理命令仍需传入 `--codex-home` / `--data-dir`，放在子命令之前。**卸载仅移除本工具管理的服务和 Hook，保留凭据、历史、插件和备份。** 已创建的独立 Python 虚拟环境也保留，可在确认路径后手动删除。

## 常用命令

| 命令 | 功能 |
| --- | --- |
| `/quota` | 查看剩余额度、提醒及重置卡 |
| `/tasks` | 浏览最近任务 |
| `/running` | 查看运行任务 |
| `/task [ID]` | 查看任务详情 |
| `/watch [ID]` | 跟踪任务进度 |
| `/diff [ID]` | 查看文件修改 |
| `/usage [ID]` | 查看任务用量 |
| `/help` | 查看使用帮助 |
| `/settings` | 调整静默模式、通知、语言和新任务目录 |
| `/logs` | 查看服务日志 |
| `/side [ID] <问题>` | 发起侧边提问 |
| `/send [ID] [消息]` | 发送任务消息 |
| `/steer [ID] [消息]` | 追加运行指令 |
| `/new <需求>` | 创建新任务 |
| `/stop [ID]` | 中止运行任务 |
| `/plugins` | [插件] 查看插件状态 |
| `/screenshot` | [插件] 截取窗口与屏幕（内置插件） |

菜单按“常用查询 → 帮助与设置 → 日志 → 发送和运行操作 → 插件”排列。额度查询固定第一项；插件入口及插件命令固定在底部，说明带 `[插件]` 标记；Telegram 最多显示 100 条命令，超出的插件命令仍可直接输入。

`/settings` 集中管理完成通知（原静音）、子代理通知、界面语言和新任务目录。打开设置不改变配置；开启／关闭按钮设置明确状态，重复点击不会反向切换。工作目录可用 `/settings cwd 绝对路径` 修改，支持路径中的空格，仅影响之后新建的任务。

`/language [zh|en]`、`/mute`、`/unmute`、`/subagents [on|off]`、`/cwd [绝对路径]` 及旧消息按钮继续可用，但不再单独占用菜单。`/help` 只显示帮助；取消待输入请使用原提示中的取消按钮。

多数带任务 ID 的命令可省略 ID，通过按钮选择任务。回复任务卡片时可以附带图片或文件，单附件上限为 20 MB；多附件请分次发送。

## 静默模式

入口：`/settings` → **🌙 静默模式** → **开启静默**。默认关闭，选择会在重启和升级后保留。

- 保留额度、任务列表、运行任务、任务详情、Diff、用量、日志、插件状态及设置查询；主动查看历史回复仍可使用。
- 禁用普通回复、附件、`/send`、`/steer`、`/new`、`/side`、`/stop`、`/watch`、问题/审批作答、插件执行和额度重置。旧按钮同样受限制。
- 暂停 Codex 回复、提问、审批、进度以及其他自动推送；已有任务继续运行，不自动中止或代为批准。开启时取消进度跟踪和未执行的额度预约。
- 关闭静默后不会补发期间被抑制的回复，也不会恢复已取消的跟踪或预约。开启前已提交的请求无法撤回。

静默模式与“关闭完成通知”相互独立：前者限制为查询和设置，后者只调整通知开关。切换静默不会覆盖原通知偏好；它也不同于 Telegram 的免打扰/无声通知。

## 任务归属与投递规则

- 验证 Desktop 程序来源、用户、环境和本地连接的服务端进程；环境重复、连接身份不明时拒绝提交。
- 外部任务只经 Desktop 当前持有者继续；找不到持有者时，先在正确 Desktop 中打开该任务。
- `/new` 创建的机器人任务使用 Desktop 配套的独立 App Server；不会自动恢复并接管原 Desktop 任务。
- Telegram 人类消息使用用户输入通道，保留用户来源；不使用代理间委派工具伪装用户输入。
- `suggestion` JSON 本身不会触发过滤；结合记录类型和明确内部来源识别预测建议。
- 发送结果不明时不自动换路或重发。先核对任务最新消息，避免手动重复执行。
- `/running` 使用当前状态核验；超时、分页上限或部分记录无法核验时明确提示。

## 额度与重置卡

`/quota` 展示账户额度，保留周额度提醒与重置卡机制。预约可选 6 / 12 / 24 小时，绑定账户、周期和指定卡，周剩余严格低于 3% 时才尝试使用。立即重置要求周剩余不高于 5%，并需要二次确认。到期、额度恢复、周期或账户变化、服务重启会使预约失效。结果未知的兑换不会自动重试或换卡。

## 数据与隐私

新安装默认把运行数据放在所选 `CODEX_HOME/telegram-bridge/`；旧安装继续使用原数据目录。Python 包目录中不保存凭据、日志或聊天缓存。

Windows 配置保存 DPAPI 加密值；macOS/Linux 保存系统钥匙串引用，Token 保留在系统凭据存储。配置文件仍包含聊天信息，不应公开。任务回复、主动请求的截图和附件会发送到 Telegram；插件具有当前用户权限。

语言偏好单独存放在数据目录的 `telegram_language.json`。它属于当前机器人配置，授权群聊成员共享同一语言；旧消息不批量重写，刷新卡片后使用新语言。第三方插件可通过 `ctx.language` / `ctx.tr()` 及 `command_descriptions` 提供双语，未提供时保留原文。

完整回复缓存默认保留 7 天；下载附件不会随回复缓存自动清理。日志、数据库、截图和验收报告均排除在 Git 和发布包之外。详见 [安全说明](SECURITY.md)。

## 开发与测试

```text
python -m pip install -e ".[dev]"
python tools/run_tests.py --suite Full
python -m build
python tools/check_distribution.py
```

测试使用临时环境和合成凭据，不连接真实 Telegram 或向 Desktop 任务提交消息。可选分组：`Communication`、`Notifications`、`Quota`、`Interactions`、`Screenshots`、`Plugins`、`SideChat`、`Security`、`Images`、`Feedback`、`Platforms`。

`python -m build` 生成开发用 wheel/sdist，不宣称通过真实通信验收。发布安装 ZIP 使用独立门禁，向明确指定的活动任务提交一次测试输入，核对对应用户消息回执并验证 Telegram 发送：

```text
python tools/build_release.py --thread-id <允许验收的活动任务完整ID>
```

凭据有歧义时传入 `--credentials`，自定义环境传入 `--codex-home`。输出位于 `dist/`；`.validation/` 和 `*.validation.json` 含本机任务关联，不得公开。详见 [测试与实机验收](docs/testing.zh-CN.md)。

## 项目结构

```text
README.md / README.en.md    中文与英文入口
src/codex_telegram_bridge/
  app.py / cli.py           服务编排与统一入口
  config.py / paths.py      凭据配置与数据路径
  telegram/                消息收发、格式和通知
  codex/                   Desktop 协议、历史和回执
  features/                运行任务、提问、额度、侧边聊天
  storage/                 持久状态
  platforms/               Windows / macOS / Linux 与 POSIX 共用适配
  management/              安装、服务和诊断
  plugins/                 插件宿主和接口
  resources/               随包插件及清单（wheel 内同样携带）
tests/                     离线回归与平台原生测试
tools/                     Python 测试、验收和发布工具
docs/                      安装、架构、插件与验收文档
```

插件开发见 [插件 API](docs/plugins.zh-CN.md)，版本变化见 [CHANGELOG](CHANGELOG.md)。本项目采用 [MIT License](LICENSE)。
