# Notification provenance and delivery

[简体中文](notifications.zh-CN.md) | **English** · [Documentation](README.en.md)

## Callback placement

The installer uses the user-level `notify` setting for completions and `PreToolUse` for question hints. `notify` is a supported external notification entry point, but `agent-turn-complete` only identifies a finished turn. Its documented common fields do not guarantee a purpose classification. [Official notification documentation](https://learn.chatgpt.com/docs/config-file/config-advanced#notifications)

Switching to `Stop`/`SubagentStop` does not replace provenance checks. `Stop` has no purpose matcher, and subagent hooks use the parent's `session_id`; the child's identity is `agent_id`. The installer does not register a second completion hook. Both compatible entry points share classification and deduplication. [Official Hooks documentation](https://learn.chatgpt.com/docs/hooks)

Bot-managed `/new` tasks use their own App Server events and management records. Side conversations have their own controller. Ephemeral does not automatically mean unwanted. Existing Desktop tasks are never taken over by another backend merely to observe events.

## Separate evidence dimensions

| Field | Meaning | Limitation |
| --- | --- | --- |
| `type` / `hook_event_name` | Event/lifecycle type | Does not identify user-facing work |
| `source` | Underlying session category | Desktop also uses `vscode` |
| `originator` | Recorded creating client | Does not identify each turn's purpose |
| `threadSource` / `thread_source` | Task purpose | Separate from the current trigger |
| `turnTrigger` / `turn_trigger` | Current turn's trigger | Can differ from task purpose |
| `ephemeral` | In-memory task | Not enough to decide delivery |
| `phase=final_answer`, JSON fields | Message phase/body format | Internal work can produce either |

The installed protocol defines `ThreadSource` as an open string and calls it an analytics classification, not an exhaustive enum. Public App Server supports thread reads and events; a separate connection is not a global subscription to the existing Desktop. [Official App Server documentation](https://learn.chatgpt.com/docs/app-server)

## Unified routing in v0.6.11

`completion_routing.classify` checks the selected `CODEX_HOME` before delivery. Full task IDs must match exactly; display prefixes and parent IDs cannot supply another task's provenance.

| Classification | Handling |
| --- | --- |
| Verified Desktop conversation | Deliver according to preferences; preserve arbitrary JSON replies |
| Verified Desktop subagent | Apply the separate subagent notification preference |
| Explicit internal purpose | Suppress titles, descriptions, summaries, predictions, suggestion generation, safety filtering, etc. |
| Unknown or conflicting provenance | Hold and record redacted structural diagnostics |
| Explicit non-Desktop origin/client | Exclude from Desktop completion notifications |

Task source and turn trigger are checked independently: `user + remote_ios` and `user + goal` are valid. Turn attribution is read only for the matching turn. The newest state database is authoritative; a matching profile-local session header can bridge database lag or migration. Conflicting records never authorize delivery. Legacy null task purpose is supported for recorded Desktop tasks.

Prompt templates and output fields such as `title`, `suggestion`, `suggestions`, or `exclude` no longer authorize delivery decisions. Legacy template recognizers remain diagnostic compatibility tools only. The separately requested exact `{"suggestions":[]}` suppression policy remains.

## Deferred verification

Missing records/origins with complete task and turn IDs can enter `telegram_completions.sqlite3`. It stores identifiers, digests, and necessary state—not raw prompts, replies, or workspace paths. Conflicting evidence cannot be removed to qualify for replay.

The worker gradually rechecks up to 200 pending entries, expiring them after 24 hours. It reconstructs the exact local turn's final answer from paginated history or legacy transcripts and requires a matching digest. It never submits new Desktop input. A shared reservation prevents duplicate completion delivery; ambiguous Telegram attempts are not retried by this queue. Reservations last seven days. Muted entries and entries crossing a quiet-mode generation are not replayed.

Unknown future sources may temporarily lack notifications until adapted. The limited callback fields cannot guarantee perfect classification of every future feature. Task details remain available for explicit inspection.

## Evidence and validation limits

The 2026-10-21 audit checked Desktop **26.1013.4894.0** and bundled backend **0.163.0-alpha.10.2**. Installed code creates separate ephemeral title/description/summary tasks; title generation sets `threadSource=thread_title` and disables lifecycle hooks, while generic completion notifications can still fire. Actual logs show the reported title task is absent from the task database and differs from the user's task. Its original callback was not retained, so its source field was not observed directly.

Read-only classification replay constructed from actual local task records covered 26 user tasks, nine subagent tasks, and four reported temporary tasks. The first two groups were verified; the last group was held. Offline tests also cover conflicting fields, shared ID prefixes, profile isolation, normal JSON, delayed records, paginated history, quiet-mode generations, concurrent reservations, and both completion entry points. Real macOS/Linux notification delivery still requires device acceptance.
