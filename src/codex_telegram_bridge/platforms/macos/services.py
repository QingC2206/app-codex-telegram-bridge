"""macos user-session service adapter; commands receive an explicit context."""
import os
from pathlib import Path
import plistlib
from ..service import restore_definition

STARTS_ON_INSTALL = True


def agent_definition(argv, label, data):
    return plistlib.dumps({'Label':label,'ProgramArguments':argv,'RunAtLoad':True,
        'KeepAlive':{'SuccessfulExit':False}, 'ThrottleInterval':10,
        'ProcessType':'Background','LimitLoadToSessionType':'Aqua',
        'StandardOutPath':str(data/'service.stdout.log'),'StandardErrorPath':str(data/'service.stderr.log')})


def snapshot(context):
    label = context.label
    path = Path.home()/'Library/LaunchAgents'/(label+'.plist')
    context.validate_definition(path)
    domain = f'gui/{os.getuid()}'
    active = context.run(['launchctl','print',domain+'/'+label],check=False).returncode == 0
    if active and not path.exists():
        raise RuntimeError('Loaded LaunchAgent has no owned definition; refusing replacement.')
    return {'platform':'darwin','path':path,'content':path.read_bytes() if path.exists() else None,
            'active':active,'domain':domain,'label':label}


def restore(context, previous):
    path = previous['path']
    context.run(['launchctl','bootout',previous['domain']+'/'+previous['label']],check=False)
    restore_definition(path, previous['content'])
    if previous['active']:
        context.run(['launchctl','bootstrap',previous['domain'],str(path)])


def manage(context, action):
    data, argv, label = context.data, context.argv, context.label
    path=Path.home()/'Library/LaunchAgents'/(label+'.plist')
    context.validate_definition(path)
    domain=f'gui/{os.getuid()}'
    if action=='status':
        return context.run(['launchctl','print',domain+'/'+label],check=False).returncode
    if action=='install':
        path.parent.mkdir(parents=True,exist_ok=True)
        if path.exists() and plistlib.loads(path.read_bytes()).get('ProgramArguments')!=argv:
            # Permit an upgrade only when the same explicit profile/data are bound.
            old=plistlib.loads(path.read_bytes()).get('ProgramArguments',[])
            if 'codex_telegram_bridge' not in old or str(data) not in old:
                raise RuntimeError('Existing LaunchAgent is not owned by this Robot.')
        context.run(['launchctl','bootout',domain+'/'+label],check=False)
        path.write_bytes(agent_definition(argv,label,data));path.chmod(0o600)
    elif action in {'stop','uninstall'}:
        context.run(['launchctl','bootout',domain+'/'+label],check=False)
        if action=='uninstall' and path.exists(): path.unlink()
    if action in {'start','install'}:
        context.run(['launchctl','bootstrap',domain,str(path)])

    return 0
