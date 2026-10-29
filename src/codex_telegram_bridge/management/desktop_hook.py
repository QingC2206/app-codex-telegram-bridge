"""Desktop Stop notifications are independent of the task execution backend.

The hook writes a local inbox only: no credentials, network calls or Desktop IPC.
Codex itself owns review/trust of this hook definition.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

from ..paths import codex_home, ensure_data_dir
from ..storage.bridge import BridgeState
from .transaction import install_files
from .hook_handlers import partition_command_handlers


def command():
    args = [sys.executable, '-m', 'codex_telegram_bridge', '--codex-home', str(codex_home()),
            '--data-dir', str(ensure_data_dir()), 'desktop-notify']
    return subprocess.list2cmdline(args) if os.name == 'nt' else shlex.join(args)


def registration_plan(remove=False):
    path = codex_home()/'hooks.json'
    manifest_path = ensure_data_dir()/'desktop-hook-install.json'
    original = path.read_bytes() if path.exists() else None
    hooks = json.loads((original or b'{"hooks": {}}').decode('utf-8-sig'))
    if not isinstance(hooks, dict) or not isinstance(hooks.get('hooks', {}), dict):
        raise ValueError('hooks.json must contain a hooks object.')
    current = command()
    previous = json.loads(manifest_path.read_text()).get('command') if manifest_path.exists() else None
    for event in ('Stop', 'SubagentStop', 'PreToolUse'):
        entries = hooks.setdefault('hooks', {}).get(event, [])
        if not isinstance(entries, list):
            raise ValueError(event+' hooks must be an array.')
        commands = {v for v in (current, previous) if v is not None}
        kept, _owned = partition_command_handlers(entries, commands.__contains__)
        if not remove:
            entry = {'hooks': [{'type': 'command', 'command': current, 'timeout': 10, 'async': True}]}
            if event == 'PreToolUse':
                entry['matcher'] = '^request_user_input(?:_async)?$'
            kept.append(entry)
        if kept:
            hooks['hooks'][event] = kept
        else:
            hooks['hooks'].pop(event, None)
    data = (json.dumps(hooks, ensure_ascii=False, indent=2)+'\n').encode()
    changes = {}
    if data != original and (original is not None or not remove):
        changes[path] = data
    changes[manifest_path] = None if remove else json.dumps(
        {'command': current, 'event': 'Stop', 'trust': 'review-in-codex'}).encode()
    return current, changes


def register(remove=False):
    current, changes = registration_plan(remove)
    install_files(changes, service=False)
    return current


def envelope(payload):
    """Retain the full hook contract; classification is shared with Windows."""
    from ..telegram.completion_routing import identity
    from ..codex.receipts import session_path
    if not isinstance(payload, dict):
        return None
    kind = payload.get('hook_event_name')
    if kind not in {'Stop', 'SubagentStop', 'PreToolUse'}:
        if payload.get('type') != 'agent-turn-complete':
            return None
        payload = {**payload, 'hook_event_name': 'Stop',
                   'last_assistant_message': payload.get('last-assistant-message')}
        kind = 'Stop'
    if kind == 'PreToolUse' and payload.get('tool_name') not in {'request_user_input', 'request_user_input_async'}:
        return None
    tid, turn, valid = identity(payload)
    if not valid or not session_path(tid, codex_home()/'sessions'):
        return None
    # Explicit transcript paths cannot name a different profile.
    if payload.get('transcript_path') and not Path(payload['transcript_path']).resolve().is_relative_to(codex_home().resolve()):
        return None
    key = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {'thread_id': tid, 'turn_id': turn, 'payload': payload,
            'key': 'hook:'+kind+':'+tid+':'+(turn if turn and kind != 'PreToolUse' else key)}


def ingest(payload, state):
    event = envelope(payload)
    if event:
        key = event.pop('key')
        # Preserve source/trigger/subagent evidence for the common classifier;
        # do not reduce the notification to just a string and thread ID.
        from ..storage.state import StateStore
        snapshot = StateStore().snapshot()
        event['quiet_generation'] = snapshot.get('quiet_generation', 0)
        event['suppressed'] = snapshot.get('quiet_mode') is True or (payload.get('hook_event_name') != 'PreToolUse' and bool(snapshot.get('muted')))
        state.receive_hook(key, event)
        state.set('desktop_hook_last_seen', datetime.now(timezone.utc).isoformat())
        return True
    return False


def main(arguments=None):
    state = None
    try:
        raw = arguments[-1] if arguments else sys.stdin.read(2*1024*1024)
        payload = json.loads(raw)
        # Filter before initializing/migrating a database or waking the service.
        if (os.environ.get('CODEX_TELEGRAM_MANAGED_APP_SERVER') != '1'
                and os.environ.get('CODEX_TELEGRAM_SIDE_CHAT') != '1' and envelope(payload)):
            state = BridgeState(ensure_data_dir()/'app-server-state.sqlite3')
            ingest(payload, state)
    except Exception as exc:
        print('Telegram Desktop hook: '+type(exc).__name__, file=sys.stderr)
    finally:
        if state:
            state.close()
    print('{}')
    return 0
