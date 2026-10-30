# Windows 实机验收

本目录是 `codex/platform-parity` 分支的 Windows 验收入口。先在 Windows 验收并记录结果，再决定是否合入 `main`；本文和模板均不表示 Windows 已通过。

首个实现提交为 `fb81934`，验收时应安装本分支最新提交。macOS 六组原始实机证据及后续修复的补验状态以 [parity.json](../parity.json) 为准；Windows 仍有下列四组待验。证据格式和门禁见 [逐规则验收](../parity-audit.md)。本目录不另建能力清单。

| 场景 ID | Windows 必验内容 | 对应规则 |
| --- | --- | --- |
| WINDOWS-CROSS-ACCOUNT-LOCK | 整机跨用户互斥、正常退出与崩溃接替、Hook 可用 | A53.R02、E02.R03 |
| WINDOWS-DESKTOP-NATIVE | Desktop 后端、正确持有者与管道、原任务交互、文字提示不复发、低额度提醒去重、子进程清理 | C01.R01、C01.R02、C02.R01、C03.R01、C04.R01、E01.R01、A52.R01、A29.R01 |
| WINDOWS-OS-CREDENTIALS | DPAPI 正常读写、不同用户拒绝、无明文回退 | N10.R04 |
| WINDOWS-SERVICE-NATIVE | 当前桌面用户的计划任务、服务归属、升级与恢复 | D04.R02、D04.R03、D04.R04 |

## 1. 准备与基线

1. 保留现有 Windows 配置及后端。Windows 默认仍为 `desktop`，本轮不执行 `configure-app-server`，不复制 Mac 的运行配置或凭据。
2. 在改变安装或停止服务前，备份该机器人的数据目录、Codex Hook/notify 配置、计划任务定义及旧安装包/运行环境。记录恢复方法；不清空原任务、消息偏移和回复关联。
3. 记录待测提交、Windows/Python/Codex Desktop 版本、实际解释器路径、测试时间。包版本仍为 `0.6.12`，仅看版本号不能证明安装了本分支。
4. 准备一个可接收测试指令的原 Desktop 对话和机器人 Telegram 聊天。跨用户测试还需要第二个真实 Windows 账户；分别用 `whoami /user` 核对 SID。提升同一账户权限或修改 `USERNAME` 不算第二个账户。
5. 原始结果放在仓库外或被忽略的 `.validation/windows/`。Token、登录材料、真实聊天数据库和未脱敏截图不提交到 Git。

已有仓库先检查并保留本地改动，再拉取分支：

```powershell
git status --short
git fetch origin
git switch --track origin/codex/platform-parity
git rev-parse HEAD
```

本地分支已存在时使用 `git switch codex/platform-parity`。不要为切换分支丢弃改动。也可以新目录克隆完整历史；审计工具需要访问清单指定的基线提交：

```powershell
git clone -c core.autocrlf=false --branch codex/platform-parity https://github.com/QingC2206/app-codex-telegram-bridge.git
```

在仓库根目录，用独立验收环境运行离线检查，不改变正在运行的服务环境：

```powershell
py -3 -m venv .venv
$TestPython = (Resolve-Path .venv\Scripts\python.exe).Path
& $TestPython -m pip install -e ".[dev]"
& $TestPython tools/run_tests.py --suite Compatibility
& $TestPython tools/run_tests.py --suite Full --report .validation/windows/full.json
```

Python 需符合 `pyproject.toml` 的版本要求。逐条检查命令退出码，失败先处理再继续。Mac 的通过和跳过数量不是 Windows 的预期计数：Windows 原生测试应实际执行，平台不适用的跳过须逐项解释，不能隐藏失败或任意跳过。

## 2. 验证目标安装

使用既有安装方式升级到本分支。若使用仓库安装器，以下是默认路径示例；自定义路径必须换成当前机器人的实际路径：

```powershell
$CodexProfile = Join-Path $env:USERPROFILE '.codex'
$BridgeData = Join-Path $CodexProfile 'telegram-bridge'
py -3 install.py --codex-home "$CodexProfile" --data-dir "$BridgeData"
$BridgePython = Join-Path $CodexProfile 'telegram-bridge-runtime\Scripts\python.exe'
& $BridgePython -m codex_telegram_bridge --codex-home "$CodexProfile" --data-dir "$BridgeData" doctor --json
& $BridgePython -m codex_telegram_bridge --codex-home "$CodexProfile" --data-dir "$BridgeData" service status
```

已有自选 Python/Conda 运行环境时沿用它，并以实际解释器替换 `$BridgePython`，不必为了验收迁移服务环境。安装后按现有说明重新加载 Desktop 的 Hook 配置。

核对安装模块的位置及内容确实来自待测提交，`runtime.json` 若存在仍选择原 Desktop 后端。`doctor` 必须真实连通 Telegram 与正确的 Desktop；`doctor --offline` 或安装成功不能替代连接检查。不要并行调用 Telegram `getUpdates` 与正式服务争抢消息。

## 3. 四组原生验收

### W1：整个系统只能有一个机器人主服务

对应 `WINDOWS-CROSS-ACCOUNT-LOCK`。

1. 用户 A 启动主控制器，确认进入工作/轮询状态。在 A 下用不同 profile、数据目录、机器人身份启动第二个隔离实例：应因整机锁拒绝，不能进入第二个轮询。
2. 在真实用户 B 的会话重复启动，仍应被拒绝。保存 A/B 实际账户身份及进程标识；不能只改变环境变量。
3. 让持有者正常退出，再由 B 启动并成功接替；此时 A 的新启动应被拒绝。
4. 记录 B 的 PID/创建时间，仅强制结束该测试主进程，再确认 A 能接替。不要按所有 Python 或 Codex 进程批量结束。
5. 主服务占锁期间实际执行已配置的通知入口，核对 `notify` 和 `desktop-notify` 可退出并产生相应记录；真实 Desktop 完成/提问通知仍可送达。
6. 恢复原用户的正式服务，确认只有一个主服务及一个收消息进程。

可使用隔离配置和合成 Telegram/Codex 传输，避免读真实 Token；但必须运行实际安装的锁和主控制器启动链，使用固定生产锁端点 `127.0.0.1:27321`，不能 mock 锁、改成随机端口或伪造拒绝。缺凭据而提前退出不算通过。现有单元测试使用临时端口，不能替代此项。

**通过条件：** 任意时刻最多一个主服务工作，跨 SID 同样成立；正常退出、崩溃均释放占用；短生命周期 Hook 不受主服务锁阻挡。缺少第二账户时记 `blocked`。

**证据：** 两个真实账户的身份核对，每次启动/拒绝/退出/接替的时间及 PID，实际探针脚本，Hook 结果和恢复后的服务状态；说明传输是否隔离。

### W2：Windows Desktop 原生链路与原任务操作

对应 `WINDOWS-DESKTOP-NATIVE`。

1. 核对实际 Desktop 后端、用户 profile、Desktop 进程及 Named Pipe 服务端 PID。多窗口/多环境时找到原任务真正持有者；VS Code、其他 CLI 或另一 profile 不能被当作该持有者。
2. 在选定的原 Desktop 对话发送简单标记任务，等待 Telegram 完成通知。直接引用通知要求回复新唯一标记；完整任务 ID 保持不变，只新增一次输入，不另建任务。
3. 让同一任务实际等待约 60 秒，从 Telegram 引用通知，选择“不打断，追加消息”。前后 Turn ID 必须相同，最终包含新标记。
4. 再做一次运行中输入，选择中止后发送。旧 Turn 确认中止/空闲在先，新输入与新 Turn 在后。结果未知时不能自动重发。
5. 保留运行中选择卡，等待对应 Turn 结束后再点击；应拒绝迟到操作，不能投到另一 Turn。已结束的旧提问也应拒绝。
6. 查询任务列表与详情，核对实际协议响应/分页读取及原任务归属。旧响应结构未在本机出现时，如实注明由自动兼容回归覆盖，不声称亲自实测了该结构。
7. 在隔离验收中制造提交检查点后的状态变化，核对任务/持有者/Turn 校验会拒绝错投；不能用放松 PID/profile 校验消除失败。
8. 记录机器人拥有的后台子进程 PID/创建时间，仅结束测试机器人主进程；核对 Windows Job Object 回收其子后台，独立 Codex Desktop 不受影响。恢复后原任务仍可继续。

**通过条件：** 输入、Turn 和结果回执属于正确的原任务；追加、中止、迟到操作及子进程生命周期符合预期。

**证据：** 脱敏的任务/Turn/Telegram 消息对应关系、动作前后时间、管道/进程归属、截图及必要日志。Bot API 发消息成功不等于原任务执行成功。

### W3：Windows 原生凭据保护

对应 `WINDOWS-OS-CREDENTIALS`。

1. 在隔离目录生成仅用于验收的随机值，通过实际安装的 Windows DPAPI 适配器 `protect_secret` / `unprotect_secret` 写入并读回；只记录相等与否，不打印明文。
2. 检查保存数据与配置没有该明文值，正式机器人 Token 未改动。
3. 把**该合成测试值**的受保护数据交给 B：先确认 B 实际读到了文件字节，再调用原生解密，应明确失败。文件访问被拒绝本身不能证明 DPAPI 跨用户保护。
4. 用隔离测试配置检查损坏密文/原生解密拒绝路径：加载必须报错，不回退明文、不忽略错误继续启动。不得损坏真实凭据文件。
5. 清理本次合成测试项目，确认正式机器人仍能收发。

**通过条件：** 当前用户正常读写，不同用户实际解密失败，拒绝/损坏路径无明文回退；真实凭据及保护策略不变。

**证据：** 读回布尔结果、不同用户原生错误类别/错误码、无明文检查、清理及服务恢复记录。

### W4：计划任务与安装管理

对应 `WINDOWS-SERVICE-NATIVE`。

1. 检查机器人自己的计划任务：运行账户及登录触发账户都是当前桌面用户，使用交互令牌及最低权限；解释器、profile、数据目录和工作目录均为正确绝对路径。
2. 从已有安装升级，确认凭据、偏好、消息偏移及回复关联保留；旧自有服务定义按归属迁移，没有遗留第二个有效主服务。
3. 实际注销/登录或重启 Windows，核对登录后启动与 Telegram 收发。不能只检查 XML 中“有登录触发器”。
4. 测试 `service stop`、`service start`、重复安装及仅结束测试主进程后的恢复；记录恢复延迟。当前定义包含每分钟调度，尚未触发不算已恢复。
5. 在隔离任务/配置下验证归属保护：身份不匹配时拒绝覆盖、停止或卸载，不误改其他用户/配置。不要拿真实无关服务做破坏性试验。
6. 在隔离安装中制造可恢复的安装/启动失败，确认旧定义及运行状态可恢复；最终恢复本分支正式服务，检查单主进程及真实收发。

**通过条件：** 当前用户服务可启动、登录恢复、重复安装和故障恢复；管理动作限于自有安装，原数据保留。

**证据：** 脱敏的前后任务定义、账户/路径、登录恢复时间、主/子进程状态、重复安装及隔离回滚记录。

## 4. 合并前的交互回归

以下复用现有规则，结果记录在对应原生场景的 `observations` 和附件中，不增设第二份规则状态。

| 场景 | Windows 实际预期 | 主要清单入口 |
| --- | --- | --- |
| 查询、任务、回答的表情 | 原消息经历 👀→🤔→🎉；实际送达后才完成；明确失败为 😢 | A16、A52 |
| 文字回答编辑器 | 打开入口不提前完成、不强制选中回复；手动引用提示可回答原题，旧题/迟到输入不产生新任务；回答或取消后反复离开/重进并重启 Telegram，输入框不恢复旧题引用。旧版本遗留状态先发送 `/help` 清除，再验证新提示不复发 | A51、A52、B07 |
| Telegram 拒绝表情 | 在唯一测试消息上受控触发真实拒绝，仍有原任务文字回执；记录注入范围并恢复 | A16.R05 |
| 详情、列表与进度 | 原答复不覆盖；独立面板复用；返回保留页码；刷新保留返回/关闭；关闭后旧刷新不复活 | A46 |
| 真实审批 | 拒绝、仅一次、本会话分别有真实执行结果；多题选项/自由文字绑定正确，原文保留 | A51、A52、B07 |
| 低额度提醒去重 | 对已提醒且仍为 0%／1% 的账户，观察至少两个完整的 5 分钟监控周期，再重启机器人：同一阈值不重复通知；手动 `/quota` 仍正常返回。重置时间秒级变化、短暂缺数据和小幅用量波动由公共自动回归覆盖，不能为制造恢复而消耗真实重置卡 | A29.R01、A29.R02 |
| 长答复、附件、静默 | 格式与分页可读，文件按钮正确；上传结果未知不自动重复；静默和恢复符合清单 | 对照 parity.json 对应能力 |

每项使用唯一标记并记录原任务与输入/结果关联。临时故障注入限于合成测试数据，不能留在正式服务。额度实测不包含消耗真实重置卡；预约仍只有 6/12/24 小时。具体插件扩展是已批准例外，通用插件宿主不是例外。

## 5. 结果与合并条件

复制 [native-evidence.template.json](native-evidence.template.json) 到本机 `.validation/windows/` 再填写。四项初始均为 `not_run`，不是可登记的通过记录。缺账户、未重启、未触发原生错误等情况写 `blocked` 或 `not_run`，保留缺口。

每个场景保存完整待测提交、环境、原生操作、时间、实际结果、证据文件及 SHA-256。全部子项实际通过后才写 `outcome: passed` 并填写 ISO 8601 `observed_at`；`rule_ids`、`method` 必须与清单一致。

合并前逐项确认：

- 自动回归无未处理失败，平台跳过有解释；11 项 Windows 专用测试没有照抄为跳过。
- 四组原生验收及交互回归均有证据，未解决项为零；改代码后补充对应回归及受影响平台复检。
- 比对实际待合并代码、测试和源集合。Git 换行、本机构建元数据、文档变化都会影响摘要；不能只复制 Mac/Windows 的 `source_digest` 把旧结果改成当前通过。
- 保留 Windows 完整报告，不直接覆盖仓库的 Mac 报告。当前审计命令读取单份 `docs/parity-tests.json`，不是多平台报告自动合并器。维护最终证据的人须审查两份报告的提交、逐项结果及源集合，保留观察时间，明确历史证据适用性。
- 审查后的 Windows 通过记录保存为 `docs/parity-native-windows.json`，用文件 SHA-256 加入 `parity.json` 的 `native_evidence.current`，并保留有效 Mac 记录。该文件目前尚不存在，因为 Windows 未实测。Mac 原始截图/日志保留在 Mac 本机，仓库中的摘要与哈希不包含那些私人文件。
- 最终运行 `python tools/audit_parity.py --require-reviewed` 与 `python tools/audit_parity.py --require-complete`。失败须解释并处理，不能把规则改为例外、删除 Windows 场景或降低校验；填写模板不能替代实测。
- 原正式服务恢复，临时进程、注入和测试凭据已清理，Windows 默认后端未改变；用户确认验收结果后才合并 `main`。

下一位执行者先读本文件、`parity.json` 和 `parity-audit.md`，沿用本分支记录修复及结果。不重新初始化另一套清单，也不因 Mac 已通过而跳过 Windows 原生项。
