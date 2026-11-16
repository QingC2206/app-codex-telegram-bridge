"""Read async-question signals from appended Codex session records."""

from dataclasses import dataclass
import json
from pathlib import Path
import re
import stat
import time

from .receipts import READ_LIMIT

_RECOVERY_BYTES = 256 * 1024
_RECOVERY_AGE = 1800
_THREAD_SUFFIX = re.compile(r"-([0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12})\.jsonl$")
_LIFECYCLE = {"task_started", "task_complete", "turn_aborted"}


@dataclass
class _Cursor:
    identity: tuple[int, int]
    offset: int
    size: int
    discard_prefix: bool = False
    checked: tuple[int, int] | None = None


class AsyncRolloutTail:
    def __init__(self):
        self.cursors: dict[Path, _Cursor] = {}
        self.initial = True

    def scan(self, sessions: Path, pending: set[str]):
        questions: dict[tuple[str, str], str] = {}
        changed: set[str] = set()
        local: set[str] = set()
        present: set[Path] = set()
        cutoff = time.time() - _RECOVERY_AGE
        for path in sessions.rglob("rollout-*.jsonl"):
            present.add(path)
            match = _THREAD_SUFFIX.search(path.name)
            thread = match.group(1) if match else ""
            if not thread:
                continue
            try:
                info = path.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    continue
                identity = (info.st_dev, info.st_ino)
                cursor = self.cursors.get(path)
                if cursor is None or cursor.identity != identity or info.st_size < cursor.size:
                    # At startup only recover recent tails. Files created while
                    # the observer is running are new from their first record.
                    offset = 0
                    if self.initial:
                        offset = (max(0, info.st_size - _RECOVERY_BYTES)
                                  if info.st_mtime >= cutoff else info.st_size)
                    cursor = _Cursor(identity, offset, info.st_size, 0 < offset < info.st_size)
                    self.cursors[path] = cursor
                cursor.size = info.st_size
                local.add(thread)
                stamp = (info.st_mtime_ns, info.st_size)
                if cursor.checked == stamp or cursor.offset == info.st_size:
                    continue
                with path.open("rb") as stream:
                    stream.seek(cursor.offset)
                    data = stream.read(min(READ_LIMIT, info.st_size - cursor.offset))
                end = data.rfind(b"\n") + 1
                if not end:
                    if len(data) == READ_LIMIT:
                        # Drop an oversized record without treating a fragment
                        # of it as the beginning of a later JSON record.
                        cursor.offset += len(data)
                        cursor.discard_prefix = True
                    else:
                        cursor.checked = stamp
                    continue
                start = data.find(b"\n") + 1 if cursor.discard_prefix else 0
                cursor.discard_prefix = False
                cursor.offset += end
                if len(data) < READ_LIMIT or cursor.offset == info.st_size:
                    cursor.checked = stamp
                _collect_signals(data[start:end], thread, thread in pending, questions, changed)
            except OSError:
                # One inaccessible or rotated file must not block other chats.
                continue
        self.cursors = {path: cursor for path, cursor in self.cursors.items() if path in present}
        self.initial = False
        return questions, changed, local


def _collect_signals(data, thread, pending, questions, changed):
    for line in data.split(b"\n"):
        question_marker = b'"questions"' in line and b'"item_completed"' in line
        state_marker = pending and any(marker in line for marker in (
            b'"task_started"', b'"task_complete"', b'"turn_aborted"',
            b'<send_user_message_question_reply>',
        ))
        if not question_marker and not state_marker:
            continue
        try:
            record = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(record, dict) or record.get("type") != "event_msg":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("thread_id", thread) != thread:
            continue
        event = payload.get("type")
        if pending and event in _LIFECYCLE:
            changed.add(thread)
        item = payload.get("item")
        if event != "item_completed" or not isinstance(item, dict):
            continue
        if (pending and item.get("type") in ("UserMessage", "SteeringUserMessage", "userMessage", "steeringUserMessage")
                and b'<send_user_message_question_reply>' in line):
            changed.add(thread)
        item_id, turn_id = item.get("id"), payload.get("turn_id")
        if (item.get("type") in ("AgentMessage", "agentMessage")
                and isinstance(item.get("questions"), list) and item["questions"]
                and isinstance(item_id, str) and item_id and isinstance(turn_id, str) and turn_id):
            questions[(thread, item_id)] = turn_id
