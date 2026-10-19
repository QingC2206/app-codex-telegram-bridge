# Local Telegram plugin API v1

[简体中文](plugins.zh-CN.md) | **English** · [Back to README](../README.en.md)

These plugins belong to Codex Telegram Bridge and are independent of the Codex app's plugin marketplace and SKILL.md files. They run as Python scripts and do not automatically call a model.

## Installation and discovery

The default plugin directory is `~/.codex/telegram-plugins` (`%USERPROFILE%\.codex\telegram-plugins` on Windows). With `CODEX_HOME`, it is that profile's `telegram-plugins/` directory. Each plugin occupies a directory whose name matches its manifest ID:

```text
telegram-plugins/
  hello/
    plugin.json
    main.py
    helpers.py       # Optional imports from the same directory
    template.md      # Optional resource read through ctx.plugin_dir
```

Prepare the complete directory outside the plugin root, then move it in. For updates, stage in a dot-prefixed directory before replacing the plugin directory. No bot restart is required. Each plugin command rescans available plugins; a background check synchronizes the Telegram menu every ten seconds. Client menus may refresh later, but typed commands work immediately.

`/plugins` shows loaded versions, commands, and errors. `/help` includes plugin commands. Set `enabled` to `false` or move the directory away to disable a plugin. Running invocations keep their original code snapshot; subsequent commands use the new version. Code or configuration changes invalidate old buttons. Incompatible APIs, syntax errors, or command conflicts disable only the affected plugins. Both parties to a plugin command conflict are disabled; core commands, including `/settings` and `/language`, cannot be overridden.

Bundled plugins are listed in `src/codex_telegram_bridge/resources/bundled_plugins.json`, for example:

```json
{"plugins":{"screenshot":["plugin.json","main.py"]},"retired":["markdown"]}
```

To bundle a plugin, add its directory under `resources/bundled_plugins/` and explicitly list each distributable file. The source package and wheel share this resource list; uninstall keeps user plugins. Do not include credentials, state, local configuration, or unrelated documentation in the list.

At startup, the bot installs missing bundled plugins and updates unmodified copies using their installation fingerprints. Previous code is backed up under `bundled-backups` in the plugin state directory. Locally modified or custom plugins with the same name are retained and reported by `/plugins`. Configuration and state remain separate. Plugins listed as `retired` are moved out of the loading directory and backed up; their commands are no longer registered.

## Minimal bilingual plugin

`plugin.json`:

```json
{
  "api_version": 1,
  "id": "hello",
  "version": "1.0.0",
  "entrypoint": "main.py",
  "enabled": true,
  "commands": {"hello": "显示问候信息"},
  "command_descriptions": {"en": {"hello": "Show a greeting"}},
  "timeout_seconds": 30,
  "cooldown_seconds": 0,
  "config": {}
}
```

`main.py`:

```python
from plugin_api import result, message, button

def handle(request, ctx):
    if request["kind"] == "command":
        return result(message(
            ctx.tr("你好！", "Hello!"),
            [[button(ctx.tr("下一页", "Next"), {"view": "next"})]],
        ))
    return result(message(ctx.tr("这是下一页。", "This is the next page.")))
```

Send `/hello` in Telegram. Use `ctx.tr` only for plugin-owned UI text. Escape filenames or titles before inserting them into Markdown, while preserving the body structure. Do not translate or rewrite user arguments or model replies.

## Requests and context

The entry point is `handle(request: dict, ctx: Context) -> dict`. Each invocation runs in a separate process. Persistent background loops or dependence on runtime state during import are unsupported.

Command request: `{"kind":"command","command":"hello","argument":"original arguments"}`.

Button request: `{"kind":"callback","action":{"view":"next"}}`. `action` is the JSON object supplied when creating the button.

| Field or method | Purpose |
| --- | --- |
| `ctx.plugin_dir` | Code and resource snapshot for this invocation; read resources here |
| `ctx.output_dir` | Temporary attachment directory; write output files here |
| `ctx.state_dir` | Persistent plugin directory for caches, databases, and other state |
| `ctx.config` | Configuration object described below |
| `ctx.language` | Selected UI language: `zh-CN` or `en`; Chinese by default |
| `ctx.tr(zh, en)` | Select plugin-owned Chinese or English text |
| `ctx.load_state()` | Read `state.json`; returns an empty object if absent |
| `ctx.save_state(value)` | Atomically replace `state.json`; invocations are serialized per plugin ID |

Configuration is read from `<CODEX_HOME>/telegram-plugin-state/<id>/config.json` when present, otherwise from the manifest's `config`. This is a full replacement, not a partial merge. State defaults to the same profile and is separate from code. Configuration is limited to 32 KB and changes take effect dynamically.

Plugins receive only the current command arguments or button action. The host does not pass the bot token, chat history, Codex session state, or arbitrary environment variables. Plugins do not need to know Telegram message IDs, authentication, or sending APIs.

These are **trusted local Python extensions**, not an operating-system security sandbox. Separate processes isolate crashes and timeouts, but code retains the desktop user's permissions. Install only plugins you wrote or reviewed. Never execute remote chat content as shell commands or run a separate `getUpdates` loop inside a plugin. Install optional dependencies in the bot's Python environment yourself; the host does not automatically install them.

## Language support and compatibility

`commands` remains the required default description map. Optional `command_descriptions` maps `zh-CN` and/or `en` to descriptions for existing commands. Each description must contain 1–100 characters. Missing translations fall back to `commands`; the host does not automatically translate third-party text.

The worker receives the selected language with its request. `ctx.language` and `ctx.tr` are additive API v1 fields; existing four-argument `Context` construction and plugins without translations remain supported. An invocation keeps the language selected when it started. Language changes do not alter callback action data, command arguments, or stored plugin state.

The bundled screenshot plugin provides both languages. To test a plugin, use `/language en`, invoke its command and buttons, then `/language zh` and repeat.

## Returned actions

All actions are validated for format and file paths before any are sent, then delivered in order. The original message gets 👌 only after successful delivery; 🤔 indicates execution. Exceptions, partial failures, or uncertain delivery do not mark success or trigger automatic retries.

```python
from plugin_api import result, message, document, photos, button

# Rich message with Markdown; separate sections with blank lines.
result(message("**Title**\n\nBody", [[button("Next", {"page": 2})]]))

# Original file; path must be inside ctx.output_dir.
result(document(ctx.output_dir / "report.md", caption="Original report"))

# Ordered album with actual pixel dimensions.
result(photos([
    {"path": str(ctx.output_dir / "1.png"), "width": 1920,
     "height": 1080, "caption": "First"},
    {"path": str(ctx.output_dir / "2.png"), "width": 1920,
     "height": 1080, "caption": "Second"}
]))

# Deliver partial results without marking the operation successful.
result(message("Some items could not be read."), ok=False)
```

Return 1–12 actions per invocation. Each message can contain 3500 characters, with at most 12 button rows and four buttons per row. For long text, use `codex_telegram_bridge.telegram.notifications.split_markdown_pages(text, limit=2700)` and put page references, not the full text, in button actions.

Button parameters are stored locally; Telegram receives only a random token. Buttons are bound to the chat, plugin revision, and panel, expire after 30 minutes, and are consumed once when clicked. Subsequent pages receive new buttons.

Command results reply to the original command. A callback's first message edits its own plugin panel; later messages and attachments reply to the original command. Plugins cannot edit arbitrary task messages. Attachments are limited to 49 MB each and 150 MB total. Photos also obey Telegram photo size and dimension limits. A `photos` action supports up to 20 images; the host splits more than ten into multiple albums. Failed photo uploads are not silently changed to file uploads.

Only one invocation of each plugin may run at a time, with at most three globally. Timeouts range from 1–180 seconds and terminate the invocation's process tree. Cooldown starts after processing, including delivery, finishes. Request deduplication is retained for seven days. Send timeouts do not resubmit operations. Failure details are stored in `last-error.txt` in the plugin state directory. Temporary output is removed after delivery.

## Retired and bundled plugins

The Markdown reader plugin was removed in v0.3.0. `/md` is no longer loaded after upgrade; original documents are not deleted. The API still supports generic file delivery.

`/screenshot` and legacy screenshot buttons remain supported. The plugin selects targets and returns photo actions; the host handles authentication, album upload, reply associations, reactions, concurrency, and cooldown. Platform adapters perform capture: Windows uses native window/display capture, macOS requires Screen Recording permission, and Linux uses visible X11 regions or Wayland portal confirmation. Locked or unavailable desktops produce explicit errors. See [installation](setup.en.md) for permissions.

## Extending the bot with Codex

Ask Codex to read this API and add a plugin directory, manifest, entry point, and isolated tests. Do not modify the main bot file or copy its token or polling loop into a plugin. Validate results using a test context and temporary output directory, then confirm loading with `/plugins`. New sending action types require a host API upgrade.
