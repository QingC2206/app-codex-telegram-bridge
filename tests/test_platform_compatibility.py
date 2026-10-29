"""Portable Windows contracts; native IPC/Task Scheduler acceptance is separate."""
from contextlib import ExitStack
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, call, patch

from codex_telegram_bridge import cli
from codex_telegram_bridge.management import install, services, runtime
from codex_telegram_bridge.backends.app_server import install as server_install
from codex_telegram_bridge.platforms import processes
from codex_telegram_bridge.platforms.desktop_access import desktop_access
from codex_telegram_bridge.platforms.windows.desktop_access import WindowsDesktopAccess


class IsolatedProfile(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.profile = self.root/'profile'; self.profile.mkdir()
        self.data = self.root/'data'; self.data.mkdir()
        env = patch.dict(os.environ, CODEX_HOME=str(self.profile), CODEX_TELEGRAM_DATA_DIR=str(self.data))
        env.start(); self.addCleanup(env.stop)


class RecoveryCommandTests(IsolatedProfile):
    def test_invalid_runtime_never_blocks_service_recovery(self):
        for damaged in ('not json', '[]', '{"backend":"unknown"}'):
            (self.data/'runtime.json').write_text(damaged)
            for action in ('status', 'stop', 'uninstall'):
                with self.subTest(runtime=damaged, action=action), patch.object(services, 'manage', return_value=0) as manage:
                    self.assertEqual(cli.main(['service', action]), 0)
                    manage.assert_called_once_with(action)

    def test_invalid_runtime_never_blocks_credential_setup(self):
        (self.data/'runtime.json').write_text('broken')
        with patch('codex_telegram_bridge.telegram.notifications.setup', return_value=0) as setup:
            self.assertEqual(cli.main(['setup']), 0)
            setup.assert_called_once_with()

    def test_uninstall_selects_owned_manifest_without_loading_runtime(self):
        (self.data/'runtime.json').write_text('broken')
        for manifest, installer in [('bridge-install.json', install), ('desktop-hook-install.json', server_install)]:
            path = self.data/manifest; path.write_text('{}')
            with self.subTest(manifest=manifest), patch.object(installer, 'uninstall', return_value=0) as remove:
                self.assertEqual(cli.main(['uninstall']), 0)
                remove.assert_called_once_with()
            path.unlink()

    def test_invalid_runtime_still_prevents_run(self):
        for damaged in ('broken', '[]', '{"backend":"unknown"}'):
            (self.data/'runtime.json').write_text(damaged)
            with self.subTest(runtime=damaged), self.assertRaises(ValueError):
                cli.main(['run'])

    def test_default_windows_dispatch_remains_desktop(self):
        self.assertEqual(runtime.load_settings(), {'backend':'desktop'})
        with patch.object(sys, 'platform', 'win32'), patch.object(install, 'install', return_value=0) as legacy, \
                patch.object(server_install, 'install') as standalone:
            self.assertEqual(cli.main(['install', '--no-service']), 0)
        legacy.assert_called_once_with(service=False, replace_notify=False)
        standalone.assert_not_called()
        self.assertFalse((self.data/'runtime.json').exists())
        self.assertIsInstance(desktop_access('win32'), WindowsDesktopAccess)


class InstallerParityTests(IsolatedProfile):
    def _originals(self):
        config=self.profile/'config.toml';config.write_text('# user setting\nmodel="example"\n')
        hooks=self.profile/'hooks.json';hooks.write_text('{"hooks":{},"custom":true}')
        return {config:config.read_bytes(),hooks:hooks.read_bytes()}

    def test_both_backends_rollback_files_and_service_when_windows_start_fails(self):
        for installer in (install, server_install):
            originals=self._originals()
            with self.subTest(backend=installer.__name__), patch.object(sys,'platform','win32'), \
                    patch('codex_telegram_bridge.codex.desktop.discover_desktop'), \
                    patch.object(services,'snapshot',return_value={'prior':'owned'}) as snapshot, \
                    patch.object(services,'manage',side_effect=[0,RuntimeError('start failed')]) as manage, \
                    patch.object(services,'restore') as restore:
                with self.assertRaisesRegex(RuntimeError,'start failed'):installer.install()
                snapshot.assert_called_once_with()
                self.assertEqual(manage.call_args_list,[call('install'),call('start')])
                restore.assert_called_once_with({'prior':'owned'})
            for path, content in originals.items():self.assertEqual(path.read_bytes(),content)
            self.assertFalse((self.data/'bridge-install.json').exists())
            self.assertFalse((self.data/'desktop-hook-install.json').exists())

    def test_foreign_service_is_rejected_before_changing_any_hooks(self):
        for installer in (install,server_install):
            originals=self._originals()
            with self.subTest(backend=installer.__name__), patch('codex_telegram_bridge.codex.desktop.discover_desktop'), \
                    patch.object(services,'snapshot',side_effect=RuntimeError('not owned')), patch.object(services,'manage') as manage:
                with self.assertRaisesRegex(RuntimeError,'not owned'):installer.install()
                manage.assert_not_called()
            for path,content in originals.items():self.assertEqual(path.read_bytes(),content)

    def test_windows_linux_start_explicitly_and_macos_bootstrap_starts_once(self):
        for platform in ('win32','linux','darwin'):
            with self.subTest(platform=platform), patch.object(sys,'platform',platform), \
                    patch.object(services,'manage',return_value=0) as manage:
                services.install_and_start()
                self.assertEqual(manage.call_args_list,[call('install')]+([] if platform=='darwin' else [call('start')]))

    def test_nonzero_start_status_is_a_transaction_failure(self):
        originals=self._originals()
        with patch.object(sys,'platform','win32'),patch.object(services,'snapshot',return_value={}), \
                patch.object(services,'manage',side_effect=[0,1]),patch.object(services,'restore') as restore:
            with self.assertRaisesRegex(RuntimeError,'startup failed'):server_install.install()
            restore.assert_called_once_with({})
        for path,content in originals.items():self.assertEqual(path.read_bytes(),content)


class BackendContracts(IsolatedProfile):
    def test_compatibility_imports_are_the_same_module_not_copied_code(self):
        for old,new in [('app','backends.desktop.bot'),('app_server_bot','backends.app_server.bot'),
                        ('app_server_features','backends.app_server.commands'),('app_server_ui','backends.app_server.ui'),
                        ('app_server_approvals','backends.app_server.approvals'),('codex.app_server','backends.app_server.client'),
                        ('codex.app_server_state','storage.bridge'),('telegram.app_server_io','telegram.delivery')]:
            with self.subTest(module=old):
                self.assertIs(importlib.import_module('codex_telegram_bridge.'+old),importlib.import_module('codex_telegram_bridge.'+new))

    def test_both_clients_launch_with_windows_hidden_window_and_owned_job(self):
        from codex_telegram_bridge.backends.desktop import bot as legacy
        from codex_telegram_bridge.backends.app_server import client as standalone
        exe=self.root/'codex.exe';exe.write_bytes(b'fixture')
        for profile in ('desktop', 'configured'):
            with self.subTest(profile=profile), ExitStack() as stack:
                stack.enter_context(patch.object(sys, 'platform', 'win32'))
                popen = stack.enter_context(patch.object(legacy.subprocess, 'Popen'))
                stack.enter_context(patch.object(legacy.threading, 'Thread'))
                job = stack.enter_context(patch.object(legacy, 'ChildProcessJob'))
                stack.enter_context(patch.object(legacy, 'locate_codex', return_value=exe))
                stack.enter_context(patch.object(legacy, 'log'))
                client = (legacy.CodexAppServer(Mock(), Mock()) if profile == 'desktop'
                          else standalone.AppServerClient(exe, self.root/'client.log'))
                transport = client if profile == 'desktop' else client._client
                stack.enter_context(patch.object(transport, '_request', return_value={}))
                stack.enter_context(patch.object(transport, 'notify'))
                transport.ensure_started() if profile == 'desktop' else client.start()
                kwargs = popen.call_args.kwargs
                self.assertEqual(kwargs['creationflags'], 0x08000000)
                self.assertNotIn('start_new_session', kwargs)
                job.return_value.attach.assert_called_once_with(popen.return_value)

    def test_posix_keeps_process_group_semantics(self):
        self.assertEqual(processes.spawn_options('darwin'),{'start_new_session':True})
        self.assertEqual(processes.spawn_options('linux'),{'start_new_session':True})
