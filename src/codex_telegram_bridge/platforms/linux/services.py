"""linux user-session service adapter; commands receive an explicit context."""
import os
from pathlib import Path
from ..service import restore_definition

STARTS_ON_INSTALL = False


def quote_argument(value):
    if '\n' in value or '\r' in value or '\x00' in value:
        raise ValueError('Invalid service command argument.')
    return '"'+value.replace('\\','\\\\').replace('"','\\"').replace('%','%%').replace('$','$$')+'"'

def unit_definition(argv):
    return ('[Unit]\nDescription=Codex Desktop Telegram Bridge\nAfter=graphical-session.target\n'
            'PartOf=graphical-session.target\n\n[Service]\nType=simple\n'
            'ExecStart='+ ' '.join(map(quote_argument,argv))+'\nRestart=on-failure\nRestartSec=10\n'
            'KillMode=control-group\nTimeoutStopSec=15\nUMask=0077\n\n[Install]\nWantedBy=default.target\n')


def snapshot(context):
    label = context.label
    path = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home()/'.config')/'systemd/user'/(label+'.service')
    context.validate_definition(path)
    active = context.run(['systemctl','--user','is-active',path.name],check=False).returncode == 0
    enabled = context.run(['systemctl','--user','is-enabled',path.name],check=False).returncode == 0
    if (active or enabled) and not path.exists():
        raise RuntimeError('Existing systemd service has no owned local definition.')
    return {'platform':'linux','path':path,'content':path.read_bytes() if path.exists() else None,
            'active':active,'enabled':enabled}


def restore(context, previous):
    path = previous['path']
    context.run(['systemctl','--user','disable','--now',path.name],check=False)
    restore_definition(path, previous['content'])
    context.run(['systemctl','--user','daemon-reload'])
    if previous['enabled']:
        context.run(['systemctl','--user','enable',path.name])
    if previous['active']:
        context.run(['systemctl','--user','start',path.name])


def manage(context, action):
    data, argv, label = context.data, context.argv, context.label
    path=Path(os.environ.get('XDG_CONFIG_HOME') or Path.home()/'.config')/'systemd/user'/(label+'.service')
    context.validate_definition(path)
    if action=='status': return context.run(['systemctl','--user','is-active',path.name],check=False).returncode
    if action=='install':
        path.parent.mkdir(parents=True,exist_ok=True)
        if path.exists() and 'codex_telegram_bridge' not in path.read_text():
            raise RuntimeError('Existing systemd unit is not owned by this Robot.')
        if path.exists(): context.run(['systemctl','--user','stop',path.name])
        path.write_text(unit_definition(argv),encoding='utf-8');path.chmod(0o600)
        context.run(['systemctl','--user','daemon-reload'])
        # Forward only desktop-session variables needed for keyring/capture.
        variables=[x for x in ('DISPLAY','WAYLAND_DISPLAY','XAUTHORITY','DBUS_SESSION_BUS_ADDRESS','XDG_RUNTIME_DIR') if x in os.environ]
        if variables: context.run(['systemctl','--user','import-environment',*variables])
        context.run(['systemctl','--user','enable',path.name])
    elif action=='uninstall':
        context.run(['systemctl','--user','disable','--now',path.name],check=False)
        if path.exists(): path.unlink()
        context.run(['systemctl','--user','daemon-reload'])
    else:
        context.run(['systemctl','--user',action,path.name])

    return 0
