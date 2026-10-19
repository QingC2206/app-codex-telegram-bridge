# 更新记录

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
