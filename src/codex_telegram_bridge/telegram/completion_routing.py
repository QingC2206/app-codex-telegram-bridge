"""Route completions by local task provenance, never by reply/prompt wording.

`notify` is a completion signal, not a user-conversation event. Its payload may
omit purpose. Thread source and turn trigger are separate dimensions. Unknown
evidence is held for verification, not silently promoted to a user reply.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import sqlite3

# Desktop 26.1013's user-visible thread predicate, plus its persisted subagents.
# ThreadSource is an open string in the protocol, not an exhaustive enum.
CONVERSATION_SOURCES = frozenset('''user page_comment agent_created_thread
agent_forked_thread ambient_suggestion_task avatar_quick_chat chatgpt_handoff
code_review codex_replay conversation_digest implement_todo
local_environment_configuration mcp_app_follow_up onboarding_checklist
security_remediation security_scan voice_chat cloud_threads.create'''.split())
INTERNAL_SOURCES = frozenset('''thread_title thread_description thread_summary
composer_predictions ambient_suggestions ambient_suggestion_safety
commit_message pull_request_message commit_pull_request_message
safety_recourse guardian_review chatgpt_hidden'''.split())
CONVERSATION_TRIGGERS = frozenset('''user composer remote_ios remote_android queue
goal edit_user_message app_tool_send_message app_tool_create_thread
send_user_message_async_question capacity_retry_automatic capacity_retry_manual
automation_heartbeat_scheduled automation_heartbeat_manual automation_scheduled
automation_manual safety_buffer_retry realtime_voice'''.split())
THREAD_FIELDS = ('threadSource', 'thread_source', 'thread-source')
TRIGGER_FIELDS = ('turnTrigger', 'turn_trigger', 'turn-trigger')
ID_FIELDS = ('thread-id', 'thread_id', 'threadId')
TURN_FIELDS = ('turn-id', 'turn_id', 'turnId')
READ_LIMIT = 4 * 1024 * 1024


@dataclass
class Decision:
    kind: str
    reason: str
    thread_id: str = ''
    turn_id: str = ''
    thread_source: str | None = None
    turn_trigger: str | None = None
    stored_status: str = 'unavailable'
    record: dict = field(default_factory=dict, repr=False)

    @property
    def deliver(self):
        return self.kind in {'conversation', 'subagent'}


def _value(containers, names, *, pattern=r'[a-z][a-z0-9_.-]{0,95}'):
    values = set()
    for container in containers:
        for name in names:
            value = container.get(name)
            if value is None or value == '':
                continue
            if not isinstance(value, str) or not re.fullmatch(pattern, value):
                return None, False
            values.add(value)
    return (next(iter(values)) if len(values) == 1 else None), len(values) <= 1


def identity(payload):
    thread = payload.get('thread') if isinstance(payload.get('thread'), dict) else {}
    turn = payload.get('turn') if isinstance(payload.get('turn'), dict) else {}
    top = {key: payload[key] for key in ID_FIELDS if key in payload}
    # SubagentStop's session_id identifies its parent. Never treat it as the
    # child ID, and never use the parent's source to authorize the child reply.
    if payload.get('hook_event_name') == 'SubagentStop' and payload.get('agent_id'):
        top['agent_id'] = payload['agent_id']
    else:
        for key in ('session_id', 'sessionId'):
            if key in payload:
                top[key] = payload[key]
    tid, valid = _value([top, {'thread-id': thread.get('id')}],
                        (*ID_FIELDS, 'session_id', 'sessionId', 'agent_id'),
                        pattern=r'[A-Za-z0-9-]{6,64}')
    turn_id, turn_valid = _value([payload, {'turn-id': turn.get('id')}], TURN_FIELDS,
                                pattern=r'[A-Za-z0-9-]{6,80}')
    return tid or '', turn_id or '', valid and turn_valid and bool(tid)


def _read_db(profile, prefix):
    paths = [p for p in profile.glob(prefix + '_*.sqlite')
             if re.fullmatch(re.escape(prefix) + r'_[0-9]+\.sqlite', p.name)]
    if not paths:
        return None
    # A failed read of the newest schema must not resurrect stale older rows.
    path = max(paths, key=lambda p: int(p.stem.rsplit('_', 1)[1])).resolve()
    if not path.is_relative_to(profile):
        return None
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=0.2)
    db.row_factory = sqlite3.Row
    return db


def _safe_rollout(profile, path, thread_id):
    if not path:
        return None
    resolved = Path(path).resolve()
    if (resolved.is_relative_to(profile) and resolved.is_file()
            and resolved.name.startswith('rollout-')
            and resolved.name.endswith('-' + thread_id + '.jsonl')):
        return resolved
    return None


def read_record(profile: Path, thread_id: str):
    """Read the selected profile only. Rollout headers bridge DB read/migration lag."""
    profile = profile.resolve()
    record = {}
    status = 'unavailable'
    try:
        db = _read_db(profile, 'state')
        if db is not None:
            try:
                row = db.execute('SELECT * FROM threads WHERE id=?', (thread_id,)).fetchone()
                status = 'stored' if row else 'missing'
                if row:
                    record = {k: row[k] for k in ('id', 'thread_source', 'source', 'originator', 'cwd', 'rollout_path') if k in row.keys()}
            finally:
                db.close()
    except (OSError, RuntimeError, sqlite3.Error):
        pass
    try:
        path = _safe_rollout(profile, record.get('rollout_path'), thread_id)
        if path is None:
            paths = []
            for folder in ('sessions', 'archived_sessions'):
                paths.extend(p for p in (profile / folder).glob('**/rollout-*-' + thread_id + '.jsonl')
                             if _safe_rollout(profile, p, thread_id))
            if len(paths) == 1:
                path = paths[0].resolve()
            elif len(paths) > 1:
                return 'conflict', {}
        if path:
            with path.open('rb') as stream:
                line = stream.readline(1024 * 1024)
            header = json.loads(line)
            meta = header.get('payload') if header.get('type') == 'session_meta' else None
            if not isinstance(meta, dict) or meta.get('id') != thread_id:
                return 'conflict', {}
            for key in ('thread_source', 'originator', 'source'):
                value = meta.get(key)
                old = record.get(key)
                # SQLite serializes SessionSource objects as JSON strings.
                if key == 'source' and isinstance(old, str) and old.startswith('{'):
                    old = json.loads(old)
                if old is not None and value is not None and old != value:
                    return 'conflict', {}
                if value is not None:
                    record[key] = value
            record['rollout_path'] = str(path)
            record['cwd'] = record.get('cwd') or meta.get('cwd')
            record['id'] = thread_id
            status = 'stored'
    except (OSError, RuntimeError, TypeError, ValueError, AttributeError):
        return 'unavailable', {}
    return status, record


def turn_evidence(profile, record, thread_id, turn_id):
    """Return attribution/final text for this exact turn, never the latest turn."""
    if not turn_id:
        return {}, None
    attribution, final = {}, None
    offset = None
    try:
        db = _read_db(profile.resolve(), 'thread_history')
        if db is not None:
            try:
                row = db.execute('SELECT * FROM thread_turns WHERE thread_id=? AND turn_id=?', (thread_id, turn_id)).fetchone()
                if row:
                    offset = row['rollout_byte_offset'] if 'rollout_byte_offset' in row.keys() else None
                    if row['status'] == 'completed' and row['final_agent_item_id']:
                        item = db.execute('SELECT item_json FROM thread_items WHERE thread_id=? AND turn_id=? AND item_id=?',
                                          (thread_id, turn_id, row['final_agent_item_id'])).fetchone()
                        parsed = json.loads(item[0]) if item and len(item[0]) <= READ_LIMIT else {}
                        if parsed.get('type') == 'agentMessage' and parsed.get('phase') in (None, 'final_answer'):
                            final = parsed.get('text')
            finally:
                db.close()
    except (OSError, RuntimeError, sqlite3.Error, ValueError, KeyError):
        pass
    try:
        path = _safe_rollout(profile.resolve(), record.get('rollout_path'), thread_id)
        if path:
            with path.open('rb') as stream:
                size = path.stat().st_size
                starts = {max(0, size - READ_LIMIT)}
                if isinstance(offset, int) and 0 <= offset < size:
                    starts.add(offset)
                for start in sorted(starts):
                    stream.seek(start)
                    for line in stream.read(READ_LIMIT).splitlines():
                        try:
                            event = json.loads(line)
                        except (ValueError, UnicodeError):
                            continue
                        body = event.get('payload')
                        if not isinstance(body, dict) or event.get('type') != 'event_msg' or body.get('turn_id') != turn_id:
                            continue
                        if body.get('type') == 'task_started':
                            candidate = body.get('turn_attribution')
                            if isinstance(candidate, dict):
                                if candidate.get('turn_id') not in (None, turn_id) or (attribution and attribution != candidate):
                                    return {'_conflict': True}, None
                                attribution = candidate
                        if body.get('type') == 'task_complete' and isinstance(body.get('last_agent_message'), str):
                            text = body['last_agent_message']
                            if final is not None and final != text:
                                return {'_conflict': True}, None
                            final = text
    except (OSError, RuntimeError, ValueError, TypeError):
        pass
    return attribution, final if isinstance(final, str) else None


def classify(payload, profile: Path):
    tid, turn_id, valid = identity(payload)
    result = Decision('unknown', 'missing-identity', tid, turn_id)
    if not valid:
        return result
    if payload.get('client') not in (None, 'Codex Desktop'):
        result.kind, result.reason = 'foreign', 'non-desktop-client'
        return result
    thread = payload.get('thread') if isinstance(payload.get('thread'), dict) else {}
    turn = payload.get('turn') if isinstance(payload.get('turn'), dict) else {}
    containers = [payload, thread]
    status, record = read_record(profile, tid)
    result.stored_status, result.record = status, record
    if status == 'conflict':
        result.reason = 'conflicting-local-records'
        return result
    source, source_ok = _value([*containers, record], THREAD_FIELDS)
    attribution, _ = turn_evidence(profile, record, tid, turn_id)
    trigger_containers = [*containers, turn, attribution]
    for container in (payload, turn):
        for key in ('turnAttribution', 'turn_attribution'):
            value = container.get(key)
            if isinstance(value, dict):
                associated, ok = _value([value], TURN_FIELDS, pattern=r'[A-Za-z0-9-]{6,80}')
                if not ok or (associated and associated != turn_id):
                    result.reason = 'conflicting-turn-attribution'
                    return result
                trigger_containers.append(value)
    trigger, trigger_ok = _value(trigger_containers, TRIGGER_FIELDS)
    result.thread_source, result.turn_trigger = source, trigger
    if not source_ok or not trigger_ok or attribution.get('_conflict'):
        result.reason = 'conflicting-attribution'
        return result
    if source in INTERNAL_SOURCES or trigger in INTERNAL_SOURCES:
        result.kind, result.reason = 'internal', 'explicit-internal-purpose'
        return result
    ephemeral, ephemeral_ok = _value(
        [{key: str(value).lower() for key, value in container.items() if key in {'ephemeral', 'isEphemeral'}}
         for container in containers], ('ephemeral', 'isEphemeral'), pattern=r'true|false')
    if not ephemeral_ok or ephemeral == 'true':
        result.reason = 'unverified-ephemeral-task'
        return result
    if status != 'stored':
        result.reason = 'unverified-local-task'
        return result
    # SessionSource=vscode is also used by Desktop. The recorded originator is
    # the client identity; neither sourceKind nor JSON output proves purpose.
    origin = record.get('originator')
    if origin != 'Codex Desktop':
        result.kind = 'foreign' if origin else 'unknown'
        result.reason = 'non-desktop-origin' if origin else 'missing-desktop-origin'
        return result
    if source is not None and source not in CONVERSATION_SOURCES and source != 'subagent':
        result.reason = 'unknown-thread-purpose'
        return result
    if trigger is not None and trigger not in CONVERSATION_TRIGGERS:
        result.reason = 'unknown-turn-trigger'
        return result
    session_source = record.get('source')
    if isinstance(session_source, str) and session_source.startswith('{'):
        try:
            session_source = json.loads(session_source)
        except ValueError:
            session_source = None
    subagent = source == 'subagent' or (isinstance(session_source, dict) and isinstance(session_source.get('subagent'), dict))
    result.kind = 'subagent' if subagent else 'conversation'
    result.reason = 'verified-desktop-task'
    return result
