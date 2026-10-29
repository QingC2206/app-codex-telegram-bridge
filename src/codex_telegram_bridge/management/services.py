"""Service ownership and compatibility facade; OS commands live in platforms/."""
import hashlib
import os
from pathlib import Path
import plistlib
import subprocess
import sys

from ..paths import codex_home, data_dir, ensure_data_dir
from ..platforms.service import ServiceContext, service_adapter


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


def _owned_arguments(argv):
    return ('codex_telegram_bridge' in argv and '--codex-home' in argv
            and '--data-dir' in argv
            and argv[argv.index('--codex-home')+1:argv.index('--codex-home')+2] == [str(codex_home())]
            and argv[argv.index('--data-dir')+1:argv.index('--data-dir')+2] == [str(data_dir())])


def _validate_definition(path):
    if not path.exists():
        return
    if path.suffix == '.plist':
        owned = _owned_arguments(plistlib.loads(path.read_bytes()).get('ProgramArguments', []))
    else:
        content = path.read_text(encoding='utf-8')
        owned = ('codex_telegram_bridge' in content
                 and _systemd_arg(str(codex_home())) in content
                 and _systemd_arg(str(data_dir())) in content)
    if not owned:
        raise RuntimeError('Existing user service is not owned by this Robot/profile.')


def windows_task(argv):
    from ..platforms.windows.services import task_definition
    return task_definition(argv, data_dir())


def _task_xml(value):
    from ..platforms.windows.services import normalize_task_xml
    return normalize_task_xml(value)


def _legacy_owned(xml):
    from ..platforms.windows.services import is_owned
    return is_owned(xml, codex_home(), data_dir())


def macos_agent(argv, label, data):
    from ..platforms.macos.services import agent_definition
    return agent_definition(argv, label, data)


def _systemd_arg(value):
    from ..platforms.linux.services import quote_argument
    return quote_argument(value)


def linux_unit(argv):
    from ..platforms.linux.services import unit_definition
    return unit_definition(argv)


def _context():
    return ServiceContext(profile=codex_home(), data=ensure_data_dir(), argv=command(),
        label=identity(), run=_run, stop_owned_processes=_stop_owned_processes,
        validate_definition=_validate_definition)


def snapshot():
    return service_adapter().snapshot(_context())


def restore(previous):
    return service_adapter(previous['platform']).restore(_context(), previous)


def manage(action):
    return service_adapter().manage(_context(), action)


def install_and_start():
    result = manage('install')
    if result not in (None, 0):
        raise RuntimeError('Service installation failed.')
    if not service_adapter().STARTS_ON_INSTALL:
        result = manage('start')
        if result not in (None, 0):
            raise RuntimeError('Service startup failed.')
