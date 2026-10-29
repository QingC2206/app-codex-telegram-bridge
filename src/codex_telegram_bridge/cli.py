"""One Python entry point for all operating systems."""
import argparse
import os
import signal
import sys
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(prog='codex-telegram-bridge')
    parser.add_argument('--codex-home', type=Path)
    parser.add_argument('--data-dir', type=Path)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('setup', help='configure Telegram and secure credentials')
    language = commands.add_parser('language', help='show or persist the UI language (default: Chinese)')
    language.add_argument('value', nargs='?', choices=['zh-CN', 'zh', 'en'])
    run = commands.add_parser('run', help='run the Telegram service in this desktop session')
    run.add_argument('--quiet-start', action='store_true')
    commands.add_parser('notify', help='Codex notification / hook entry point')
    commands.add_parser('desktop-notify', help='Desktop Stop hook inbox; independent of task backend')
    doctor = commands.add_parser('doctor', help='read-only checks; never submits a task message')
    doctor.add_argument('--json', action='store_true')
    doctor.add_argument('--offline', action='store_true', help='only check local installation')
    install = commands.add_parser('install', help='register Codex hooks and the user service')
    install.add_argument('--no-service', action='store_true')
    install.add_argument('--replace-notify', action='store_true', help='explicitly replace another notify command')
    commands.add_parser('uninstall', help='remove owned hooks/service, keeping credentials and messages')
    service = commands.add_parser('service')
    service.add_argument('action', choices=['install', 'start', 'stop', 'status', 'uninstall'])
    configure = commands.add_parser('configure-app-server', help='select the official app-server backend')
    configure.add_argument('--codex', type=Path)
    configure.add_argument('--cwd', type=Path, required=True)
    configure.add_argument('--model')
    configure.add_argument('--effort', choices=['low', 'medium', 'high', 'xhigh', 'max'])
    args, rest = parser.parse_known_args(argv)
    if rest and args.command != 'notify':
        parser.error('unrecognized arguments: '+' '.join(rest))
    if args.codex_home:
        os.environ['CODEX_HOME'] = str(args.codex_home.expanduser().resolve())
    if args.data_dir:
        os.environ['CODEX_TELEGRAM_DATA_DIR'] = str(args.data_dir.expanduser().resolve())
    from .paths import ensure_data_dir
    ensure_data_dir()
    if args.command == 'language':
        from .i18n import get_language, set_language
        print(set_language(args.value) if args.value else get_language())
        return 0
    if args.command == 'desktop-notify':
        from .management.desktop_hook import main as desktop_notify
        return desktop_notify()
    # Recovery and credential setup must work even when backend settings break.
    if args.command == 'service':
        from .management.services import manage
        return manage(args.action)
    if args.command == 'setup':
        from .telegram.notifications import setup
        return setup()
    if args.command == 'uninstall':
        from .paths import ensure_data_dir
        data = ensure_data_dir()
        legacy = (data/'bridge-install.json').is_file()
        stop_hook = (data/'desktop-hook-install.json').is_file()
        if legacy:
            from .management.install import uninstall
            result = uninstall()
            if stop_hook:
                from .management.desktop_hook import register
                register(remove=True)
            return result
        if stop_hook:
            from .backends.app_server.install import uninstall
            return uninstall()
        raise RuntimeError('No managed installation record; no configuration was changed.')
    from .management.runtime import load_settings, configure_app_server
    if args.command == 'configure-app-server':
        return configure_app_server(args.codex, args.cwd, args.model, args.effort)
    runtime = load_settings()
    if runtime['backend'] == 'app-server' and args.command in {'run', 'doctor', 'install', 'notify'}:
        if args.command == 'notify':
            from .management.desktop_hook import main as desktop_notify
            return desktop_notify(rest)
        if args.command == 'install':
            from .backends.app_server.install import install
            return install(service=not args.no_service)
        from .backends.app_server.bot import doctor
        from .backends.app_server.runtime import AppServerBot
        if args.command == 'doctor':
            return doctor(runtime, offline=args.offline)
        bot = AppServerBot(runtime)
        if os.name != 'nt':
            signal.signal(signal.SIGTERM, lambda *_: bot.stop.set())
            signal.signal(signal.SIGINT, lambda *_: bot.stop.set())
        return bot.run(quiet_start=args.quiet_start)
    if args.command == 'run':
        from .backends.desktop import bot as app
        # SIGTERM should run shutdown/finally on POSIX user services.
        if os.name != 'nt':
            signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        sys.argv = [sys.argv[0], '--service'] + (['--quiet-start'] if args.quiet_start else [])
        return app.main()
    if args.command == 'notify':
        from .telegram.notifications import main as notify
        sys.argv = [sys.argv[0], *rest]
        return notify()
    if args.command == 'doctor':
        from .management.doctor import doctor
        return doctor(offline=args.offline, as_json=args.json)
    if args.command == 'install':
        from .management.install import install
        return install(service=not args.no_service, replace_notify=args.replace_notify)
