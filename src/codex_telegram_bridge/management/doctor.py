import importlib.metadata
import json
import platform
import sys
from .. import __version__
from ..paths import codex_home, data_dir


def doctor(*, offline=False, as_json=False):
    report = {'version': __version__, 'os': platform.system(), 'python': platform.python_version(),
              'codex_home': str(codex_home()), 'data_dir': str(data_dir()), 'desktop_only': True}
    dependencies = {}
    for name in ('psutil', 'keyring', 'tomlkit', 'mss'):
        try:
            dependencies[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            dependencies[name] = 'missing'
    report['dependencies'] = dependencies
    report['success'] = sys.platform in {'win32','darwin','linux'} and 'missing' not in dependencies.values()
    if not offline and report['success']:
        from .connection_check import check_connection
        result = check_connection()
        report['connection'] = result
        report['success'] = result['success']
    report['live_task_submission_tested'] = False
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['success'] else 1
