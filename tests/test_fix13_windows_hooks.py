"""R01/R05 regressions; temporary profiles and fake OS process/service calls."""
import io
import json
import os
import shlex
import subprocess
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from codex_telegram_bridge.codex import desktop
from codex_telegram_bridge.management import desktop_hook, install, services
from codex_telegram_bridge.management.hook_handlers import direct_command_arguments
from codex_telegram_bridge.platforms.processes import spawn_options
from codex_telegram_bridge.plugins import host


def shell_command(args):
    return subprocess.list2cmdline(args) if os.name == 'nt' else shlex.join(args)


class PluginSpawnContractTests(unittest.TestCase):
    def test_plugin_worker_receives_each_platform_options_once(self):
        for platform in ('win32', 'darwin', 'linux'):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                plugin = host.Plugin('fixture', {'entrypoint': 'main.py', 'timeout_seconds': 1},
                                     {'main.py': b'pass'}, {}, 'fixture')
                runner = host.PluginHost(Mock(), root/'plugins', root/'state')
                result = {'actions': [{'type': 'message', 'text': 'fixture'}]}
                process = Mock()
                process.communicate.side_effect = lambda *a, **k: (root/'result.json').write_text(json.dumps(result))
                with patch.object(host, 'spawn_options', return_value=spawn_options(platform)), \
                        patch.object(host.subprocess, 'Popen', return_value=process) as popen, \
                        patch.object(host, 'ChildProcessJob') as job:
                    self.assertEqual(runner.execute(plugin, {'kind': 'command'}, root), result)
                popen.assert_called_once()
                job.return_value.attach.assert_called_once_with(process)
                kwargs = popen.call_args.kwargs
                if platform == 'win32':
                    self.assertEqual(kwargs['creationflags'], 0x08000000)
                    self.assertNotIn('start_new_session', kwargs)
                else:
                    self.assertTrue(kwargs['start_new_session'])
                    self.assertEqual(kwargs.get('creationflags', 0), 0)


class MixedHookPreservationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.profile = self.root/'profile'; self.profile.mkdir()
        self.data = self.root/'data'; self.data.mkdir()
        for context in (
            patch.dict(os.environ, CODEX_HOME=str(self.profile), CODEX_TELEGRAM_DATA_DIR=str(self.data)),
            patch.object(desktop, 'discover_desktop', return_value=Mock()),
            patch.object(services, 'manage', return_value=0),
        ):
            context.start(); self.addCleanup(context.stop)
        self.hooks = self.profile/'hooks.json'
        self.foreign = {'type': 'command', 'command': 'other-notifier', 'timeout': 7}
        self.mcp = {'type': 'mcp_tool', 'server': 'audit', 'tool': 'log'}

    def group(self, command):
        return {'matcher': '^request_user_input$', 'custom': 'keep metadata',
                'hooks': [self.foreign, {'type': 'command', 'command': command}, self.mcp]}

    def test_desktop_install_reinstall_preserves_mixed_group_handlers(self):
        command = shell_command(['python', '-m', 'codex_telegram_bridge', '--data-dir', str(self.data), 'notify'])
        group = self.group(command)
        other_event = [{'hooks': [self.foreign]}]
        self.hooks.write_text(json.dumps({'custom': True, 'hooks': {
            'PreToolUse': [group], 'Stop': other_event}}))
        expected = {**group, 'hooks': [self.foreign, self.mcp]}
        for _ in range(2):
            with redirect_stdout(io.StringIO()): install.install(service=False)
            value = json.loads(self.hooks.read_text())
            self.assertEqual(value['hooks']['PreToolUse'][0], expected)
            self.assertEqual(len(value['hooks']['PreToolUse']), 2)
            self.assertEqual(value['hooks']['Stop'], other_event)
            self.assertTrue(value['custom'])

    def test_desktop_uninstall_removes_only_owned_handler_from_mixed_group(self):
        with redirect_stdout(io.StringIO()): install.install(service=False)
        value = json.loads(self.hooks.read_text())
        owned = value['hooks']['PreToolUse'][0]
        owned['hooks'].extend([self.foreign, self.mcp]); owned['custom'] = 'keep'
        self.hooks.write_text(json.dumps(value))
        with redirect_stdout(io.StringIO()): install.uninstall()
        expected = {**owned, 'hooks': [self.foreign, self.mcp]}
        self.assertEqual(json.loads(self.hooks.read_text())['hooks']['PreToolUse'], [expected])

    def test_app_server_registration_reinstall_and_removal_keep_mixed_groups(self):
        original = {event: [self.group(desktop_hook.command())]
                    for event in ('Stop', 'SubagentStop', 'PreToolUse')}
        self.hooks.write_text(json.dumps({'hooks': original}))
        for _ in range(2):
            desktop_hook.register()
            value = json.loads(self.hooks.read_text())['hooks']
            for event, groups in original.items():
                self.assertEqual(value[event][0], {**groups[0], 'hooks': [self.foreign, self.mcp]})
                self.assertEqual(len(value[event]), 2)
        desktop_hook.register(remove=True)
        value = json.loads(self.hooks.read_text())['hooks']
        for event, groups in original.items():
            self.assertEqual(value[event], [{**groups[0], 'hooks': [self.foreign, self.mcp]}])

    def test_app_server_removal_respects_previous_command_and_other_profile(self):
        previous = 'old-python -m codex_telegram_bridge --data-dir old-data desktop-notify'
        other = {'type': 'command', 'command': 'python -m codex_telegram_bridge --data-dir other-data desktop-notify'}
        group = self.group(previous); group['hooks'].append(other)
        self.hooks.write_text(json.dumps({'hooks': {'Stop': [group]}}))
        (self.data/'desktop-hook-install.json').write_text(json.dumps({'command': previous}))
        desktop_hook.register(remove=True)
        expected = {**group, 'hooks': [self.foreign, self.mcp, other]}
        self.assertEqual(json.loads(self.hooks.read_text())['hooks']['Stop'], [expected])

    def test_desktop_ownership_requires_exact_entrypoint_and_data_directory(self):
        foreign = [
            f'python -m codex_telegram_bridge --data-dir {self.data}-other notify',
            f'python audit.py --label codex_telegram_bridge --path {self.data}',
            f'python -m other_module --data-dir {self.data} notify',
            f'python -m codex_telegram_bridge --codex-home {self.profile}-other --data-dir {self.data} notify',
            f'python -m codex_telegram_bridge --data-dir {self.data} setup',
            f'python -m codex_telegram_bridge --data-dir {self.data} --data-dir {self.data} notify',
        ]
        own = shell_command(['/older Python/python3.13', '-m', 'codex_telegram_bridge',
                             '--codex-home', str(self.profile), '--data-dir', str(self.data), 'notify'])
        groups = [{'matcher': 'keep', 'hooks': [{'type': 'command', 'command': c} for c in foreign+[own]]}]
        self.hooks.write_text(json.dumps({'hooks': {'PreToolUse': groups}}))
        with redirect_stdout(io.StringIO()): install.install(service=False)
        remaining = json.loads(self.hooks.read_text())['hooks']['PreToolUse'][0]['hooks']
        self.assertEqual([h['command'] for h in remaining], foreign)
        with redirect_stdout(io.StringIO()): install.uninstall()
        remaining = json.loads(self.hooks.read_text())['hooks']['PreToolUse'][0]['hooks']
        self.assertEqual([h['command'] for h in remaining], foreign)

    def test_legacy_script_path_and_python_upgrade_are_owned_exactly(self):
        script = shell_command(['/old Python/pythonw.exe', str(self.data/'telegram_notify.py')])
        self.assertTrue(install._owned_command(script))
        self.assertFalse(install._owned_command(script+' --setup'))
        self.assertFalse(install._owned_command(script.replace('telegram_notify.py', 'telegram_notify.py.bak')))

    def test_windows_argument_parser_preserves_backslashes_and_spaces(self):
        args = [r'C:\Program Files\Python\pythonw.exe', '-m', 'codex_telegram_bridge',
                '--codex-home', r'C:\User Data\codex', '--data-dir', r'C:\User Data\bridge', 'notify']
        self.assertEqual(direct_command_arguments(subprocess.list2cmdline(args), windows=True), args)

    def test_recorded_previous_python_command_is_still_owned_after_upgrade(self):
        args = ['/previous-runtime/custom-python', '-m', 'codex_telegram_bridge',
                '--data-dir', str(self.data), 'notify']
        (self.data/'bridge-install.json').write_text(json.dumps({'installed_notify': args}))
        self.assertTrue(install._owned_command(shell_command(args)))
        self.assertFalse(install._owned_command(shell_command([*args[:-1], 'setup'])))


if __name__ == '__main__':
    unittest.main()
