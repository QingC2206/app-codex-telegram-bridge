# 安装与 Telegram 配置

**简体中文** | [English](setup.en.md) · [返回 README](../README.md)

## 环境要求

需要已登录并运行的 Codex Desktop、64 位 Python 3.10+（推荐 3.11 或更新版本），以及 Telegram Bot API 与首次安装时的 PyPI 网络访问。

macOS 需要支持 `venv` 的 Python 和可用 Keychain，截图需录屏授权。Linux 需要 Python venv 支持、解锁的 Secret Service 密钥环、systemd 用户会话和桌面环境；Wayland 截图需 Portal 确认。

仅连接 Desktop 及其配套后端，不回退到 VS Code 或独立 CLI。Windows 已完成实机通信验收；macOS/Linux 仍需对应设备验收，按实验性支持发布。多台电脑同时运行时各用一个 Bot Token。

## 1. 打开官方 BotFather

在 Telegram 中打开 [@BotFather](https://t.me/BotFather)，确认用户名准确无误，然后点击 **Start**。

BotFather 是 Telegram 官方用于创建和管理机器人的机器人。

## 2. 创建机器人

向 BotFather 发送：

```text
/newbot
```

BotFather 会依次要求填写两项内容。

### 机器人名称

这是聊天列表中显示的名称，可以使用中文。例如：

```text
我的 Codex 通知助手
```

### 机器人用户名

这是机器人的唯一用户名，需要满足以下要求：

- 长度为 5–32 个字符
- 只能使用英文字母、数字和下划线
- 必须以 `bot` 结尾

例如：

```text
my_codex_notify_bot
```

如果用户名已被使用，换一个名字后重新发送即可。

## 3. 复制 Bot Token

创建成功后，BotFather 会发送一段 Token，格式类似：

```text
<一串数字>:<一串随机字符>
```

完整复制这段 Token，稍后安装 Codex Telegram Bot 时需要输入。

Token 相当于机器人的密码。任何拿到 Token 的人都可以控制机器人，因此不要：

- 把 Token 发给其他人
- 把 Token 放进截图、聊天记录或公开 Issue
- 把 Token 写进 Git 仓库或公开配置文件

## 4. 给新机器人发送 `/start`

点击 BotFather 提供的机器人链接，打开刚创建的机器人，然后点击 **Start**，或者发送：

```text
/start
```

这一步很重要：Telegram 机器人不能主动与从未联系过它的用户开始对话。安装向导也需要通过这条消息识别你的 Chat ID。

## 5. 运行安装向导

克隆仓库，或解压源码安装包后在目录中打开终端：

```text
git clone https://github.com/QingC2206/app-codex-telegram-bridge.git
cd app-codex-telegram-bridge
```

Windows 执行 `py -3 install.py`；macOS/Linux 执行 `python3 install.py`。默认中文；希望安装向导和 Telegram 从一开始使用英文时，在命令末尾加 `--language en`。升级时不传该参数会保留现有选择。

安装器在所选 Codex 环境下创建 `telegram-bridge-runtime/`，安装 Python 包，配置安全凭据、通知 Hook 和当前用户后台服务，不改全局 Python。安装失败时不要改用管理员/root 用户重跑。

根据提示完成以下操作：

1. 粘贴刚才复制的 Bot Token。输入时窗口不会显示字符，这是正常现象。
2. 如果检测到多个聊天，选择接收 Codex 通知的聊天。
3. 选择是否显示回复预览、接收子代理通知以及是否静默发送。
4. 等待 Telegram 中出现测试成功消息。
5. 完全退出并重新打开 Codex Desktop。

安装完成后，可以在 Telegram 中发送：

```text
/help
```

查看机器人支持的全部 Codex 指令。

发送 `/language` 可通过按钮选择中文或英文；`/language en` 切换英文，`/language zh` 切回中文。选择会保留到下次启动和升级。

## 自定义环境与服务管理

```text
python install.py --codex-home <Codex环境绝对路径> --data-dir <机器人数据绝对路径>
```

新数据默认位于 `<CODEX_HOME>/telegram-bridge/`；唯一旧凭据目录可原位复用，保留消息关联。多个候选目录时须指定 `--data-dir`。`--no-service` 仅配置 Hook、不安装服务；`--replace-notify` 用于明确替换已有通知命令。

Windows 使用当前用户 DPAPI，macOS 使用 Keychain，Linux 使用 Secret Service；密钥环缺失或锁定时不降级为明文。不要把 Windows 加密凭据复制到其他系统，在新设备重新配置。

管理命令使用安装器创建的 Python。macOS/Linux 示例：

```sh
~/.codex/telegram-bridge-runtime/bin/python -m codex_telegram_bridge doctor
~/.codex/telegram-bridge-runtime/bin/python -m codex_telegram_bridge service status
~/.codex/telegram-bridge-runtime/bin/python -m codex_telegram_bridge language en
```

Windows 示例：

```powershell
& "$env:USERPROFILE\.codex\telegram-bridge-runtime\Scripts\python.exe" -m codex_telegram_bridge doctor
```

可将 `doctor` 替换为 `setup`、`language zh`、`language en`、`service start`、`service stop` 或 `uninstall`。自定义环境的 `--codex-home` / `--data-dir` 放在子命令之前。本机 CLI 修改语言后，发送 Telegram `/language` 或重启服务刷新命令菜单。

## 更新与卸载

执行 `git pull --ff-only` 后重新运行本平台安装命令。现有凭据、消息关联和语言偏好会保留。同一 Token 不应交给多个运行中的轮询服务。

使用安装器的 Python 执行 `python -m codex_telegram_bridge uninstall`，只移除本工具管理的服务和 Hook，保留凭据、历史关联、插件、备份、语言偏好和虚拟环境。

## Token 泄露或丢失怎么办

重新打开 [@BotFather](https://t.me/BotFather)，撤销泄露 Token 并生成新 Token，然后使用上面的安装器 Python 运行配置命令：

```powershell
python -m codex_telegram_bridge setup
```

旧 Token 不应继续使用。

## 官方资料

- [Telegram：BotFather 与创建机器人](https://core.telegram.org/bots/features#botfather)
- [Telegram：获取 Bot Token](https://core.telegram.org/bots/tutorial#obtain-your-bot-token)


继续阅读 [使用指南](usage.zh-CN.md)、[插件 API](plugins.zh-CN.md) 和 [实机验收](testing.zh-CN.md)。命令菜单在服务启动和语言切换时同步。
