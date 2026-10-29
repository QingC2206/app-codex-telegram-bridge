"""Focused management evidence in temporary profiles; never starts services."""
from concurrent.futures import ThreadPoolExecutor
import importlib
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import Mock, patch

from codex_telegram_bridge import i18n
from codex_telegram_bridge.storage import state as storage


class ManagementContractTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()

    def test_atomic_state_replace_failure_preserves_old_file_and_user_values(self):
        path = self.root/'state.json'
        store = storage.StateStore(path)
        self.assertFalse(store.snapshot()['quiet_mode'])
        self.assertEqual(store.snapshot()['managed_threads'], {})
        store.update(default_cwd='old', user_setting='keep')
        old = path.read_bytes()
        def fail_replace(temporary, target):
            self.assertEqual(target, path)
            self.assertEqual(temporary.parent, path.parent)
            self.assertEqual(path.read_bytes(), old)
            self.assertEqual(json.loads(temporary.read_text())['default_cwd'], 'new')
            raise OSError('replace failed')
        with patch.object(storage.os, 'replace', side_effect=fail_replace):
            with self.assertRaisesRegex(OSError, 'replace failed'):
                store.update(default_cwd='new')
        self.assertEqual(path.read_bytes(), old)
        reread = storage.StateStore(path)
        self.assertEqual(reread.snapshot()['default_cwd'], 'old')
        reread.update(default_cwd='new')
        saved = storage.StateStore(path).snapshot()
        self.assertEqual(saved['default_cwd'], 'new')
        self.assertEqual(saved['user_setting'], 'keep')

    def test_cli_binds_profile_before_importing_business_controller(self):
        profile, data = self.root/'profile with spaces', self.root/'data with spaces'
        script = textwrap.dedent('''
            import importlib.abc, importlib.util, os, sys
            from pathlib import Path
            from codex_telegram_bridge import cli
            assert 'codex_telegram_bridge.core.bot' not in sys.modules
            class Guard(importlib.abc.MetaPathFinder, importlib.abc.Loader):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname == 'codex_telegram_bridge.backends.desktop.bot':
                        return importlib.util.spec_from_loader(fullname, self)
                def create_module(self, spec): return None
                def exec_module(self, module):
                    from codex_telegram_bridge.core import bot
                    assert bot.HOOK_DIR == Path(sys.argv[2]).resolve()
                    assert bot.SESSION_ROOT == Path(sys.argv[1]).resolve()/'sessions'
                    module.main = lambda: 0
            sys.meta_path.insert(0, Guard())
            assert cli.main(['--codex-home', sys.argv[1], '--data-dir', sys.argv[2], 'run']) == 0
            print('PROFILE_BOUND_BEFORE_IMPORT')
        ''')
        result = subprocess.run([sys.executable, '-B', '-c', script, str(profile), str(data)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'PROFILE_BOUND_BEFORE_IMPORT')

    def test_invalid_cli_runtime_exits_nonzero_before_controller_start(self):
        data = self.root/'data'; data.mkdir()
        (data/'runtime.json').write_text('not json')
        result = subprocess.run([sys.executable, '-B', '-m', 'codex_telegram_bridge',
            '--codex-home', str(self.root/'profile'), '--data-dir', str(data), 'run'],
            capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((data/'app-server-state.sqlite3').exists())
        self.assertFalse((data/'telegram_bot.state.json').exists())

    def test_both_offline_doctors_do_not_connect_or_read_credentials(self):
        desktop = importlib.import_module('codex_telegram_bridge.management.doctor')
        runtime = importlib.import_module('codex_telegram_bridge.backends.app_server.runtime')
        check = importlib.import_module('codex_telegram_bridge.management.connection_check')
        executable = self.root/'codex'; executable.write_bytes(b'fixture')
        with patch.object(desktop.importlib.metadata, 'version', return_value='fixture'), \
                patch.object(check, 'check_connection') as network, \
                patch.object(runtime, 'AppServerClient') as client, \
                patch.object(runtime, 'credentials') as credentials, \
                patch.object(runtime, 'telegram_call') as telegram, patch('builtins.print') as output:
            self.assertEqual(desktop.doctor(offline=True), 0)
            self.assertFalse(json.loads(output.call_args.args[0])['live_task_submission_tested'])
            self.assertEqual(runtime.doctor({'codex_executable':str(executable),
                                             'cwd':str(self.root)}, offline=True), 0)
        network.assert_not_called(); client.assert_not_called()
        credentials.assert_not_called(); telegram.assert_not_called()

    def test_language_overrides_are_isolated_between_overlapping_threads(self):
        barrier = threading.Barrier(2)
        def translate(language):
            with i18n.using_language(language):
                barrier.wait(timeout=3)
                value = i18n.tr('刷新')
                barrier.wait(timeout=3)
            return value, i18n.get_language()
        with patch.object(i18n, 'data_dir', return_value=self.root), ThreadPoolExecutor(2) as pool:
            i18n._read_preference.cache_clear()
            zh = pool.submit(translate, 'zh')
            en = pool.submit(translate, 'en')
            self.assertEqual(zh.result(timeout=5), ('刷新', 'zh-CN'))
            self.assertEqual(en.result(timeout=5), ('Refresh', 'zh-CN'))
            self.assertEqual(i18n.get_language(), 'zh-CN')

    def test_authorized_old_screenshot_callback_reaches_shared_plugin_only_on_request(self):
        from codex_telegram_bridge.core import bot as core
        bot = core.TelegramCodexBot('111:fixture', {'chat_id':'123'})
        bot.plugins.command = Mock(return_value=True)
        bot._answer_callback = Mock(); bot._operation_reaction = Mock(return_value=True)
        bot.send = Mock(return_value={'message_id':70}); bot.edit_message = Mock()
        bot.app.request = Mock()
        bot.plugins.command.assert_not_called()
        with patch.object(core.telegram_notify_module, 'navigation_view', return_value=None):
            bot.handle_callback({'id':'cb', 'data':'screenshot|desktop',
                'from':{'id':123,'is_bot':False},
                'message':{'chat':{'id':123,'type':'private'}, 'message_id':70}})
        bot.plugins.command.assert_called_once_with('screenshot', 'desktop')
        bot.app.request.assert_not_called()

    def test_plugin_directory_and_entrypoint_symlinks_cannot_escape_trusted_root(self):
        from codex_telegram_bridge.plugins.host import PluginHost
        root = self.root/'plugins'; root.mkdir()
        outside = self.root/'outside'; outside.mkdir()
        manifest = {'id':'demo','api_version':1,'version':'1','entrypoint':'main.py',
                    'commands':{'demo':'Demo'}}
        (outside/'plugin.json').write_text(json.dumps(manifest))
        (outside/'main.py').write_text("raise RuntimeError('must never import')")
        try:
            (root/'demo').symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('Creating local symlinks is not permitted on this host')
        host = PluginHost(Mock(), root, self.root/'state')
        host.discover()
        self.assertNotIn('demo', host.plugins)
        self.assertIn('demo', host.errors)
        (root/'demo').unlink(); (root/'demo').mkdir()
        (root/'demo/plugin.json').write_text(json.dumps(manifest))
        (root/'demo/main.py').symlink_to(outside/'main.py')
        host.discover()
        self.assertNotIn('demo', host.plugins)
        self.assertIn('demo', host.errors)

    def test_service_command_binds_absolute_interpreter_profile_and_data(self):
        from codex_telegram_bridge.management import services
        profile, data = self.root/'profile', self.root/'data with spaces'
        with patch.dict(os.environ, CODEX_HOME=str(profile), CODEX_TELEGRAM_DATA_DIR=str(data)):
            argv = services.command()
        self.assertTrue(Path(argv[0]).is_absolute())
        self.assertEqual(Path(argv[0]).name.lower().startswith('python'), True)
        self.assertEqual(argv[argv.index('--codex-home')+1], str(profile.resolve()))
        self.assertEqual(argv[argv.index('--data-dir')+1], str(data.resolve()))
        self.assertEqual(argv[-2:], ['run', '--quiet-start'])

    def test_windows_service_uses_the_current_desktop_account(self):
        from codex_telegram_bridge.platforms.windows import services
        with patch.object(services.getpass, 'getuser', return_value='FixtureUser'), \
                patch.dict(os.environ, USERDOMAIN='FixtureDomain'):
            tree = ET.fromstring(services.task_definition(['python', 'run'], self.root))
        users = [item.text for item in tree.iter() if item.tag.endswith('UserId')]
        self.assertEqual(users, ['FixtureDomain\\FixtureUser', 'FixtureDomain\\FixtureUser'])
        self.assertEqual(next(item.text for item in tree.iter() if item.tag.endswith('LogonType')), 'InteractiveToken')

    def test_both_uninstallers_preserve_route_cache_credential_and_backup_bytes(self):
        from codex_telegram_bridge.management import install, services
        from codex_telegram_bridge.backends.app_server import install as app_install
        for index, installer in enumerate((install, app_install)):
            profile = self.root/f'profile-{index}'; profile.mkdir()
            data = self.root/f'data-{index}'; data.mkdir()
            paths = ['telegram_notify.credentials.json', 'telegram_bot.state.json',
                     'app-server-state.sqlite3', 'telegram_reactions.sqlite3',
                     'telegram_message_routes/route.json', 'telegram_replies/reply.json',
                     'backups/original.json']
            expected = {}
            for relative in paths:
                path = data/relative; path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(json.dumps({'fixture':relative}, ensure_ascii=False).encode())
                expected[path] = path.read_bytes()
            with patch.dict(os.environ, CODEX_HOME=str(profile), CODEX_TELEGRAM_DATA_DIR=str(data)), \
                    patch('codex_telegram_bridge.codex.desktop.discover_desktop'), \
                    patch.object(services, 'manage', return_value=0), patch('builtins.print'):
                self.assertEqual(installer.install(service=False), 0)
                self.assertEqual(installer.uninstall(), 0)
            for path, raw in expected.items():
                self.assertEqual(path.read_bytes(), raw, str(path))

    def test_plugin_start_and_idle_watch_do_not_execute_screenshot(self):
        from codex_telegram_bridge.plugins import host as plugins
        bot = Mock()
        bot.stop_event.is_set.return_value = False
        bot.stop_event.wait.side_effect = [False, False, True]
        bot._set_commands.return_value = True
        host = plugins.PluginHost(bot, self.root/'plugins', self.root/'state')
        host.execute = Mock(); host.launch = Mock()
        with patch.object(plugins.threading, 'Thread') as thread, \
                patch.object(plugins.subprocess, 'Popen') as process:
            host.start()
            self.assertIn('screenshot', host.plugins)
            thread.return_value.start.assert_called_once()
            host._watch()
        self.assertEqual(bot.stop_event.wait.call_count, 3)
        bot._set_commands.assert_called_once()
        host.execute.assert_not_called(); host.launch.assert_not_called(); process.assert_not_called()

    def test_timed_out_plugin_worker_pid_has_exited(self):
        import psutil
        from codex_telegram_bridge.plugins import host as plugins
        host = plugins.PluginHost(Mock(), self.root/'plugins', self.root/'state')
        code = ("import os,time\n"
                "def handle(request,ctx):\n"
                "    (ctx.output_dir/'worker-pid').write_text(str(os.getpid()))\n"
                "    time.sleep(20)\n")
        plugin = plugins.Plugin('slow', {'entrypoint':'main.py', 'timeout_seconds':1},
                                {'main.py':code.encode()}, {}, 'fixture')
        work = self.root/'work'; work.mkdir()
        with self.assertRaises(TimeoutError):
            host.execute(plugin, {'kind':'command'}, work)
        pid = int((work/'output/worker-pid').read_text())
        self.assertFalse(psutil.pid_exists(pid), 'Timed-out owned worker is still alive')

    @unittest.skipUnless(os.name == 'posix', 'POSIX CLI SIGTERM contract')
    def test_cli_sigterm_runs_shutdown_before_releasing_controller_lock(self):
        marker = self.root/'shutdown.txt'
        script = textwrap.dedent('''
            import os,sys,time
            from pathlib import Path
            os.environ['CODEX_HOME'] = sys.argv[1]
            os.environ['CODEX_TELEGRAM_DATA_DIR'] = sys.argv[2]
            from codex_telegram_bridge.core import bot as core
            from codex_telegram_bridge.platforms import locking
            from codex_telegram_bridge import cli
            locking._MACHINE_ENDPOINT = ('127.0.0.1', 0)
            marker = Path(sys.argv[3])
            def record(value):
                with marker.open('a') as stream: stream.write(value+'\\n')
            class Lock(locking.SingleInstance):
                def __exit__(self,*args):
                    super().__exit__(*args)
                    record('released')
            class Bot(core.TelegramCodexBot):
                def __init__(self,*args): pass
                def _run_service(self,**kwargs):
                    print('READY',flush=True)
                    time.sleep(60)
                def shutdown(self): record('closed')
            core.SingleInstance = Lock
            core.TelegramCodexBot = Bot
            core.load_config = lambda: ({'chat_id':'123'}, '111:fixture')
            core.log = lambda *args: None
            raise SystemExit(cli.main(['run']))
        ''')
        process = subprocess.Popen([sys.executable, '-B', '-c', script,
            str(self.root/'profile'), str(self.root/'data'), str(marker)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        self.addCleanup(cleanup)
        with selectors.DefaultSelector() as ready:
            ready.register(process.stdout, selectors.EVENT_READ)
            self.assertTrue(ready.select(timeout=5), 'Fixture did not become ready')
            self.assertEqual(process.stdout.readline().strip(), 'READY')
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=5)
        events = marker.read_text().splitlines()
        self.assertIn('closed', events)
        self.assertIn('released', events)
        self.assertLess(events.index('closed'), events.index('released'))


if __name__ == '__main__':
    unittest.main()
