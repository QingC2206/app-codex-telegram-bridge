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
    subprocess.run([str(python), '-m', 'pip', 'install', '--upgrade', str(root)], env=env, check=True)
    command = [str(python), '-m', 'codex_telegram_bridge']
    # Existing users retain their credential store and do not reconfigure.
    probe = subprocess.run([str(python), '-c',
        'from codex_telegram_bridge.paths import data_dir; raise SystemExit(0 if (data_dir()/"telegram_notify.credentials.json").is_file() else 1)'],env=env)
    if probe.returncode and not args.skip_setup:
        result = subprocess.run([*command, 'setup'], env=env)
        if result.returncode not in (0,2):
            return result.returncode
    options = (['--no-service'] if args.no_service else []) + (['--replace-notify'] if args.replace_notify else [])
    return subprocess.call([*command, 'install', *options], env=env)


if __name__ == '__main__':
    raise SystemExit(main())
