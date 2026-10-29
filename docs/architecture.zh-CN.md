# 架构与兼容边界

**简体中文** | [English](architecture.en.md) · [返回 README](../README.md)

主程序、安装、卸载、测试、发布使用同一 Python 包。`platforms/` 内封装系统能力，业务代码复用消息类型、投递日志、任务归属和权限判断。

## 消息流

```text
Telegram 更新 → 授权聊天/发送者校验 → 命令或回复路由
                                      ↓
                          Desktop 身份与环境核验
                                      ↓
                       当前持有者的用户输入通道
                                      ↓
                         投递记录与用户消息回执
                                      ↓
                        Hook / 任务事件 → Telegram
```

默认 Desktop 后端在 Windows 上继续使用 App Tools 查询，并在发送前缺少持有者时打开原任务界面。macOS 普通文字续聊使用独立访问策略，见下文。Telegram 人类输入始终使用 follower IPC 的 start/steer 机制，不能替换为 `send_message_to_thread` 代理委派。`/new` 等明确创建的机器人任务由机器人自己的 Desktop 配套后端管理，不对这类任务自动导航恢复。

外部任务在 owner discovery 明确返回无接收者、尚未写入用户消息时，允许一次界面恢复：固定原 Desktop 进程和原生连接，验证同环境任务 ID 后调用 `navigate_to_codex_page`，再轮询持有者。打开界面会切换 Desktop 当前显示的任务，不通过独立后端 `thread/resume` 接管。导航和后续探测共用最多 10 秒的恢复预算；导航成功本身不算通道恢复。发送前再次检查实例、任务和指定 Turn；恢复失败、跨实例或来源歧义时停止。提交后的异常或结果未知不进入恢复流程，旧失败消息不会自动补发。查询、能力探测和静默模式均不触发导航。

默认 Desktop 后端的完成通知由统一的来源分类器处理：按完整任务 ID 核对当前环境的任务记录、Desktop 创建来源和本轮触发方式。用户对话正常投递，明确内部用途过滤，未知来源保持待核验；不按正文 JSON 或输入模板推断。待核验队列只保存标识与校验摘要，从原回合恢复答复并去重，静默期间不补发。字段含义、钩子选择、诊断及兼容边界见[通知来源与投递](notifications.zh-CN.md)。异步提问仍绑定 task、turn、item 和问题索引，提交先记账，结果不明不重发。

Desktop 用户消息请求的默认等待为 **10 秒**。若返回超时或提交结果未知，Robot 再进行最多 **10 秒**的本地回执核对：只认可本次提交后、同一任务内内容匹配的用户消息及明确的消息/回合 ID；追加到指定回合时还要求回合一致。核对成功按已接收处理，否则提示“提交结果未知”，保留待核对记录且不自动重发。Desktop 自身的内部超时可能更短，此设置不会修改 Desktop 程序。

### 后台运行与窗口持有者

`remote_ios` 可以启动本机后台 Turn，而窗口尚未登记 owner。状态查询使用同环境 App Tools 的 `read_thread`；本地 `notLoaded`、`interrupted` 和未闭合的 rollout 只作为历史或候选信息。运行列表与 stop/steer 选择器共享实时核验，侧栏未加载但存在活动记录的任务也需复核。

中止能力探测只查询 owner、原生工具目录和当前任务，不导航。用户明确中止后，固定实例及当前 Turn；仅在未发送中止帧且无 owner 时恢复窗口一次。恢复后、发帧前再次读取原 Turn，使用 interrupt v4 的 `expectedTurnId`，验证实际中止回执。“中断后发送”还需确认 Desktop 已空闲；任何未知结果阻止后续发送。异步回答在恢复后仍无 owner 时回到待回答状态，不能标记已提交。

Windows 的全局 `codex-ipc` 代理可能被另一个 Cockpit 实例持有。当前仍要求代理 PID 与目标 Desktop 一致；不会为支持远程 iOS 而跳过该校验。多账号任意启动顺序的完整适配是独立边界。

## 系统边界

| 系统能力 | Windows | macOS | Linux |
| --- | --- | --- | --- |
| Desktop 身份 | Store 程序路径、进程、环境 | App Bundle 标识与 Electron 元数据 | Electron 包元数据及当前用户 |
| 本地通信 | 命名管道；服务端 PID 校验 | Unix socket；文件属主、权限、peer PID/UID | Unix socket；文件属主、权限、SO_PEERCRED |
| Token | 现有 DPAPI 格式 | Keychain | Secret Service |
| 后台启动 | 当前用户任务计划 | Aqua 会话 LaunchAgent | systemd 用户服务 |
| 进程收尾 | Windows Job Object | 独立进程组、父进程监护 | 独立进程组、父进程监护及服务 cgroup |
| 截图 | 原生窗口及显示器 | 屏幕录制授权、窗口及显示器 | X11 可见区域；Wayland Portal 确认 |

只选择能证明属于 Desktop 的后端，拒绝 VS Code、PATH CLI 和不匹配的 `CODEX_CLI_PATH`。同环境多实例、跨环境同任务 ID、无法读取实例环境、端点身份变化均停止提交。

macOS 支持安装包内 `Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex` 的嵌套后端布局，并优先使用已验证 Desktop 的配套运行进程。

Unix follower 端点按所选环境的 `ipc/ipc.sock` 发现，兼容旧版当前用户临时目录端点。App Tools 使用 Desktop/配套子进程公布的 `codex-app-tools/<id>.sock` 或兼容 browser-use 地址。协议是四字节小端长度加 JSON。传输、查询和业务路由分别封装；不是把所有 Unix socket 都当成同一种协议。

这些 Desktop 本地接口属于版本相关实现，不宣称是稳定公开 API。macOS 的本地 peer 授权、系统录屏权限或密钥环可能拒绝访问；程序报告失败并停止，不关闭权限检查。`doctor` 失败时附脱敏错误及 Desktop 版本，不能仅凭 CI 结果判定兼容。

## 配置与资源

- `paths.py` 统一处理 `CODEX_HOME` 和机器人数据目录，避免依赖 Python 文件所在位置。
- 新数据目录默认 `<CODEX_HOME>/telegram-bridge/`，唯一旧凭据目录可原位复用，多个候选时必须指定。
- 源码和 wheel 使用同一份 `resources/bundled_plugins/`，没有两个需要同步的插件副本。
- 插件工作进程保留 v1 的 `plugin_api` 导入；它是可信本地代码扩展，不是权限沙箱。
- 系统服务绑定绝对 Python 路径、Codex 环境和数据目录；本机运行数据不会进入 wheel、源码 ZIP 或 Git。
- `i18n.py` 只通过 `resources/messages.en.json` 翻译显式标记的界面模板，参数原样插入，不扫描或翻译整条待发送正文。
- 语言默认 `zh-CN`，单独保存在机器人数据目录。通知进程读取同一偏好，插件工作进程接收明确语言；按钮路由和用户输入不依赖翻译后的标签。

## 平台与后端分层

平台和执行后端是两个独立维度，不按操作系统复制整套机器人。

| 层 | 职责与入口 |
| --- | --- |
| `core/bot.py`、`features/` | 两端唯一的命令、提问、表情、审批、侧聊、额度和任务编排 |
| `backends/desktop/` | 默认入口，转发到公共控制器，保留 Windows 配置行为 |
| `backends/app_server/` | 显式运行时配置、旧数据迁移和回调转换，不复制业务规则 |
| `platforms/windows/` | 进程发现、Named Pipe、DPAPI、Job Object、隐藏窗口、任务计划、截图、原生 Desktop 访问策略 |
| `platforms/macos/` | Keychain、LaunchAgent、截图权限、原 Desktop 文字续聊的本机归属校验和打开原对话 |
| `platforms/posix/`、`platforms/linux/` | Unix socket、进程组等共用能力，以及 Linux 专属系统能力 |
| `codex/`、`telegram/`、`storage/` | 共用协议与回执、富文本/卡片/格式化、投递与持久状态 |
| `management/` | 共用配置、安装事务、诊断；服务命令委派给平台适配器 |

旧 `app.py`、`app_server_*`、`codex/app_server*`、`telegram/app_server_io.py` 导入路径保留为兼容别名。业务类已移入 `core/bot.py`，两端使用同一组方法；所有 JSONL 连接也复用 `CodexAppServer`，旧客户端只是封装。额度、耗时、差异格式化及通知渲染共用。

两个安装器都先核验服务归属，再备份 Hook/配置/安装记录；安装或启动失败时恢复文件与旧服务。Windows 和 Linux 安装后显式启动，macOS 由 bootstrap 启动一次。两个执行后端统一使用 Windows `CREATE_NO_WINDOW` 与原 Job Object 策略，POSIX 保持独立进程组。`service`、`setup`、按安装记录卸载不依赖有效的 `runtime.json`，损坏配置仍可恢复服务；`run` 对无效配置继续拒绝启动。

macOS 的 `MacOSDesktopAccess` 核验当前 profile、未归档会话、rollout 来源及持有者。缺少持有者时通过已验证 App Bundle 打开原线程；未知提交不重试。实时读取使用只读 follower 订阅，核验发布者、任务、主机、协议版本、快照修订和最新历史边界。严格 Turn 操作共享此读取，未知状态拒绝操作。没有侧栏接口时逐一读取元数据候选，超时明确提示不完整。Windows 保留原 App Tools 校验和导航。

安装和迁移见 [app-server 模式](app-server.md)。能力与逻辑的唯一清单是 [parity.json](parity.json)。旧 macOS 队列、命令、UI 和审批实现已退役；SQLite 保留旧标识、草稿和投递日志，不重放任务。Stop、SubagentStop 和提问 Hook 共享分类、去重和交互规则。

公共增强同时接受原 Windows 契约与当前入口回归：快速完成回包竞态、失败表情、未知提交、过期审批、UTF-16 分页和延迟投递关联。Windows 实机按约另验。仅具体第三方插件扩展例外；通用提问、表单、侧聊、额度和内置截图都在范围内。代码复检不能替代 Telegram 实际界面验收。

## 账户额度与侧聊边界

额度观测、提醒去重、重置卡历史及兑换防重复记录由公共账户状态层管理，两种后端使用同一规则。账户切换作废旧按钮、选择、确认和预约；切回账户可以读取自己的历史，但不恢复旧授权。无法确认归属的额度事件仅触发重新读取，不直接用于跨账户比较。排队通知和延迟回执也必须匹配账户及当前授权代际。

旧版没有账户标识的观测记录保留为历史，新账户建立自己的基线。旧版未确认或尚需防重复保护的兑换记录保留为归属待核对记录，不能通过清空缓存、重启或另一账户的额度恢复来解除。身份识别以实际连接的 App Server 返回信息为准；接口未提供可验证的组织或工作区标识时，不宣称能区分同一登录身份下的不同组织额度。

升级时，旧队列里既没有账户标记、也没有明确任务或交互关联的后台通知会保留为待核对，不能靠正文猜测归属后自动发送。其中可能包含普通后台提示；已明确关联的任务消息及结果不确定的发送记录保持原有处理规则。该保守迁移只执行一次，新通知正常携带其关联信息。

侧聊在专用 App Server 进程中禁用 Codex 内置多代理工具，并保持只读及拒绝审批策略。新进程和每次续聊都核验实际约束；重连后旧侧会话失效，重新创建时再次核验。无法验证所需配置能力时拒绝本次侧聊，不把问题转给主任务。主任务和用户全局配置不受影响。此限制不等于对任意插件、MCP 或外部委派服务的完整隔离保证。

包内 `__version__` 是唯一人工维护的版本号。安装包元数据、发布归档名称和归档内兼容 `VERSION` 文件从它派生。诊断中的 `bridge_version` 与 `python_version` 分别表示机器人及解释器版本；旧字段保留其既有含义，以兼容已有调用方。
