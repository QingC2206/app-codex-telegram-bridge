# Testing and real-device verification

[简体中文](testing.zh-CN.md) | **English** · [Back to README](../README.en.md)

## Automated checks

```text
python -m pip install -e ".[dev]"
python tools/run_tests.py --suite Full
python -m build
python tools/check_distribution.py
```

Tests use temporary Codex profiles and bot data. They cover replies, message origins, internal-suggestion filtering, task queries, asynchronous questions, quota, images, tables, plugins, and safety boundaries. Platform coverage includes configuration preservation and rollback, legacy data discovery, refusal of plaintext credential fallback, service argument escaping, single-instance locking, Desktop package provenance, and real Unix socket framing and peer checks.

Language tests cover Chinese defaults, persistent selection, profile isolation, separate notification processes, command menus, authorized callbacks, unmodified user/model content, unchanged callback data, translation placeholders, and plugin workers. They never send real Telegram messages or submit Desktop tasks.

GitHub Actions runs Python 3.10/3.11/3.13 on Windows, Ubuntu, and macOS. Native tests run only on their applicable platform. CI without a Desktop or Telegram account does not test real task delivery.

v0.6.11 provenance tests cover independent task/turn attribution, colliding ID prefixes, deferred verification, normal JSON, paginated history, concurrent deduplication, quiet-mode generations, and subagent questions that must not reserve completions. See [notification provenance and delivery](notifications.en.md). Local read-only replay is not a new mobile acceptance test.

## Read-only checks on your computer

The v0.4.1 Windows acceptance verified real task submission, user-origin receipts in the same turn, Telegram notifications, mobile direct replies, `/send`, question-card answers, and installation from a standalone wheel. These are historical real-device checks, not claims that CI performs mobile tests. macOS/Linux still require real Desktop, credential-store, service, and capture verification.

Use the installed virtual environment's Python:

```text
python -m codex_telegram_bridge doctor
```

This checks the Telegram API and Desktop connection without creating/resuming tasks, submitting task messages, or calling Telegram `getUpdates`. Do not publish personal paths verbatim. `doctor --offline` checks only the local installation and dependencies, not real communication.

## One explicitly authorized connection probe

Open an active Desktop task that may receive a test message. From the source directory:

```text
python tools/verify_connection.py --thread-id <full-task-id> --send-probe --report .validation/live.json
```

The probe submits one marked user input, requires its actual user-message receipt in the same turn, and verifies Telegram sending. Unknown outcomes do not trigger resubmission. It does not consume mobile updates; mobile inbound delivery requires the steps below.

## First real-device check, especially macOS/Linux

1. Pull the repository, keep Codex Desktop running, and run the platform installer. Restart Desktop to load hooks.
2. Run `doctor` with the installed interpreter and any custom profile arguments.
3. Complete a normal Desktop task. Confirm Telegram receives its reply and does not forward prediction suggestions as final answers.
4. Reply directly to that task card. Confirm the original task receives one user message, without an agent-delegation envelope.
5. Use `/send` to continue the same task and `/running` to check live status.
6. Ask the task to present an option question and a free-text question. Answer each from Telegram; old buttons must not submit twice.
7. Test attachments, full-reply pages, read-only side chats, and `/screenshot`. Grant required capture permissions; Wayland needs local confirmation.
8. Send `/language en`, inspect `/help`, `/running`, and a new completion notification, then switch back with `/language zh`. Existing task text and callback routing must stay intact.
9. Restart the bot and confirm credentials, reply associations, and language preference remain. Check that isolated profiles do not cross-route messages.

If endpoint discovery or peer authorization fails, retain the current service state and report a redacted error, OS, and Desktop version. Do not alter the sandbox, disable identity checks, resume another client's task in a separate backend, or repeatedly submit a command with an uncertain outcome.

## iOS background-task acceptance

Offline tests cover unloaded-task replies, `/send`, append, interrupt, asynchronous answers, live status, and progress. They include changed turns, restarted instances, quiet mode, lost acknowledgements, and prohibition of independent backend takeover. These tests do not replace real mobile inbound acceptance.

Start a disposable task on this computer from iOS, leaving its desktop window unopened. Query `/running` and task details first; neither should switch the window. Then test Reply, `/send`, append, asynchronous answers, stop, and “Interrupt and send” separately. Verify exactly one intended input per action in the original task. Expired questions and changed turns must reject the operation; uncertain stop outcomes must block follow-up input. Repeat in quiet mode to verify blocking. With two instances, verify the account and profile without weakening PID checks.

## Releases

```text
python tools/build_release.py --thread-id <full-active-task-id-authorized-for-testing>
```

The release gate verifies real submission, a user-message receipt, Telegram delivery, the complete offline suite, and unchanged source hashes. Publish only the source ZIP and its checksum; keep `.validation/` and `*.validation.json` local. Wheel and source distributions can be built separately for installation tests, but a successful build does not prove communication.

## Settings and menu acceptance

Check that `/quota` is always first, followed by `/tasks` and `/running` and plugin status and plugin commands are last, with `[Plugin]` in English or `[插件]` in Chinese. Opening `/settings` pages must preserve preferences and pending input. Enable and disable completion/subagent notifications separately; repeat clicks must keep the selected state. Switch languages through settings and verify menu text and back navigation, then restore the original preferences. Check `/settings cwd PATH` and legacy aliases; `/help` must preserve pending input.

## Quiet mode acceptance

Enable quiet mode and confirm `/quota`, `/tasks`, `/running`, and settings remain usable. Task messages, attachments, and old action buttons must not submit work. Completion replies and questions must not be pushed or replayed after disabling the mode. Restarting preserves the mode and original notification preferences; tracking and quota reservations must not resume automatically.
