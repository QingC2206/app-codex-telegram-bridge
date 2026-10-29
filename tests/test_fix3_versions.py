"""One authored version across source, distribution and diagnostic outputs."""
import ast
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

import tomlkit

from codex_telegram_bridge import __version__
from codex_telegram_bridge.management import doctor as desktop
from codex_telegram_bridge.backends.app_server import runtime


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_INIT = 'src/codex_telegram_bridge/__init__.py'
spec = importlib.util.spec_from_file_location('version_release', ROOT/'tools/build_release.py')
release = importlib.util.module_from_spec(spec)
with patch.object(sys, 'path', [str(ROOT/'tools'), *sys.path]):
    spec.loader.exec_module(release)
installer_spec = importlib.util.spec_from_file_location('version_installer', ROOT/'install.py')
installer = importlib.util.module_from_spec(installer_spec)
installer_spec.loader.exec_module(installer)


class VersionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_build_configuration_has_no_independent_version_input(self):
        config = tomlkit.parse((ROOT/'pyproject.toml').read_text())
        self.assertNotIn('version', config['project'])
        self.assertIn('version', config['project']['dynamic'])
        self.assertEqual(config['tool']['setuptools']['dynamic']['version'],
                         {'attr': 'codex_telegram_bridge.__version__'})
        self.assertNotIn('VERSION', release.source_files())
        self.assertNotIn('VERSION', (ROOT/'MANIFEST.in').read_text().split())
        tree = ast.parse((ROOT/PACKAGE_INIT).read_bytes())
        definitions = [node for node in tree.body if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Name) and target.id == '__version__' for target in node.targets)]
        self.assertEqual(len(definitions), 1)
        self.assertEqual(release.source_version({PACKAGE_INIT: (ROOT/PACKAGE_INIT).read_bytes()}), __version__)

    def test_source_archive_uses_captured_literal_and_replaces_stale_compatibility_file(self):
        # A build must not execute the source module or consult installed metadata.
        source = b'__version__ = "7.8.9"\nraise RuntimeError("must not execute")\n'
        content = {PACKAGE_INIT: source, 'VERSION': b'1.0.0\n', 'install.py': b'# fixture\n'}
        with patch('importlib.metadata.version', side_effect=AssertionError('installed version is irrelevant')):
            archive = release.write_source_archive(content, self.root/'dist')
        self.assertEqual(archive.name, 'codex-telegram-bridge-v7.8.9.zip')
        with zipfile.ZipFile(archive) as zipped:
            self.assertEqual(zipped.read('codex-telegram-bridge-v7.8.9/VERSION'), b'7.8.9\n')
            self.assertEqual(zipped.read('codex-telegram-bridge-v7.8.9/'+PACKAGE_INIT), source)
            self.assertEqual(sum(name.endswith('/VERSION') for name in zipped.namelist()), 1)
        self.assertEqual(content['VERSION'], b'1.0.0\n')
        self.assertFalse((self.root/'VERSION').exists())

    def test_invalid_or_ambiguous_version_stops_before_creating_archive(self):
        for source in [b'', b'__version__ = "1.2.3"\n__version__ = "2.3.4"',
                       b'__version__ = calculate_version()', b'__version__ = 123',
                       b'__version__ = "v1.2.3"', b'__version__ = "../other"']:
            with self.subTest(source=source), self.assertRaises((ValueError, TypeError)):
                release.write_source_archive({PACKAGE_INIT: source}, self.root/'dist')
            self.assertFalse((self.root/'dist').exists())

    def test_source_import_is_not_replaced_by_an_older_installed_distribution(self):
        checkout = self.root/'checkout'
        source_init = checkout/PACKAGE_INIT
        source_init.parent.mkdir(parents=True)
        source_init.write_bytes((ROOT/PACKAGE_INIT).read_bytes())
        old_site = self.root/'old-site'; old_site.mkdir()
        metadata = old_site/'app_codex_telegram_bridge-0.0.1.dist-info'; metadata.mkdir()
        (metadata/'METADATA').write_text('Metadata-Version: 2.1\nName: app-codex-telegram-bridge\nVersion: 0.0.1\n')
        old_package = old_site/'codex_telegram_bridge'; old_package.mkdir()
        (old_package/'__init__.py').write_text('__version__ = "0.0.1"\n')
        script = ('import json,importlib.metadata,codex_telegram_bridge as package; '
                  'print(json.dumps([package.__version__,package.__file__,'
                  'importlib.metadata.version("app-codex-telegram-bridge")]))')
        result = subprocess.run([sys.executable, '-c', script], cwd=self.root, capture_output=True,
                                text=True, check=True, env={**os.environ, 'PYTHONDONTWRITEBYTECODE':'1',
                                'PYTHONPATH':str(checkout/'src')+os.pathsep+str(old_site)})
        version, source, installed = json.loads(result.stdout)
        self.assertEqual(installed, '0.0.1')
        self.assertEqual(version, __version__)
        self.assertEqual(Path(source).resolve(), source_init.resolve())

    def test_both_doctors_share_explicit_versions_and_preserve_legacy_fields(self):
        output = io.StringIO()
        with patch.object(desktop.importlib.metadata, 'version', return_value='dependency-fixture'), \
             patch.object(desktop, 'codex_home', return_value=self.root), \
             patch.object(desktop, 'data_dir', return_value=self.root), redirect_stdout(output):
            self.assertEqual(desktop.doctor(offline=True), 0)
        first = json.loads(output.getvalue())
        output = io.StringIO()
        with patch.object(runtime, 'AppServerClient', side_effect=AssertionError('offline')), \
             patch.object(runtime, 'credentials', side_effect=AssertionError('offline')), redirect_stdout(output):
            self.assertEqual(runtime.doctor({'codex_executable':sys.executable, 'cwd':str(self.root)}, offline=True), 0)
        second = json.loads(output.getvalue())
        for report in (first, second):
            self.assertEqual(report['bridge_version'], __version__)
            self.assertEqual(report['python_version'], sys.version.split()[0])
        self.assertEqual(first['version'], __version__)
        self.assertEqual(first['python'], first['python_version'])
        self.assertEqual(second['version'], second['python_version'])
        self.assertEqual(second['python'], sys.executable)

    def test_release_gate_failure_never_writes_an_archive(self):
        init = self.root/PACKAGE_INIT; init.parent.mkdir(parents=True)
        init.write_text('__version__ = "1.2.3"\n')
        def reject(command, **kwargs):
            Path(command[command.index('--report')+1]).write_text(json.dumps({'success': False}))
        with patch.object(release, 'ROOT', self.root), \
             patch.object(release, 'source_files', return_value=[PACKAGE_INIT]), \
             patch.object(release, 'audit'), patch.object(release.subprocess, 'run', side_effect=reject) as probe, \
             patch.object(release, 'write_source_archive') as archive, \
             patch.object(sys, 'argv', ['build_release.py', '--thread-id', 'fixture-thread']):
            with self.assertRaisesRegex(RuntimeError, 'Incomplete delivery evidence'):
                release.main()
        probe.assert_called_once(); archive.assert_not_called()

    def test_release_main_derives_archive_and_compatibility_file_after_acceptance(self):
        init = self.root/PACKAGE_INIT; init.parent.mkdir(parents=True)
        init.write_text('__version__ = "1.2.3"\n')
        (self.root/'VERSION').write_text('9.9.9\n')
        def accept(command, **kwargs):
            if '--report' in command:
                Path(command[command.index('--report')+1]).write_text(json.dumps({
                    'success':True, 'desktop_attempts':1, 'desktop_route':'desktop-user-input',
                    'receipt_origin':'user', 'thread_id':'fixture-thread',
                    'checks':{'desktop_delivery':'passed','telegram_delivery':'passed'}}))
        with patch.object(release, 'ROOT', self.root), \
             patch.object(release, 'source_files', return_value=[PACKAGE_INIT]), \
             patch.object(release, 'audit'), patch.object(release.subprocess, 'run', side_effect=accept) as commands, \
             patch.object(sys, 'argv', ['build_release.py', '--thread-id', 'fixture-thread']), redirect_stdout(io.StringIO()):
            release.main()
        self.assertEqual(commands.call_count, 2)
        self.assertIn('--send-probe', commands.call_args_list[0].args[0])
        self.assertEqual(commands.call_args_list[1].args[0][-2:], ['--suite', 'Full'])
        archive = self.root/'dist/codex-telegram-bridge-v1.2.3.zip'
        with zipfile.ZipFile(archive) as zipped:
            self.assertEqual(zipped.read('codex-telegram-bridge-v1.2.3/VERSION'), b'1.2.3\n')
        self.assertTrue(archive.with_suffix('.zip.sha256').is_file())
        self.assertTrue(archive.with_suffix('.validation.json').is_file())

    def test_windows_source_installer_uses_pip_root_without_reading_version_file(self):
        returns = iter([3, 0, 3])  # No old install, successful pip, no credentials.
        with patch.object(installer, '__file__', str(self.root/'install.py')), \
             patch.object(installer, 'os', SimpleNamespace(name='nt', environ={})), \
             patch.object(installer.venv, 'EnvBuilder'), \
             patch.object(installer.subprocess, 'run', side_effect=lambda *a, **k: SimpleNamespace(returncode=next(returns))) as run, \
             patch.object(installer.subprocess, 'call', return_value=0) as call, \
             patch.object(sys, 'argv', ['install.py', '--codex-home', str(self.root/'profile'), '--skip-setup', '--no-service']):
            self.assertEqual(installer.main(), 0)
        python = str(self.root/'profile/telegram-bridge-runtime/Scripts/python.exe')
        self.assertEqual(run.call_args_list[1].args[0], [python, '-m', 'pip', 'install', '--upgrade', str(self.root)])
        self.assertEqual(call.call_args.args[0], [python, '-m', 'codex_telegram_bridge', 'install', '--no-service'])
        self.assertFalse((self.root/'VERSION').exists())


if __name__ == '__main__':
    unittest.main()
