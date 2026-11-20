# Notification provenance and delivery

[简体中文](notifications.zh-CN.md) | **English** · [Documentation](README.en.md)

## Desktop suggestion sources

With message previews enabled, the default `desktop-hook` source collects composer suggestions from `notify` and `Stop` before notification filtering. It recognizes explicit `composer_predictions` provenance. The legacy form requires the known Desktop generation request and a temporary task absent from the readable profile records. User-requested `{"suggestion":"…"}` JSON in an ordinary chat is delivered normally.

The full generation thread and turn IDs join two callbacks. Structured source fields identify the original chat and turn, for example `responsesapiClientMetadata.prediction_source_thread_id` and `prediction_source_turn_id`. The text and binding may arrive in either order. The bridge verifies that the hook descended from the selected Desktop process and that this process approved the original turn with one matching candidate. Unbound candidates stay private. If a build supplies the text only in a `thread/sample` response, these two hooks cannot see it.

When a callback identifies the source turn alone, the bridge can resolve the generation's parent from the profile's `thread_spawn_edges` table and recheck that link each time the suggestion is used. A parent without a source turn stays pending.

The profile-local `telegram_hook_suggestions.sqlite3` holds up to 1,024 generation records. Access and periodic service cleanup remove records older than 47 hours. The bridge identifies the login session using the Desktop process and file metadata for the profile's `auth.json`. An unverifiable session keeps the ordinary card. Changes to the process, login file or quiet-mode generation invalidate old associations. Ordinary notification logs omit candidate text. On macOS the final decision is read from the Desktop application log directory. [Codex log locations](https://learn.chatgpt.com/docs/reference/troubleshooting)

The bundled Windows source uses the approach contributed in PR #3. A dedicated shortcut launches the selected Microsoft Store Desktop with a local relay for its bundled backend. The relay forwards the original stdio bytes and pairs each `composer_predictions` request to `thread/sample` with the response for that request ID. It verifies the source thread, turn, candidate identity and approval recorded by that Desktop process. Only a uniquely attributable suggestion can be read. [Setup](setup.en.md#one-tap-desktop-suggestions) · [PR #3](https://github.com/QingC2206/app-codex-telegram-bridge/pull/3)

Candidate text stays in the relay's memory. The Robot reads approved results through a local named pipe with process checks and a Windows DPAPI-protected capability. Account-change events, changes to login-file metadata and a relay restart invalidate prior buttons. Missing or conflicting evidence, or an incompatible Desktop update, leaves ordinary completion notifications available.

An installed Python package can register a callable in the `codex_telegram_bridge.desktop_suggestions` entry-point group. Select its exact name in the profile's `desktop_suggestion_source` notification setting and restart the Robot. The callable receives the verified `DesktopEndpoint`, full thread ID and completed turn ID. It reads an existing local result for the currently signed-in account. The adapter is trusted local code and must independently establish that the candidate passed Desktop's final check.

| Returned fields | Meaning |
| --- | --- |
| `hostId`, `threadId`, `sourceTurnId` | Exact target. Only the local Desktop host is accepted. |
| `accountId`, `userId` | Current Desktop principal bound to the target. Return the same context with null text while a suggestion is pending. |
| `principalEpoch` | Both built-in sources bind results to the current Desktop session. Other adapters supply the account fields above. |
| `responseId`, `text` | Identity and full literal text of the approved candidate. Return `None` for an unverifiable owner. |

The bridge pins the original Desktop instance and principal when sending the ordinary card. It checks the candidate again before deleting the old card, after deletion and before submitting user input. Its profile-local SQLite journal retains prepared Telegram text and hashed identities for up to 47 hours of active operation. The outgoing text card keeps the reply target, topic and delivery preferences. A lost acknowledgement for the replacement or the task input never triggers an automatic duplicate. An authorized click on the replacement can confirm its message ID. Telegram reactions on the deleted message are lost. [Telegram deletion semantics](https://core.telegram.org/bots/api#deletemessages)

## Callback placement

The installer uses the user-level `notify` setting for completions and `PreToolUse` for question hints. `notify` is a supported external notification entry point, but `agent-turn-complete` only identifies a finished turn. Its documented common fields do not guarantee a purpose classification. [Official notification documentation](https://learn.chatgpt.com/docs/config-file/config-advanced#notifications)

Switching to `Stop`/`SubagentStop` does not replace provenance checks. `Stop` has no purpose matcher, and subagent hooks use the parent's `session_id`; the child's identity is `agent_id`. The installer does not register a second completion hook. Both compatible entry points share classification and deduplication. [Official Hooks documentation](https://learn.chatgpt.com/docs/hooks)

Bot-managed `/new` tasks use their own App Server events and management records. Side conversations have their own controller. Ephemeral does not automatically mean unwanted. Existing Desktop tasks are never taken over by another backend merely to observe events.

## Separate evidence dimensions

| Field | Meaning | Limitation |
| --- | --- | --- |
| `type` / `hook_event_name` | Event/lifecycle type | Does not identify user-facing work |
| `client` | Current controller's display label | Not the local task's execution origin; mobile control can change it |
| `source` | Underlying session category | Desktop also uses `vscode` |
| `originator` | Recorded creating client | Does not identify each turn's purpose |
| `threadSource` / `thread_source` | Task purpose | Separate from the current trigger |
| `turnTrigger` / `turn_trigger` | Current turn's trigger | Can differ from task purpose |
| `ephemeral` | In-memory task | Not enough to decide delivery |
| `phase=final_answer`, JSON fields | Message phase/body format | Internal work can produce either |

The installed protocol defines `ThreadSource` as an open string and calls it an analytics classification, not an exhaustive enum. Public App Server supports thread reads and events; a separate connection is not a global subscription to the existing Desktop. [Official App Server documentation](https://learn.chatgpt.com/docs/app-server)

## Unified routing

`completion_routing.classify` checks the selected `CODEX_HOME` before delivery. Full task IDs must match exactly; display prefixes and parent IDs cannot supply another task's provenance.

| Classification | Handling |
| --- | --- |
| Verified Desktop conversation | Deliver according to preferences; preserve arbitrary JSON replies |
| Verified Desktop subagent | Apply the separate subagent notification preference |
| Explicit internal purpose | Suppress titles, descriptions, summaries, predictions, suggestion generation, safety filtering, etc. |
| Unknown or conflicting provenance | Hold and record redacted structural diagnostics |
| Explicit non-Desktop creating origin in local records | Exclude from Desktop completion notifications |

Task source and turn trigger are checked independently: `user + remote_ios` and `user + goal` are valid. Turn attribution is read only for the matching turn. The newest state database is authoritative; a matching profile-local session header can bridge database lag or migration. Conflicting records never authorize delivery. Legacy null task purpose is supported for recorded Desktop tasks.

### Normal answers controlled from iOS / Android

Local tasks started, continued, queued, or edited from a phone are included. Desktop-only routing constrains execution and the bot's input transport, not the controlling device. Mobile control uses the connected host's projects and configuration. [Official remote connections guide](https://learn.chatgpt.com/docs/remote-connections)

v0.6.12 removes the early rejection of every callback whose `client` differs from `Codex Desktop`. Such callbacks require both a profile-local Desktop task and a known normal trigger recorded locally for the exact turn. A callback merely claiming `remote_ios` or `remote_android` is insufficient. Queue, edit, and retry turns also qualify without depending on mobile product display names. Normal JSON, subagent preferences, quiet mode, and deduplication retain their existing handling.

After edits, the database-selected current rollout takes precedence, including suffixed filenames and Windows `\\?\` paths. Resolved paths must stay inside the selected profile and the header's full task ID must match. Multiple candidates without a database pointer are held. If history projection lags and quick reads miss attribution, a bounded scan covers at most the final 64 MiB with per-line limits; missing proof remains deferred.

Prompt templates and output fields such as `title`, `suggestion`, `suggestions`, or `exclude` no longer authorize delivery decisions. Legacy template recognizers remain diagnostic compatibility tools only. The separately requested exact `{"suggestions":[]}` suppression policy remains.

## Deferred verification

Missing records, origins, or required turn evidence with complete task and turn IDs can enter `telegram_completions.sqlite3`. It stores identifiers, digests, and necessary state—not raw prompts, replies, client labels, or workspace paths. The exact-turn evidence requirement survives replay. Conflicting evidence cannot be removed to qualify for replay.

The worker gradually rechecks up to 200 pending entries, expiring them after 24 hours. It reconstructs the exact local turn's final answer from paginated history or legacy transcripts and requires a matching digest. It never submits new Desktop input. A shared reservation prevents duplicate completion delivery; ambiguous Telegram attempts are not retried by this queue. Reservations last seven days. Muted entries and entries crossing a quiet-mode generation are not replayed.

Unknown future sources may temporarily lack notifications until adapted. The limited callback fields cannot guarantee perfect classification of every future feature. Task details remain available for explicit inspection.

## Evidence and validation limits

The 2026-10-21 audit checked Desktop **26.1013.4894.0** and bundled backend **0.163.0-alpha.10.2**. Installed code creates separate ephemeral title/description/summary tasks; title generation sets `threadSource=thread_title` and disables lifecycle hooks, while generic completion notifications can still fire. Actual logs show the reported title task is absent from the task database and differs from the user's task. Its original callback was not retained, so its source field was not observed directly.

Read-only classification replay constructed from actual local task records covered 26 user tasks, nine subagent tasks, and four reported temporary tasks. The first two groups were verified; the last group was held. Offline tests also cover conflicting fields, shared ID prefixes, profile isolation, normal JSON, delayed records, paginated history, quiet-mode generations, concurrent reservations, and both completion entry points. Real macOS/Linux notification delivery still requires device acceptance.

Later that day, actual notification logs with Desktop **26.1016.6573.0** showed phone-controlled turns rejected as `non-desktop-client`, while their recorded task origin remained Desktop. After the fix, read-only replay of 12 actual iOS/queued turns with a synthetic client label verified all 12. Original callbacks were not retained; this is not fresh phone-to-Telegram acceptance. Android also has offline regression coverage.
