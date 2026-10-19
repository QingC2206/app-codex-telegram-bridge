"""Native transport/lifetime tests plus fail-closed platform discovery fixtures."""
import json
import os
from pathlib import Path
import plistlib
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from codex_telegram_bridge import paths, config
from codex_telegram_bridge.platforms import discovery
from codex_telegram_bridge.platforms.locking import SingleInstance
from codex_telegram_bridge.platforms.posix import discovery as posix
from codex_telegram_bridge.platforms.posix.transport import UnixTransport, validate_peer
from codex_telegram_bridge.codex.errors import DesktopBridgeSafetyError, DesktopIpcError


def asar(resources, name='openai-codex-electron'):
    resources.mkdir(parents=True,exist_ok=True)
    content=json.dumps({'name':name}).encode()
    header=json.dumps({'files':{'package.json':{'size':len(content),'offset':'0'}}}).encode()
    (resources/'app.asar').write_bytes(struct.pack('<4I',4,len(header)+8,len(header)+4,len(header))+header+content)


class PathAndCredentialsTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.root=Path(temporary.name).resolve()
        env=patch.dict(os.environ,{'CODEX_HOME':str(self.root),'CODEX_TELEGRAM_DATA_DIR':''})
        env.start();self.addCleanup(env.stop)

    def test_new_data_is_separate_from_package(self):
        self.assertEqual(paths.data_dir(),self.root/'telegram-bridge')

    def test_legacy_routes_and_credentials_reused_without_copying(self):
        legacy=self.root/'hooks';legacy.mkdir();(legacy/'telegram_notify.credentials.json').write_text('{}')
        self.assertEqual(paths.data_dir(),legacy)

    def test_ambiguous_legacy_installations_require_explicit_selection(self):
        for folder in ['hooks','hooks/codex-telegram-bot']:
            p=self.root/folder;p.mkdir(parents=True,exist_ok=True);(p/'telegram_notify.credentials.json').write_text('{}')
        with self.assertRaisesRegex(RuntimeError,'Multiple'):paths.data_dir()
        with patch.dict(os.environ,CODEX_TELEGRAM_DATA_DIR=str(self.root/'chosen')):
            self.assertEqual(paths.data_dir(),self.root/'chosen')

    def test_keyring_config_contains_only_reference(self):
        backend=Mock()
        with patch.object(config.sys,'platform','linux'),patch.object(config,'_keyring',return_value=backend):
            # Avoid constructing Path while os.name is mocked on Windows.
            value=config.store_token('unit-test-token',self.root)
        self.assertNotIn('unit-test-token',json.dumps(value))
        backend.set_password.assert_called_once()

    def test_no_plaintext_fallback_on_keyring_error(self):
        with patch.object(config,'_keyring',side_effect=RuntimeError('locked')):
            with self.assertRaisesRegex(RuntimeError,'locked'):
                config.load_token({'token_keyring':'a'*64})

    def test_profile_lock_excludes_another_instance_and_releases(self):
        a,b=SingleInstance('same'),SingleInstance('same')
        try:
            self.assertTrue(a.acquire());self.assertFalse(b.acquire())
            a.close();self.assertTrue(b.acquire())
        finally:a.close();b.close()


class PosixDiscoveryTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.root=Path(temporary.name).resolve()

    def test_linux_desktop_package_is_required_not_cli_basename(self):
        executable=self.root/'app/codex';executable.parent.mkdir();executable.write_bytes(b'fixture')
        with patch.object(posix.sys,'platform','linux'):
            self.assertIsNone(posix.desktop_resources(executable))
            asar(executable.parent/'resources')
            self.assertEqual(posix.desktop_resources(executable),executable.parent/'resources')
            asar(executable.parent/'resources','vscode')
            self.assertIsNone(posix.desktop_resources(executable))

    def test_mac_bundle_identity_and_electron_metadata_both_required(self):
        contents=self.root/'Codex.app/Contents';exe=contents/'MacOS/Codex';exe.parent.mkdir(parents=True);exe.touch()
        asar(contents/'Resources')
        info={'CFBundleIdentifier':'com.openai.codex','CFBundleExecutable':'Codex'}
        (contents/'Info.plist').write_bytes(plistlib.dumps(info))
        with patch.object(posix.sys,'platform','darwin'):
            self.assertEqual(posix.desktop_resources(exe),contents/'Resources')
            info['CFBundleIdentifier']='com.microsoft.VSCode'
            (contents/'Info.plist').write_bytes(plistlib.dumps(info))
            self.assertIsNone(posix.desktop_resources(exe))

    def test_asar_header_is_bounded(self):
        path=self.root/'app.asar';path.write_bytes(struct.pack('<4I',4,2**31,0,2**31))
        self.assertEqual(posix._asar_name(path),'')

    def test_unpaired_cli_override_rejected_before_returning_runtime(self):
        from codex_telegram_bridge.codex import desktop
        process=Mock();process.exe.return_value=str(self.root/'app/codex')
        with patch.object(desktop,'discover_desktop',return_value=Mock(pid=123)), \
             patch.object(posix.psutil,'Process',return_value=process), \
             patch.object(posix,'desktop_resources',return_value=self.root/'resources'), \
             patch.dict(os.environ,CODEX_CLI_PATH=str(self.root/'.vscode/codex')):
            with self.assertRaises(DesktopBridgeSafetyError):posix.locate_runtime(self.root)

    @unittest.skipIf(os.name=='nt','Native POSIX endpoint paths')
    def test_native_endpoint_grammar_rejects_traversal_and_wrong_service(self):
        self.assertTrue(discovery.valid_native_endpoint('/tmp/codex-app-tools/test-uuid.sock'))
        for value in ['/tmp/codex-app-tools/../evil.sock','/tmp/other/test.sock','/tmp/codex-app-tools/a\n.sock','relative.sock']:
            self.assertFalse(discovery.valid_native_endpoint(value),value)


@unittest.skipIf(os.name=='nt','Unix sockets and OS peer credentials')
class NativeUnixTransportTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='cb-',dir='/tmp');self.addCleanup(temporary.cleanup)
        self.path=Path(temporary.name)/'test.sock'
        self.listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.listener.bind(str(self.path));self.path.chmod(0o600)
        self.listener.listen();self.listener.settimeout(3);self.addCleanup(self.listener.close)
        self.transport=UnixTransport(timeout=2);self.transport.PIPE_PATH=str(self.path);self.transport.expected_server_pid=os.getpid()

    def test_real_peer_and_fragmented_coalesced_frames(self):
        def server():
            handle,_=self.listener.accept()
            with handle:
                raw=UnixTransport.encode_frame({'a':1})+UnixTransport.encode_frame({'b':'中文'})
                handle.sendall(raw[:2]);time.sleep(.01);handle.sendall(raw[2:])
        worker=threading.Thread(target=server);worker.start()
        handle=self.transport._open()
        try:
            self.assertEqual(self.transport._next_message(handle,time.monotonic()+2),{'a':1})
            self.assertEqual(self.transport._next_message(handle,time.monotonic()+2),{'b':'中文'})
        finally:handle.close();worker.join(3)

    def test_wrong_server_pid_rejected_before_any_write(self):
        self.transport.expected_server_pid=os.getpid()+100000
        with self.assertRaises(DesktopBridgeSafetyError):self.transport._open()

    def test_group_writable_socket_rejected(self):
        self.path.chmod(0o620)
        with self.assertRaises(DesktopBridgeSafetyError):self.transport._open()

    def test_symlink_socket_rejected(self):
        link=self.path.parent/'link.sock';link.symlink_to(self.path);self.transport.PIPE_PATH=str(link)
        with self.assertRaises(DesktopBridgeSafetyError):self.transport._open()

    def test_invalid_frame_is_not_interpreted_as_json(self):
        self.transport._buffer.extend(struct.pack('<I',0))
        with self.assertRaises(DesktopIpcError):self.transport._next_message(Mock(),time.monotonic()+1)

    def test_abrupt_parent_exit_reaps_owned_child(self):
        script="""import os,subprocess,sys,time
from codex_telegram_bridge.platforms.processes import ChildProcessJob,spawn_options
job=ChildProcessJob()
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],**spawn_options())
job.attach(child)
print(child.pid,flush=True)
time.sleep(.5)
os._exit(0)
"""
        result=subprocess.run([sys.executable,'-c',script],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        import psutil
        process=psutil.Process(int(result.stdout.strip())) if psutil.pid_exists(int(result.stdout.strip())) else None
        deadline=time.monotonic()+5
        while process and process.is_running() and process.status()!=psutil.STATUS_ZOMBIE and time.monotonic()<deadline:time.sleep(.1)
        if process and process.is_running() and process.status()!=psutil.STATUS_ZOMBIE:
            process.kill();self.fail('Owned child survived its parent')
