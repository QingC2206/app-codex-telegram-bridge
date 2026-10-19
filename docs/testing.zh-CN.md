# 测试与实机验收

## 自动检查

```text
python -m pip install -e ".[dev]"
python tools/run_tests.py --suite Full
python -m build
python tools/check_distribution.py
```

测试使用临时 Codex 环境和机器人数据目录。原有业务场景继续覆盖回复、消息来源、内部建议过滤、任务查询、异步提问、额度、图片、表格、插件及安全边界。新增测试覆盖安装配置保留/回滚、旧数据发现、凭据不可用时拒绝明文回退、服务参数转义、单实例互斥、Desktop 包来源和 Unix 真实 socket 分帧及 peer 校验。

GitHub Actions 在 Windows、Ubuntu、macOS 运行 Python 3.10/3.11/3.13；原生平台测试仅在对应系统执行。没有 Desktop 或 Telegram 账号的 CI 不执行真实任务投递。

## 当前电脑只读检查

0.4.0 发布验收：Windows 上完成真实任务投递、同一 Turn 的用户来源回执、Telegram 发送，以及独立 wheel 安装/随包插件检查。三系统 CI 覆盖 426 项测试，各系统跳过不适用的原生测试。macOS/Linux 的真实 Desktop 连接、密钥环、服务启动和截图仍需对应电脑验收；手机发送 `/send` 和直接回复没有由 CI 模拟为实测通过。

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
8. 重启机器人，确认凭据、消息关联和任务回复能力仍在。存在隔离订阅时确认不串环境。

若出现端点发现或 peer 授权错误，保留原服务状态并报告脱敏错误、系统和 Desktop 版本。不要更改沙盒、关闭系统校验、将另一客户端的任务恢复到独立后端，或反复发送同一条不确定的命令。

## 发布

```text
python tools/build_release.py --thread-id <允许验收的活动任务完整ID>
```

发布门禁核验真实投递、用户消息回执、Telegram 发送、全部离线测试，以及验收前后源码哈希。只发布 `dist/*.zip` 和对应校验和；`.validation/` 和 `*.validation.json` 留在本机。wheel/sdist 可独立构建用于 CI 和安装验证，不以构建成功冒充通信验收。
