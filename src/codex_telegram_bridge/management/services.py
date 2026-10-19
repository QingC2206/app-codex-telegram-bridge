"""Native user-session services, generated and managed entirely by Python."""
import getpass
import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

from ..paths import codex_home, data_dir, ensure_data_dir


def identity():
    return 'codex-telegram-bridge-'+hashlib.sha256(str(codex_home()).encode()).hexdigest()[:12]


def command():
    executable = Path(sys.executable)
    if os.name == 'nt' and executable.with_name('pythonw.exe').exists():
        executable = executable.with_name('pythonw.exe')
    return [str(executable), '-m', 'codex_telegram_bridge', '--codex-home',str(codex_home()),
            '--data-dir',str(data_dir()),'run','--quiet-start']


def _run(args, *, check=True, **kwargs):
    return subprocess.run(args, check=check, capture_output=True,
                          creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0), **kwargs)


def _systemd_arg(value):
    if '\n' in value or '\r' in value or '\x00' in value:
        raise ValueError('Invalid service command argument.')
    return '"'+value.replace('\\','\\\\').replace('"','\\"').replace('%','%%').replace('$','$$')+'"'


def linux_unit(argv):
    return ('[Unit]\nDescription=Codex Desktop Telegram Bridge\nAfter=graphical-session.target\n'
            'PartOf=graphical-session.target\n\n[Service]\nType=simple\n'
            'ExecStart='+ ' '.join(map(_systemd_arg,argv))+'\nRestart=on-failure\nRestartSec=10\n'
            'KillMode=control-group\nTimeoutStopSec=15\nUMask=0077\n\n[Install]\nWantedBy=default.target\n')


def macos_agent(argv, label, data):
    return plistlib.dumps({'Label':label,'ProgramArguments':argv,'RunAtLoad':True,
        'KeepAlive':{'SuccessfulExit':False}, 'ThrottleInterval':10,
        'ProcessType':'Background','LimitLoadToSessionType':'Aqua',
        'StandardOutPath':str(data/'service.stdout.log'),'StandardErrorPath':str(data/'service.stderr.log')})


def windows_task(argv):
    ns='http://schemas.microsoft.com/windows/2004/02/mit/task'
    ET.register_namespace('',ns)
    def child(parent,name,text=None,**attrs):
        node=ET.SubElement(parent,'{'+ns+'}'+name,attrs);node.text=text;return node
    root=ET.Element('{'+ns+'}Task',{'version':'1.2'})
    triggers=child(root,'Triggers')
    trigger=child(triggers,'LogonTrigger');child(trigger,'Enabled','true')
    trigger=child(triggers,'TimeTrigger');child(trigger,'StartBoundary','2020-01-01T00:00:00')
    repetition=child(trigger,'Repetition');child(repetition,'Interval','PT1M');child(repetition,'StopAtDurationEnd','false')
    principals=child(root,'Principals');principal=child(principals,'Principal',id='Author')
    domain=os.environ.get('USERDOMAIN'); user=getpass.getuser()
    child(principal,'UserId',f'{domain}\\{user}' if domain else user)
    child(principal,'LogonType','InteractiveToken');child(principal,'RunLevel','LeastPrivilege')
    settings=child(root,'Settings');child(settings,'MultipleInstancesPolicy','IgnoreNew')
    child(settings,'DisallowStartIfOnBatteries','false');child(settings,'StopIfGoingOnBatteries','false')
    child(settings,'StartWhenAvailable','true');child(settings,'ExecutionTimeLimit','PT0S')
    actions=child(root,'Actions',Context='Author');execute=child(actions,'Exec')
    child(execute,'Command',argv[0]);child(execute,'Arguments',subprocess.list2cmdline(argv[1:]))
    child(execute,'WorkingDirectory',str(data_dir()))
    return ET.tostring(root,encoding='utf-16',xml_declaration=True)


def _legacy_owned(xml):
    try:
        root=ET.fromstring(xml)
        args=' '.join(n.text or '' for n in root.iter() if n.tag.endswith('Arguments'))
        return ('codex_telegram_bridge' in args and str(codex_home()) in args) or (
            str(data_dir()/'telegram_bot.py').lower() in args.lower())
    except ET.ParseError:
        return False


def _stop_owned_processes():
    import psutil
    own_data=data_dir().resolve()
    for proc in psutil.process_iter(['pid','exe','cmdline']):
        args=proc.info['cmdline'] or []
        if proc.pid==os.getpid() or not proc.info['exe'] or Path(proc.info['exe']).stem.lower() not in {'python','pythonw','python3','python3.10','python3.11','python3.12','python3.13','python3.14','py','pyw'}:
            continue
        legacy=any(Path(arg).is_absolute() and Path(arg).resolve()==own_data/'telegram_bot.py' for arg in args)
        current=('codex_telegram_bridge' in args and 'run' in args and str(own_data) in args)
        if not legacy and not current:
            continue
        try:
            children=proc.children(recursive=True)
            # psutil Process methods check PID reuse against creation time.
            proc.terminate()
            _,alive=psutil.wait_procs([proc,*children],timeout=5)
            for child in alive: child.kill()
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as exc:
            raise RuntimeError('Cannot stop the old Robot process safely; close that Robot before upgrading.') from exc


def manage(action):
    data=ensure_data_dir(); argv=command(); label=identity()
    if sys.platform=='win32':
        default=(Path.home()/'.codex').resolve()
        name='Codex Telegram Bot' if codex_home()==default else label
        query=_run(['schtasks.exe','/Query','/TN',name,'/XML'],check=False)
        exists=query.returncode==0
        if exists and not _legacy_owned(query.stdout):
            raise RuntimeError('The existing scheduled task is not owned by this Robot/profile.')
        if action=='status':
            print('installed' if exists else 'not installed');return 0 if exists else 1
        if action=='install':
            if exists:
                backup=data/'backups';backup.mkdir(exist_ok=True)
                previous=backup/'previous-startup-task.xml'
                if not previous.exists(): previous.write_bytes(query.stdout)
                _run(['schtasks.exe','/Change','/TN',name,'/Disable'])
                _stop_owned_processes()
            with tempfile.TemporaryDirectory() as folder:
                xml=Path(folder)/'task.xml';xml.write_bytes(windows_task(argv))
                _run(['schtasks.exe','/Create','/TN',name,'/XML',str(xml)]+(['/F'] if exists else []))
            return 0
        if not exists: return 0
        flags={'start':['/Run'],'stop':['/End'],'uninstall':['/Delete','/F']}[action]
        if action in {'stop','uninstall'}:
            _run(['schtasks.exe','/Change','/TN',name,'/Disable'])
            _stop_owned_processes()
        if action=='start': _run(['schtasks.exe','/Change','/TN',name,'/Enable'])
        _run(['schtasks.exe',*flags,'/TN',name],check=action!='stop')
    elif sys.platform=='darwin':
        path=Path.home()/'Library/LaunchAgents'/(label+'.plist')
        domain=f'gui/{os.getuid()}'
        if action=='status':
            return _run(['launchctl','print',domain+'/'+label],check=False).returncode
        if action=='install':
            path.parent.mkdir(parents=True,exist_ok=True)
            if path.exists() and plistlib.loads(path.read_bytes()).get('ProgramArguments')!=argv:
                # Permit an upgrade only when the same explicit profile/data are bound.
                old=plistlib.loads(path.read_bytes()).get('ProgramArguments',[])
                if 'codex_telegram_bridge' not in old or str(data) not in old:
                    raise RuntimeError('Existing LaunchAgent is not owned by this Robot.')
            _run(['launchctl','bootout',domain+'/'+label],check=False)
            path.write_bytes(macos_agent(argv,label,data));path.chmod(0o600)
        elif action in {'stop','uninstall'}:
            _run(['launchctl','bootout',domain+'/'+label],check=False)
            if action=='uninstall' and path.exists(): path.unlink()
        if action in {'start','install'}:
            _run(['launchctl','bootstrap',domain,str(path)])
    elif sys.platform.startswith('linux'):
        path=Path(os.environ.get('XDG_CONFIG_HOME') or Path.home()/'.config')/'systemd/user'/(label+'.service')
        if action=='status': return _run(['systemctl','--user','is-active',path.name],check=False).returncode
        if action=='install':
            path.parent.mkdir(parents=True,exist_ok=True)
            if path.exists() and 'codex_telegram_bridge' not in path.read_text():
                raise RuntimeError('Existing systemd unit is not owned by this Robot.')
            if path.exists(): _run(['systemctl','--user','stop',path.name])
            path.write_text(linux_unit(argv),encoding='utf-8');path.chmod(0o600)
            _run(['systemctl','--user','daemon-reload'])
            # Forward only desktop-session variables needed for keyring/capture.
            variables=[x for x in ('DISPLAY','WAYLAND_DISPLAY','XAUTHORITY','DBUS_SESSION_BUS_ADDRESS','XDG_RUNTIME_DIR') if x in os.environ]
            if variables: _run(['systemctl','--user','import-environment',*variables])
            _run(['systemctl','--user','enable',path.name])
        elif action=='uninstall':
            _run(['systemctl','--user','disable','--now',path.name],check=False)
            if path.exists(): path.unlink()
            _run(['systemctl','--user','daemon-reload'])
        else:
            _run(['systemctl','--user',action,path.name])
    else:
        raise RuntimeError('Unsupported operating system.')
    return 0
