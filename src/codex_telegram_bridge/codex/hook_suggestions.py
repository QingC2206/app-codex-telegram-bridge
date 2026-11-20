"""Collect composer candidates from Desktop completion hooks before delivery filtering."""
from contextlib import closing, contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import time
from uuid import UUID

import psutil

from .composer_approval import TTL, approved_in_log, auth_stamp
from .desktop import discover_desktop
from .suggestion_source import HOOK_SOURCE, ID
from ..telegram import completion_routing as routing
from ..telegram import notifications as notify


PURPOSE = 'composer_predictions'
MAX_SAMPLES = 1024
_next_cleanup = 0.0
_PAIRS = (('prediction_source_thread_id', 'prediction_source_turn_id'),
          ('sourceThreadId', 'sourceTurnId'), ('source_thread_id', 'source_turn_id'),
          ('parentThreadId', 'parentTurnId'), ('parent_thread_id', 'parent_turn_id'))
_METADATA = ('responsesapiClientMetadata', 'responsesapi_client_metadata', 'turnAttribution', 'turn_attribution')


def _target(payload):
    # Never take the event's own IDs or IDs mentioned in a prompt/assistant reply.
    containers = [payload]
    for field in _METADATA:
        if field in payload and payload[field] is not None:
            if not isinstance(payload[field], dict):
                raise ValueError('Invalid composer metadata')
            containers.append(payload[field])
    parts = [set(), set()]
    for container in containers:
        for first, second in _PAIRS:
            pair = [container.get(first), container.get(second)]
            for values, item in zip(parts, pair):
                if item is not None:
                    if not isinstance(item, str) or not ID.fullmatch(item):
                        raise ValueError('Invalid composer target')
                    values.add(item)
    if any(len(values) > 1 for values in parts):
        raise ValueError('Conflicting composer target')
    return tuple(next(iter(values), None) for values in parts)


def _reply(payload):
    if ('last-assistant-message' in payload and 'last_assistant_message' in payload and
            payload['last-assistant-message'] != payload['last_assistant_message']):
        raise ValueError('Conflicting composer results')
    raw = notify._assistant_message(payload)
    if raw is None:
        return None  # The other transport may deliver just the binding.
    if len(raw) > 32768:
        raise ValueError('Oversized composer envelope')
    pairs = json.loads(raw, object_pairs_hook=lambda items: items)
    if (not isinstance(pairs, list) or len(pairs) != 1 or not isinstance(pairs[0], tuple)
            or pairs[0][0] != 'suggestion'):
        raise ValueError('Unexpected composer envelope')
    text = pairs[0][1]
    if text is None:
        return ''  # An explicit abstention cannot acquire text from a retry.
    if not isinstance(text, str) or len(text) > 3900 or any(
            ord(c) < 32 and c not in '\n\t' for c in text):
        raise ValueError('Invalid composer text')
    text.encode('utf-8')  # Reject unpaired surrogates.
    return text if text.strip() else ''


def _context(endpoint):
    if endpoint.profile != notify.SOURCE_STATE_ROOT.resolve():
        return None
    stamp = auth_stamp(endpoint.profile)
    if stamp is None:
        return None
    born = psutil.Process(endpoint.pid).create_time()
    key = [str(endpoint.profile), endpoint.pid, born, endpoint.pipe, stamp, notify.completion_generation()]
    session_start = max(born, stamp[2] / 1_000_000_000)
    return sha256(json.dumps(key).encode()).hexdigest()[:32], session_start


def _path():
    return notify.HOOK_DIR / 'telegram_hook_suggestions.sqlite3'


@contextmanager
def _journal():
    path = _path()
    if path.is_symlink():
        raise OSError('Invalid hook suggestion journal')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
    with closing(sqlite3.connect(path, timeout=2)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA secure_delete=ON')
        db.execute('BEGIN IMMEDIATE')
        db.execute('''CREATE TABLE IF NOT EXISTS samples(
            epoch TEXT, child TEXT, turn TEXT, thread_hint TEXT, turn_hint TEXT,
            text TEXT, digest TEXT, response TEXT, blocked INTEGER,
            created REAL, PRIMARY KEY(epoch,child,turn))''')
        db.execute('''CREATE TABLE IF NOT EXISTS targets(
            epoch TEXT, child TEXT, turn TEXT, thread TEXT, source_turn TEXT,
            PRIMARY KEY(epoch,child,turn,thread,source_turn),
            FOREIGN KEY(epoch,child,turn) REFERENCES samples ON DELETE CASCADE)''')
        db.execute('CREATE INDEX IF NOT EXISTS target_lookup ON targets(thread,source_turn,epoch)')
        db.execute('DELETE FROM samples WHERE created<=?', (time.time() - TTL,))
        yield db


def _remember(epoch, event, target, text, response, *, binding_only=False, invalid=False):
    key = (epoch, *event)
    digest = sha256(text.encode()).hexdigest() if text is not None else None
    if binding_only:
        text = None
    with _journal() as db:
        old = db.execute('SELECT * FROM samples WHERE epoch=? AND child=? AND turn=?', key).fetchone()
        if old is None:
            if db.execute('SELECT count(*) FROM samples').fetchone()[0] >= MAX_SAMPLES:
                db.execute('''UPDATE samples SET blocked=1,text=NULL WHERE (epoch,child,turn) IN
                    (SELECT epoch,child,turn FROM targets WHERE epoch=? AND thread=? AND source_turn=?)''',
                    (epoch, *target))
                return
            db.execute('INSERT INTO samples VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (*key, *target, None if invalid else text, digest, response, int(invalid), time.time()))
        else:
            previous = (old['thread_hint'], old['turn_hint'])
            pair = tuple(new or known for new, known in zip(target, previous))
            if any(new and known and new != known for new, known in zip(target, previous)):
                invalid = True
                earlier = tuple(known or new for new, known in zip(target, previous))
                if all(earlier):
                    db.execute('INSERT OR IGNORE INTO targets VALUES (?,?,?,?,?)', (*key, *earlier))
            target = pair
            invalid = (invalid or bool(old['blocked']) or target == event
                       or (old['response'] is not None and response is not None and old['response'] != response)
                       or (old['digest'] is not None and digest is not None and old['digest'] != digest))
            merged = text if text is not None else old['text']
            db.execute('''UPDATE samples SET text=?,digest=coalesce(digest,?),response=coalesce(response,?),
                       blocked=?,thread_hint=coalesce(thread_hint,?),turn_hint=coalesce(turn_hint,?)
                       WHERE epoch=? AND child=? AND turn=?''',
                       (None if invalid else merged, digest, response, int(invalid), *target, *key))
        if all(target):
            db.execute('INSERT OR IGNORE INTO targets VALUES (?,?,?,?,?)', (*key, *target))
        bindings = db.execute('SELECT thread,source_turn FROM targets WHERE epoch=? AND child=? AND turn=? LIMIT 2',
                              key).fetchall()
        if len(bindings) > 1:
            db.execute('UPDATE samples SET blocked=1,text=NULL WHERE epoch=? AND child=? AND turn=?', key)
        elif bindings:
            return tuple(bindings[0])
        elif target[1]:
            return target  # Wake matching turn(s) to check the recorded fork parent.


def capture(payload, decision=None):
    """Called in the actual hook process. Never authorizes or suppresses a completion."""
    if (not isinstance(payload, dict) or payload.get('hook_event_name') not in (None, 'Stop') or
            (payload.get('hook_event_name') != 'Stop' and payload.get('type') != 'agent-turn-complete') or
            payload.get('agent_id') or payload.get('client') not in (None, 'Codex Desktop') or
            os.environ.get('CODEX_TELEGRAM_MANAGED_APP_SERVER') == '1' or
            os.environ.get('CODEX_TELEGRAM_SIDE_CHAT') == '1'):
        return
    if decision is None:
        # The inbox hook may run before its ephemeral child has any rollout.
        # Avoid doing provenance I/O on all ordinary notifications.
        raw = notify._assistant_message(payload)
        if raw is not None and not raw.lstrip().startswith('{'):
            return
        decision = routing.classify(payload, notify.SOURCE_STATE_ROOT)
    if not all(ID.fullmatch(value or '') for value in (decision.thread_id, decision.turn_id)):
        return
    purposes = {decision.thread_source, decision.turn_trigger}
    explicit = (decision.kind == 'internal' and PURPOSE in purposes
                and decision.thread_source in routing.CONVERSATION_SOURCES | {None, PURPOSE}
                and decision.turn_trigger in routing.CONVERSATION_TRIGGERS | {None, PURPOSE})
    legacy = (decision.reason == 'unverified-local-task' and decision.stored_status == 'missing'
              and purposes == {None} and notify._composer_prediction_request(payload))
    binding_only = (not legacy and decision.reason == 'unverified-local-task' and
                    decision.stored_status == 'missing' and purposes == {None})
    if not (explicit or legacy or binding_only) or decision.record.get('originator') not in (None, 'Codex Desktop'):
        return
    if binding_only and not any(field in payload for field in (*_METADATA, *(key for pair in _PAIRS for key in pair))):
        return
    containers = [payload, *(payload.get(field) for field in ('thread', *_METADATA))]
    if any(container.get(field) not in (None, 'local') for container in containers if isinstance(container, dict)
           for field in ('hostId', 'host_id', 'sourceHostId', 'source_host_id')):
        return
    if notify.notifications_muted() or not notify.CONFIG_PATH.is_file():
        return
    settings = json.loads(notify.CONFIG_PATH.read_text(encoding='utf-8-sig'))
    if (settings.get('include_message_preview') is not True or
            settings.get('desktop_suggestion_source', HOOK_SOURCE) not in (HOOK_SOURCE, 'windows-desktop')):
        return
    transcript = payload.get('transcript_path')
    if transcript is not None and (not isinstance(transcript, str) or
            not Path(transcript).resolve().is_relative_to(notify.SOURCE_STATE_ROOT.resolve())):
        return
    endpoint = discover_desktop(notify.SOURCE_STATE_ROOT)
    if not any(p.pid == endpoint.pid for p in psutil.Process().parents()):
        return
    context = _context(endpoint)
    if context is None:
        return
    event = (decision.thread_id, decision.turn_id)
    target = (None, None)
    try:
        target = _target(payload)
        if binding_only and not any(target):
            return
        if target == event:
            raise ValueError('Composer child cannot identify its own source turn')
        response = payload.get('responseId', payload.get('response_id'))
        if (response is not None and (not isinstance(response, str) or not 0 < len(response.strip()) <= 256
                or payload.get('response_id', response) != response)):
            raise ValueError('Invalid composer response ID')
        text = _reply(payload)
    except (ValueError, RecursionError):
        if not binding_only:
            _remember(context[0], event, target, None, None, invalid=True)
        return
    if text is not None or any(target):
        bound = _remember(context[0], event, target, text, response, binding_only=binding_only)
        if bound:
            from ..features.suggested_replies import wake_hook_card
            wake_hook_card(*bound)


def _candidate(epoch, thread, turn):
    if not _path().is_file():
        return None
    with _journal() as db:
        # Some callbacks identify only the source turn. The profile may acquire
        # the fork edge later. A native parent alone never supplies a source turn.
        rows = db.execute('''SELECT samples.* FROM targets JOIN samples USING(epoch,child,turn)
            WHERE epoch=? AND thread=? AND source_turn=? LIMIT 2''', (epoch, thread, turn)).fetchall()
        partial = db.execute('''SELECT * FROM samples WHERE epoch=? AND turn_hint=?
            AND thread_hint IS NULL''', (epoch, turn)).fetchall()
        for row in partial:
            parent = routing.spawn_parent(notify.SOURCE_STATE_ROOT, row['child'])
            if not ID.fullmatch(parent or ''):
                return None
            if parent == thread:
                rows.append(row)
    if len(rows) == 1 and not rows[0]['blocked'] and rows[0]['text']:
        row = rows[0]
        seen = row['created']
        # UUIDv7 carries the creation millisecond of this specific generation.
        # Otherwise only decisions after the first callback can be attributed.
        generated = UUID(row['turn'])
        started = (generated.int >> 80) / 1000
        if generated.version != 7:
            started = seen
        elif not 0 <= seen - started <= TTL:
            return None
        return row['child'] + '/' + row['turn'], row['text'], started
    return None


def has_target(thread, turn):
    if not _path().is_file():
        return False
    with _journal() as db:
        return db.execute('''SELECT 1 FROM targets WHERE thread=? AND source_turn=? UNION ALL
            SELECT 1 FROM samples WHERE thread_hint IS NULL AND turn_hint=? LIMIT 1''',
                          (thread, turn, turn)).fetchone() is not None


def prune_if_due():
    """Expire orphaned text even if the service receives no more callbacks."""
    global _next_cleanup
    now = time.monotonic()
    if now >= _next_cleanup:
        _next_cleanup = now + 300
        if _path().is_file():
            with _journal():
                pass  # Opening the journal expires all old rows and their bindings.


def read_approved(endpoint, thread, turn):
    if not all(isinstance(value, str) and ID.fullmatch(value) for value in (thread, turn)):
        return None
    context = _context(endpoint)
    if context is None:
        return None
    epoch, session_start = context
    scope = dict(hostId='local', threadId=thread, sourceTurnId=turn, principalEpoch=epoch)
    candidate = _candidate(epoch, thread, turn)
    if candidate and approved_in_log(endpoint.pid, (thread, turn), max(session_start, candidate[2]), callback=True):
        if _context(endpoint) != context:
            return None
        if _candidate(epoch, thread, turn) == candidate:
            return dict(scope, responseId=candidate[0], text=candidate[1])
    return scope
