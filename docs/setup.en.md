# Installation and Telegram setup

[简体中文](setup.zh-CN.md) | **English** · [Back to README](../README.en.md)

## Requirements

- A running, signed-in Codex Desktop and 64-bit Python 3.10+ (3.11 or later recommended).
- Access to the Telegram Bot API and PyPI for initial dependencies.
- On macOS: a Python installation with `venv`, an available Keychain, and Screen Recording permission for screenshots.
- On Linux: Python venv support, an unlocked Secret Service keyring, a systemd user session, and a desktop session. Wayland screenshots require portal confirmation.

The bridge uses Desktop and its bundled backend only. It does not fall back to VS Code or a standalone CLI. Windows communication has been verified on a real desktop; macOS/Linux remain experimental until verified on those systems.

## Create a Telegram bot

1. Open the official [@BotFather](https://t.me/BotFather) and send `/newbot`.
2. Choose a display name, then a unique username ending in `bot`, such as `my_codex_notify_bot`.
3. Save the returned bot token privately. It acts as the bot's password; do not put it in screenshots, issues, or Git.
4. Open your new bot and send `/start`. The setup wizard uses this message to identify the receiving chat.

Use a separate bot token for every simultaneously running installation. Two polling services must not share a token.

## Install

Clone the repository, or extract a release source ZIP and open a terminal in its directory:

```text
git clone https://github.com/QingC2206/app-codex-telegram-bridge.git
cd app-codex-telegram-bridge
```

Windows:

```powershell
py -3 install.py
```

macOS / Linux:

```sh
python3 install.py
```

Chinese is the default. For English setup prompts and Telegram UI from the start, add `--language en`, for example:

```sh
python3 install.py --language en
```

On Windows, use `py -3 install.py --language en`. Omitting the flag on an upgrade preserves the current choice.

The installer creates `telegram-bridge-runtime/` under the selected Codex home, installs the Python package, and configures credentials, notification hooks, and a user service. It does not change global Python. Do not rerun failures as administrator or root.

The wizard asks you to:

1. Paste the token; input is hidden.
2. Select a chat if multiple chats are detected, or enter a Chat ID explicitly.
3. Choose reply previews, subagent notifications, and silent delivery.
4. Check for the test message in Telegram.
5. Fully quit and reopen Codex Desktop, reviewing new hooks if prompted.

Send `/help`, then `/running`. Use `/language` to select a language later; `/language en` and `/language zh` switch directly.

## Profiles, data, and services

```text
python install.py --codex-home <absolute-codex-home> --data-dir <absolute-bot-data-directory>
```

New data defaults to `<CODEX_HOME>/telegram-bridge/`. A unique legacy credential directory is reused with its message associations. If multiple candidates exist, specify `--data-dir`. `--no-service` configures hooks without installing a service; `--replace-notify` explicitly replaces an existing notification command.

Windows stores tokens using the current user's DPAPI. macOS uses Keychain; Linux uses Secret Service. A missing or locked keyring does not trigger plaintext storage. Do not migrate Windows encrypted tokens to another OS; run setup on the new machine.

Run management commands using the installed Python. On macOS/Linux:

```sh
~/.codex/telegram-bridge-runtime/bin/python -m codex_telegram_bridge doctor
~/.codex/telegram-bridge-runtime/bin/python -m codex_telegram_bridge service status
~/.codex/telegram-bridge-runtime/bin/python -m codex_telegram_bridge language en
```

On Windows:

```powershell
& "$env:USERPROFILE\.codex\telegram-bridge-runtime\Scripts\python.exe" -m codex_telegram_bridge doctor
```

Replace `doctor` with `setup`, `language en`, `language zh`, `service start`, `service stop`, or `uninstall` as needed. With a custom profile, put `--codex-home` / `--data-dir` before the subcommand. Changing language through the local CLI updates text rendering; use Telegram `/language` or restart the service to refresh command menus.

## Updates and uninstall

Run `git pull --ff-only`, then rerun the installation command for your platform. Existing credentials, message associations, and the language preference are retained. Avoid multiple running copies with the same token.

`python -m codex_telegram_bridge uninstall`, using the installed interpreter, removes owned services and hooks. It keeps credentials, historical associations, plugins, backups, the language preference, and the virtual environment.

If a token is exposed, use BotFather to revoke and replace it, then run `setup` with the installed interpreter. Do not keep using the exposed token.

Continue with the [usage guide](usage.en.md), [plugin API](plugins.en.md), and [verification checklist](testing.en.md).

Official references: [BotFather](https://core.telegram.org/bots/features#botfather), [obtaining a bot token](https://core.telegram.org/bots/tutorial#obtain-your-bot-token).
