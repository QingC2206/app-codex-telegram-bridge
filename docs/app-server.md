# 配置 app-server 运行时

Windows 默认入口与显式配置的 app-server 入口使用同一个公共控制器。命令、回复、提问、审批、表情、静默、侧聊、额度和渲染不再各维护一套。官方 JSONL 连接也复用同一个客户端。

原 Desktop 任务仍经其持有者的用户输入通道继续，任务 ID 与上下文保持不变。仅明确 `/new` 创建的机器人任务可由独立 app-server 恢复。不会通过新建任务、代理委派或签名绕过处理原对话。

## 安装与配置

要求 Python 3.10+，已登录的 Codex 和已配置的 Telegram。可在 `llm-loc` 环境安装，不需要独立 Python 环境。

```sh
conda activate llm-loc
python -m pip install .
python -m codex_telegram_bridge configure-app-server \
  --codex /Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex \
  --cwd "$HOME/workspaces/telegram-codex"
python -m codex_telegram_bridge doctor
python -m codex_telegram_bridge install
python -m codex_telegram_bridge service status
```

目录必须已存在；可执行文件路径以实际安装为准。服务保存绝对 Python、profile 和数据路径。Token 留在系统凭据存储，SSL 校验始终开启。

安装器合并 `Stop`、`SubagentStop` 和匹配补充问题的 `PreToolUse`，保留已有 `notify` 与其他处理器。Hook 信任由 Codex 管理，安装器不修改信任记录。macOS 用户服务运行于 Aqua 会话，钥匙串授权在系统窗口完成。

## 一致的操作规则

- 回复任务消息继续原任务；运行中提供追加、中断后发送、侧边聊天和取消。
- 无任务关联的普通文字要求选择目标，不隐式使用历史选择，也不自动新建。
- `/cwd` 仅更改之后新任务的默认目录；只有 `/new` 创建任务。
- `/tasks`、`/running`、`/task`、`/watch`、`/diff`、`/usage`、`/quota` 使用公共查询和卡片。
- `/settings` 包含语言、完成通知、子代理和静默；配置运行时另有 `/model`、`/effort`。
- `/side` 创建只读侧聊。通用审批、简单 MCP 表单、异步提问和截图入口均在本轮范围内。
- `/file 任务ID 相对路径` 下载工作目录内不超过 20 MiB 的文件，拒绝越界及指向外部的符号链接。
- 私聊核验本人；群聊须显式配置 `allowed_user_ids`。

原消息表情为 👀 收到、🤔 处理、🎉 结果已投递、😢 明确失败。任务接收不等于完成。提交结果未知保持未确认，不自动重复任务。Telegram 明确限流仅重试输出；网络投递未知不盲目重发。

## 平台差异与旧数据

Windows 保留原 App Tools 实时查询和命名管道身份校验。macOS 使用已验证 PID/UID/profile 的 follower 只读快照；严格 Turn 操作检查新鲜快照，未知协议或状态拒绝操作。这里的 Desktop 本地协议依赖版本，不承诺为稳定公开 API。

macOS 未加载的原任务可能不回答持有者查询。只有在尚未发送用户输入时，适配器才允许打开已核验的原任务，并在总时限内用短查询等待窗口注册；窗口只打开一次。Windows 不启用这条超时恢复策略。签名、实例、profile 检查失败和提交结果未知都不会触发重发。

升级只把旧任务归属、偏好、消费游标、消息关联和完整答复迁入公共状态。旧数据库、草稿、选择和回执保留；已有任务不重放。`ux` 按钮转换到公共回调。旧“另建任务”按钮只能下载原草稿，不能改变目标或执行输入。

完整清单见 [parity.json](parity.json)，旧测试迁移见 [test-migration.json](test-migration.json)。运行：

```sh
python tools/run_tests.py --suite Full --report docs/parity-tests.json
python tools/audit_parity.py --record-final-review REVIEW_ID --reviewer REVIEWER
python tools/audit_parity.py --require-reviewed --html /tmp/codex-parity.html
python tools/audit_parity.py --require-complete
```

先实际审读各规则和公共层归属，再用实际复检编号与审核者替换 `REVIEW_ID`、`REVIEWER`。记录复检不会把待验规则变成完成。`--require-reviewed` 检查具体断言映射、本轮源码及回归指纹；`--require-complete` 另检查对应版本的 Telegram 界面证据文件内容。未解锁、未操作界面的场景必须保留待验收。Windows 实机按用户约定另行验收；具体第三方插件扩展例外不包含通用能力。详见 [逐规则验收](parity-audit.md)。
