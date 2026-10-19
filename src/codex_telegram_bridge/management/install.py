"""Register only this tool's settings; preserve unrelated config and hooks."""
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import uuid

import tomlkit

from ..paths import codex_home, ensure_data_dir
from . import services


def _write(path, data):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.tmp-'+uuid.uuid4().hex)
    try:
        temporary.write_bytes(data);temporary.chmod(0o600);os.replace(temporary,path)
    finally:
        if temporary.exists(): temporary.unlink()


def notify_command():
    return [sys.executable,'-m','codex_telegram_bridge','--codex-home',str(codex_home()),
            '--data-dir',str(ensure_data_dir()),'notify']


def _hook_owned(entry):
    for hook in entry.get('hooks',[]):
        command=hook.get('command','')
        if not isinstance(command,str): continue
        if ('codex_telegram_bridge' in command and str(ensure_data_dir()) in command) or str(ensure_data_dir()/'telegram_notify.py') in command:
            return True
    return False


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
    owned=[entry for entry in entries if _hook_owned(entry)]
    if not manifest.get('previous_hooks'): manifest['previous_hooks']=copy.deepcopy(owned)
    shell_command=subprocess.list2cmdline(command) if os.name=='nt' else shlex.join(command)
    container['PreToolUse']=[entry for entry in entries if not _hook_owned(entry)]+[
        {'matcher':'^request_user_input(?:_async)?$','hooks':[{'type':'command','command':shell_command,'async':True,'timeout':30}]}]
    backup=data/'backups'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ');backup.mkdir(parents=True)
    for p,content in originals.items():
        if content is not None: _write(backup/p.name,content)
    service_before = services.snapshot() if service else None
    service_changed = False
    try:
        _write(config_path,tomlkit.dumps(config).encode())
        _write(hooks_path,(json.dumps(hooks,ensure_ascii=False,indent=2)+'\n').encode())
        if service:
            service_changed = True
            services.manage('install')
            if sys.platform!='darwin': services.manage('start')
        _write(manifest_path,json.dumps(manifest,ensure_ascii=False,indent=2).encode())
    except Exception:
        for p,content in originals.items():
            if content is not None: _write(p,content)
            elif p.exists(): p.unlink()
        if service_changed:
            try:
                services.restore(service_before)
            except Exception as restore_error:
                raise RuntimeError(f'Installation failed and service rollback failed; backups: {backup}') from restore_error
        raise
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
            hooks['hooks']['PreToolUse']=[entry for entry in entries if not _hook_owned(entry)]
            _write(path,json.dumps(hooks,ensure_ascii=False,indent=2).encode())
    manifest_path.unlink()
    print('Hooks and service removed. Credentials, message routes and backups retained: '+str(data))
    return 0
