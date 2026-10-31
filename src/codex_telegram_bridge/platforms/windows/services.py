"""windows user-session service adapter; commands receive an explicit context."""
import getpass
import os
from pathlib import Path
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET

STARTS_ON_INSTALL = False


def task_definition(argv, data):
    ns='http://schemas.microsoft.com/windows/2004/02/mit/task'
    domain=os.environ.get('USERDOMAIN'); user=getpass.getuser()
    account=f'{domain}\\{user}' if domain else user
    ET.register_namespace('',ns)
    def child(parent,name,text=None,**attrs):
        node=ET.SubElement(parent,'{'+ns+'}'+name,attrs);node.text=text;return node
    root=ET.Element('{'+ns+'}Task',{'version':'1.2'})
    triggers=child(root,'Triggers')
    trigger=child(triggers,'LogonTrigger');child(trigger,'Enabled','true')
    # An unscoped logon trigger covers all users and requires elevation.
    child(trigger,'UserId',account)
    trigger=child(triggers,'TimeTrigger');child(trigger,'StartBoundary','2020-01-01T00:00:00')
    repetition=child(trigger,'Repetition');child(repetition,'Interval','PT1M');child(repetition,'StopAtDurationEnd','false')
    principals=child(root,'Principals');principal=child(principals,'Principal',id='Author')
    child(principal,'UserId',account)
    child(principal,'LogonType','InteractiveToken');child(principal,'RunLevel','LeastPrivilege')
    settings=child(root,'Settings');child(settings,'MultipleInstancesPolicy','IgnoreNew')
    child(settings,'DisallowStartIfOnBatteries','false');child(settings,'StopIfGoingOnBatteries','false')
    child(settings,'StartWhenAvailable','true');child(settings,'ExecutionTimeLimit','PT0S')
    actions=child(root,'Actions',Context='Author');execute=child(actions,'Exec')
    child(execute,'Command',argv[0]);child(execute,'Arguments',subprocess.list2cmdline(argv[1:]))
    child(execute,'WorkingDirectory',str(data))
    return ET.tostring(root,encoding='utf-16',xml_declaration=True)

def normalize_task_xml(value):
    """schtasks may emit UTF-8/ANSI bytes with a UTF-16 XML declaration."""
    if isinstance(value, bytes):
        if value.startswith((b'\xff\xfe',b'\xfe\xff')) or b'\x00' in value[:40]:
            value = value.decode('utf-16')
        else:
            try:
                value = value.decode('utf-8-sig')
            except UnicodeDecodeError:
                if os.name != 'nt':
                    raise
                value = value.decode('mbcs')
    root = ET.fromstring(value)
    return ET.tostring(root,encoding='utf-16',xml_declaration=True)

def is_owned(xml, profile, data):
    try:
        root=ET.fromstring(normalize_task_xml(xml))
        actions=[n for n in root.iter() if n.tag.rsplit('}',1)[-1]=='Actions']
        if len(actions)!=1 or len(actions[0])!=1:
            return False
        action=actions[0][0]
        if action.tag.rsplit('}',1)[-1]!='Exec':
            return False
        fields={n.tag.rsplit('}',1)[-1]:n.text or '' for n in action}
        if len(fields)!=len(action) or not re.fullmatch(r'pythonw?(?:\d+(?:\.\d+)*)?(?:\.exe)?',
                re.split(r'[/\\]',fields.get('Command',''))[-1],re.I):
            return False
        from ...management.hook_handlers import direct_command_arguments
        args=direct_command_arguments(fields.get('Arguments',''),windows=True)
        if not args:
            return False
        def same_path(value, expected):
            candidate=Path(value).expanduser()
            return candidate.is_absolute() and candidate.resolve()==expected.resolve()
        # Older script installations are owned only by this exact data path.
        if args[0]!='-m':
            return (same_path(args[0],data/'telegram_bot.py')
                    and args[1:] in (['--service'],['--service','--quiet-start']))
        if args[:2]!=['-m','codex_telegram_bridge']:
            return False
        options={}; tail=args[2:]
        while tail and tail[0].startswith('--'):
            if len(tail)<2 or tail[0] not in {'--codex-home','--data-dir'} or tail[0] in options:
                return False
            options[tail[0]]=tail[1];tail=tail[2:]
        return (tail in (['run'],['run','--quiet-start'])
                and set(options)=={'--codex-home','--data-dir'}
                and same_path(options['--codex-home'],profile)
                and same_path(options['--data-dir'],data))
    except (ET.ParseError,UnicodeError,OSError,RuntimeError,ValueError):
        return False


def snapshot(context):
    label = context.label
    name = 'Codex Telegram Bot' if context.profile == (Path.home()/'.codex').resolve() else label
    result = context.run(['schtasks.exe', '/Query', '/TN', name, '/XML'], check=False)
    content = normalize_task_xml(result.stdout) if result.returncode == 0 else None
    if content is not None and not is_owned(content, context.profile, context.data):
        raise RuntimeError('Existing scheduled task is not owned by this Robot/profile.')
    return {'platform': 'win32', 'name': name, 'content': content}


def restore(context, previous):
    content = previous['content']
    name = previous['name']
    context.run(['schtasks.exe','/Change','/TN',name,'/Disable'],check=False)
    context.stop_owned_processes()
    context.run(['schtasks.exe','/End','/TN',name],check=False)
    if content is None:
        context.run(['schtasks.exe','/Delete','/TN',name,'/F'],check=False)
    else:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'task.xml';path.write_bytes(content)
            context.run(['schtasks.exe','/Create','/TN',name,'/XML',str(path),'/F'])
    # The restored task retains its enabled setting and original triggers.
    return


def manage(context, action):
    data, argv, label = context.data, context.argv, context.label
    default=(Path.home()/'.codex').resolve()
    name='Codex Telegram Bot' if context.profile==default else label
    query=context.run(['schtasks.exe','/Query','/TN',name,'/XML'],check=False)
    exists=query.returncode==0
    if exists:
        query.stdout = normalize_task_xml(query.stdout)
    if exists and not is_owned(query.stdout, context.profile, context.data):
        raise RuntimeError('The existing scheduled task is not owned by this Robot/profile.')
    if action=='status':
        print('installed' if exists else 'not installed');return 0 if exists else 1
    if action=='install':
        if exists:
            backup=data/'backups';backup.mkdir(exist_ok=True)
            previous=backup/'previous-startup-task.xml'
            if not previous.exists(): previous.write_bytes(query.stdout)
            context.run(['schtasks.exe','/Change','/TN',name,'/Disable'])
            context.stop_owned_processes()
        with tempfile.TemporaryDirectory() as folder:
            xml=Path(folder)/'task.xml';xml.write_bytes(task_definition(argv, context.data))
            context.run(['schtasks.exe','/Create','/TN',name,'/XML',str(xml)]+(['/F'] if exists else []))
        return 0
    if not exists: return 0
    flags={'start':['/Run'],'stop':['/End'],'uninstall':['/Delete','/F']}[action]
    if action in {'stop','uninstall'}:
        context.run(['schtasks.exe','/Change','/TN',name,'/Disable'])
        context.stop_owned_processes()
    if action=='start': context.run(['schtasks.exe','/Change','/TN',name,'/Enable'])
    context.run(['schtasks.exe',*flags,'/TN',name],check=action!='stop')

    return 0
