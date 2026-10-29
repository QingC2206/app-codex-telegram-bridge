"""Offline regression runner. No real Telegram credentials or Desktop writes."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
GROUPS = {
    'AppServer': ['app_server', 'app_server_gaps', 'app_server_ui', 'shared_controller', 'controller_delivery'],
    'Compatibility': ['platform_compatibility', 'desktop_bridge', 'management', 'platforms.test_macos_desktop_access'],
    'Communication': ['desktop_bridge','desktop_receipts','connection_check','protocol_adaptation','async_inputs','remote_tasks'],
    'Notifications': ['telegram_tables','notification_diagnostics','internal_requests','completion_routing'], 'Quota': ['telegram_quota'],
    'Interactions': ['interactions','running_tasks','i18n','settings','quiet_mode'], 'Screenshots': ['screen_capture'],
    'Plugins': ['plugins'], 'SideChat': ['side_chat'], 'Security': ['hardening'],
    'Images': ['local_images'], 'Feedback': ['operation_feedback', 'controller_delivery'],
    'Platforms': ['platforms','management', 'platforms.test_macos_desktop_access'],
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--suite', nargs='+', choices=[*GROUPS, 'Full'], default=['Full'])
    parser.add_argument('--report', type=Path, help='write individual outcomes and source hashes')
    args = parser.parse_args()
    modules = [name if '.' in name else 'test_'+name for group in args.suite for name in GROUPS.get(group, [])]
    command = [sys.executable, '-m', 'unittest']
    command += ['discover', '-s', str(ROOT/'tests'), '-v'] if 'Full' in args.suite else ['-v', *dict.fromkeys(modules)]
    if args.report:
        command = [sys.executable, str(ROOT/'tools/test_report.py'), str(args.report.resolve()),
                   *([] if 'Full' in args.suite else dict.fromkeys(modules))]
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
