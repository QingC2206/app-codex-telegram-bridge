"""Local Desktop thread provenance checks."""
import json
import re
import sqlite3

from ..codex.errors import DesktopBridgeSafetyError
from ..i18n import tr as _t
from ..paths import resolved_rollout


def validate_local_desktop_thread(endpoint, thread_id: str) -> None:
    """Verify an unarchived Desktop rollout inside the selected profile."""
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
        transcript = resolved_rollout(row[0])
        if not transcript.is_relative_to(profile / 'sessions'):
            raise ValueError('transcript outside Desktop profile')
        with transcript.open(encoding='utf-8') as stream:
            record = json.loads(stream.readline(2 * 1024 * 1024))
        header = record.get('payload', {})
        if (record.get('type') != 'session_meta' or header.get('id') != thread_id
                or isinstance(header.get('source'), dict)):
            raise ValueError('not the original Desktop conversation')
        if header.get('originator') != 'Codex Desktop':
            from ..storage.state import StateStore
            managed = StateStore().snapshot().get('managed_threads', {}).get(thread_id) or {}
            if managed.get('origin') != 'bot_created':
                raise ValueError('not a Desktop conversation')
    except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError) as exc:
        raise DesktopBridgeSafetyError(_t('原 Desktop 任务的本机记录无法核实，消息未发送。请检查该任务是否已归档或迁移。')) from exc
