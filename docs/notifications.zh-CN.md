# 通知来源与投递

**简体中文** | [English](notifications.en.md) · [文档目录](README.md)

## Desktop 建议来源

消息预览开启时，默认来源 `desktop-hook` 在通知过滤前收集 `notify` 或 `Stop` 中的输入框建议。明确的 `composer_predictions` 来源可以识别这类回调。旧格式需匹配 Desktop 的生成请求，且在本环境可读的任务记录中查无该临时任务。正常对话里用户要求的 `{"suggestion":"…"}` 仍按普通回复投递。

两次回调可按生成任务的完整 ID 和生成轮次汇合。原任务和原轮次取自回调的结构化来源字段，例如 `responsesapiClientMetadata.prediction_source_thread_id` 与 `prediction_source_turn_id`。正文或来源均可先到。Robot 在确认钩子由对应 Desktop 进程派生、候选唯一且同一进程明确放行该原轮次后，才会挂到原完成卡上。归属不明的建议不会发成独立消息。Desktop 只在 `thread/sample` 的返回值里提供原文时，这两个 Hook 无法取得它。

如果回调给出了原轮次但缺少原任务 ID，Robot 还可以从当前 profile 的 `thread_spawn_edges` 核对生成任务的父任务，并在使用时复核。只有父任务、没有原轮次的记录会继续等待。

Hook 候选暂存在 Robot 数据目录的 `telegram_hook_suggestions.sqlite3`，最多 1024 条生成记录。访问时和后台服务运行期间会清理超过 47 小时的记录。Robot 通过当前进程和 profile 中 `auth.json` 的文件元数据核对登录状态。无法核实时保留普通卡。Desktop 重启、登录文件变化或静默模式切换会使旧关联失效。普通通知日志不记录候选原文。macOS 的 Desktop 放行记录取自应用日志目录。[Codex 日志位置](https://learn.chatgpt.com/docs/reference/troubleshooting)

内置 Windows 来源复用了 PR #3 的取值方法。专用快捷方式让选定的 Microsoft Store Desktop 通过本地中转程序启动自己的配套后端。中转程序按原样转发标准输入输出，将 `thread/sample` 的 `composer_predictions` 请求与同一请求 ID 的响应配对。它核对来源任务、轮次、候选标识和该 Desktop 进程的放行记录。只有同一轮能对应到唯一建议时，Robot 才能读取原文。[启用方式](setup.zh-CN.md#一键发送-desktop-建议) · [PR #3](https://github.com/QingC2206/app-codex-telegram-bridge/pull/3)

候选原文暂存在中转进程内存中。Robot 通过核对进程身份的本地命名管道读取结果，连接凭据由 Windows DPAPI 保护。中转程序按账号变更事件和登录文件的元数据使旧按钮失效。Desktop 重启后，旧会话的建议也随之失效。缺少来源、证据冲突或 Desktop 版本改变协议时，仍然发送普通完成通知。

可信本地 Python 包可以在 `codex_telegram_bridge.desktop_suggestions` 入口组注册读取函数。在对应 profile 的通知配置中将 `desktop_suggestion_source` 设为入口名称，随后重启 Robot。函数接收已核实的 `DesktopEndpoint`、完整任务 ID 和完成轮次 ID，只读取当前登录账号已有的结果。适配器有责任核实候选已经通过 Desktop 的最终检查。

| 返回字段 | 用途 |
| --- | --- |
| `hostId`、`threadId`、`sourceTurnId` | 精确匹配本机 Desktop、原任务与原轮次。 |
| `accountId`、`userId` | 当前 Desktop 账号和用户。建议尚在生成时，返回这些关联字段并将正文设为空值。 |
| `principalEpoch` | 两个内置来源使用当前 Desktop 会话标识。其他适配器使用上面的账号字段。 |
| `responseId`、`text` | 最终候选标识和完整原文。无法确认账号或任务归属时，函数返回 `None`。 |

Robot 在原卡发出时记录 Desktop 实例及账号关联，在删除原卡前、删除后和提交用户输入前重新核验。profile 本地的 SQLite 日志保存排版后的通知及标识摘要，在功能运行时清理超过 47 小时的记录。新卡沿用回复引用、话题和通知设置。新卡或用户输入的回执不明时会停止重试。用户点击已送达的新卡时可以补认消息 ID。Telegram 已删除消息上的表情回应无法迁移。[Telegram 删除规则](https://core.telegram.org/bots/api#deletemessages)

## 回调位置与边界

安装器把完成通知接在用户配置的 `notify`，把提问提示接在 `PreToolUse`。`notify` 是官方支持的外部通知入口，但 `agent-turn-complete` 只代表一个回合结束，不能保证这是用户需要阅读的答复；官方列出的常见字段不包含用途分类。[官方通知文档](https://learn.chatgpt.com/docs/config-file/config-advanced#notifications)

`Stop`/`SubagentStop` 同样是生命周期事件，换钩子不能替代来源核验。`Stop` 不支持按用途配置 matcher；子代理钩子的 `session_id` 是父任务，必须使用 `agent_id` 识别子任务。安装器没有再注册第二套完成钩子；兼容入口与 `notify` 共用分类和去重。[官方 Hooks 文档](https://learn.chatgpt.com/docs/hooks)

Robot 自己通过 `/new` 管理的任务使用其 App Server 事件和管理记录；独立侧边提问由侧聊控制器发送。不能把所有临时任务都视作垃圾。外部 Desktop 任务仍使用原实例，不会为了订阅事件而另起后端接管。

## 各字段分别说明什么

| 字段 | 用途 | 不应据此推断 |
| --- | --- | --- |
| `type` / `hook_event_name` | 回调或生命周期类型 | 用户答复、标题生成等业务用途 |
| `client` | 当前控制端的显示标签 | 不能代替本地任务的执行来源；手机控制时可以不同于 Desktop |
| `source` | CLI、VS Code、子代理等底层会话类别 | `vscode` 也被 Desktop 使用，不能直接当作 VS Code 扩展 |
| `originator` | 记录中的创建客户端 | 具体一轮是人工输入还是内部生成 |
| `threadSource` / `thread_source` | 任务用途，如 `user`、`thread_title` | 当前回合由哪个入口触发 |
| `turnTrigger` / `turn_trigger` | 本轮触发方式，如 `composer`、`remote_ios`、`goal` | 它不必与任务来源相等 |
| `ephemeral` | 不持久化的临时任务 | 单独不足以判断是否应通知用户 |
| `phase=final_answer`、JSON 字段 | 消息阶段和正文格式 | 内部任务也可以生成最终答复和普通文本 |

本机协议把 `ThreadSource` 定义为开放字符串，并称其为分析用途分类，不能假设存在完整、稳定的枚举。公开 App Server 提供线程读取及事件，但一个新的独立连接不是现有 Desktop 全局事件订阅。[官方 App Server 文档](https://learn.chatgpt.com/docs/app-server)

## 统一分类

所有完成通知先进入 `completion_routing.classify`，只核对所选 `CODEX_HOME`。任务 ID 必须精确匹配；前 8 位只作显示，不能拿父任务或同前缀任务的记录补证据。

| 分类 | 条件 | 处理 |
| --- | --- | --- |
| 用户对话 | 同环境任务记录、Desktop 创建来源、已知可见任务用途；兼容旧记录的空用途 | 按通知设置投递，正文可以是任意 JSON |
| 子代理 | 同环境子任务记录及 Desktop 来源 | 遵循独立的子代理通知开关 |
| 明确内部用途 | 标题、描述、摘要、输入预测、建议生成、安全筛选等来源标记 | 不创建 Telegram 消息、回复缓存或完成回应 |
| 来源不明 | 缺记录、未知用途/触发方式、字段冲突 | 不直接转发；记录脱敏结构诊断 |
| 其他执行来源 | 本地记录明确由非 Desktop 客户端创建 | 不进入 Desktop 通知流 |

任务来源和回合触发分开核验；例如 `user + remote_ios`、`user + goal` 都有效。按完整回合 ID 读取 `task_started.turn_attribution`，不借用另一个回合的触发标记。优先读最新状态数据库；同环境、同 ID 的会话头可在数据库迁移或短暂不可读时补证。数据库与会话头冲突时暂停投递。

### iOS / Android 的正常答复

手机发起、继续、排队或编辑的本机任务属于正常通知范围。Desktop 专用限制约束任务执行及 Robot 的发送通道，不限制操作设备。移动端连接本机 Desktop 后使用同一主机的项目和配置。[官方远程连接说明](https://learn.chatgpt.com/docs/remote-connections)

v0.6.12 修正了仅允许 `client=Codex Desktop` 的前置检查。其他客户端标签必须有同环境 Desktop 任务记录，以及同一回合在本地记录的已知正常触发方式；仅在回调中声称 `remote_ios` / `remote_android` 不足以放行。`queue`、编辑和重试等正常回合也适用，不依赖移动应用显示名称。正常 JSON、子代理开关、静默和去重照常处理。

编辑后使用数据库指定的当前会话文件，兼容额外文件名后缀和 Windows `\\?\` 长路径；校验实际路径仍在当前环境内，且会话头完整 ID 一致。没有数据库指向且存在多个候选时不猜测。历史索引未同步、快速读取未找到来源时，最多扫描会话末尾 64 MiB，并限制单行读取；仍缺证据则待核验。

输入模板和 `suggestion`、`suggestions`、`exclude`、`title` 等输出字段不再参与投递授权。旧模板识别仅保留为诊断兼容工具。用户此前明确要求的精确 `{"suggestions":[]}` 过滤仍单独保留。

## 待核验与去重

缺少本地记录、创建来源或所需回合证据，但任务及回合 ID 完整的通知，可进入本地 `telegram_completions.sqlite3`。队列只存标识、校验摘要和必要状态，不保存原始提示词、答复、客户端标签或工作区路径；保留“必须有同一回合证据”的标记。来源冲突不能通过丢掉冲突字段进入补发流程。

后台逐步复核，最多保留 200 条待核验记录，24 小时后过期。只有同一任务、同一回合的最终答复与摘要一致，才补投递一次。支持分页历史和旧会话记录；不向 Desktop 发送新输入。进入 Telegram 投递前先保留去重记录，结果未知不由此队列重试。投递记录保留 7 天；静默期间的记录及跨静默代次的待核验记录不会补发。

未知的新功能可能暂时没有通知，需要诊断并升级适配；不能宣称有限字段可百分之百识别所有未来来源。普通答复可在 Desktop 或 Robot 的任务详情中主动查看。

## 核验依据

2026-10-21 对照了 Desktop **26.1013.4894.0**、配套后端 **0.163.0-alpha.10.2** 的本地协议及代码：标题、描述、摘要等使用独立临时任务；标题生成设置 `threadSource=thread_title`，同时关闭生命周期 hooks，仍可能触发通用完成通知。真实日志确认本次标题通知的完整任务 ID 不在任务库，且与用户任务不同；没有保留该次原始通知，不能声称在其载荷中直接看到了来源字段。

根据实际任务记录构造的只读分类回放覆盖 26 个用户任务、9 个子代理任务和 4 个已报告的临时任务。前两组来源可确认，最后一组保持待核验、不直接转发。离线测试另覆盖字段冲突、同前缀 ID、双环境隔离、正常 JSON、延迟入库、分页历史、静默代次、并发去重与两个完成入口。macOS/Linux 的真实通知链路仍需各自设备验收。

同日晚间 Desktop **26.1016.6573.0** 的实际通知日志显示，多个手机回合因 `non-desktop-client` 被拦截；本地记录确认它们的任务创建来源仍是 Desktop。修复后，用 12 个真实 iOS/排队回合的元数据加测试客户端标签进行只读回放，均通过来源核验。原始回调未留存，该回放不等于重新完成手机到 Telegram 的端到端验收；Android 另有离线回归覆盖。
