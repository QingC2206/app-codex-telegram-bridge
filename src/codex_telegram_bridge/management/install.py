"""Register only this tool's settings; preserve unrelated config and hooks."""
import copy
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

import tomlkit

from ..paths import codex_home, ensure_data_dir
from . import services
from .transaction import atomic_write as _write, install_files
from .hook_handlers import direct_command_arguments, partition_command_handlers


def notify_command():
    return [sys.executable,'-m','codex_telegram_bridge','--codex-home',str(codex_home()),
            '--data-dir',str(ensure_data_dir()),'notify']


def _owned_command(command):
    data = ensure_data_dir()
    args = direct_command_arguments(command)
    if args == notify_command():
        return True
    manifest = data/'bridge-install.json'
    if manifest.is_file():
        try:
            recorded = json.loads(manifest.read_text(encoding='utf-8')).get('installed_notify')
            if isinstance(recorded, list) and all(isinstance(arg, str) for arg in recorded) and args == recorded:
                return True
        except (OSError, ValueError, AttributeError):
            pass
    if not args or not re.fullmatch(r'pythonw?(?:\d+(?:\.\d+)*)?(?:\.exe)?',
                                   re.split(r'[/\\]', args[0])[-1], re.I):
        return False

    def same_path(value, expected):
        try:
            candidate = Path(value).expanduser()
            return candidate.is_absolute() and candidate.resolve() == expected.resolve()
        except (OSError, RuntimeError, ValueError):
            return False

    # The pre-package installer called this exact script without extra flags.
    if len(args) == 2 and same_path(args[1], data/'telegram_notify.py'):
        return True
    if args[1:3] != ['-m', 'codex_telegram_bridge']:
        return False
    options = {}
    tail = args[3:]
    while tail and tail[0].startswith('--'):
        key, equal, value = tail[0].partition('=')
        if key not in {'--codex-home', '--data-dir'} or key in options:
            return False
        if equal:
            tail = tail[1:]
        elif len(tail) >= 2:
            value, tail = tail[1], tail[2:]
        else:
            return False
        options[key] = value
    return (tail == ['notify'] and '--data-dir' in options
            and same_path(options['--data-dir'], data)
            and ('--codex-home' not in options
                 or same_path(options['--codex-home'], codex_home())))


def _hook_owned(entry):
    return bool(partition_command_handlers([entry], _owned_command)[1])


def install(*, service=True, replace_notify=False):
    # Read-only preflight before touching user configuration.
    from ..codex.desktop import discover_desktop
    discover_desktop()
    data=ensure_data_dir(); profile=codex_home()
    config_path=profile/'config.toml'; hooks_path=profile/'hooks.json'; manifest_path=data/'bridge-install.json'
    originals={p:p.read_bytes() if p.exists() else None for p in (config_path,hooks_path,manifest_path)}
    config=tomlkit.parse((originals[config_path] or b'').decode('utf-8-sig'))
    hooks=json.loads((originals[hooks_path] or b'{"hooks":{}}').decode('utf-8-sig'))
    if not isinstance(hooks,dict) or not isinstance(hooks.get('hooks',{}),dict):
        raise ValueError('hooks.json must contain a hooks object.')
    previous=config.get('notify')
    command=notify_command()
    ours=previous and (('codex_telegram_bridge' in previous and str(data) in previous)
                       or str(data/'telegram_notify.py') in previous)
    if previous and not ours and not replace_notify:
        raise RuntimeError('Another notify command exists. Review it, then use install --replace-notify to replace it explicitly.')
    manifest=json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.exists() else {
        'previous_notify':list(previous) if previous and not ours else None,'previous_hooks':[], 'schema':1}
    manifest['installed_notify']=command
    config['notify']=command
    container=hooks.setdefault('hooks',{})
    entries=container.get('PreToolUse',[])
    if not isinstance(entries,list): raise ValueError('PreToolUse hooks must be an array.')
    kept, owned = partition_command_handlers(entries, _owned_command)
    if not manifest.get('previous_hooks'): manifest['previous_hooks']=copy.deepcopy(owned)
    shell_command=subprocess.list2cmdline(command) if os.name=='nt' else shlex.join(command)
    container['PreToolUse']=kept+[
        {'matcher':'^request_user_input(?:_async)?$','hooks':[{'type':'command','command':shell_command,'async':True,'timeout':30}]}]
    install_files({
        config_path: tomlkit.dumps(config).encode(),
        hooks_path: (json.dumps(hooks,ensure_ascii=False,indent=2)+'\n').encode(),
        manifest_path: json.dumps(manifest,ensure_ascii=False,indent=2).encode(),
    }, service=service, originals=originals)
    print('Installed. Restart Codex Desktop to load hooks. Data: '+str(data))
    return 0


def uninstall():
    data=ensure_data_dir(); manifest_path=data/'bridge-install.json'
    if not manifest_path.exists():
        raise RuntimeError('No managed installation record; no configuration was changed.')
    manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    services.manage('uninstall')
    path=codex_home()/'config.toml'
    if path.exists():
        config=tomlkit.parse(path.read_text(encoding='utf-8-sig'))
        if config.get('notify')==manifest['installed_notify']:
            previous=manifest.get('previous_notify')
            if previous is not None: config['notify']=previous
            else: del config['notify']
            _write(path,tomlkit.dumps(config).encode())
    path=codex_home()/'hooks.json'
    if path.exists():
        hooks=json.loads(path.read_text(encoding='utf-8-sig'))
        entries=hooks.get('hooks',{}).get('PreToolUse',[])
        if entries:
            hooks['hooks']['PreToolUse']=partition_command_handlers(entries, _owned_command)[0]
            _write(path,json.dumps(hooks,ensure_ascii=False,indent=2).encode())
    manifest_path.unlink()
    print('Hooks and service removed. Credentials, message routes and backups retained: '+str(data))
    return 0
