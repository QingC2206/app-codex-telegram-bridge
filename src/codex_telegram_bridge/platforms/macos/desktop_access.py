"""macOS Desktop access: verified local ownership and existing-thread deep links."""
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import time

from ..desktop_access import NativeDesktopAccess
from ..discovery import desktop_roots
from ...codex.errors import DesktopBridgeSafetyError
from ...i18n import tr as _t


def validate_local_desktop_thread(endpoint, thread_id: str) -> None:
    """Verify a Desktop target without using the separately signed App Tools API.

    On macOS the follower broker accepts local clients while App Tools requires
    app-owned process ancestry. For an ordinary reply, the live broker owner
    determines whether to steer or start a turn. Its verified PID/UID and this
    same-profile, Desktop-origin check are sufficient; no active-turn snapshot
    is inferred from the database. Strict turn-specific operations read a
    fresh snapshot from the verified follower owner.
    """
    if not endpoint.profile or not re.fullmatch(r'[A-Za-z0-9-]{6,64}', thread_id):
        raise DesktopBridgeSafetyError(_t('无法确认原 Desktop 任务的本机归属，未提交消息。'))
    profile = endpoint.profile.resolve()
    databases = sorted((p for p in profile.glob('state_*.sqlite')
                        if re.fullmatch(r'state_[0-9]+\.sqlite', p.name)),
                       key=lambda p: int(p.stem.split('_')[1]), reverse=True)
    try:
        if not databases:
            raise ValueError('missing Desktop metadata')
        db = sqlite3.connect(databases[0].resolve().as_uri() + '?mode=ro', uri=True, timeout=0.2)
        try:
            row = db.execute('SELECT rollout_path, archived FROM threads WHERE id=?',
                             (thread_id,)).fetchone()
        finally:
            db.close()
        if not row or row[1]:
            raise ValueError('missing or archived Desktop thread')
        transcript = Path(row[0]).resolve()
        if not transcript.is_relative_to(profile / 'sessions'):
            raise ValueError('transcript outside Desktop profile')
        with transcript.open(encoding='utf-8') as stream:
            record = json.loads(stream.readline(2 * 1024 * 1024))
        header = record.get('payload', {})
        from ...storage.state import StateStore
        managed = StateStore().snapshot().get('managed_threads', {}).get(thread_id) or {}
        bot_created = managed.get('origin') == 'bot_created'
        if (record.get('type') != 'session_meta' or header.get('id') != thread_id
                or header.get('originator') != 'Codex Desktop' and not bot_created
                or isinstance(header.get('source'), dict)):
            raise ValueError('not the original Desktop conversation')
    except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError) as exc:
        raise DesktopBridgeSafetyError(_t('原 Desktop 任务的本机记录无法核实，消息未发送。请检查该任务是否已归档或迁移。')) from exc



class MacOSDesktopAccess(NativeDesktopAccess):
    # An unloaded renderer can leave discovery unanswered on macOS. Opening
    # the verified existing task is safe before any user-input frame is sent.
    recover_discovery_unavailable = True

    def reader(self, transport_factory, timeout, discover):
        from ...codex.follower import FollowerReader
        return FollowerReader(transport_factory, timeout, discover,
                              validate_thread=validate_local_desktop_thread)

    def validate_reply(self, endpoint, thread_id, reader, *, start_if_inactive,
                       expected_turn_id, check_instance):
        if start_if_inactive and not expected_turn_id:
            validate_local_desktop_thread(endpoint, thread_id)
            check_instance()
            return
        super().validate_reply(endpoint, thread_id, reader,
            start_if_inactive=start_if_inactive, expected_turn_id=expected_turn_id,
            check_instance=check_instance)

    def open_thread(self, endpoint, thread_id, reader, *, deadline, check_instance):
        # Only load the existing task. No prompt or new-task fallback is encoded.
        validate_local_desktop_thread(endpoint, thread_id)
        records = [r for r in desktop_roots() if int(r['ProcessId']) == endpoint.pid]
        if len(records) != 1:
            raise DesktopBridgeSafetyError(_t('Desktop 在提交前已重启，未发送消息；请重新检查任务。'))
        executable = Path(records[0]['ExecutablePath']).resolve()
        if executable.parent.name != 'MacOS' or executable.parent.parent.name != 'Contents':
            raise DesktopBridgeSafetyError(_t('Desktop 的用户目录存在歧义，未提交消息。'))
        check_instance()
        subprocess.run(['/usr/bin/open', '-a', str(executable.parent.parent.parent),
                        'codex://threads/' + thread_id], check=True,
                       timeout=max(0.1, deadline-time.monotonic()),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
