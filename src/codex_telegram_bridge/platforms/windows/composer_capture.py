"""Observe only Desktop composer samples and account boundaries on local stdio."""
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time

from ...codex.suggestion_source import ID


SOURCE = 'windows-desktop'
TTL = 47 * 3600
MAX_TURNS = 1024
MAX_FRAME = 2 * 1024 * 1024
AUTH_METHODS = {'account/updated', 'account/login/start', 'account/login/completed',
                'account/logout', 'account/switch', 'account/authTokens/refresh'}
SAFETY = re.compile(rb'^(\S+) info \[cumberbatch\] cumberbatch_safety_decision '
                    rb'sourceThreadId=([0-9a-f-]{36}) sourceTurnId=([0-9a-f-]{36}) suppressed=(true|false)\b')


def key_for(params):
    if not isinstance(params, dict):
        return None
    thread, turn = params.get('threadId'), params.get('expectedTurnId')
    if all(isinstance(x, str) and ID.fullmatch(x) for x in (thread, turn)):
        return thread, turn
    return None


def auth_stamp(profile):
    # A Cockpit/external account switch may replace this file without an RPC.
    # Check metadata only; token refreshes conservatively expire old buttons.
    try:
        stat = (profile / 'auth.json').stat()
        return stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns
    except OSError:
        return None


def approved_in_log(pid, key, requested):
    local = os.environ.get('LOCALAPPDATA')
    if not local:
        return False
    roots = (Path(local) / 'Codex/Logs',
             Path(local) / 'Packages/OpenAI.Codex_2p2nqsd0c76g0/LocalCache/Local/Codex/Logs')
    now = time.time()
    if not 0 <= now - requested <= TTL:
        return False
    # Include the intervening day around midnight, local time and UTC.
    dates = {dt.strftime('%Y/%m/%d') for ts in (requested, (requested + now)/2, now)
             for dt in (datetime.fromtimestamp(ts), datetime.fromtimestamp(ts, timezone.utc))}
    needles = tuple(s.encode('ascii') for s in key)
    found = 0
    for root in roots:
        for day in dates:
            for file in (root/day).glob(f'codex-desktop-*-{pid}-t0-i*-*.log'):
                try:
                    with file.open('rb') as log:
                        # Inspect only recent decisions from this exact Desktop.
                        # Missing or truncated evidence yields no suggestion.
                        size = file.stat().st_size
                        offset = max(0, size - 1024 * 1024)
                        log.seek(offset)
                        if offset:
                            log.readline()
                        for line in log:
                            if b'cumberbatch_safety_decision' not in line or any(k not in line for k in needles):
                                continue
                            row = SAFETY.match(line)
                            if not row or row.group(2, 3) != needles:
                                continue
                            stamp = datetime.fromisoformat(row[1].decode().replace('Z', '+00:00')).timestamp()
                            if stamp < requested or row[4] != b'false' or found:
                                return False
                            found += 1
                except (OSError, ValueError, OverflowError):
                    return False
    return found == 1


@dataclass
class Sample:
    requested: float
    response_id: str = ''
    text: str = ''
    ambiguous: bool = False


class Capture:
    def __init__(self, profile, desktop_pid, *, decision=approved_in_log):
        self.profile = profile
        self.desktop_pid = desktop_pid
        self.decision = decision
        self.lock = threading.RLock()
        self.stamp = auth_stamp(profile)
        self.epoch = secrets.token_hex(16)
        self.samples = {}
        self.pending = {}

    def reset(self):
        with self.lock:
            self.epoch = secrets.token_hex(16)
            self.samples.clear()
            self.pending.clear()
            self.stamp = auth_stamp(self.profile)

    def _refresh(self):
        if auth_stamp(self.profile) != self.stamp:
            self.reset()

    def _prune(self):
        cutoff = time.time() - TTL
        for key in [k for k, sample in self.samples.items() if sample.requested < cutoff]:
            del self.samples[key]
        self.pending = {rid: key for rid, key in self.pending.items() if key in self.samples}

    def consume(self, line, *, incoming):
        # Ordinary requests, conversation JSON replies and model token deltas
        # do not enter the sample cache.
        if not (b'"account/' in line or (incoming and b'"thread/sample"' in line)
                or (self.pending and b'"id"' in line)):
            return
        try:
            msg = json.loads(line)
        except (ValueError, RecursionError):
            return
        if not isinstance(msg, dict):
            return
        method = msg.get('method')
        if isinstance(method, str) and method in AUTH_METHODS:
            self.reset()
            return
        rid = msg.get('id')
        if type(rid) not in (int, str) or isinstance(rid, str) and len(rid) > 256:
            return
        with self.lock:
            self._refresh()
            if self.stamp is None:
                return
            if incoming:
                if method != 'thread/sample':
                    if method is not None and rid in self.pending:
                        self.samples[self.pending.pop(rid)].ambiguous = True
                    return
                params = msg.get('params')
                key = key_for(params)
                if not key or params.get('threadSource') != 'composer_predictions':
                    if rid in self.pending:
                        self.samples[self.pending.pop(rid)].ambiguous = True
                    return
                self._prune()
                previous = self.pending.pop(rid, None)
                if previous is not None:
                    self.samples[previous].ambiguous = True
                if key in self.samples:
                    self.samples[key].ambiguous = True
                    self.samples[key].text = ''
                    return
                if len(self.samples) >= MAX_TURNS:
                    return
                sample = self.samples[key] = Sample(time.time())
                meta = params.get('responsesapiClientMetadata')
                sample.ambiguous = (previous is not None or not isinstance(meta, dict) or
                    (meta.get('prediction_source_thread_id'), meta.get('prediction_source_turn_id')) != key or
                    params.get('turnTrigger', 'composer_predictions') != 'composer_predictions')
                if not sample.ambiguous:
                    self.pending[rid] = key
                return
            if method is not None:
                return
            key = self.pending.pop(rid, None)
            if key is None or 'error' in msg:
                return
            result = msg.get('result')
            if not isinstance(result, dict) or result.get('sourceTurnId') != key[1]:
                return
            response_id, raw = result.get('responseId'), result.get('text')
            if not isinstance(response_id, str) or not 0 < len(response_id.strip()) <= 256 or not isinstance(raw, str):
                return
            try:
                obj = json.loads(raw)
            except (ValueError, RecursionError):
                return
            text = obj.get('suggestion') if isinstance(obj, dict) else None
            if isinstance(text, str) and text.strip() and len(text) <= 3900 and not self.samples[key].ambiguous:
                self.samples[key].response_id = response_id
                self.samples[key].text = text

    def lookup(self, thread, turn):
        if not all(isinstance(s, str) and ID.fullmatch(s) for s in (thread, turn)):
            return None
        key = thread, turn
        with self.lock:
            self._refresh()
            if self.stamp is None:
                return None
            epoch = self.epoch
            scope = {'hostId': 'local', 'threadId': thread, 'sourceTurnId': turn, 'principalEpoch': epoch}
            sample = self.samples.get(key)
            if not sample or sample.ambiguous or not sample.text or sample.requested + TTL <= time.time():
                return scope
        # Disk reads are on the read-only publisher, never on Desktop's RPC pump.
        allowed = self.decision(self.desktop_pid, key, sample.requested)
        with self.lock:
            self._refresh()
            if self.epoch != epoch:
                return None
            if not allowed or sample is not self.samples.get(key) or sample.ambiguous:
                return scope
            return dict(scope, responseId=sample.response_id, text=sample.text)
