# 本地 Telegram 插件接口 v1

本插件机制属于 Codex Telegram Robot，和 Codex 应用自身的插件市场、SKILL.md 互相独立。插件用纯 Python 执行，不会自动调用模型。

## 安装和动态发现

默认插件目录为 `%USERPROFILE%\.codex\telegram-plugins`。设置 CODEX_HOME 时改为 `%CODEX_HOME%\telegram-plugins`。每个插件占一个子目录，目录名必须和清单 id 一致：

```text
telegram-plugins/
  hello/
    plugin.json
    main.py
    helpers.py       # 可选，同目录导入
    template.md      # 可选，读取 ctx.plugin_dir 下资源
```

在外面准备完整目录，再移入插件目录；更新建议先放到以点开头的临时目录，再整体替换。无需重启机器人。每次插件命令会重新扫描，后台每 10 秒同步一次 Telegram 命令菜单。Telegram 客户端可能稍后才刷新菜单，直接输入命令无需等待菜单。

`/plugins` 显示已加载版本、命令和错误；`/help` 汇总当前插件命令。将清单的 enabled 设为 false 或移走目录即停用。运行中的任务继续使用启动时的代码快照，后续命令使用新版。代码或配置改变后，旧按钮失效，需重新发送命令。API 不兼容、语法错误或命令冲突只停用对应插件，不影响其他插件。命令冲突的双方都会停用，核心命令不能被覆盖。

随包插件由程序目录 `bundled_plugins.json` 管理，例如：

```json
{"plugins":{"screenshot":["plugin.json","main.py"]},"retired":["markdown"]}
```

发布其他插件时，在 bundled_plugins 下添加插件目录并把需要发布的文件逐项加入此清单；构建、安装、卸载共用清单。不要把文档、凭据、状态或本机配置加入发布清单。机器人启动时安装缺失插件，并根据安装指纹更新未修改的随包插件；升级前的代码备份到插件状态目录 bundled-backups。发现本地修改或同名自定义插件时保留本机版本，并在 `/plugins` 中提示。配置和状态在独立目录保留。retired 中的插件会移出加载目录并备份，不再注册其命令。

## 最小例子

plugin.json：

```json
{
  "api_version": 1,
  "id": "hello",
  "version": "1.0.0",
  "entrypoint": "main.py",
  "enabled": true,
  "commands": {"hello": "显示问候信息"},
  "timeout_seconds": 30,
  "cooldown_seconds": 0,
  "config": {"prefix": "你好"}
}
```

main.py：

```python
from plugin_api import result, message, button

def handle(request, ctx):
    if request["kind"] == "command":
        name = request.get("argument", "") or "朋友"
        return result(message(
            ctx.config.get("prefix", "你好") + "，" + name,
            [[button("再看一页", {"view": "next"})]],
        ))
    return result(message("这是按钮对应的下一页。"))
```

复制后在 Telegram 发送 `/hello 名字`。上面的示例字符串不应直接用于插入任意 Markdown 元数据；正式插件应对文件名、标题等文本转义，并保留正文的 Markdown 结构。

## 请求与上下文

入口固定为 `handle(request: dict, ctx: Context) -> dict`，每次调用独立进程；不支持常驻后台循环或 import 时依赖运行状态。

命令请求：`{"kind":"command","command":"hello","argument":"参数原文"}`。

按钮请求：`{"kind":"callback","action":{"view":"next"}}`，action 即插件创建按钮时提供的 JSON 对象。

上下文：

| 字段／方法 | 用途 |
|---|---|
| ctx.plugin_dir | 本次调用的代码、资源快照；只作为资源读取路径 |
| ctx.output_dir | 本次发送附件的临时目录；必须把输出文件写到这里 |
| ctx.state_dir | 此插件的持久目录，可保存缓存、数据库等 |
| ctx.config | 配置对象，见下文 |
| ctx.load_state() | 读取 state.json；未创建时返回空对象 |
| ctx.save_state(value) | 原子替换 state.json；插件按 id 串行执行 |

配置优先读取 `%CODEX_HOME%\telegram-plugin-state\<id>\config.json`，不存在时使用清单 config。该配置是完整替换，不是部分合并。状态默认路径同样在用户 `.codex` 下，和代码目录分开。单份配置限 32 KB。配置更改动态生效。

插件只能接收当前命令参数或按钮数据，机器人不会向插件传递 Bot Token、聊天历史、Codex 会话状态或任意环境变量。插件不必了解 Telegram 消息 ID、鉴权或发送 API。

这是**可信本地 Python 扩展**，不是系统权限沙箱：独立进程隔离崩溃与超时，但 Python 代码仍拥有当前 Windows 用户的权限。只安装自己编写或审核过的插件。不要将远程聊天内容作为 shell 命令执行，不要在插件中自行调用 getUpdates。插件如需第三方库，应由用户在机器人所使用的 Python 环境中安装，宿主不会自动安装依赖。

## 返回动作

所有动作先完成格式和文件路径校验，再按顺序发送；成功送达后才把原消息表情改为 👌。执行中为 🤔；插件异常、部分失败或发送状态不明确时不会标记成功，也不会自动重试。

```python
from plugin_api import result, message, document, photos, button

# 富消息；标题/字段用空行分段，正文保留 Markdown。
result(message("**标题**\n\n正文", [[button("下一页", {"page": 2})]]))

# 原文件；path 必须在 ctx.output_dir 内。
result(document(ctx.output_dir / "报告.md", caption="报告原文件"))

# 多张图片作为相册；每项提供实际像素尺寸，顺序即发送顺序。
result(photos([
    {"path": str(ctx.output_dir / "1.png"), "width": 1920,
     "height": 1080, "caption": "第一张"},
    {"path": str(ctx.output_dir / "2.png"), "width": 1920,
     "height": 1080, "caption": "第二张"}
]))

# 部分结果可送达，但不要标记成功。
result(message("部分项目无法读取。"), ok=False)
```

一次返回 1–12 个动作；单消息 3500 字符，最多 12 行按钮，每行 4 个。长文可使用 `telegram_notify.split_markdown_pages(text, limit=2700)` 按页返回，在按钮 action 中放页码等引用，勿放整篇正文。按钮参数由宿主在本机保存，Telegram 仅收到随机令牌；按钮绑定聊天、插件版本和面板，30 分钟到期，点击后一次性消费。后续页面会生成新按钮。

命令结果引用原 Telegram 命令；按钮返回的第一条消息编辑插件自己的面板，后续消息及附件引用原命令。插件不能编辑任意任务消息。附件单个最多 49 MB，总计 150 MB；照片还受 Telegram 照片尺寸和大小限制。一次 photos 最多 20 张，超过 10 张由宿主拆为多组。原图上传不改为文件兜底。

同一插件一次只运行一个调用，全局最多 3 个。超时可设 1–180 秒，超时终止调用进程树。冷却时间从本次处理（包括发送）结束后计算。消息请求去重保存 7 天；发送超时不重新提交，以免重复发送或重复执行有副作用的插件。失败详情保存在插件状态目录 `last-error.txt`。临时输出发送完后清理。

## 已移除的插件

Markdown 阅读插件在 v0.3.0 移除。升级后 `/md` 不再加载；原文档不会删除。插件接口仍支持通用文件发送。

## 内置截图插件

`/screenshot` 不变，旧截图按钮继续兼容。截图算法仍在 screen_capture.py，插件只编排采集目标并返回照片动作；身份验证、相册上传、原消息引用、表情、并发和冷却统一由插件宿主管理。Codex 被遮挡时独立捕获，最小化时短暂恢复到后台并还原；每个显示器单独一张。锁屏等无法采集的情况明确返回提示。

## 给 Codex 扩展功能

可以让 Codex 读取此接口说明，在固定插件目录新增子目录，编写清单、入口和独立回归测试。不要修改机器人主文件，也不要在插件内复制 Bot Token 或轮询逻辑。用测试上下文和临时输出目录先验证返回数据，再通过 /plugins 核实加载结果。增加新的发送类型时才需要升级宿主 API 版本。
