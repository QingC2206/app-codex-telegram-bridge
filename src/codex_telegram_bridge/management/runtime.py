"""Explicit runtime selection; Desktop remains the default for existing installs."""
import json
import os
from pathlib import Path
import shutil

from ..paths import ensure_data_dir


def settings_path():
    return ensure_data_dir() / 'runtime.json'


def load_settings():
    path = settings_path()
    value = json.loads(path.read_text()) if path.exists() else {'backend': 'desktop'}
    if not isinstance(value, dict) or value.get('backend') not in ('desktop', 'app-server'):
        raise ValueError('Unknown robot backend')
    return value


def configure_app_server(executable, cwd, model=None, effort=None):
    binary = Path(executable or shutil.which('codex') or '').expanduser().resolve()
    workspace = Path(cwd).expanduser().resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError('Codex executable is unavailable')
    if not workspace.is_dir():
        raise ValueError('工作目录必须已存在。')
    value = {'backend': 'app-server', 'codex_executable': str(binary), 'cwd': str(workspace),
             'model': model, 'effort': effort, 'turn_timeout_seconds': 1800}
    path = settings_path()
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.chmod(0o600)
    os.replace(temporary, path)
    print('Configured official app-server backend. Workspace: ' + str(workspace))
    return 0
