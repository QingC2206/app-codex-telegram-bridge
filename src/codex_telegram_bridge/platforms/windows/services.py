"""windows user-session service adapter; commands receive an explicit context."""
import getpass
import os
from pathlib import Path
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
        args=' '.join(n.text or '' for n in root.iter() if n.tag.endswith('Arguments'))
        return ('codex_telegram_bridge' in args and str(profile) in args) or (
            str(data/'telegram_bot.py').lower() in args.lower())
    except (ET.ParseError,UnicodeError):
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
