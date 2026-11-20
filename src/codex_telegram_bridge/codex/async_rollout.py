"""Find async-question signals in local rollouts. Only Desktop history can publish a card."""
from dataclasses import dataclass, field
from hashlib import blake2s
from itertools import islice
import json
import os
from pathlib import Path
import re
import stat
import time

from .history import question_answers
from .receipts import READ_LIMIT

_RECOVERY_BYTES = 256 * 1024
_INVENTORY_INTERVAL = 30
_HOT_SECONDS = 300
_FILES_PER_POLL = 24
_BYTES_PER_POLL = 2 * READ_LIMIT
_THREAD_SUFFIX = re.compile(r"-([0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12})"
                            r"(?:_[A-Za-z0-9-]{6,80})?\.jsonl$")
_TERMINAL = {"task_complete", "turn_aborted"}


@dataclass
class _Cursor:
    identity: tuple[int, int]
    size: int
    mtime: int
    offset: int
    discard_prefix: bool
    caught_up: bool
    anchor: bytes


@dataclass
class Scan:
    questions: dict[tuple[str, str], str] = field(default_factory=dict)
    refresh: set[str] = field(default_factory=set)
    recover: set[str] = field(default_factory=set)
    positions: dict[Path, _Cursor] = field(default_factory=dict)


class AsyncRolloutTail:
    def __init__(self):
        self.cursors = {}
        self.hot = set()
        self.paths = {}
        self.due = {}
        self.next_inventory = float("-inf")

    def scan(self, sessions: Path, interested: set[str]) -> Scan:
        now = time.monotonic()
        inventory = now >= self.next_inventory
        if inventory:
            paths = {}
            for path in sessions.rglob("rollout-*.jsonl"):
                match = _THREAD_SUFFIX.search(path.name)
                if match:
                    paths[path] = match[1]
            self.paths = paths
            self.hot.intersection_update(paths)
            self.cursors = {p: c for p, c in self.cursors.items() if p in paths}
            self.due = {p: info for p, info in self.due.items() if p in paths}
            self.next_inventory = now + _INVENTORY_INTERVAL
        candidates = self.paths if inventory else self.hot.copy()
        if interested and not inventory:
            candidates.update(path for path, thread in self.paths.items() if thread in interested)
        hot_since = time.time() - _HOT_SECONDS
        for path in candidates:
            try:
                info = path.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                self.due.pop(path, None)
                continue
            if info.st_mtime >= hot_since:
                self.hot.add(path)
            else:
                self.hot.discard(path)
            cursor = self.cursors.get(path)
            if (cursor is None or not cursor.caught_up or cursor.mtime != info.st_mtime_ns
                    or cursor.size != info.st_size or cursor.identity != (info.st_dev, info.st_ino)):
                self.due[path] = info

        result = Scan()
        if not self.due:
            return result
        # Reserve a slot for the oldest backlog even under constant writes.
        # Prefer current questions and recently changed files for the rest.
        oldest = next(iter(self.due))
        recent = sorted(self.due, key=lambda p: (self.paths[p] in interested, self.due[p].st_mtime_ns),
                        reverse=True)
        budget = _BYTES_PER_POLL
        for path in islice(dict.fromkeys([oldest, *recent]), _FILES_PER_POLL):
            thread = self.paths[path]
            try:
                if not path.resolve().is_relative_to(sessions.resolve()):
                    self.due.pop(path, None)
                    continue
                cursor, read = self._read(path, thread, thread in interested, result)
            except OSError:
                # Rotate a busy/inaccessible file behind its healthy neighbors.
                self.due[path] = self.due.pop(path)
                continue
            result.positions[path] = cursor
            budget -= read
            if budget < READ_LIMIT:
                break
        return result

    def acknowledge(self, scan: Scan):
        """Advance offsets only after the caller has journaled every observed signal."""
        for path, cursor in scan.positions.items():
            info = self.due.pop(path)
            self.cursors[path] = cursor
            if not cursor.caught_up:
                self.due[path] = info

    def _read(self, path, thread, interested, result):
        with path.open("rb") as stream:
            info = os.fstat(stream.fileno())
            identity = (info.st_dev, info.st_ino)
            size = info.st_size
            previous = self.cursors.get(path)
            reset = (previous is None or previous.identity != identity or size < previous.size
                     or (size == previous.size and previous.mtime != info.st_mtime_ns))
            if not reset and size != previous.size:
                # A truncate-and-regrow between polls can preserve the inode and increase the size.
                reset = _anchor(stream, previous.offset) != previous.anchor
            if reset:
                offset = max(0, size - _RECOVERY_BYTES)
                discard = False
                if offset:
                    stream.seek(offset - 1)
                    discard = stream.read(1) != b"\n"
            else:
                offset, discard = previous.offset, previous.discard_prefix
            stream.seek(offset)
            data = stream.read(min(READ_LIMIT, size - offset))
            end = data.rfind(b"\n") + 1
            consumed = end or (len(data) if len(data) == READ_LIMIT else 0)
            anchor = _anchor(stream, offset + consumed)
        start = data.find(b"\n") + 1 if discard else 0
        terminal = _collect_signals(data[start:end], thread, interested, result)
        if reset and offset and not terminal:
            # A live question may precede the sampled tail, at any file age.
            result.recover.add(thread)
        if end:
            if discard and not terminal:
                result.recover.add(thread)
            offset += end
            discard = False
        elif len(data) == READ_LIMIT:
            offset += len(data)
            discard = True
            result.recover.add(thread)
        caught_up = offset == size or len(data) < READ_LIMIT
        return _Cursor(identity, size, info.st_mtime_ns, offset, discard, caught_up, anchor), len(data)


def _anchor(stream, offset):
    length = min(64, offset)
    stream.seek(offset - length)
    return blake2s(stream.read(length), digest_size=16).digest()


def _collect_signals(data, thread, interested, result):
    questions = {}
    ended, active = set(), set()
    for line in data.split(b"\n"):
        if not any(marker in line for marker in (b'"questions"', b'"task_started"', b'"task_complete"',
                b'"turn_aborted"', b'send_user_message_question_reply')):
            continue
        try:
            record = json.loads(line)
        except (ValueError, UnicodeError, RecursionError):
            continue
        if not isinstance(record, dict):
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("thread_id", thread) != thread:
            continue
        event = payload.get("type")
        if record.get("type") == "response_item":
            if (interested and event == "function_call_output" and payload.get("namespace") == "codex_app"
                    and payload.get("name") == "send_message_to_thread"
                    and question_answers([{**payload, "type": "functionCallOutput"}])):
                result.refresh.add(thread)
            continue
        if record.get("type") != "event_msg":
            continue
        turn = payload.get("turn_id")
        if event in ("task_started", "task_complete", "turn_aborted"):
            if isinstance(turn, str) and turn:
                if event in _TERMINAL:
                    ended.add(turn)
                    active.discard(turn)
                else:
                    ended.discard(turn)
                    active.add(turn)
            if interested:
                result.refresh.add(thread)
            continue
        item = payload.get("item")
        if event != "item_completed" or not isinstance(item, dict):
            continue
        kind = item.get("type")
        if interested and kind in ("UserMessage", "SteeringUserMessage", "userMessage", "steeringUserMessage"):
            if b'send_user_message_question_reply' in line:
                result.refresh.add(thread)
        item_id = item.get("id")
        if (kind in ("AgentMessage", "agentMessage") and isinstance(item.get("questions"), list)
                and item["questions"] and isinstance(item_id, str) and item_id
                and isinstance(turn, str) and turn):
            questions[(thread, item_id)] = turn
            active.add(turn)
            ended.discard(turn)
    # A terminal record in an obsolete copy cannot close a question in a rewritten file.
    result.questions.update((key, turn) for key, turn in questions.items() if turn not in ended)
    return bool(ended) and not active
