"""Bounded metadata-only completion verification queue and delivery reservations.

No prompt, response, credential, or workspace path is persisted here. A delayed
reply is reconstructed from the exact local turn and must match its digest.
An attempted Telegram send is never automatically replayed by this queue.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

from . import completion_routing as routing

TTL = 86400
RETENTION = 7 * 86400
MAX_PENDING = 200


def digest(thread, turn, text):
    return hashlib.sha256(json.dumps([thread, turn, text], ensure_ascii=False).encode()).hexdigest()


def profile_key(profile):
    return hashlib.sha256(os.path.normcase(str(profile.resolve())).encode()).hexdigest()


@contextmanager
def database(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise OSError('Completion queue must not be a symlink')
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(descriptor)
    db = sqlite3.connect(path, timeout=2.0)
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS completions (profile TEXT, thread TEXT, turn TEXT, digest TEXT, metadata TEXT, created REAL, next_check REAL, attempts INTEGER, state TEXT, PRIMARY KEY(profile,thread,turn))')
            db.execute('DELETE FROM completions WHERE created<?', (time.time()-RETENTION,))
        yield db
    finally:
        db.close()


def defer(payload, decision, profile, path, *, generation=0):
    thread, turn, valid = routing.identity(payload)
    body = payload.get('last-assistant-message', payload.get('last_assistant_message'))
    if not valid or not turn or not isinstance(body, str):
        return False
    # Conflicting/malformed attributes cannot be repaired by deleting the bad
    # evidence on replay. Keep these in structural diagnostics only.
    if decision.reason not in {'unverified-local-task', 'missing-desktop-origin', 'unverified-local-turn'}:
        return False
    metadata = {'_quiet_generation': generation}
    if decision.requires_turn_evidence:
        # Never store an arbitrary client label or turn a mobile callback into
        # a legacy Desktop callback on replay. Only the extra proof requirement
        # survives; attribution must still come from the exact local turn.
        metadata['_requires_turn_evidence'] = True
    if decision.thread_source:
        if decision.thread_source not in routing.CONVERSATION_SOURCES | {'subagent'}:
            return False
        metadata['threadSource'] = decision.thread_source
    if decision.turn_trigger:
        if decision.turn_trigger not in routing.CONVERSATION_TRIGGERS:
            return False
        metadata['turnTrigger'] = decision.turn_trigger
    now = time.time()
    key = profile_key(profile)
    with database(path) as db, db:
        count = db.execute("SELECT COUNT(*) FROM completions WHERE profile=? AND state='pending'", (key,)).fetchone()[0]
        if count >= MAX_PENDING:
            return False
        db.execute('INSERT OR IGNORE INTO completions VALUES (?,?,?,?,?,?,?,0,?)',
                   (key, thread, turn, digest(thread, turn, body), json.dumps(metadata), now, now+2, 'pending'))
    return True


def reserve(payload, profile, path):
    """Reserve before side effects; hook/notify/verification share this identity."""
    thread, turn, valid = routing.identity(payload)
    if not valid or not turn:
        return True  # Legacy hooks without a verifiable turn cannot be deduped.
    key = profile_key(profile)
    body = payload.get('last-assistant-message', payload.get('last_assistant_message', ''))
    value = digest(thread, turn, body)
    with database(path) as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT state,digest FROM completions WHERE profile=? AND thread=? AND turn=?', (key, thread, turn)).fetchone()
        if row and (row[0] != 'pending' or row[1] != value):
            db.rollback()
            return False
        db.execute('INSERT INTO completions VALUES (?,?,?,?,?,?,?,0,?) ON CONFLICT(profile,thread,turn) DO UPDATE SET state=excluded.state',
                   (key, thread, turn, value, '{}', time.time(), 0, 'attempted'))
        db.commit()
    return True


def ready(profile, path, *, muted=False, generation=0):
    """Bound each scan; rechecking provenance never resubmits work to Desktop."""
    if not Path(path).is_file():
        return []
    key, now = profile_key(profile), time.time()
    with database(path) as db, db:
        db.execute("UPDATE completions SET state='expired' WHERE profile=? AND state='pending' AND created<?", (key, now-TTL))
        if muted:
            db.execute("UPDATE completions SET state='muted' WHERE profile=? AND state='pending'", (key,))
            return []
        rows = db.execute("SELECT thread,turn,digest,metadata,attempts FROM completions WHERE profile=? AND state='pending' AND next_check<=? ORDER BY next_check LIMIT 8", (key, now)).fetchall()
        for thread, turn, _, _, attempts in rows:
            db.execute('UPDATE completions SET next_check=?,attempts=attempts+1 WHERE profile=? AND thread=? AND turn=?',
                       (now+min(300, 2**min(8, attempts+2)), key, thread, turn))
    replies = []
    for thread, turn, expected, encoded, _ in rows:
        metadata = json.loads(encoded)
        if metadata.pop('_quiet_generation', 0) != generation:
            with database(path) as db, db:
                db.execute("UPDATE completions SET state='muted' WHERE profile=? AND thread=? AND turn=? AND state='pending'", (key, thread, turn))
            continue
        payload = {'type': 'agent-turn-complete', 'client': 'Codex Desktop', 'thread-id': thread, 'turn-id': turn, **metadata}
        decision = routing.classify(payload, profile)
        if decision.deliver:
            _, text = routing.turn_evidence(profile, decision.record, thread, turn)
            if isinstance(text, str) and digest(thread, turn, text) == expected:
                replies.append({**payload, 'last-assistant-message': text, 'cwd': decision.record.get('cwd', '')})
        elif decision.kind in {'internal', 'foreign'}:
            with database(path) as db, db:
                db.execute("UPDATE completions SET state=? WHERE profile=? AND thread=? AND turn=? AND state='pending'", (decision.kind, key, thread, turn))
    return replies
