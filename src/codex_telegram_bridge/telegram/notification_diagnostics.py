"""Bounded, local structural samples. Never persist raw notification payloads."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

MAX_SAMPLES = 100
RETENTION_SECONDS = 7 * 86400
MAX_RECORD_BYTES = 32768

# Only protocol field names may be written verbatim. Unknown dictionary keys
# can themselves contain user text, file paths or credentials.
FIELDS = frozenset('''type client hook_event_name thread-id thread_id threadId id
turn-id turn_id turnId thread turn source sourceKind source_kind threadSource
thread_source thread-source turnTrigger turn_trigger turn-trigger origin originator
purpose metadata context ephemeral isEphemeral input-messages input_messages input
messages role content text last-assistant-message last_assistant_message cwd model
timestamp created_at createdAt event data params payload session_id sessionId
agent_id agent_type parent_thread_id status suggestions suggestion title description
prompt appId appIds pluginId outputSchema responseSchema properties required items
additionalProperties $schema exclude reason'''.split())
PURPOSE_FIELDS = frozenset('''threadSource thread_source thread-source turnTrigger
turn_trigger turn-trigger source sourceKind source_kind purpose origin originator'''.split())
SAFE_PURPOSES = frozenset('''composer_predictions ambient_suggestions ambient_suggestion_safety
user agent_created_thread agent_forked_thread ambient_suggestion_task cli vscode exec
appServer subAgent unknown codex_desktop'''.split())


def _shape(value, field='', *, depth=0, budget=None):
    budget = [180] if budget is None else budget
    budget[0] -= 1
    if budget[0] < 0:
        return {'type': 'omitted'}
    if value is None:
        return {'type': 'null'}
    if isinstance(value, bool):
        result = {'type': 'boolean'}
        if field in {'ephemeral', 'isEphemeral', 'additionalProperties'}:
            result['value'] = value
        return result
    if isinstance(value, str):
        result = {'type': 'string', 'length': len(value)}
        allowed = (SAFE_PURPOSES if field in PURPOSE_FIELDS else
                   {'Codex Desktop'} if field == 'client' else
                   {'agent-turn-complete', 'message', 'input_text', 'output_text', 'response_item'} if field == 'type' else
                   {'Stop', 'SubagentStop'} if field == 'hook_event_name' else
                   {'user', 'assistant', 'developer', 'system', 'tool'} if field == 'role' else set())
        if value in allowed:
            result['value'] = value
        return result
    if isinstance(value, (int, float)):
        return {'type': 'number'}
    if isinstance(value, (dict, list)):
        result = {'type': 'object' if isinstance(value, dict) else 'array', 'count': len(value)}
        if depth >= 5 or budget[0] <= 0:
            result['truncated'] = True
            return result
        if isinstance(value, dict):
            fields = {}
            for index, (key, child) in enumerate(value.items()):
                if index >= 48 or budget[0] <= 0:
                    break
                name = key if key in FIELDS else f'<redacted-key-{index}>'
                fields[name] = _shape(child, key if key in FIELDS else '', depth=depth+1, budget=budget)
            result['fields'] = fields
            result['truncated'] = len(fields) != len(value)
        else:
            result['items'] = [_shape(child, depth=depth+1, budget=budget) for child in value[:3] if budget[0] > 0]
            result['truncated'] = len(result['items']) != len(value)
        return result
    return {'type': 'unsupported'}


def summarize(payload, *, stored_status, stored_source, composer_match):
    from .internal_requests import generation_request_source, SAFETY_PREFIX, SAFETY_CANDIDATES
    inputs = payload.get('input-messages', payload.get('input_messages'))
    latest = inputs[-1] if isinstance(inputs, list) and inputs else None
    text = latest.strip().replace('\r\n', '\n') if isinstance(latest, str) and len(latest) <= 2*1024*1024 else ''
    body = payload.get('last-assistant-message', payload.get('last_assistant_message'))
    try:
        parsed = json.loads(body) if isinstance(body, str) and len(body) <= 2*1024*1024 else None
    except (ValueError, TypeError):
        parsed = None
    return {
        'schema_version': 1,
        'decision': 'preserved_unknown_source',
        'payload_shape': _shape(payload),
        'latest_input_shape': _shape(latest),
        'reply_json_shape': _shape(parsed),
        'stored_thread': {'status': stored_status if stored_status in {'missing', 'stored', 'unavailable'} else 'unavailable',
                          'source': stored_source if stored_source in SAFE_PURPOSES else None},
        # These flags describe known request-template fragments, not provenance.
        # They deliberately never feed the notification filtering decision.
        'request_features': {
            'composer_template_match': composer_match is True,
            'latest_input_length': len(latest) if isinstance(latest, str) else None,
            'latest_input_inspected': isinstance(latest, str) and len(latest) <= 2*1024*1024,
            'ambient_overview_prefix': text.startswith('# Overview\n\nGenerate 0 to 3 hyperpersonalized suggestions for what this user can do with Codex in '),
            'rules_section': '\n# Rules\n' in text,
            'response_format_section': '\n# Response format\n' in text,
            'recent_project_tasks': '\nRecent Codex tasks in this project:\n' in text,
            'recent_projectless_tasks': '\nRecent Projectless Codex tasks with the same cwd:\n' in text,
            'dismissed_suggestions_section': '\nAvoid repeating these previously dismissed suggestions:\n' in text,
            'ambient_template_match': generation_request_source(payload) == 'ambient_suggestions',
            'safety_overview_prefix': text.startswith(SAFETY_PREFIX),
            'safety_candidates_section': SAFETY_CANDIDATES in text,
            'safety_template_match': generation_request_source(payload) == 'ambient_suggestion_safety',
        },
    }


def record(payload, path: Path, *, stored_status, stored_source, composer_match):
    """Deduplicate hook/notify repeats and serialize concurrent writers locally.

    Exceptions are handled by the caller. No credentials or network needed.
    Deletion/retention is applied whenever a sample is written.
    """
    snapshot = summarize(payload, stored_status=stored_status, stored_source=stored_source, composer_match=composer_match)
    encoded = json.dumps(snapshot, ensure_ascii=False, separators=(',', ':'))
    if len(encoded.encode('utf-8')) > MAX_RECORD_BYTES:
        raise ValueError('Diagnostic record exceeds size limit')
    # Hash only random protocol IDs plus the already-redacted structural sample.
    ids = [str(payload.get(key) or '') for key in ('thread-id', 'thread_id', 'turn-id', 'turn_id')]
    event_id = hashlib.sha256(json.dumps([ids, encoded]).encode('utf-8')).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise OSError('Diagnostic database must not be a symlink')
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(descriptor)
    db = sqlite3.connect(path, timeout=0.2)
    try:
        with db:
            db.execute('CREATE TABLE IF NOT EXISTS samples (id TEXT PRIMARY KEY, first_seen REAL, last_seen REAL, occurrences INTEGER, snapshot TEXT)')
            now = time.time()
            db.execute('DELETE FROM samples WHERE last_seen<?', (now-RETENTION_SECONDS,))
            db.execute('INSERT INTO samples VALUES (?,?,?,1,?) ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen, occurrences=occurrences+1',
                       (event_id, now, now, encoded))
            db.execute('DELETE FROM samples WHERE id NOT IN (SELECT id FROM samples ORDER BY last_seen DESC, rowid DESC LIMIT ?)', (MAX_SAMPLES,))
        return event_id[:12]
    finally:
        db.close()
