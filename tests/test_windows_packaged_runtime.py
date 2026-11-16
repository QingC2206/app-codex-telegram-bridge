"""Desktop package identity, upgrade hints and Windows runtime ownership."""
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from codex_telegram_bridge.platforms.windows import runtime


class PackagedRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.profile = self.root / 'profile'
        self.profile.mkdir()
        self.cache = self.root / 'OpenAI/Codex/bin'
        self.packages = {}
        self.bundle = self.package('26.1111.2228.0')
        self.legacy = self.cache / 'old-build/codex.exe'
        self.legacy.parent.mkdir(parents=True)
        self.legacy.write_bytes(b'legacy Desktop fixture')
        for context in (
            patch.object(runtime, 'os', SimpleNamespace(name='nt', environ=os.environ)),
            patch.dict(os.environ, {'LOCALAPPDATA': str(self.root), 'CODEX_CLI_PATH': ''}),
            patch.object(runtime, 'codex_home', return_value=self.profile),
            patch.object(runtime, '_registered_package_path', side_effect=self.packages.get),
        ):
            context.start()
            self.addCleanup(context.stop)

    def package(self, version):
        directory = self.root / 'WindowsApps' / ('OpenAI.Codex_' + version + '_x64__2p2nqsd0c76g0')
        backend = directory / 'app/resources/codex.exe'
        backend.parent.mkdir(parents=True)
        backend.write_bytes(b'packaged Desktop fixture')
        (directory / 'app/ChatGPT.exe').write_bytes(b'Desktop app fixture')
        self.packages[directory.name] = directory
        return backend

    def process(self, pid, path, parent=0):
        return Mock(info={'pid': pid, 'ppid': parent, 'name': path.name, 'exe': str(path)})

    def app(self, backend):
        return backend.parent.parent / 'ChatGPT.exe'

    def discover(self, processes):
        with patch('psutil.process_iter', return_value=processes):
            return runtime._running_desktop_codex(self.cache)

    def test_registered_bundle_in_config_is_accepted(self):
        import tomlkit
        (self.profile / 'config.toml').write_text(tomlkit.dumps(
            {'mcp_servers': {'node_repl': {'env': {'CODEX_CLI_PATH': str(self.bundle)}}}}), encoding='utf-8')
        self.assertEqual(runtime.locate_codex(discover=lambda root: []), self.bundle)

    def test_environment_hint_uses_registered_bundle_not_newer_cache(self):
        os.utime(self.legacy, (2000000000, 2000000000))
        with patch.dict(os.environ, CODEX_CLI_PATH=str(self.bundle)):
            self.assertEqual(runtime.locate_codex(discover=lambda root: []), self.bundle)

    def test_unregistered_and_lookalike_package_paths_are_rejected(self):
        root = self.bundle.parent.parent.parent
        with patch.object(runtime, '_registered_package_path', return_value=None):
            self.assertFalse(runtime._desktop_codex_path(self.bundle, self.cache))
        copied = self.root / 'copied/WindowsApps' / root.name / 'app/resources/codex.exe'
        copied.parent.mkdir(parents=True)
        copied.write_bytes(b'not the registered package')
        self.assertFalse(runtime._desktop_codex_path(copied, self.cache))
        for path in (root / 'app/other/codex.exe', root / 'app/resources/other.exe',
                     self.root / '.vscode/codex.exe', self.root / 'cli/codex.exe'):
            with self.subTest(path=path):
                self.assertFalse(runtime._desktop_codex_path(path, self.cache))

    def test_other_publisher_is_rejected_even_if_registered(self):
        root = self.root / 'WindowsApps/OpenAI.Codex_26.1111.2228.0_x64__otherpublisher'
        self.packages[root.name] = root
        self.assertFalse(runtime._desktop_codex_path(root / 'app/resources/codex.exe', self.cache))

    def test_foreign_override_is_rejected_with_a_valid_running_desktop(self):
        with patch.dict(os.environ, CODEX_CLI_PATH=str(self.root / '.vscode/codex.exe')):
            with self.assertRaisesRegex(ValueError, '禁止 VS Code'):
                runtime.locate_codex(discover=lambda root: [self.bundle])

    def test_running_bundle_is_selected_and_bot_or_vscode_children_are_excluded(self):
        processes = [self.process(10, self.app(self.bundle)), self.process(11, self.bundle, 10),
                     self.process(12, self.legacy, 99), self.process(13, self.root / '.vscode/codex.exe', 10)]
        self.assertEqual(self.discover(processes), [self.bundle])
        self.assertEqual(self.discover(processes[2:]), [])

    def test_desktop_without_loaded_backend_uses_its_verified_bundle(self):
        with patch('psutil.process_iter', return_value=[self.process(10, self.app(self.bundle))]):
            self.assertEqual(runtime.locate_codex(), self.bundle)

    def test_running_legacy_child_precedes_its_apps_unused_bundle(self):
        self.assertEqual(self.discover([self.process(10, self.app(self.bundle)),
                                       self.process(11, self.legacy, 10)]), [self.legacy])

    def test_other_packages_child_is_not_paired_to_this_desktop(self):
        other = self.package('26.1112.1000.0')
        self.assertEqual(self.discover([self.process(10, self.app(self.bundle)),
                                       self.process(11, other, 10)]), [self.bundle])

    def test_multiple_active_package_versions_fail_closed(self):
        other = self.package('26.1112.1000.0')
        processes = [self.process(10, self.app(self.bundle)), self.process(20, self.app(other))]
        with patch('psutil.process_iter', return_value=processes):
            with self.assertRaisesRegex(RuntimeError, '多个不同版本'):
                runtime.locate_codex()

    def test_missing_retired_hint_only_yields_to_verified_active_package(self):
        old = self.package('26.1110.1000.0')
        del self.packages[old.parent.parent.parent.name]
        old.unlink()
        with patch.dict(os.environ, CODEX_CLI_PATH=str(old)):
            self.assertEqual(runtime.locate_codex(discover=lambda root: [self.bundle]), self.bundle)
            with self.assertRaises(ValueError):
                runtime.locate_codex(discover=lambda root: [])
            with self.assertRaises(ValueError):
                runtime.locate_codex(discover=lambda root: [self.legacy])

    def test_existing_unregistered_hint_is_not_silently_ignored(self):
        old = self.package('26.1110.1000.0')
        del self.packages[old.parent.parent.parent.name]
        with patch.dict(os.environ, CODEX_CLI_PATH=str(old)):
            with self.assertRaises(ValueError):
                runtime.locate_codex(discover=lambda root: [self.bundle])

    def test_retired_hint_in_another_location_is_rejected(self):
        old = self.root / 'untrusted/WindowsApps/OpenAI.Codex_26.1110.1000.0_x64__2p2nqsd0c76g0/app/resources/codex.exe'
        with patch.dict(os.environ, CODEX_CLI_PATH=str(old)):
            with self.assertRaises(ValueError):
                runtime.locate_codex(discover=lambda root: [self.bundle])


class NativePackageLookupTests(unittest.TestCase):
    def test_failed_or_unbounded_package_lookup_returns_no_trusted_path(self):
        from ctypes import wintypes
        import ctypes
        def call(result, size_value):
            def query(name, size, buffer):
                ctypes.cast(size, ctypes.POINTER(wintypes.UINT)).contents.value = size_value
                return result
            return Mock(side_effect=query)
        with patch.object(runtime, 'os', SimpleNamespace(name='nt')):
            for query in (call(15700, 0), call(122, 0), call(122, 1000000)):
                with self.subTest(query=query), patch.object(runtime.ctypes, 'WinDLL',
                        return_value=SimpleNamespace(GetPackagePathByFullName=query), create=True):
                    self.assertIsNone(runtime._registered_package_path('missing'))
            with patch.object(runtime.ctypes, 'WinDLL', side_effect=OSError('unavailable'), create=True):
                self.assertIsNone(runtime._registered_package_path('missing'))


if __name__ == '__main__':
    unittest.main()
