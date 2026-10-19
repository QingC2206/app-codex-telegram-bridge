"""Discover Desktop by its application bundle, never by PATH or CLI name."""
import json
import os
from pathlib import Path
import plistlib
import struct
import sys

import psutil

from ...codex.errors import DesktopBridgeSafetyError, DesktopIpcUnavailable


def _asar_name(path: Path) -> str:
    """Read only Electron package metadata, with bounded lengths/offsets."""
    with path.open('rb') as stream:
        _, header_size, _, size = struct.unpack('<4I', stream.read(16))
        if not 0 < size <= header_size <= 16 * 1024 * 1024:
            return ''
        header = json.loads(stream.read(size))
        record = header['files']['package.json']
        length, offset = int(record['size']), int(record['offset'])
        if record.get('unpacked') or not 0 < length < 1024 * 1024 or offset < 0:
            return ''
        stream.seek(8 + header_size + offset)
        return json.loads(stream.read(length)).get('name', '')


def desktop_resources(executable: Path) -> Path | None:
    executable = executable.resolve()
    if sys.platform == 'darwin':
        if executable.parent.name != 'MacOS' or executable.parent.parent.name != 'Contents':
            return None
        contents = executable.parent.parent
        try:
            info = plistlib.loads((contents/'Info.plist').read_bytes())
            if (info.get('CFBundleIdentifier') not in {'com.openai.codex', 'com.openai.chat'}
                    or info.get('CFBundleExecutable') != executable.name):
                return None
        except (OSError, ValueError):
            return None
        resources = contents/'Resources'
    else:
        resources = executable.parent/'resources'
    try:
        if _asar_name(resources/'app.asar') != 'openai-codex-electron':
            return None
    except (OSError, ValueError, KeyError, TypeError, struct.error):
        return None
    return resources


def desktop_roots():
    records = []
    for process in psutil.process_iter(['pid', 'exe', 'cmdline', 'uids']):
        info = process.info
        if not info['exe'] or not info['uids'] or info['uids'].real != os.getuid():
            continue
        if any(arg.startswith('--type=') for arg in (info['cmdline'] or [])):
            continue
        resources = desktop_resources(Path(info['exe']))
        if resources:
            records.append({'ProcessId': info['pid'], 'ExecutablePath': info['exe'],
                            'Resources': str(resources)})
    return records


def _runtime_allowed(path, resources):
    path = Path(path).resolve()
    if path.name != 'codex':
        return False
    if path.is_relative_to(resources.resolve()):
        return True
    # Desktop extracts versioned runtimes under its user application data.
    if sys.platform == 'darwin':
        roots = [Path.home()/'Library/Application Support/OpenAI/Codex/bin',
                 Path.home()/'Library/Application Support/Codex/bin']
    else:
        data = Path(os.environ.get('XDG_DATA_HOME') or Path.home()/'.local/share')
        roots = [data/'OpenAI/Codex/bin', data/'Codex/bin', data/'codex/bin']
    return any(path.parent.parent == root.resolve() for root in roots)


def process_environment(pid):
    try:
        process = psutil.Process(pid)
        resources = desktop_resources(Path(process.exe()))
        if not resources or process.uids().real != os.getuid():
            raise DesktopBridgeSafetyError('Desktop process identity changed.')
        env = process.environ()
        selected = {k: env[k] for k in ('CODEX_HOME','HOME','CODEX_APP_TOOLS_PIPE_PATH') if k in env}
        if not selected.get('HOME') and not selected.get('CODEX_HOME'):
            raise DesktopBridgeSafetyError('Cannot verify the Desktop profile from its process environment.')
        pipes = set()
        if selected.get('CODEX_APP_TOOLS_PIPE_PATH'):
            pipes.add(selected['CODEX_APP_TOOLS_PIPE_PATH'])
        # POSIX environ can be an initial snapshot; the app-server inherits the
        # dynamically created pipe. Only direct, Desktop-paired children count.
        for child in process.children():
            try:
                if _runtime_allowed(child.exe(), resources):
                    child_env = child.environ()
                    if child_env.get('CODEX_APP_TOOLS_PIPE_PATH'):
                        pipes.add(child_env['CODEX_APP_TOOLS_PIPE_PATH'])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if len(pipes) > 1:
            raise DesktopBridgeSafetyError('Desktop has ambiguous native endpoints.')
        if pipes:
            selected['CODEX_APP_TOOLS_PIPE_PATH'] = pipes.pop()
        return selected
    except (psutil.Error, OSError) as exc:
        raise DesktopBridgeSafetyError('Cannot verify the running Desktop environment.') from exc


def locate_runtime(profile: Path) -> Path:
    from ...codex.desktop import discover_desktop
    endpoint = discover_desktop(profile)
    process = psutil.Process(endpoint.pid)
    resources = desktop_resources(Path(process.exe()))
    if not resources:
        raise DesktopBridgeSafetyError('Desktop executable changed.')
    configured = os.environ.get('CODEX_CLI_PATH')
    if configured and not _runtime_allowed(configured, resources):
        raise DesktopBridgeSafetyError('CODEX_CLI_PATH must belong to Codex Desktop; VS Code and standalone CLI are forbidden.')
    running = {Path(child.exe()).resolve() for child in process.children()
               if _runtime_allowed(child.exe(), resources)}
    if len(running) > 1:
        raise DesktopBridgeSafetyError('Multiple Desktop runtime versions are running.')
    if running:
        return running.pop()
    candidates = [resources/'codex', resources/'bin/codex']
    if configured:
        candidates.insert(0, Path(configured))
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK) and _runtime_allowed(path, resources):
            return path.resolve()
    raise DesktopIpcUnavailable('Desktop-paired runtime unavailable. Open a local Desktop task first.')
