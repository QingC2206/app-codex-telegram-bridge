"""Read an already approved composer result from an explicitly installed adapter.

The stock app-server prediction notification has no candidate-level Desktop
approval. Ordinary completions, instruction text and model samples are never
inputs to this reader.
"""
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from importlib.metadata import entry_points
import json
import re

from .desktop import DesktopIpcUnavailable, completed_turn_target, validate_thread_profile
from ..paths import codex_home


GROUP = 'codex_telegram_bridge.desktop_suggestions'
ID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')


@lru_cache(maxsize=4)
def installed(name):
    matches = list(entry_points(group=GROUP, name=name))
    return matches[0] if len(matches) == 1 else None


@lru_cache(maxsize=4)
def reader_for(name):
    """Only an administrator-selected, locally installed adapter can supply text."""
    adapter = installed(name)
    return adapter.load() if adapter else None


@dataclass(frozen=True)
class Suggestion:
    text: str
    fingerprint: str


@dataclass(frozen=True)
class Observation:
    owner: str
    suggestion: Suggestion | None


def observe(desktop, name, thread, turn, *, endpoint=None, keep_pending_owner=False):
    reader = reader_for(name)
    if reader is None:
        return None
    endpoint = endpoint or desktop.native.discover()
    if endpoint.profile != codex_home():
        return None
    validate_thread_profile(endpoint, thread)
    # The reader must get the final result for the *current* Desktop principal.
    # None also represents revocation, account change, abstention or cache loss.
    value = reader(endpoint, thread, turn)
    if not isinstance(value, dict) or (value.get('hostId'), value.get('threadId'),
            value.get('sourceTurnId')) != ('local', thread, turn):
        return None
    ids = [value.get(field) for field in ('accountId', 'userId')]
    if not all(isinstance(part, str) and part.strip() and len(part) <= 256 for part in ids):
        return None
    scope = [name, str(endpoint.profile), endpoint.pid, endpoint.pipe, thread, turn, *ids]
    owner = sha256(json.dumps(scope).encode()).hexdigest()
    desktop._check_instance(endpoint, thread)
    text = value.get('text')
    if text is None:
        return Observation(owner, None)
    response_id = value.get('responseId')
    if not isinstance(response_id, str) or not response_id.strip() or len(response_id) > 256:
        return None
    if (not isinstance(text, str) or not text.strip() or len(text) > 3900
            or any(ord(c) < 32 and c not in '\t\n' for c in text)):
        return None
    try:
        completed_turn_target(desktop._reader(endpoint).read_thread(thread), thread, turn)
    except (DesktopIpcUnavailable, TimeoutError):
        if not keep_pending_owner:
            raise
        desktop._check_instance(endpoint, thread)
        return Observation(owner, None)
    desktop._check_instance(endpoint, thread)
    fingerprint = sha256(json.dumps([owner, response_id, text], ensure_ascii=False).encode()).hexdigest()
    return Observation(owner, Suggestion(text, fingerprint))


def read_current(desktop, name, thread, turn, *, owner, endpoint=None):
    result = observe(desktop, name, thread, turn, endpoint=endpoint)
    return result.suggestion if result and result.owner == owner else None
