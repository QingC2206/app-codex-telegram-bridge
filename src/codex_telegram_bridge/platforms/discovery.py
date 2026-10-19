"""OS dispatch for verified Desktop processes and local peer identity."""
import os
import re
from pathlib import Path
from ..codex.errors import DesktopBridgeSafetyError


def desktop_roots():
    if os.name != 'nt':
        from .posix.discovery import desktop_roots as roots
        return roots()
    import psutil
    records = []
    for process in psutil.process_iter(['pid', 'exe', 'cmdline']):
        info = process.info
        if not info['exe']:
            continue
        path = Path(info['exe']).resolve()
        if (path.name.lower() == 'chatgpt.exe' and path.parent.name.lower() == 'app'
                and path.parent.parent.name.lower().startswith('openai.codex_')
                and path.parent.parent.parent.name.lower() == 'windowsapps'
                and not any(arg.startswith('--type=') for arg in (info['cmdline'] or []))):
            records.append({'ProcessId': info['pid'], 'ExecutablePath': str(path)})
    return records


def process_environment(pid):
    if os.name == 'nt':
        from .windows.discovery import _process_environment
        return _process_environment(pid)
    from .posix.discovery import process_environment as read
    return read(pid)


def valid_windows_endpoint(value):
    return bool(re.fullmatch(r'\\\\\.\\pipe\\codex-(?:app-tools|browser-use)-[A-Za-z0-9][A-Za-z0-9-]{0,127}', value))


def valid_native_endpoint(value):
    if os.name == 'nt':
        return valid_windows_endpoint(value)
    path = Path(value)
    # Current Desktop creates a private directory and a unique .sock file.
    return (path.is_absolute() and '..' not in path.parts
            and path.parent.name in {'codex-app-tools','codex-browser-use'}
            and bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,127}\.sock',path.name))
            and not any(ord(c)<32 for c in value))


def validate_pipe_server(handle, expected_pid):
    if os.name == 'nt':
        from .windows.discovery import validate_pipe_server as validate
    else:
        from .posix.transport import validate_peer as validate
    return validate(handle, expected_pid)
