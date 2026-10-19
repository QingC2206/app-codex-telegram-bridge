# 安全说明

本工具可以读取当前 Codex 环境中的任务信息，并接受授权 Telegram 用户对任务发送的指令。请使用自己管理的机器人和可信的聊天。

## 凭据与数据

- `telegram_notify.credentials.json` 包含 Windows DPAPI 加密值或系统钥匙串引用，以及聊天配置。即使 Token 已加密，也不要上传该文件。
- 不要公开 Codex 的 `auth.json`、个人 `config.toml`、会话文件或其他账户凭据。
- 日志、SQLite 数据库、消息关联、附件、完整回复缓存及 `.validation/` 报告可能包含聊天或任务内容，只应保存在本机。
- `.gitignore` 防止常见本地文件被新加入 Git；已经被跟踪的文件仍需从版本记录中处理，单独添加忽略规则不会清除历史内容。

## 权限边界

Windows 使用 DPAPI；macOS 使用 Keychain；Linux 使用 Secret Service。安全存储不可用时不回退到明文。服务以当前桌面用户身份运行。任务内容会发送到配置的 Telegram 聊天；截图按用户请求捕获窗口及显示器画面。

本地插件具有当前用户权限。请审核插件代码和配置，并在 [插件文档](docs/plugins.zh-CN.md) 描述的目录中管理它们。

Desktop 任务的提交和中止依赖其当前持有者。实例归属不明确或发送结果未知时，请先核对任务状态，避免重复执行指令。

## 报告问题

普通问题可以提交 [Issue](https://github.com/QingC2206/app-codex-telegram-bridge/issues)。附日志、截图或复现步骤前，隐藏 Token、Chat ID、私有会话内容及个人路径。

涉及凭据或可被利用的安全问题，请通过维护者提供的私下渠道沟通；如果仓库已启用 GitHub 私密漏洞报告，也可以使用该入口。不要在公开 Issue 中发布密钥或可利用的细节。

如果 Bot Token 已泄漏，应先通过 BotFather 轮换，再使用安装向导重新配置。
