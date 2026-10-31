# 更新记录

## 未发布 / Unreleased

- 整合 Windows/macOS 共用的控制器、投递、反馈及额度状态逻辑，平台适配层继续负责原生连接、进程清理、凭据和服务安装；保留 Windows 的 Desktop 默认后端。
- 修复文字回答提示遗留强制引用的问题；低额度已提醒阈值跨轮询、连接恢复及 Robot 重启保留，避免重复发送相同提醒。
- 计划任务归属同时核对完整 Codex profile 和 Robot 数据目录；拒绝缺失、重复或相对路径参数、其他模块、Shell 包装及多执行动作，避免同 profile 下另一套数据的服务被覆盖或停止。保留精确路径的旧版脚本入口。
- 修复收据路径测试未关闭 SQLite 连接导致的 Windows 临时目录清理失败，保留原有完整任务 ID 和环境边界断言。
- 增加 Windows 服务归属回归测试；本机原生验收结果和未完成项见 `docs/windows/README.md`。
- Share controller, delivery, feedback, and quota-state behavior across Windows and macOS while retaining native connection, process cleanup, credential, and service adapters. Windows keeps its Desktop backend by default.
- Stop text-answer prompts from leaving a forced reply selected. Preserve previously alerted quota thresholds across polling, reconnection, and Robot restarts to prevent repeated alerts.
- Bind scheduled-task ownership to both the exact Codex profile and Robot data directory. Reject ambiguous scope arguments, relative paths, other modules, shell wrappers, and multiple actions before managing an unrelated service. Preserve the exact legacy script entry point.
- Close the SQLite connection in the receipt-path regression so Windows can remove its temporary directory, while retaining full-thread-ID and profile-boundary assertions.
- Add Windows service ownership coverage and record native acceptance progress and remaining checks in `docs/windows/README.md`.

## 0.6.12 — 2026-10-21

- 修复 iOS / Android 远程发起、继续及排队回合因 `client` 标签不同被误判为非 Desktop 而漏通知的问题。核验同环境 Desktop 创建记录和同一回合的本地触发来源，不按移动应用名称建立白名单。
- 适配编辑后的会话文件后缀及 Windows 长路径格式；优先使用数据库指向的当前文件，保留完整 ID、会话头和环境隔离核验。历史索引未同步时，对长回合增加有上限的来源扫描。
- 待核验队列保留移动回合的额外证据要求，避免补投递时丢失条件；改善并发写入锁等待。内部消息过滤、子代理开关、静默和去重仍生效，Robot 发送通道继续仅使用 Desktop。
- Restore notifications for local tasks started, continued, or queued from iOS / Android. A changed callback `client` label no longer excludes work whose Desktop origin and exact local turn are verified; no mobile product-name allowlist is required.
- Support rewritten rollout suffixes and Windows extended-length paths, using the current database-selected file with full-ID, header, and profile checks. Add a bounded attribution scan for long turns before history projection catches up.
- Preserve the extra turn-evidence requirement during deferred delivery and tolerate brief concurrent queue writes. Internal filtering, subagent preferences, quiet mode, deduplication, and Desktop-only input routing remain enforced.

## 0.6.11 — 2026-10-21

- 重做完成通知的来源核验：按完整任务 ID、同环境 Desktop 记录、任务用途与独立回合触发方式分类；修复标题/描述等临时任务被误转发。正常 JSON 不按字段或输入模板过滤，未知来源不再直接放行。
- 增加只保存标识和校验摘要的待核验队列；从同一任务、同一回合恢复原答复，处理短暂入库延迟。补充跨入口去重、分页历史、子代理 ID、静默代次保护及中英文通知机制文档。
- 修复并发完成回调写入脱敏诊断时的数据库锁冲突；显式串行化写事务，并允许最多 2 秒的短暂锁等待。
- Route completions by the full task ID, profile-local Desktop records, task purpose, and independent turn attribution. Stop leaking ephemeral title/description work. Preserve normal JSON without body/template filtering; hold unknown provenance.
- Add metadata-only deferred verification that restores the exact original reply after transient record lag. Cover cross-entry-point deduplication, paginated history, subagent IDs, quiet-mode generations, and bilingual notification documentation.
- Serialize structural diagnostic writes with an explicit write transaction and a bounded 2-second lock wait to tolerate concurrent completion callbacks.

## 0.6.10

- 修复 Desktop 临时任务遗漏来源字段时，非空 `suggestions` 建议和 `exclude` 安全筛选结果被误转发的问题。匹配已核对的应用生成输入模板，要求同环境数据库可读且完整任务 ID 不存在；保留明确来源和冲突检查。
- 正常任务的同形 JSON、引用示例和未知模板继续保留；不按返回字段一刀切。将 `exclude` 类未知来源加入脱敏诊断，不记录原始输入和回答。
- Stop forwarding ambient suggestions and safety-filter output from unattributed ephemeral Desktop tasks. Match verified generation input templates only when the profile database is readable and the full task ID is absent; preserve explicit attribution and conflict checks.
- Preserve normal JSON replies, quoted examples, and unknown templates. Extend structural diagnostics to unknown `exclude` results without recording raw prompts or answers.

## 0.6.9

- 适配 iOS 远程启动但窗口尚未加载的本机 Desktop 任务：直接回复、`/send`、追加和异步回答支持按需恢复原窗口；中止及“中断后发送”增加同实例、同 Turn 核验和一次恢复。
- 运行状态、详情、进度及中止/追加选择器使用 Desktop 实时状态；查询和能力探测不导航。异步回答未找到接收者时保留待回答状态。
- 保留 10 秒恢复预算、Desktop 专用、静默及不重复提交约束。更新中英文使用与验收文档，明确 Windows 双开全局 IPC 代理的现有限制。
- Support local Desktop tasks started remotely from iOS before their window loads. Replies, `/send`, append, and asynchronous answers recover the original window on demand; stop and interrupt-then-send verify the same instance and turn before dispatch.
- Use live Desktop state for running tasks, details, progress, and stop/append selectors. Queries and capability probes never navigate. Missing answer recipients leave questions awaiting an answer.
- Preserve the 10-second recovery budget, Desktop-only routing, quiet mode, and no automatic duplicate submission. Update bilingual usage and acceptance documentation, including the Windows dual-instance broker limitation.

## 0.6.8

- 修复后台任务存在、Desktop 回复窗口未登记时直接回复和 `/send` 失败的问题：发送前明确找不到持有者时，在同环境 Desktop 打开原任务一次，再验证回复通道；导航会切换 Desktop 当前显示的任务。
- 导航与持有者探测共用最多 10 秒恢复预算；锁定原进程和环境，发送前复核任务与指定 Turn。接口缺失、实例重启或通道未恢复时不发送，不回退到独立后端或代理委派。
- 恢复仅适用于尚未发送的外部任务消息；查询、静默模式、机器人自建任务和提交结果未知均不触发恢复，历史失败消息不自动补发。保留原有 10 秒提交等待与回执核对机制。
- Recover direct replies and `/send` when a background task has no registered Desktop reply owner: open the existing task once in the verified profile before input, then verify the owner. Navigation changes the visible Desktop task.
- Bound navigation and probes to a shared 10-second recovery budget, pin the original process/profile, and recheck the task and expected turn before sending. Missing interfaces, restarts, or failed recovery stop delivery without backend takeover or agent delegation.
- Never recover after a possibly submitted input, replay old failures, or navigate during queries, quiet mode, or bot-managed task delivery. Existing submission and receipt timeouts remain unchanged.

## 0.6.7

- 异步问题卡片使用 Telegram 原生 HTML 富文本：加粗问题标题，末尾状态保持普通字重，工作区和任务 ID 使用等宽样式，原问题与回答分为引用块。
- 沿用 Telegram 主题配色，兼容亮暗主题；保留明确段落间距，同步中文和英文文案。
- 转义标题、问题和回答中的特殊字符，按 UTF-16 长度裁剪预览，保留长问题及回答的全文入口；静默模式继续阻止自动推送。
- Render asynchronous question cards with native Telegram HTML: a bold title, a regular-weight status, code-styled metadata, and separate question/answer blockquotes. Use theme colors and explicit spacing in both languages.
- Escape user content, bound previews by UTF-16 length, and retain full-text access for long questions and answers. Quiet mode still suppresses automatic delivery.

## 0.6.6

- 修复 Desktop 已接收消息但本地 rollout 文件滞后时的“提交结果未知”误报：增加同环境 Desktop 实时历史的只读回执核对。
- 严格匹配任务、新回合开始时间、首条用户消息原文摘要和唯一消息 ID；旧回合追加、截断内容或来源不符继续保持结果未知。
- 回执核对总预算仍为 10 秒，失败不重发，重复提交按本地记录顺序分配独立回执。
- Reconcile timed-out submissions against read-only live Desktop history when local rollouts lag. Require an exact first user message in a fresh turn, preserve receipt uniqueness and the 10-second budget, and never resubmit.

## 0.6.5

- 问题卡片统一为 `✒️[问题(状态)] 任务标题`，展示工作区、任务短 ID、原问题和用户回答；任务元数据只读查询，缺失时明确显示未知工作区。
- 正文完成/错误图标改为 🥳 / 🥲；Telegram 原生表情回应使用支持的 🎉 / 😢，兼容升级前的待重试反馈。
- Use question cards with task titles, workspaces, short task IDs, original questions, and user answers. Use 🥳 / 🥲 in message text and supported 🎉 / 😢 Telegram reactions, including retries from older versions.

## 0.6.4

- 异步问题卡片保留原问题和用户回答，区分已提交、已回答、待确认及已结束；移除旧作答按钮，长回答可查看全文或下载。
- 保存 Telegram 选项/文字答案，支持从 Desktop 作答记录回填，升级保留原问题和消息关联；界面中英文同步更新。
- Preserve asynchronous question cards with the original question and answer, distinct submission/confirmation/closure states, and full-text access for long answers. Persist Telegram answers and recover Desktop answers from history without resubmitting.

## 0.6.3

- 完成通知去除首尾空白后，整条正文恰好等于 `{"suggestions":[]}` 时不再转发；此规则独立于来源识别，不扩大到其他 JSON、代码块或带说明的回复。
- Hook 与 Desktop 通知均在发送和完成状态处理前过滤，日志单独标记字符串匹配原因；其他未知来源的建议结构仍保留脱敏诊断。
- Suppress completion notifications whose entire body is exactly `{"suggestions":[]}` after trimming surrounding whitespace. Keep other JSON, code blocks, and explanatory replies; log literal matching separately from provenance detection.

## 0.6.2

- 为来源未知的建议结构通知添加本地脱敏诊断：字段类型、已知来源枚举、最新输入的模板特征，不保存正文、路径、凭据或未知字段名。
- 样本按事件去重，最多 100 条，每次写入清理超过 7 天的记录；采集失败不影响通知，当前过滤规则不变。
- Add bounded local structural diagnostics for unclassified suggestion-shaped notifications. Preserve existing forwarding; record no raw message text or credentials.

## 0.6.1

- Desktop 用户输入默认超时改为 10 秒；提交结果未知时再只读核对接收回执，最多等待 10 秒，禁止自动重发。
- 已匹配的新用户消息回执按成功处理；未确认、来源不符或回合不符继续显示“结果未知”，不再误报普通命令失败。
- 保留直接回复与 `/send` 的消息关联、Desktop 专用限制和静默设置，补充延迟回执与去重回归测试。
- Use a 10-second Desktop request timeout and up to 10 seconds of local receipt reconciliation, without resubmission. Confirmed user receipts count as accepted; unverified outcomes remain unknown instead of being reported as command failures.

## 0.6.0

- 新增持久化静默模式，默认关闭，通过设置启用，仅保留查询和设置，禁用任务提交、问题作答、插件执行和额度重置。
- 暂停完成通知、异步问题、审批、进度与其他自动推送；旧按钮和附件同样受限，已排队回复不会在关闭静默后补发。
- 开启时取消进度跟踪与未执行的额度预约，保留原通知偏好和已有任务；补充双语说明及回归测试。
- Add persistent quiet mode: queries/settings only, no task submissions or automatic messages; cancel tracking and quota reservations without interrupting existing tasks or overwriting notification preferences.

## 0.5.3

- 插件入口及插件命令固定在菜单底部，说明统一添加 `[插件]` / `[Plugin]` 标记；额度查询继续保持第一项。
- Keep plugin status and plugin commands at the bottom, prefix their descriptions with `[插件]` / `[Plugin]`, and retain quota first.

## 0.5.2

- 将 `/quota` 固定为 Telegram 菜单第一项，同步帮助和双语文档。
- Keep `/quota` first in the Telegram menu, help, and bilingual documentation.

## 0.5.1

- Telegram 菜单按查询、设置、插件和执行操作分组排序；常用查询靠前，中止任务最后，中英文说明统一精简。
- 新增 `/settings`，集中管理完成通知、子代理通知、语言和新任务目录；旧命令与按钮继续兼容。
- 设置页面只读打开，开关使用明确状态；`/help` 不再清除待回复输入。
- 补充设置授权、重复点击、配置保留、菜单容量和双语回归测试。
- Reorder and shorten the bilingual Telegram menu; add a unified settings panel with explicit notification states, language, and the new task folder. Keep legacy commands and buttons. Help now preserves pending input.

## 0.5.0

- 文档默认中文，补齐安装、使用、插件、架构和验收英文版，各页面支持语言切换。
- Telegram 新增 `/language`、`/language en`、`/language zh`，默认中文，按机器人数据目录持久保存，不跟随客户端语言自动改变。
- 核心界面、通知、提问、额度和内置截图采用显式双语文案；任务标题、用户消息、模型正文和回调数据保持原样。
- 安装器支持 `--language en`；插件 API v1 兼容新增 `ctx.language`、`ctx.tr()` 和可选的双语命令说明。
- 新增语言偏好跨进程读取、环境隔离、权限、原文保留、菜单、插件语言及翻译占位符测试。

## 0.4.1

- 修复 Windows `schtasks /XML` 的输出编码可能与 XML 声明不一致，造成已安装服务被误判为非本工具服务。
- 服务查询、备份和恢复统一为有效的 UTF-16 XML，保留中文路径与原有配置。
- Windows 登录触发器明确绑定当前用户，避免创建“所有用户登录”触发器时要求管理员权限。


首次公开的源码快照基于 `0.3.17`。以下记录概括对应开发版本；当前安装、使用方式和限制以 [README](README.md) 为准。

## 0.4.0

- 迁入 `src/` Python 包；测试、文档、平台适配、插件资源和管理工具分开。
- 安装、升级、卸载、检查和发布统一使用 Python；增加 wheel/sdist 与独立虚拟环境安装。
- 新增 macOS/Linux Unix socket、Desktop 程序识别、服务、系统钥匙串、截图和进程清理适配。
- 继续保留 Desktop 专用、环境隔离、人类消息来源、提交不确定时不重发的约束。
- 旧 Windows 凭据、状态与任务回复关联在原数据目录继续使用；卸载默认保留数据。
- 三平台自动回归；macOS/Linux 真实 Desktop 通信在实机验收前标为实验性支持。

## 0.3.17

- 新增 `/running` 与 Telegram 菜单入口，显示运行任务、工作区及等待状态。
- “运行中”筛选使用相同的 Desktop 实时核验，支持刷新、分页、详情和回复。
- 补查原生最近列表范围外的候选任务；迁移后缺少会话文件的记录直接查询 Desktop。
- 一次读取固定到同一 Desktop 实例，连接变化或核验不完整时明确提示。
- 从运行列表打开详情后保留返回的筛选与页码。

## 0.3.16

- 通过 Desktop 持有者的用户输入通道提交 Telegram 消息，保留用户消息身份。
- 发送前检查环境、进程、管道和任务归属；同名任务在多环境中冲突时停止提交。
- 普通继续与异步问题回答分别处理；回答不能自动启动新 Turn。
- 真实投递验收要求匹配 `role=user` 接收记录及对应 Turn。

## 0.3.15

- 修正异步回答的 XML 转义、回答关联和按钮回收。
- 恢复对应 Turn 的 Telegram 回复关联，不重发已提交的答案。

## 0.3.14

- 适配分页任务历史、异步提问和交互生命周期。
- 处理请求失效及部分 MCP 标量表单；不支持的交互提示回 Desktop。
- 内部建议按已知来源识别，正常 JSON 回复保留。
- 中止操作要求确认实际中止的 Turn，能力不可用时明确提示。

## 0.3.13

- 新增真实连接与投递验收；发布构建通过验收后才生成安装包。
- 将只读检查、真实投递和离线回归的结果分别报告。
- 测试可按组选择，完整回归使用 `Full`。

## 0.3.12

- 兼容新版 `codex-app-tools-*` 本机连接地址，保留旧地址格式兼容。
- 校验本机命名管道及其服务端进程，避免混用隔离环境。

## 0.3.10–0.3.11

- 根据明确的内部用途来源及已确认的应用生成请求识别输入框建议。
- 来源不明或属于正常任务时保留回复，不单凭 `suggestion` 字段过滤。
