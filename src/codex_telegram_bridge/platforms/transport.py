import os
from pathlib import Path
import tempfile
from ..paths import codex_home

if os.name == 'nt':
    from .windows.transport import WindowsTransport as Transport
else:
    from .posix.transport import UnixTransport as Transport


def default_broker_path(profile=None):
    if os.name == 'nt':
        return r'\\.\pipe\codex-ipc'
    current = (profile or codex_home())/'ipc/ipc.sock'
    if current.exists():
        return str(current)
    # Older Desktop versions use a per-user broker in the system temp folder.
    uid = os.getuid()
    legacy = Path(tempfile.gettempdir())/'codex-ipc'/(f'ipc-{uid}.sock' if uid else 'ipc.sock')
    return str(legacy if legacy.exists() else current)
