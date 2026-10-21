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

1. Keep Codex Desktop running with the correct account and profile. Tasks may be started locally or remotely from iOS.
2. Send `/tasks` to browse tasks or `/running` to see currently running tasks. Buttons open details, progress, diffs, and usage.
3. Use Telegram's Reply action on a task notification, details, or progress card to send text to its associated task. You can also attach an image or file.
4. Alternatively, send `/send`, select a task, and enter the next instruction after the prompt. `/send ID message` also works.

Ordinary text does not automatically select the most recent task. Without a task association, the bot asks you to select one. If an old message has no association, open the corresponding task details first.

If no reply owner is found before submission, the bot tries to open the target task once in the same Desktop profile, with up to 10 seconds for channel recovery. This changes the task visible on your computer; input is sent only after the channel is verified. Recovery failure does not take over the task or replay earlier failed messages. Queries and quiet mode do not switch tasks.

### Tasks started from iOS before their desktop window loads

You do not need to open the task manually first. Direct replies, `/send`, `/steer`, and asynchronous answers share the recovery mechanism above. `/stop` and “Interrupt and send” also recover the original window, then verify and interrupt the specified turn. If that turn ends or changes during recovery, the operation stops. An uncertain interrupt acknowledgement prevents the follow-up message.

`/running`, task details, progress tracking, and stop/append selectors verify live state through Desktop. Queries and button rendering do not load windows. Unloaded metadata and stale lifecycle records are not authoritative live status. Question cards retain the original question, answer, and status.

Here, “remote” means iOS started a task on this computer's Desktop. Desktop must remain online with a matching account and profile. Cockpit instances on Windows may share a global IPC broker: if another instance owns that broker, unverified operations remain blocked. This update preserves that protection and does not establish support for every dual-instance startup order.

### Replying to running tasks

When directly replying to a running task, choose:

- **Append without interrupting:** append to the current task.
- **Interrupt current turn and send:** available only when Desktop provides a verifiable interruption capability.
- **Side chat:** independent, read-only questions without interrupting the main task.
- **Cancel:** do not submit the message.

Use `/steer` to append to a running task and `/stop` to request interruption of a controllable task. The bot does not take over existing Desktop tasks or switch to VS Code to work around a failed connection.

## Questions, approvals, and attachments

Codex questions provide options or a text-answer entry point. Answer asynchronous questions using an option, text entry, or a direct reply to the question card; the answer remains associated with the original question and turn. Expired or answered questions cannot be submitted again. Complex or unsupported approvals need to be handled in Desktop.

Question cards use native Telegram HTML with `✒️ Question · Task title (Answered)`: the title is bold and the trailing status uses regular weight for less emphasis. The workspace and short task ID use code styling; the original question and user answer appear in separate blockquotes with explicit paragraph spacing. Colors follow Telegram’s light or dark theme. Awaiting-answer, submitted, awaiting-confirmation, and closed cards share this layout. Completion/error text uses 🥳 / 🥲; native message reactions use 🎉 / 😢 because Telegram does not support 🥳 / 🥲 as ordinary reactions.

After an answer, the original card keeps the question and your answer and removes answering buttons. It distinguishes submitted, answered, awaiting confirmation, and closed states. Answers entered in Desktop are filled in when their history is available. Long content can be viewed in full or downloaded as Markdown. Answers follow the existing 30-day local question-record cleanup policy; old closed cards without a readable answer record cannot be reconstructed.

Replying to a task card continues the task; replying to a question card answers that specific question. A received-message acknowledgment does not itself prove submission. If the outcome is unknown, check the latest Desktop messages before retrying.

After a submission timeout, the bot checks receipts for up to 10 seconds, including read-only live Desktop history when needed. Live verification requires the exact first user message in a turn started after submission, matching the task, text digest, and unique item ID. Steering into older turns or insufficient evidence remains unconfirmed; the bot never resubmits automatically.

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

Most task commands support button selection when the ID is omitted. `/help` preserves pending input and quota reservations. See the [README](../README.en.md) for full parameters and quota rules.

## Quiet mode

Open `/settings` → **🌙 Quiet mode** → **Enable quiet mode**. It is off by default, and the choice survives restarts and upgrades.

- Keep quota, task lists, running tasks, task details, diffs, usage, logs, plugin status, and settings available. Explicitly viewing historical replies remains supported.
- Disable text replies, attachments, `/send`, `/steer`, `/new`, `/side`, `/stop`, `/watch`, question/approval answers, plugin execution, and quota resets. Existing buttons follow the same restrictions.
- Pause Codex replies, questions, approvals, progress, and other automatic messages. Existing tasks keep running without automatic interruption or approval. Enabling quiet mode cancels progress tracking and unexecuted quota reservations.
- Disabling quiet mode does not replay suppressed replies or restore cancelled tracking/reservations. Requests submitted before enabling it cannot be recalled.

Quiet mode is independent of completion notification preferences: it limits the bot to queries and settings without overwriting those preferences. It is also separate from Telegram's sound/mute setting.

## Side chats and plugins

Reply to a main-task message with `/side question`, or use `/side ID question`. Reply to a side-chat answer to continue that session. Sessions expire after three idle hours or a service restart. Side chats do not perform changes on the main task; closing one preserves existing answers.

`/screenshot` uses a bundled plugin. macOS requires Screen Recording permission; Linux Wayland requires desktop portal confirmation. Custom plugins are trusted local Python code. See the [plugin API](plugins.en.md).

Desktop user-input requests default to a **10-second** timeout. On a timeout or unknown submission outcome, the bot spends up to **10 more seconds** checking local receipts. Confirmation requires a matching user message recorded after this submission in the same task, with explicit message and turn IDs; an expected target turn must also match. A verified receipt counts as accepted. Otherwise the outcome stays unknown, the journal is retained, and nothing is automatically resent. Desktop may have its own shorter internal timeout; this setting does not modify Desktop itself.

## Redacted notification diagnostics

Completion notifications have one explicit exception: if the entire body equals `{"suggestions":[]}` after trimming surrounding whitespace, it is skipped before delivery and diagnostic sampling. This is literal equality, without JSON parsing or normalization. Different internal spacing, nonempty arrays, extra fields, code blocks, and explanatory text are not suppressed by this rule. Logs use `exact-empty-suggestions-literal`, separately from provenance detection.

When a notification has unknown provenance and a `suggestion` / `suggestions` shaped reply, the bot keeps forwarding it under the existing rules and saves a structural sample in `telegram_notification_diagnostics.sqlite3` in its data directory. Replies with other shapes and notifications with classified provenance are not sampled. A legitimate JSON reply with the same suggestion shape may be structurally sampled, but is still forwarded.

Samples contain field types, array counts, string lengths, known provenance enum values, database lookup status, and flags for observed request-template features in the latest input. They exclude reply/prompt text, file paths, credentials, full task IDs, and unknown field names. Unknown keys become positional placeholders. Template flags are diagnostic evidence only and do not control filtering.

Repeated samples are counted together, with at most 100 samples retained. Each write removes samples not seen in seven days. Diagnostic write failures do not block notifications. The normal log's `diagnostic_sample` identifies the sample. The database stays local and is neither uploaded nor packaged. Collection does not automatically suppress messages; actual samples must be reviewed before changing provenance detection.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Desktop owner not found / reply channel not ready | The bot tried to open the task in the same Desktop profile. Check the target task and `CODEX_HOME` on your computer; retry only after confirming that this message was not sent |
| Unknown submission outcome | Check the latest Desktop messages before resending |
| Menu still uses the old language | Reopen the menu later or send `/language`; typed commands do not depend on the menu |
| Old cards remain in Chinese | Refresh or reopen the card; original replies retain their original text |
| Two computers alternate receiving messages or report polling conflicts | Use a separate bot token for each running bot |
| Question content or a plugin remains Chinese in English mode | Model content is not automatically translated; third-party plugins need their own English text |

See [installation](setup.en.md) for setup and upgrades, and [testing](testing.en.md) for real-device communication checks.
