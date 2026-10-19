"""Offline regression runner. No real Telegram credentials or Desktop writes."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
GROUPS = {
    'Communication': ['desktop_bridge','desktop_receipts','connection_check','protocol_adaptation','async_inputs'],
    'Notifications': ['telegram_tables'], 'Quota': ['telegram_quota'],
    'Interactions': ['interactions','running_tasks','i18n','settings'], 'Screenshots': ['screen_capture'],
    'Plugins': ['plugins'], 'SideChat': ['side_chat'], 'Security': ['hardening'],
    'Images': ['local_images'], 'Feedback': ['operation_feedback'],
    'Platforms': ['platforms','management'],
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--suite', nargs='+', choices=[*GROUPS, 'Full'], default=['Full'])
    args = parser.parse_args()
    modules = ['test_'+name for group in args.suite for name in GROUPS.get(group, [])]
    command = [sys.executable, '-m', 'unittest']
    command += ['discover', '-s', str(ROOT/'tests'), '-v'] if 'Full' in args.suite else ['-v', *dict.fromkeys(modules)]
    with tempfile.TemporaryDirectory(prefix='codex-bridge-tests-') as temporary:
        base = Path(temporary)
        data = base/'data'; data.mkdir()
        env = {**os.environ, 'CODEX_HOME':str(base/'codex'), 'CODEX_TELEGRAM_DATA_DIR':str(data),
               'PYTHONDONTWRITEBYTECODE':'1', 'PYTHONUTF8':'1',
               'PYTHONPATH':os.pathsep.join([str(ROOT/'src'),str(ROOT/'tests')])}
        print('Offline tests; real Telegram/Desktop delivery is not exercised.', flush=True)
        return subprocess.call(command, cwd=ROOT, env=env)


if __name__ == '__main__':
    raise SystemExit(main())
