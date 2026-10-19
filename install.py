"""Install into a private venv; never changes the user's global Python."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import venv


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--codex-home', type=Path, default=Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex'))
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--no-service', action='store_true')
    parser.add_argument('--replace-notify', action='store_true')
    parser.add_argument('--skip-setup', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    env = {**os.environ, 'CODEX_HOME': str(args.codex_home.expanduser().resolve())}
    if args.data_dir:
        env['CODEX_TELEGRAM_DATA_DIR'] = str(args.data_dir.expanduser().resolve())
    runtime = args.codex_home.expanduser().resolve()/'telegram-bridge-runtime'
    python = runtime/('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not python.exists():
        venv.EnvBuilder(with_pip=True).create(runtime)
    command = [str(python), '-m', 'codex_telegram_bridge']
    installed = subprocess.run([str(python), '-c',
        'import importlib.util; raise SystemExit(0 if importlib.util.find_spec("codex_telegram_bridge") else 3)'], env=env)
    if installed.returncode not in (0,3):
        return installed.returncode
    stopped = False
    if installed.returncode == 0:
        status = subprocess.run([str(python), '-c',
            'from codex_telegram_bridge.management.services import snapshot; raise SystemExit(0 if snapshot()["content"] is not None else 3)'], env=env)
        if status.returncode not in (0,3):
            return status.returncode
        if status.returncode == 0:
            subprocess.run([*command, 'service', 'stop'], env=env, check=True)
            stopped = True
    try:
        subprocess.run([str(python), '-m', 'pip', 'install', '--upgrade', str(root)], env=env, check=True)
    except subprocess.CalledProcessError:
        if stopped:
            subprocess.run([*command, 'service', 'start'], env=env)
        raise
    def finish(code):
        if stopped and (code or args.no_service):
            subprocess.run([*command, 'service', 'start'], env=env)
        return code
    # Existing users retain their credential store and do not reconfigure.
    probe = subprocess.run([str(python), '-c',
        'from codex_telegram_bridge.paths import data_dir; raise SystemExit(0 if (data_dir()/"telegram_notify.credentials.json").is_file() else 3)'],env=env)
    if probe.returncode not in (0,3):
        return finish(probe.returncode)
    if probe.returncode == 3 and not args.skip_setup:
        result = subprocess.run([*command, 'setup'], env=env)
        if result.returncode not in (0,2):
            return finish(result.returncode)
    options = (['--no-service'] if args.no_service else []) + (['--replace-notify'] if args.replace_notify else [])
    return finish(subprocess.call([*command, 'install', *options], env=env))


if __name__ == '__main__':
    raise SystemExit(main())
