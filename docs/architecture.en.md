# Architecture and compatibility boundaries

[简体中文](architecture.zh-CN.md) | **English** · [Back to README](../README.en.md)

The service, installer, uninstaller, tests, and release tooling use one Python package. `platforms/` encapsulates operating-system capabilities; application code shares message types, delivery journals, task ownership, and authorization checks.

## Message flow

```text
Telegram update → Authorized chat/sender check → Command or reply routing
                                                   ↓
                                    Desktop identity/profile verification
                                                   ↓
                                      Current owner's user-input channel
                                                   ↓
                                       Delivery journal and user receipt
                                                   ↓
                                       Hook / task event → Telegram
```

App Tools are used for queries only. Human Telegram input uses follower IPC start/steer, never `send_message_to_thread` agent delegation. Delivery is rejected when an existing task has no Desktop owner. Only bot-managed tasks created through `/new` use the bot's own backend bundled with Desktop.

Notification filtering uses message types and verifiable internal origins. A normal JSON reply containing `suggestion` is not sufficient evidence for filtering. Asynchronous questions are bound to task, turn, item, and question index. Submission is journaled before dispatch; timeouts and unknown outcomes do not trigger automatic resending.

Desktop user-input requests default to a **10-second** timeout. On a timeout or unknown submission outcome, the bot spends up to **10 more seconds** checking local receipts. Confirmation requires a matching user message recorded after this submission in the same task, with explicit message and turn IDs; an expected target turn must also match. A verified receipt counts as accepted. Otherwise the outcome stays unknown, the journal is retained, and nothing is automatically resent. Desktop may have its own shorter internal timeout; this setting does not modify Desktop itself.

## Operating-system boundaries

| Capability | Windows | macOS | Linux |
| --- | --- | --- | --- |
| Desktop identity | Store executable path, process, environment | App Bundle identity and Electron metadata | Electron package metadata and current user |
| Local transport | Named pipes; server PID validation | Unix socket; ownership, permissions, peer PID/UID | Unix socket; ownership, permissions, SO_PEERCRED |
| Token storage | Existing DPAPI format | Keychain | Secret Service |
| Background startup | Current-user scheduled task | Aqua-session LaunchAgent | systemd user service |
| Child cleanup | Windows Job Object | Separate process group and parent watchdog | Separate process group, parent watchdog, service cgroup |
| Screenshots | Native windows and displays | Screen Recording permission, windows and displays | X11 visible region; Wayland portal confirmation |

Only backends proven to belong to Desktop are selected. VS Code, CLI binaries on PATH, and mismatched `CODEX_CLI_PATH` are rejected. Multiple instances sharing a profile, duplicate task IDs across profiles, unreadable instance environments, or changed endpoint identities stop submission.

macOS supports the nested backend at `Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex` and prefers the verified Desktop instance's running backend.

Unix follower endpoints are discovered at the selected profile's `ipc/ipc.sock`, with compatibility for legacy endpoints in the current user's temporary directory. App Tools use `codex-app-tools/<id>.sock` or compatible browser-use endpoints published by Desktop or its bundled child process. Framing uses a four-byte little-endian length followed by JSON. Transport, queries, and application routing are separate; Unix sockets are not all treated as one protocol.

These local Desktop interfaces are version-dependent implementations, not a claimed stable public API. Local peer authorization, screen-capture permissions, or keyrings can deny access. The bridge reports failure without disabling checks. When `doctor` fails, report the redacted error and Desktop version; CI alone does not establish compatibility.

## Configuration, resources, and language

- `paths.py` resolves `CODEX_HOME` and bot data independently of the Python source location.
- New data defaults to `<CODEX_HOME>/telegram-bridge/`. A unique legacy credential directory can be reused; multiple candidates require explicit selection.
- Source and wheel builds share `resources/bundled_plugins/`.
- Plugin workers preserve the v1 `plugin_api` import. Plugins are trusted local code, not a permissions sandbox.
- Services bind absolute Python paths, profiles, and data directories. Local runtime data is excluded from wheel, source ZIP, and Git.
- `i18n.py` translates only explicitly marked application templates through `resources/messages.en.json`. Substitution values are passed unchanged; no translation is applied to arbitrary outbound message text.
- Language defaults to `zh-CN` and is stored separately in the bot data directory. Notification processes read the same preference; plugin workers receive an explicit language. Callbacks and user inputs do not depend on translated labels.

## Module organization

`app.py` retains task-interaction orchestration to preserve command behavior. IPC, platform discovery, credentials, state, and diagnostics are separate modules. Further extraction should follow feature boundaries and existing regression tests, without duplicating application state merely to shorten a file.
