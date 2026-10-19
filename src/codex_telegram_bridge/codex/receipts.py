"""Read-only correlation of Desktop messages with explicit rollout turn IDs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any

READ_LIMIT = 4 * 1024 * 1024


def prompt_digest(thread_id: str, text: str, *, origin: str = 'user') -> str:
    if origin == 'user':
        # The caller pins the task's rollout. A type prefix separates normal
        # user input from an old delegation with the same-looking body.
        return hashlib.sha256(('user\0' + text).encode('utf-8')).hexdigest()
    if origin != 'delegation':
        raise ValueError('Unknown message origin')
    # Desktop escapes delegation text, including the typed async-answer tags.
    # Quotes remain literal; only XML text delimiters are escaped.
    text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    envelope = ("<codex_delegation>\n  <source_thread_id>" + thread_id
                + "</source_thread_id>\n  <input>" + text + "</input>\n</codex_delegation>")
    return hashlib.sha256(envelope.encode("utf-8")).hexdigest()


def session_path(thread_id: str, sessions: Path) -> Path | None:
    if not re.fullmatch(r"[A-Za-z0-9-]{6,64}", thread_id):
        return None
    state_root = sessions.parent.resolve()
    def valid(value: Any) -> Path | None:
        if not isinstance(value, str) or not value:
            return None
        path = Path(value).resolve()
        if (path.is_relative_to(state_root) and path.name.startswith("rollout-")
                and path.name.endswith(thread_id + ".jsonl") and path.is_file()):
            return path
        return None
    databases = [p for p in state_root.glob("state_*.sqlite") if re.fullmatch(r"state_[0-9]+\.sqlite", p.name)]
    databases.sort(key=lambda p: int(p.stem.split('_')[1]), reverse=True)
    for database in databases:
        try:
            db = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)
            try:
                row = db.execute("SELECT rollout_path FROM threads WHERE id=?", (thread_id,)).fetchone()
            finally:
                db.close()
            path = valid(row[0]) if row else None
            if path:
                return path
        except (OSError, sqlite3.Error):
            continue
    for root in (sessions, state_root / "archived_sessions"):
        for path in root.glob(f"**/rollout-*{thread_id}.jsonl"):
            candidate = valid(str(path))
            if candidate:
                return candidate
    return None


def checkpoint(thread_id: str, sessions: Path) -> tuple[str, int]:
    path = session_path(thread_id, sessions)
    return (str(path), path.stat().st_size) if path else ("", 0)


def read_receipts(path: Path, offset: int) -> tuple[list[dict[str, Any]], int]:
    """Scan complete appended records; leave an unfinished final line for retry."""
    with path.open("rb") as f:
        size = path.stat().st_size
        if offset < 0 or offset > size:
            # A replaced/truncated file cannot prove which inputs are new.
            return [], offset
        f.seek(offset)
        data = f.read(READ_LIMIT)
    end = data.rfind(b"\n") + 1
    if not end:
        return [], offset + len(data) if len(data) == READ_LIMIT else offset
    receipts = []
    for line in data[:end].splitlines():
        try:
            record = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        payload = record.get("payload") if isinstance(record, dict) else None
        if not isinstance(payload, dict) or record.get("type") != "response_item":
            continue
        metadata = payload.get("internal_chat_message_metadata_passthrough")
        origin = ''
        digest = ''
        if payload.get('type') == 'message' and payload.get('role') == 'user':
            content = payload.get('content')
            if (isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict)
                    and content[0].get('type') == 'input_text' and isinstance(content[0].get('text'), str)):
                origin = 'user'
                digest = prompt_digest('', content[0]['text'])
        elif (payload.get('type') == 'function_call_output' and payload.get('namespace') == 'codex_app'
              and payload.get('name') == 'send_message_to_thread' and isinstance(payload.get('output'), str)):
            # Keep reconciliation of pre-upgrade pending records working. New
            # input never uses this route, and live acceptance rejects it.
            origin = 'delegation'
            digest = hashlib.sha256(payload['output'].encode('utf-8')).hexdigest()
        if not origin:
            continue
        item_id = payload.get("id")
        turn_id = metadata.get("turn_id") if isinstance(metadata, dict) else None
        if not isinstance(turn_id, str) or not turn_id or not isinstance(item_id, str) or not item_id:
            continue
        receipts.append({"item_id": item_id, "turn_id": turn_id,
                         "created": metadata.get("create_time"),
                         "origin": origin, "digest": digest})
    return receipts, offset + end


def completion_turn(thread_id: str, final_message: Any, sessions: Path) -> str:
    """Recover missing hook IDs only from an unambiguous matching completion."""
    if not isinstance(final_message, str) or not final_message.strip():
        return ""
    path = session_path(thread_id, sessions)
    if path is None:
        return ""
    try:
        with path.open("rb") as f:
            f.seek(max(0, path.stat().st_size - READ_LIMIT))
            data = f.read(READ_LIMIT)
    except OSError:
        return ""
    matches = set()
    for line in data.splitlines():
        try:
            record = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        payload = record.get("payload") if isinstance(record, dict) else None
        if (isinstance(payload, dict) and record.get("type") == "event_msg"
                and payload.get("type") == "task_complete"
                and str(payload.get("last_agent_message") or "").strip() == final_message.strip()
                and isinstance(payload.get("turn_id"), str) and payload["turn_id"]):
            matches.add(payload["turn_id"])
    return next(iter(matches)) if len(matches) == 1 else ""


def native_start_receipts(history: Any, thread_id: str) -> list[dict[str, Any]]:
    """Only the first user item of an explicitly dated new turn proves a start.

    Live items lack per-item timestamps. Never use this fallback for steering
    into an older turn, assistant text, quoted tool output, or truncated input.
    """
    import math
    if (not isinstance(history, dict) or not isinstance(history.get('thread'), dict)
            or history['thread'].get('id') != thread_id):
        return []
    turns = history.get('turns')
    if not isinstance(turns, list):
        return []
    result = []
    for turn in turns[:3]:
        if not isinstance(turn, dict):
            continue
        started = turn.get('startedAt')
        turn_id = turn.get('id')
        items = turn.get('items')
        if (type(started) not in (int, float) or not math.isfinite(started)
                or not isinstance(turn_id, str) or not turn_id
                or not isinstance(items, list) or not items):
            continue
        # Restrict to the first item, not an arbitrary later same-text message.
        item = items[0]
        if not isinstance(item, dict) or item.get('type') != 'userMessage':
            continue
        item_id, content = item.get('id'), item.get('content')
        if (not isinstance(item_id, str) or not item_id or not isinstance(content, list)
                or len(content) != 1 or not isinstance(content[0], dict)
                or content[0].get('type') != 'text' or not isinstance(content[0].get('text'), str)
                or item.get('truncated') or item.get('textTruncated') or content[0].get('truncated')):
            continue
        result.append({'turn_id': turn_id, 'item_id': item_id, 'created': started,
                       'origin': 'user', 'digest': prompt_digest(thread_id, content[0]['text'])})
    return sorted(result, key=lambda receipt: receipt['created'])
