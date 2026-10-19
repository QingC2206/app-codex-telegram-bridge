# Usage guide

[简体中文](usage.zh-CN.md) | **English** · [Back to README](../README.en.md)

## Getting started and choosing a language

After installation and restarting Codex Desktop, send `/help` in Telegram. The interface defaults to Chinese; your phone or Telegram client language does not override it.

- `/settings` → **Language**: show Chinese / English selection buttons.
- `/language en`: switch to English.
- `/language zh`: switch back to Chinese.

The choice is stored in `telegram_language.json` in the current bot data directory and survives restarts and upgrades. Command menus, subsequent task cards, notification headings, buttons, question prompts, and the bundled screenshot plugin use the selected language. Existing messages are not rewritten in bulk; reopen or refresh a card to get the new language. Telegram's command menu may take a moment to refresh.

Task titles, file paths, user input, Codex replies, and model-generated questions/options stay in their original language. Switching languages does not ask a model to translate, change task ownership, or modify submitted content. Third-party plugins must supply their own translations; underlying service errors and developer logs may remain in their original language.

Each bot configuration has one receiving chat. The language preference belongs to that bot data directory, so authorized members of a group share the same interface language. Existing chat and sender authorization still applies to language changes.

## Finding tasks and continuing a conversation

1. Open the task in the correct Codex Desktop profile.
2. Send `/tasks` to browse tasks or `/running` to see currently running tasks. Buttons open details, progress, diffs, and usage.
3. Use Telegram's Reply action on a task notification, details, or progress card to send text to its associated task. You can also attach an image or file.
4. Alternatively, send `/send`, select a task, and enter the next instruction after the prompt. `/send ID message` also works.

Ordinary text does not automatically select the most recent task. Without a task association, the bot asks you to select one. If an old message has no association, open the corresponding task details first.

When directly replying to a running task, choose:

- **Append without interrupting:** append to the current task.
- **Interrupt current turn and send:** available only when Desktop provides a verifiable interruption capability.
- **Side chat:** independent, read-only questions without interrupting the main task.
- **Cancel:** do not submit the message.

Use `/steer` to append to a running task and `/stop` to request interruption of a controllable task. The bot does not take over existing Desktop tasks or switch to VS Code to work around a failed connection.

## Questions, approvals, and attachments

Codex questions provide options or a text-answer entry point. Answer asynchronous questions using an option, text entry, or a direct reply to the question card; the answer remains associated with the original question and turn. Expired or answered questions cannot be submitted again. Complex or unsupported approvals need to be handled in Desktop.

Replying to a task card continues the task; replying to a question card answers that specific question. A received-message acknowledgment does not itself prove submission. If the outcome is unknown, check the latest Desktop messages before retrying.

Send attachments by replying to a task or side-chat message. Each attachment is limited to 20 MB; send multiple attachments separately. Full replies can be paginated or downloaded as Markdown. Switching interface language does not translate original content or JSON, or filter a response merely because it contains a `suggestion` field.

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
| `/settings` | Adjust notifications, language, and the new task folder |
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

Most task commands support button selection when the ID is omitted. `/help` preserves pending input and quota reservations. See the [README](../README.en.md) for full parameters and quota rules.

## Side chats and plugins

Reply to a main-task message with `/side question`, or use `/side ID question`. Reply to a side-chat answer to continue that session. Sessions expire after three idle hours or a service restart. Side chats do not perform changes on the main task; closing one preserves existing answers.

`/screenshot` uses a bundled plugin. macOS requires Screen Recording permission; Linux Wayland requires desktop portal confirmation. Custom plugins are trusted local Python code. See the [plugin API](plugins.en.md).

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Desktop owner not found | Open the task in the correct Desktop instance and check that `CODEX_HOME` matches |
| Unknown submission outcome | Check the latest Desktop messages before resending |
| Menu still uses the old language | Reopen the menu later or send `/language`; typed commands do not depend on the menu |
| Old cards remain in Chinese | Refresh or reopen the card; original replies retain their original text |
| Two computers alternate receiving messages or report polling conflicts | Use a separate bot token for each running bot |
| Question content or a plugin remains Chinese in English mode | Model content is not automatically translated; third-party plugins need their own English text |

See [installation](setup.en.md) for setup and upgrades, and [testing](testing.en.md) for real-device communication checks.
