# App Codex Telegram Bridge

[简体中文](README.md) | **English**

Control Codex Desktop from Telegram — notifications, task management, and messaging.

Receive replies, continue existing tasks, answer questions, inspect running tasks, and request screenshots from Telegram. **0.6.7 · Python 3.10+ · Windows / macOS / Linux · MIT**.

Documentation and the Telegram interface support Chinese and English, with Chinese as the default. Open `/settings` → **Language** to choose with buttons, or `/language en` / `/language zh` to switch directly. The choice survives restarts and upgrades. Task content, model replies, and user input are not automatically translated.

[Installation](docs/setup.en.md) · [Usage](docs/usage.en.md) · [Plugin API](docs/plugins.en.md) · [Documentation index](docs/README.en.md)

The bridge connects only to **Codex Desktop** and its bundled backend. It never falls back to the VS Code extension or a standalone Codex CLI found on `PATH`. Messages for existing Desktop tasks go through the Desktop instance that owns them; the bot does not take over those tasks through a separate backend.

## Platform support and verification

| Platform | Implementation | Verification on a real desktop |
| --- | --- | --- |
| Windows | Named pipes, DPAPI, Task Scheduler, native window and screen capture | Notifications, mobile direct replies, `/send`, question-card answers, and user-origin receipts verified |
| macOS | Unix sockets, Keychain, LaunchAgent, window and screen capture | Automated tests covered; real Desktop/Telegram communication awaits verification on a Mac |
| Linux | Unix sockets, Secret Service, systemd user service, X11/Wayland capture | Automated tests covered; real Desktop/Telegram communication awaits verification on a Linux desktop |

CI runs Python 3.10, 3.11, and 3.13 on all three systems, covering application regressions, installation configuration, and native process and connection tests. **Passing CI does not prove real Desktop or mobile communication.** macOS and Linux support remains experimental until verified on those systems.

Desktop's local interfaces may change between versions. If connection verification fails, submission stops. The bridge does not switch clients or bypass operating-system process or screenshot permissions. See [architecture and compatibility boundaries](docs/architecture.en.md) and [verification steps](docs/testing.en.md).

Desktop user-input requests default to a **10-second** timeout. On a timeout or unknown submission outcome, the bot spends up to **10 more seconds** checking local receipts. Confirmation requires a matching user message recorded after this submission in the same task, with explicit message and turn IDs; an expected target turn must also match. A verified receipt counts as accepted. Otherwise the outcome stays unknown, the journal is retained, and nothing is automatically resent. Desktop may have its own shorter internal timeout; this setting does not modify Desktop itself.

## Features

- **Notifications and two-way messaging:** completion notifications, full replies, Markdown and tables, pagination, and downloads. Reply to a task card or use `/send` to continue the original task.
- **Task management:** `/running` checks live task status. Browse tasks, details, progress, diffs, and token usage; append instructions or stop a turn when supported.
- **Questions and approvals:** options, text answers, asynchronous questions, and compatible approval requests. Expired or answered buttons are invalidated; submissions with an unknown outcome are not automatically retried.
- **Attachments and plugins:** upload images and files, display local images, use the bundled `/screenshot` plugin, or add custom Python plugins.
- **Side chats and quota:** read-only side questions, quota information, low-quota alerts, and reset-card operations supported by the account and interface.
- **Background service:** starts with the current user's desktop session through Windows Task Scheduler, a macOS LaunchAgent, or a Linux systemd user service.

## Installation

You need a running, signed-in Codex Desktop, 64-bit Python 3.10+, and access to the Telegram Bot API. Python 3.11 or later is recommended.

1. Open the official [@BotFather](https://t.me/BotFather) in Telegram and send `/newbot`. Choose a name and a username ending in `bot`, save the token privately, then open your new bot and send `/start`. The installer will ask for the token and identify the receiving chat. See the [detailed setup guide](docs/setup.en.md).
2. Clone the repository:

```text
git clone https://github.com/QingC2206/app-codex-telegram-bridge.git
cd app-codex-telegram-bridge
```

3. On Windows, run:

```powershell
py -3 install.py
```

On macOS or Linux, run:

```sh
python3 install.py
```

The installer creates a private Python virtual environment at `telegram-bridge-runtime/` inside the selected Codex home. It installs dependencies and configures Telegram credentials, notification hooks, and the user service. It does not modify your global Python installation or require a PowerShell installation script. Installation failures return an error; do not retry as administrator or root.

4. Fully quit and reopen Codex Desktop, review the new hooks when prompted, then send `/help` or `/running` in Telegram.

Add `--language en` to the installation command for English setup prompts and the initial interface. Without it, new installations default to Chinese and upgrades preserve the current choice.

The first installation downloads dependencies from PyPI. On macOS, use a Python installation that supports `venv`. Linux requires your distribution's Python venv support, a Secret Service keyring, and a systemd user session. Headless servers are outside the scope of this Desktop bridge.

### Platform permissions

- **Windows:** credentials use the current user's DPAPI protection. The background service does not require administrator privileges.
- **macOS:** the first Keychain access may request permission. For screenshots, grant Screen Recording permission to the Python executable that actually runs the bot. Missing permission produces an explicit error.
- **Linux:** the Secret Service keyring should be unlocked after login. A missing or locked keyring does not trigger a plaintext fallback. X11 window capture includes the visible region and may contain overlapping windows. Wayland uses the desktop screenshot portal and requires confirmation at the computer; unattended per-window capture is not guaranteed.

### Isolated profiles and custom directories

Use `CODEX_HOME` or `--codex-home` to select the Desktop profile. The bridge still verifies that the running Desktop instance belongs to that profile.

```text
python install.py --codex-home <absolute-codex-home> --data-dir <absolute-bot-data-directory>
```

Use `--no-service` to configure hooks without installing a background service, for manual startup. If another notification command already exists, installation stops; use `--replace-notify` only when you intend to replace it.

## Updating, managing, and uninstalling

```text
git pull --ff-only
```

Then rerun `install.py` with the command for your platform. If exactly one legacy credential directory is found, the bridge keeps using its credentials, state, reply associations, and attachments. If multiple legacy directories are found, specify `--data-dir` explicitly. Do not copy Windows DPAPI credentials to another operating system; run the setup wizard on the new computer.

Use a separate Telegram bot on each computer when testing multiple machines. Do not give the same bot token to multiple running long-polling services. This also prevents Mac test messages from reaching Windows tasks.

Run management commands with the Python executable created by the installer. Examples for macOS and Linux:

```sh
~/.codex/telegram-bridge-runtime/bin/python -m codex_telegram_bridge doctor
~/.codex/telegram-bridge-runtime/bin/python -m codex_telegram_bridge service status
~/.codex/telegram-bridge-runtime/bin/python -m codex_telegram_bridge run
~/.codex/telegram-bridge-runtime/bin/python -m codex_telegram_bridge uninstall
```

Windows example:

```powershell
& "$env:USERPROFILE\.codex\telegram-bridge-runtime\Scripts\python.exe" -m codex_telegram_bridge doctor
```

Other subcommands include `setup`, `service install/start/stop/status/uninstall`, and `doctor --offline`. You can also run `python -m pip install .` in a virtual environment you manage yourself, then use the `codex-telegram-bridge` command.

For a custom profile, continue passing `--codex-home` and/or `--data-dir` before the subcommand. **Uninstall removes only the services and hooks managed by this tool; credentials, history, plugins, and backups remain.** The private Python virtual environment also remains and can be removed manually after verifying its path.

## Common commands

| Command | Purpose |
| --- | --- |
| `/quota` | View remaining quota, alerts, and reset cards |
| `/tasks` | Browse recent tasks |
| `/running` | View running tasks |
| `/task [ID]` | View task details |
| `/watch [ID]` | Track task progress |
| `/diff [ID]` | View file changes |
| `/usage [ID]` | View task usage |
| `/help` | View command help |
| `/settings` | Adjust quiet mode, notifications, language, and the new task folder |
| `/logs` | View service logs |
| `/side [ID] <question>` | Ask a side question |
| `/send [ID] [message]` | Send a task message |
| `/steer [ID] [message]` | Append to a running task |
| `/new <request>` | Create a new task |
| `/stop [ID]` | Stop a running task |
| `/plugins` | [Plugin] View plugin status |
| `/screenshot` | [Plugin] Capture windows and screens (bundled plugin) |

The menu groups frequent queries first, followed by help and settings, logs, messaging and execution commands, then plugins. Quota is always first. Plugin status and plugin commands stay at the bottom, with a `[Plugin]` description prefix. Telegram displays up to 100 commands; omitted plugin commands can still be typed directly.

`/settings` brings together completion notifications (previously mute), subagent notifications, interface language, and the new task folder. Opening settings changes nothing. Enable/disable buttons set explicit states, so repeated clicks do not toggle them back. Use `/settings cwd ABSOLUTE_PATH` to change the folder, including paths with spaces; it applies only to newly created tasks.

`/language [zh|en]`, `/mute`, `/unmute`, `/subagents [on|off]`, `/cwd [absolute-path]`, and existing message buttons remain supported, but the preference commands no longer occupy separate menu entries. `/help` only displays help. Use the Cancel button on the original prompt to cancel pending input.

Most commands that accept a task ID let you omit it and select a task using buttons. You can attach an image or file when replying to a task card. The limit is 20 MB per attachment; send multiple attachments separately.

## Quiet mode

Open `/settings` → **🌙 Quiet mode** → **Enable quiet mode**. It is off by default, and the choice survives restarts and upgrades.

- Keep quota, task lists, running tasks, task details, diffs, usage, logs, plugin status, and settings available. Explicitly viewing historical replies remains supported.
- Disable text replies, attachments, `/send`, `/steer`, `/new`, `/side`, `/stop`, `/watch`, question/approval answers, plugin execution, and quota resets. Existing buttons follow the same restrictions.
- Pause Codex replies, questions, approvals, progress, and other automatic messages. Existing tasks keep running without automatic interruption or approval. Enabling quiet mode cancels progress tracking and unexecuted quota reservations.
- Disabling quiet mode does not replay suppressed replies or restore cancelled tracking/reservations. Requests submitted before enabling it cannot be recalled.

Quiet mode is independent of completion notification preferences: it limits the bot to queries and settings without overwriting those preferences. It is also separate from Telegram's sound/mute setting.

## Task ownership and message delivery

- The bridge verifies the Desktop executable's origin, user, profile, and the server process behind the local connection. It rejects ambiguous profiles or unverifiable connection identities.
- Existing external tasks continue only through their current Desktop owner. If the owner cannot be found, open the task in the correct Desktop instance first.
- Bot-managed tasks created with `/new` use a separate App Server bundled with Desktop. This backend does not automatically resume or take over existing Desktop tasks.
- Human messages from Telegram use the user-input channel and retain their user origin. Agent-to-agent delegation tools are not used to impersonate user input.
- A JSON response containing `suggestion` is not filtered merely because of that field. Prediction suggestions are identified using the record type and explicit internal-source evidence.
- An unknown submission outcome does not trigger automatic rerouting or resending. Check the task's latest messages before retrying manually.
- `/running` verifies current status and explicitly reports timeouts, pagination limits, or records it cannot verify.

## Quota and reset cards

`/quota` shows account quota and provides weekly-quota alerts and reset-card controls. A reservation can last 6, 12, or 24 hours and is bound to an account, quota cycle, and specific card. It attempts redemption only when the remaining weekly quota is strictly below 3%. An immediate reset requires remaining weekly quota of 5% or less and a second confirmation. Expiration, quota recovery, a cycle or account change, or a service restart invalidates the reservation. Redemption with an unknown outcome is not automatically retried, and the bridge does not switch to another card.

## Data and privacy

New installations store runtime data in `CODEX_HOME/telegram-bridge/` by default. Existing installations keep their original data directory. Credentials, logs, and chat caches are not stored in the Python package directory.

Windows configuration stores DPAPI-encrypted values. macOS and Linux configuration stores references to the system credential store, where the token is held. Configuration files still contain chat information and should not be published. Task replies, explicitly requested screenshots, and attachments are sent to Telegram. Plugins run with the current user's permissions.

The language preference is stored separately in `telegram_language.json` in the bot data directory. It belongs to the current bot configuration; authorized group members share one language. Old messages are not rewritten in bulk; refresh cards for the new language. Third-party plugins can use `ctx.language` / `ctx.tr()` and `command_descriptions` to provide translations, otherwise their original text is retained.

Full-reply caches are kept for seven days by default. Downloaded attachments are not automatically deleted with the reply cache. Logs, databases, screenshots, and acceptance reports are excluded from Git and release packages. See the [security policy (Chinese)](SECURITY.md).

## Development and testing

```text
python -m pip install -e ".[dev]"
python tools/run_tests.py --suite Full
python -m build
python tools/check_distribution.py
```

Tests use temporary environments and synthetic credentials. They do not connect to real Telegram accounts or submit messages to Desktop tasks. Available test groups include `Communication`, `Notifications`, `Quota`, `Interactions`, `Screenshots`, `Plugins`, `SideChat`, `Security`, `Images`, `Feedback`, and `Platforms`.

`python -m build` creates development wheel and source distributions; it does not certify real communication. Building a release ZIP uses a separate gate that submits one test input to an explicitly selected active task, verifies the corresponding user-message receipt, and checks Telegram delivery:

```text
python tools/build_release.py --thread-id <full-active-task-id-authorized-for-testing>
```

Pass `--credentials` if credential selection is ambiguous, and `--codex-home` for a custom profile. Output goes to `dist/`. Files in `.validation/` and `*.validation.json` contain local task associations and must not be published. See [testing and real-device verification](docs/testing.en.md).

## Project structure

```text
README.md / README.en.md    Chinese and English entry points
src/codex_telegram_bridge/
  app.py / cli.py           Service orchestration and unified entry point
  config.py / paths.py      Credential configuration and data paths
  telegram/                Messaging, formatting, and notifications
  codex/                   Desktop protocol, history, and receipts
  features/                Running tasks, questions, quota, and side chats
  storage/                 Persistent state
  platforms/               Windows / macOS / Linux and shared POSIX adapters
  management/              Installation, services, and diagnostics
  plugins/                 Plugin host and API
  resources/               Bundled plugins and manifests (also in the wheel)
tests/                     Offline regressions and native platform tests
tools/                     Python tools for tests, verification, and releases
docs/                      Setup, architecture, plugin, and verification guides
```

See the [plugin API](docs/plugins.en.md) for plugin development and the [changelog (Chinese)](CHANGELOG.md) for version history. Licensed under the [MIT License](LICENSE).
