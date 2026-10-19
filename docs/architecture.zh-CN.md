# 架构与兼容边界

**简体中文** | [English](architecture.en.md) · [返回 README](../README.md)

主程序、安装、卸载、测试、发布使用同一 Python 包。`platforms/` 内封装系统能力，业务代码复用消息类型、投递日志、任务归属和权限判断。

## 消息流

```text
Telegram 更新 → 授权聊天/发送者校验 → 命令或回复路由
                                      ↓
                          Desktop 身份与环境核验
                                      ↓
                       当前持有者的用户输入通道
                                      ↓
                         投递记录与用户消息回执
                                      ↓
                        Hook / 任务事件 → Telegram
```

App Tools 仅用于查询。Telegram 人类输入使用 follower IPC 的 start/steer 机制，不能替换为 `send_message_to_thread` 代理委派。原任务没有 Desktop 持有者时拒绝投递；仅 `/new` 创建的机器人任务由机器人自己的 Desktop 配套后端管理。

通知过滤依据消息类型及可确认的内部来源；包含 `suggestion` 的正常 JSON 内容不构成过滤理由。异步提问绑定 task、turn、item 和问题索引，提交先记账，超时或结果不明时不自动重发。

Desktop 用户消息请求的默认等待为 **10 秒**。若返回超时或提交结果未知，Robot 再进行最多 **10 秒**的本地回执核对：只认可本次提交后、同一任务内内容匹配的用户消息及明确的消息/回合 ID；追加到指定回合时还要求回合一致。核对成功按已接收处理，否则提示“提交结果未知”，保留待核对记录且不自动重发。Desktop 自身的内部超时可能更短，此设置不会修改 Desktop 程序。

## 系统边界

| 系统能力 | Windows | macOS | Linux |
| --- | --- | --- | --- |
| Desktop 身份 | Store 程序路径、进程、环境 | App Bundle 标识与 Electron 元数据 | Electron 包元数据及当前用户 |
| 本地通信 | 命名管道；服务端 PID 校验 | Unix socket；文件属主、权限、peer PID/UID | Unix socket；文件属主、权限、SO_PEERCRED |
| Token | 现有 DPAPI 格式 | Keychain | Secret Service |
| 后台启动 | 当前用户任务计划 | Aqua 会话 LaunchAgent | systemd 用户服务 |
| 进程收尾 | Windows Job Object | 独立进程组、父进程监护 | 独立进程组、父进程监护及服务 cgroup |
| 截图 | 原生窗口及显示器 | 屏幕录制授权、窗口及显示器 | X11 可见区域；Wayland Portal 确认 |

只选择能证明属于 Desktop 的后端，拒绝 VS Code、PATH CLI 和不匹配的 `CODEX_CLI_PATH`。同环境多实例、跨环境同任务 ID、无法读取实例环境、端点身份变化均停止提交。

macOS 支持安装包内 `Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex` 的嵌套后端布局，并优先使用已验证 Desktop 的配套运行进程。

Unix follower 端点按所选环境的 `ipc/ipc.sock` 发现，兼容旧版当前用户临时目录端点。App Tools 使用 Desktop/配套子进程公布的 `codex-app-tools/<id>.sock` 或兼容 browser-use 地址。协议是四字节小端长度加 JSON。传输、查询和业务路由分别封装；不是把所有 Unix socket 都当成同一种协议。

这些 Desktop 本地接口属于版本相关实现，不宣称是稳定公开 API。macOS 的本地 peer 授权、系统录屏权限或密钥环可能拒绝访问；程序报告失败并停止，不关闭权限检查。`doctor` 失败时附脱敏错误及 Desktop 版本，不能仅凭 CI 结果判定兼容。

## 配置与资源

- `paths.py` 统一处理 `CODEX_HOME` 和机器人数据目录，避免依赖 Python 文件所在位置。
- 新数据目录默认 `<CODEX_HOME>/telegram-bridge/`，唯一旧凭据目录可原位复用，多个候选时必须指定。
- 源码和 wheel 使用同一份 `resources/bundled_plugins/`，没有两个需要同步的插件副本。
- 插件工作进程保留 v1 的 `plugin_api` 导入；它是可信本地代码扩展，不是权限沙箱。
- 系统服务绑定绝对 Python 路径、Codex 环境和数据目录；本机运行数据不会进入 wheel、源码 ZIP 或 Git。
- `i18n.py` 只通过 `resources/messages.en.json` 翻译显式标记的界面模板，参数原样插入，不扫描或翻译整条待发送正文。
- 语言默认 `zh-CN`，单独保存在机器人数据目录。通知进程读取同一偏好，插件工作进程接收明确语言；按钮路由和用户输入不依赖翻译后的标签。

## 模块迁移

`app.py` 保留原有任务交互编排，以维持既有命令行为；IPC、平台发现、凭据、状态和自测已分离。后续拆分业务类应按功能进行，并沿用回归测试，避免为缩短文件而复制同一份业务状态。
