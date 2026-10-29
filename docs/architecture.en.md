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

The default Desktop backend on Windows continues using App Tools for queries and opening an existing task when pre-input discovery finds no owner. Ordinary macOS text replies use the separate access policy described below. Human Telegram input always uses follower IPC start/steer, never `send_message_to_thread` agent delegation. Only bot-managed tasks created through `/new` use the bot's own backend bundled with Desktop; navigation recovery is disabled for these tasks.

For external tasks, recovery runs once only after owner discovery definitively finds no receiver and no user input has been written. It pins the original Desktop process and native connection, verifies the local task ID, calls `navigate_to_codex_page`, and probes the owner again. Navigation changes the visible task; it never takes ownership through a separate backend's `thread/resume`. Navigation and subsequent probes share a recovery budget of up to 10 seconds. A navigation acknowledgement alone does not prove channel readiness. Instance, task, and expected turn are checked again before input. Recovery failure, another instance, or an ambiguous profile stop submission. Post-dispatch errors and unknown outcomes never trigger recovery, and old failed messages are not replayed. Queries, capability probes, and quiet mode never navigate.

The default Desktop backend uses a shared completion provenance classifier: match the full task ID against the selected profile, recorded Desktop origin, and independent turn attribution. Verified conversations are delivered, internal purposes are suppressed, and unknown evidence is held. JSON bodies and prompt templates do not establish routing authority. A metadata-only verification queue restores the exact original reply with deduplication and quiet-mode checks. See [notification provenance and delivery](notifications.en.md) for fields, hook placement, diagnostics, and limitations. Asynchronous answers remain bound to task, turn, item, and question index; submission is journaled and uncertain results are not resent.

Desktop user-input requests default to a **10-second** timeout. On a timeout or unknown submission outcome, the bot spends up to **10 more seconds** checking local receipts. Confirmation requires a matching user message recorded after this submission in the same task, with explicit message and turn IDs; an expected target turn must also match. A verified receipt counts as accepted. Otherwise the outcome stays unknown, the journal is retained, and nothing is automatically resent. Desktop may have its own shorter internal timeout; this setting does not modify Desktop itself.

### Background execution and window ownership

`remote_ios` can start a local background turn before a window registers its owner. Runtime queries use the matching profile's App Tools `read_thread`; local `notLoaded`, `interrupted`, and unclosed rollout records are history or candidate hints. Running lists and stop/steer selectors share live verification, including unloaded sidebar entries with active lifecycle records.

Interrupt capability probes only query the owner, native tool catalog, and current task. An explicit stop pins the instance and turn. A missing owner permits one window recovery only before any interrupt frame; the original turn is checked again immediately before dispatch. Interrupt v4 carries `expectedTurnId`, and the actual interrupted turn must match. “Interrupt and send” also requires a confirmed idle Desktop state; unknown outcomes block the follow-up. An asynchronous answer with no owner after recovery returns to awaiting an answer, rather than being marked submitted.

The global Windows `codex-ipc` broker may belong to another Cockpit instance. The bridge still requires its PID to match the target Desktop; iOS support does not bypass that check. Supporting every multi-account startup order is a separate compatibility boundary.

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

## Platform and backend layers

Operating system and execution backend are separate choices. There is no complete bot copy per OS.

| Layer | Responsibility |
| --- | --- |
| `core/bot.py`, `features/` | One implementation of commands, reactions, questions, approvals, side chats, quota policy and task orchestration |
| `backends/desktop/` | Default compatibility entry into the common controller; Windows configuration behavior is preserved |
| `backends/app_server/` | Explicit runtime configuration, migration and callback translation; no second business engine |
| `platforms/windows/` | Process discovery, named pipes, DPAPI, Job Objects, hidden windows, Task Scheduler, capture and native Desktop access |
| `platforms/macos/` | Keychain, LaunchAgent, capture permissions, verified local Desktop ownership and opening the original conversation |
| `platforms/posix/`, `platforms/linux/` | Shared Unix sockets/process groups and Linux-specific system capabilities |
| `codex/`, `telegram/`, `storage/` | Shared protocol/receipts, rendering/formatting, delivery and persistent state |
| `management/` | Shared configuration, installation transaction and diagnostics; service operations delegate to adapters |

Old `app.py`, `app_server_*`, `codex/app_server*` and `telegram/app_server_io.py` imports alias the same implementation modules. They do not contain copied implementations. Desktop orchestration keeps its existing command behavior; both backends reuse quota, duration, diff formatting and completion rendering.

Both installers verify service ownership before changing files, back up hooks/configuration/manifests, and restore files and the previous service after an installation or startup failure. Windows and Linux start explicitly after installation; macOS starts once through bootstrap. Both execution backends use Windows `CREATE_NO_WINDOW` and the original Job Object policy; POSIX retains separate process groups. Recovery commands (`service`, `setup`, manifest-based uninstall) work with a damaged `runtime.json`, while `run` still rejects invalid settings.

`MacOSDesktopAccess` validates ordinary text replies against the current profile's unarchived Desktop session, rollout provenance and verified owner. Missing-owner recovery opens the original thread through the verified app bundle. Recovery deadlines, single submission and no retry on unknown outcomes are unchanged. Strict turn-specific operations still require live queries. `WindowsDesktopAccess` retains the original App Tools checks and navigation; it does not use the macOS local-record fallback.

All business methods now live in `core/bot.py`; JSONL connections share `CodexAppServer`. macOS reads fresh snapshots from the verified follower owner, validating publisher, host, task, version, revision and the newest history boundary. Strict turn actions use these reads. Metadata candidates are checked individually when no sidebar catalogue exists; incomplete scans are labelled. Windows keeps its original App Tools checks.

See [app-server mode](app-server.md) and the single [capability ledger](parity.json). The former macOS queue, commands, UI and approval engines are retired. The old SQLite database preserves identifiers, drafts and delivery records without replaying tasks. Stop, SubagentStop and question hooks share provenance and deduplication.

Common improvements are reviewed against Windows contracts and current-entry tests: completion-before-ack, failed reactions, uncertain writes, stale approvals, UTF-16 pagination and delayed delivery bindings. Native Windows acceptance remains a separate step. Only specific third-party plugin extensions are excluded. Code review does not replace Telegram UI acceptance.

## Account quota and side-chat boundaries

Quota observations, alert deduplication, reset-credit history and redemption guards have one shared account state layer for both backends. Switching accounts invalidates old buttons, selections, confirmations and reservations. Returning to an account restores its own history without restoring old authorization. Events with unverified ownership trigger a fresh read instead of cross-account comparisons. Queued notifications and delayed receipts must also match the account and authorization generation.

Legacy observations without account identity remain historical records; each account establishes a new baseline. Legacy redemptions with uncertain outcomes or outstanding duplicate protection remain quarantined until their ownership is established. Clearing a cache, restarting or observing another account's quota recovery cannot release these guards. Identity comes from the connected App Server. When the interface lacks a verifiable organization or workspace identifier, isolation between organizations sharing one login identity is not claimed.

During upgrade, queued legacy background notices lacking both account attribution and explicit task or interaction routing are retained for review rather than classified from their body and sent automatically. This can include ordinary service notices. Explicitly routed task messages and deliveries with uncertain outcomes keep their existing handling. This conservative migration runs once; newly queued messages carry their applicable routing context.

Side chats disable Codex built-in multi-agent tools in their dedicated App Server process, alongside read-only and approval-denial policies. Each new process and every follow-up verify the effective policy. Old sessions expire after reconnection; a newly created side session must verify the policy again. If the required configuration capability cannot be verified, the side request is rejected without forwarding it to the parent. The main task and global user configuration are unchanged. This does not establish complete isolation from arbitrary plugins, MCP tools or external delegation services.

The package's `__version__` is the only manually maintained version. Package metadata, release archive names and the compatibility `VERSION` file inside release archives derive from it. Diagnostics distinguish `bridge_version` from `python_version`; legacy fields retain their existing meanings for callers.
