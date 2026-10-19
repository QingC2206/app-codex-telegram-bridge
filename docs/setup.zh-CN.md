# 使用 BotFather 创建 Telegram Robot 并获取 Token

整个过程通常只需要几分钟。

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

解压 Codex Telegram Bot 安装包，双击：

```text
python install.py
```

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

## Token 泄露或丢失怎么办

重新打开 [@BotFather](https://t.me/BotFather)，发送 `/token`，选择对应机器人并生成新的 Token，然后重新运行：

```powershell
python -m codex_telegram_bridge setup
```

旧 Token 不应继续使用。

## 官方资料

- [Telegram：BotFather 与创建机器人](https://core.telegram.org/bots/features#botfather)
- [Telegram：获取 Bot Token](https://core.telegram.org/bots/tutorial#obtain-your-bot-token)


新增菜单命令：`screenshot - 一次回传 Codex 和各显示器截图相册`。机器人启动时会自动同步。
