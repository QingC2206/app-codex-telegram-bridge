# 测试与实机验收

**简体中文** | [English](testing.en.md) · [返回 README](../README.md)

本分支的 Windows 实机验收集中在 [windows/README.md](windows/README.md)：四组原生验收、重点交互回归、证据模板与合并条件。Windows 验收通过后再由用户决定合并主分支。

## 自动检查

```text
python -m pip install -e ".[dev]"
python tools/run_tests.py --suite Full
python -m build
python tools/check_distribution.py
```

测试使用临时 Codex 环境和机器人数据目录。原有业务场景继续覆盖回复、消息来源、内部建议过滤、任务查询、异步提问、额度、图片、表格、插件及安全边界。新增测试覆盖安装配置保留/回滚、旧数据发现、凭据不可用时拒绝明文回退、服务参数转义、单实例互斥、Desktop 包来源和 Unix 真实 socket 分帧及 peer 校验。

GitHub Actions 在 Windows、Ubuntu、macOS 运行 Python 3.10/3.11/3.13；原生平台测试仅在对应系统执行。没有 Desktop 或 Telegram 账号的 CI 不执行真实任务投递。

v0.6.11 的通知来源测试覆盖任务来源与回合触发独立核验、同前缀完整 ID、未知来源待核验、正常 JSON、分页历史、并发去重、静默代次，以及子代理提问不占用完成通知记录。详见[通知来源与投递](notifications.zh-CN.md)。本机只读回放不是手机端重新验收。

v0.6.12 补充不同客户端标签下的 iOS / Android、排队、编辑及重试回合，真实元数据只读回放、重写会话文件、Windows 长路径、长回合缺少历史索引、移动端待核验条件保留和跨入口去重。实机验收时，分别从 iOS / Android 在已连接主机上新建任务、继续任务并排队发送；每个正常完成答复应只在 Telegram 出现一次。静默开启时不推送，内部标题/建议不推送；通知可直接回复到对应 Desktop 任务。

## 当前电脑只读检查

0.4.1 的历史实机验收已完成 Windows 任务投递、同一 Turn 的用户来源回执、Telegram 通知、手机直接回复、`/send`、提问卡片回答，以及独立 wheel 安装/随包插件检查。这些实机结果不是 CI 手机测试。macOS/Linux 的真实 Desktop 连接、密钥环、服务启动和截图仍需对应电脑验收。

语言测试覆盖中文默认值、偏好持久化、环境隔离、独立通知进程、命令菜单、授权按钮、用户/模型原文保留、回调数据不变、翻译占位符及插件工作进程；不向真实 Telegram 或 Desktop 任务提交消息。

安装后使用安装器虚拟环境中的 Python：

```text
python -m codex_telegram_bridge doctor
```

检查 Telegram API 和 Desktop 连接，不创建/恢复任务，不向任务提交消息，也不调用 Telegram `getUpdates`。输出中的个人路径不应原样公开。

`doctor --offline` 仅检查本地安装和依赖；不把它的通过视作真实连接通过。

## 一次明确授权的通信自检

在 Desktop 打开一个正在运行且允许接收测试消息的任务。从源码目录运行：

```text
python tools/verify_connection.py --thread-id <任务完整ID> --send-probe --report .validation/live.json
```

它提交一次带标记的用户输入，要求在对应任务读到同一 Turn 的真实用户消息回执，再验证 Telegram 发送。任何阶段结果不明均不自动补发。检查本身不会消费手机发来的更新；手机入站必须由以下人工步骤验证。

## macOS / Linux 首次实机检查

1. 拉取 GitHub 代码，保持 Codex Desktop 运行，执行 `python3 install.py`；重新启动 Desktop 加载 Hook。
2. 使用 `~/.codex/telegram-bridge-runtime/bin/python -m codex_telegram_bridge doctor`，确认连接通过。自定义环境带上对应参数。
3. 在 Desktop 让一条普通任务完成，确认 Telegram 收到正常回复，建议预测内容没有被当作最终答复转发。
4. 直接回复这条任务卡片，确认原任务收到一次用户消息，没有“由另一个 Codex 任务转发”包装。
5. 使用 `/send` 选中同一任务继续，使用 `/running` 查看实时运行列表。
6. 让任务发起一个选项问题和一个自由文字问题，从 Telegram 分别回答；旧按钮不能重复提交。
7. 测试附件、长回复分页、只读侧边聊天和 `/screenshot`。按系统要求授权截图；Wayland 需在桌面确认。
8. 发送 `/language en`，检查 `/help`、`/running` 和新完成通知，再用 `/language zh` 切回。任务正文及按钮路由应保持不变。
9. 重启机器人，确认凭据、消息关联、语言偏好和任务回复能力仍在。存在隔离订阅时确认不串环境。

若出现端点发现或 peer 授权错误，保留原服务状态并报告脱敏错误、系统和 Desktop 版本。不要更改沙盒、关闭系统校验、将另一客户端的任务恢复到独立后端，或反复发送同一条不确定的命令。

## iOS 后台任务验收

自动测试覆盖未加载任务的直接回复、`/send`、追加、中止、异步回答、实时状态及进度；覆盖 Turn 变化、实例重启、静默、丢失回执和禁止独立后端接管。新增路径的自动测试不能替代手机真实入站验收。

实机验收时，从 iOS 在本机启动一个可中止的测试任务，保持桌面未打开该任务。先查询 `/running` 和任务详情，确认不会切换窗口。再分别测试直接回复、`/send`、追加、异步回答、中止和“中断后发送”；每轮都检查原任务只收到一次预期输入。旧问题已结束或 Turn 改变时应拒绝操作，中止结果不明时不发送后续指令。开启静默后重复操作应被拦截。双开时核对账号及环境，不能用放宽 PID 检查作为验收通过的条件。

## 发布

```text
python tools/build_release.py --thread-id <允许验收的活动任务完整ID>
```

发布门禁核验真实投递、用户消息回执、Telegram 发送、全部离线测试，以及验收前后源码哈希。只发布 `dist/*.zip` 和对应校验和；`.validation/` 和 `*.validation.json` 留在本机。wheel/sdist 可独立构建用于 CI 和安装验证，不以构建成功冒充通信验收。

## 设置与菜单验收

检查 `/quota` 固定为菜单第一项，其后为 `/tasks`、`/running`，插件入口及插件命令位于底部，中文说明带 `[插件]`、英文说明带 `[Plugin]`。打开 `/settings` 各页面应不改配置或待输入任务；完成通知和子代理通知分别开关，重复点击保持所选状态。通过语言页切换中英文后检查菜单和返回按钮，最后恢复原偏好。验证 `/settings cwd 路径` 及旧命令可用；`/help` 不清除待输入。

## 静默模式验收

开启静默后确认 `/quota`、`/tasks`、`/running` 和设置仍可用，任务消息、附件和旧操作按钮均不提交。产生完成回复及提问时应无自动推送；关闭后不补发。重启后模式保留，原通知偏好不变；跟踪与额度预约不会自行恢复。

## 分层重构兼容回归

```text
python tools/run_tests.py --suite Compatibility
python tools/run_tests.py --suite Full
```

`Compatibility` 在当前主机用显式平台策略和模拟系统命令验证：Windows 默认后端不变、损坏配置不阻断恢复命令、两种安装器在启动失败时完整回滚、Windows 隐藏窗口/Job Object 调用、原生任务校验与 macOS 独立策略。macOS 测试使用临时目录和显式访问适配器，避免通过修改平台名称假装原生文件系统。

本轮 Windows 验收限定为兼容回归；Windows 实机另行验收，未把 Mac 上通过的测试当作 Windows Task Scheduler、Named Pipe 或用户会话实测。后续 Windows 验收须在保留 Desktop 默认后端的条件下确认安装后自动运行、重启恢复、原任务引用回复、同 Turn 追加、停止及审批；结果未知不得重发。只有单独批准迁移后端时才执行 `configure-app-server`。

app-server 的菜单、卡片、审批和错误使用同一语言偏好；测试同时核验模型正文、中文任务标题、回调值和原始用户输入不会被翻译。完整测试保持临时 profile 和数据目录，不读取真实 Token。

表情回归使用 `python tools/run_tests.py --suite Feedback`，同时覆盖原 Desktop 与 app-server 后端。验收时观察用户原消息下的 👀 → 🤔 → 🎉；失败为 😢。原 Desktop 引用回复、机器人任务、追加指令、审批回答和查询按钮分别验证；结果尚未送达、限流、静默通知和未知提交不能提前标为完成。

## 本轮 Windows 对齐门禁

`docs/parity.json` 是 Windows 基线与已批准新增行为的逐规则清单。每条逻辑有稳定 ID，分别关联经人工审读的具体测试断言或实机场景；数量由审计输出计算。`docs/test-migration.json` 逐项解释退役独立控制器的 98 个测试场景去向。自动队列等与 Windows 不一致的旧行为标记为有意对齐，不能静默删除测试。

完整回归可用 `--report docs/parity-tests.json` 输出每项结果和完整源文件集合的指纹。`python tools/audit_parity.py --require-reviewed` 要求逐规则映射、具体测试定义、本轮回归与最终公共层复检都一致；新增、删除或修改实现、测试、工具、清单或安装文档都会使旧证据失效。`--require-complete` 还要求对应版本的 macOS Telegram 界面场景记录。历史通过字样、API 回执和模拟控制器测试都不能代替本轮界面验收。更新步骤和证据格式见 [逐规则验收](parity-audit.md)。
