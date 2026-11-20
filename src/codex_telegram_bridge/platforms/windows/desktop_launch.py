"""A per-profile shortcut. The CLI override exists only in its Desktop child."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from ...management.transaction import atomic_write
from ...paths import codex_home, data_dir
from ...codex.suggestion_source import HOOK_SOURCE
from .composer_capture import SOURCE
from .desktop_proxy import PROXY, PROFILE_MARKER, bundled_runtime


def _config():
    return data_dir() / 'telegram_notify.credentials.json'


def enabled():
    try:
        cfg = json.loads(_config().read_text(encoding='utf-8-sig'))
        return cfg.get('desktop_suggestion_source') == SOURCE and cfg.get('include_message_preview') is True
    except (OSError, ValueError, AttributeError):
        return False


def _powershell(script, **values):
    command = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    env = dict(os.environ, **values)
    args = [str(command), '-NoLogo', '-NoProfile', '-NonInteractive', '-Command',
            "$ErrorActionPreference='Stop'\n[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)\n" + script]
    return subprocess.run(args, env=env, capture_output=True, encoding='utf-8', check=True,
                          creationflags=subprocess.CREATE_NO_WINDOW, timeout=20).stdout.strip()


def registered_desktop():
    install = _powershell("Get-AppxPackage -Name OpenAI.Codex | "
                          "Where-Object PackageFamilyName -eq 'OpenAI.Codex_2p2nqsd0c76g0' | "
                          "Select-Object -ExpandProperty InstallLocation")
    paths = [Path(line) / 'app/ChatGPT.exe' for line in install.splitlines()]
    if len(paths) != 1 or not bundled_runtime(paths[0]):
        raise RuntimeError('Cannot verify the installed Microsoft Store Codex Desktop.')
    return paths[0]


def _user_data(args):
    for i, arg in enumerate(args):
        if arg.startswith('--user-data-dir='):
            return Path(arg.partition('=')[2]).resolve()
        if arg == '--user-data-dir' and i + 1 < len(args):
            return Path(args[i+1]).resolve()
    return None


def _profiles():
    import psutil
    from ..discovery import desktop_roots, process_environment
    for root in desktop_roots():
        pid = int(root['ProcessId'])
        env = process_environment(pid)
        home = env.get('CODEX_HOME')
        if not home:
            base = env.get('USERPROFILE') or env.get('HOME')
            if not base or (env.get('HOME') and Path(env['HOME']).resolve() != Path(base).resolve()):
                raise RuntimeError('Cannot identify a running Desktop profile.')
            home = Path(base) / '.codex'
        if not Path(home).is_absolute():
            raise RuntimeError('Cannot identify a running Desktop profile.')
        yield Path(home).resolve(), _user_data(psutil.Process(pid).cmdline()), pid


def _name(profile):
    suffix = '' if profile == (Path.home()/'.codex').resolve() else ' - ' + hashlib.sha256(
        str(profile).encode()).hexdigest()[:10]
    return 'Codex Desktop - Telegram' + suffix + '.lnk'


def shortcut(action, desktop=None, user_data=None):
    profile = codex_home()
    target = Path(sys.executable).with_name('pythonw.exe')
    args = ['-I', '-m', 'codex_telegram_bridge', '--codex-home', str(profile),
            '--data-dir', str(data_dir()), 'desktop-suggestions', 'start']
    if action == 'enable' and not target.is_file():
        raise FileNotFoundError('The installed Robot requires pythonw.exe.')
    extra = ['--desktop-user-data-dir', str(user_data)] if user_data is not None else []
    script = """
$p = Join-Path ([Environment]::GetFolderPath('DesktopDirectory')) $env:BRIDGE_LINK_NAME
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut($p)
$exists = [IO.File]::Exists($p)
$ours = $exists -and ($s.TargetPath -ieq $env:BRIDGE_LINK_TARGET) -and
    (($s.Arguments -ceq $env:BRIDGE_LINK_BASE) -or $s.Arguments.StartsWith($env:BRIDGE_LINK_BASE + ' --desktop-user-data-dir '))
if ($env:BRIDGE_LINK_ACTION -eq 'status') { if ($ours) { $p }; exit 0 }
if ($exists -and !$ours) { throw 'Another shortcut already owns this name.' }
if ($env:BRIDGE_LINK_ACTION -eq 'disable') { if ($ours) { [IO.File]::Delete($p) }; exit 0 }
$s.TargetPath = $env:BRIDGE_LINK_TARGET
$s.Arguments = $env:BRIDGE_LINK_ARGS
$s.WorkingDirectory = $env:BRIDGE_LINK_WORK
$s.IconLocation = $env:BRIDGE_LINK_ICON + ',0'
$s.Description = 'Launch this Codex Desktop profile with Telegram suggested replies'
$s.WindowStyle = 1
$s.Save()
$p
"""
    return _powershell(script, BRIDGE_LINK_ACTION=action, BRIDGE_LINK_NAME=_name(profile),
                       BRIDGE_LINK_TARGET=str(target), BRIDGE_LINK_BASE=subprocess.list2cmdline(args),
                       BRIDGE_LINK_ARGS=subprocess.list2cmdline(args + extra),
                       BRIDGE_LINK_WORK=str(profile), BRIDGE_LINK_ICON=str(desktop or ''))


def launch_env(profile, directory, proxy):
    if not proxy.is_file():
        raise FileNotFoundError('Install the Windows Desktop suggestion relay in this Robot environment.')
    return dict(os.environ, CODEX_HOME=str(profile), CODEX_TELEGRAM_DATA_DIR=str(directory),
                CODEX_CLI_PATH=str(proxy), **{PROFILE_MARKER: str(profile)})


def start(user_data):
    if not enabled():
        raise RuntimeError('Enable the suggestion source and message previews for this Robot first.')
    profile = codex_home()
    if profile != (Path.home()/'.codex').resolve() and user_data is None:
        raise RuntimeError('An isolated Desktop profile requires its verified Electron user-data directory.')
    for active, active_data, _ in _profiles():
        if active == profile:
            raise RuntimeError('该 Desktop 已在运行。下次正常退出后，请使用 Telegram 专用快捷方式启动。'
                               '\nThis Desktop is already running. Use the Telegram shortcut after quitting it.')
        if active_data == user_data:
            raise RuntimeError('Another Desktop may share the Electron user-data directory.')
    desktop = registered_desktop()
    args = [str(desktop)]
    if user_data is not None:
        args += ['--user-data-dir=' + str(user_data)]
    env = launch_env(profile, data_dir(), Path(sys.executable).with_name(PROXY))
    subprocess.Popen(args, env=env, cwd=str(desktop.parent))
    return 0


def manage(action, user_data=None):
    if os.name != 'nt':
        raise RuntimeError('The bundled suggestion source requires Windows Codex Desktop.')
    if action == 'start':
        try:
            return start(user_data.resolve() if user_data else None)
        except Exception as exc:
            if sys.stderr is not None:
                raise
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, str(exc), 'Codex Desktop - Telegram', 0x40)
            return 1
    profile = codex_home()
    if action == 'status':
        from ...codex.desktop import DesktopEndpoint
        from .suggestion_pipe import read_approved
        active = [pid for home, _, pid in _profiles() if home == profile]
        empty = '00000000-0000-0000-0000-000000000000'
        try:
            live = len(active) == 1 and read_approved(DesktopEndpoint(active[0], None, profile), empty, empty)
        except (OSError, RuntimeError, ValueError):
            live = False
        print(json.dumps(dict(enabled=enabled(), profile=str(profile), shortcut=shortcut('status') or None,
                              relay='connected' if live else 'inactive'), ensure_ascii=True))
        return 0
    cfg = json.loads(_config().read_text(encoding='utf-8-sig'))
    if cfg.get('desktop_suggestion_source') not in (None, '', SOURCE, HOOK_SOURCE):
        raise RuntimeError('This profile already uses a different suggestion adapter.')
    if action == 'enable':
        from ...management.runtime import load_settings
        if load_settings()['backend'] != 'desktop' or cfg.get('include_message_preview') is not True:
            raise RuntimeError('Select the Desktop backend and enable message previews first.')
        active = [d for home, d, _ in _profiles() if home == profile]
        if len(active) > 1 or (profile != (Path.home()/'.codex').resolve() and (not active or active[0] is None)):
            raise RuntimeError('Open exactly one isolated Desktop with a dedicated user-data directory first.')
        launch_env(profile, data_dir(), Path(sys.executable).with_name(PROXY))
        desktop = registered_desktop()
        existed = shortcut('status')
        link = shortcut('enable', desktop, active[0] if active else None)
        cfg['desktop_suggestion_source'] = SOURCE
        try:
            atomic_write(_config(), (json.dumps(cfg, ensure_ascii=False, indent=2) + '\n').encode())
        except OSError:
            if not existed:
                shortcut('disable')
            raise
        print(link)
    elif action == 'disable':
        shortcut('disable')
        if cfg.get('desktop_suggestion_source') != '':
            cfg['desktop_suggestion_source'] = ''
            atomic_write(_config(), (json.dumps(cfg, ensure_ascii=False, indent=2) + '\n').encode())
    return 0
