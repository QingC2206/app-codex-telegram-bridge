"""Specific offline evidence for platform and history audit rules."""
import base64
import ctypes
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from codex_telegram_bridge.codex.history import read_thread_history, question_answers
from codex_telegram_bridge.codex import receipts
from codex_telegram_bridge.platforms.desktop_access import desktop_access, NativeDesktopAccess
from codex_telegram_bridge.platforms.macos.desktop_access import MacOSDesktopAccess
from codex_telegram_bridge.platforms.windows.desktop_access import WindowsDesktopAccess
from codex_telegram_bridge.platforms.windows import credentials
from codex_telegram_bridge.platforms.macos import capture as mac
from codex_telegram_bridge.platforms.linux import capture as linux
from codex_telegram_bridge.platforms.posix.capture import image_result


class PlatformRuleTests(unittest.TestCase):
    def test_windows_inventory_filters_store_main_process_and_helpers(self):
        from codex_telegram_bridge.platforms.windows import discovery
        import psutil
        with tempfile.TemporaryDirectory() as folder:
            exe=Path(folder)/'WindowsApps/OpenAI.Codex_fixture/app/ChatGPT.exe'
            records=[Mock(info={'pid':1,'exe':str(exe),'cmdline':[str(exe)]}),
                     Mock(info={'pid':2,'exe':str(exe),'cmdline':[str(exe),'--type=renderer']}),
                     Mock(info={'pid':3,'exe':str(Path(folder)/'vscode/ChatGPT.exe'),'cmdline':[]})]
            with patch.object(psutil,'process_iter',return_value=records):
                self.assertEqual(discovery.desktop_roots(),[{'ProcessId':1,'ExecutablePath':str(exe.resolve())}])

    def test_windows_pipe_server_pid_api_mismatch_and_denial_fail_closed(self):
        from codex_telegram_bridge.platforms.windows import discovery
        from codex_telegram_bridge.codex.errors import DesktopBridgeSafetyError
        kernel=Mock()
        def pid(handle,output):output._obj.value=44;return True
        kernel.GetNamedPipeServerProcessId.side_effect=pid
        with patch.object(discovery,'os',SimpleNamespace(name='nt')), \
             patch.object(ctypes,'WinDLL',return_value=kernel,create=True):
            discovery.validate_pipe_server(55,44)
            with self.assertRaises(DesktopBridgeSafetyError):discovery.validate_pipe_server(55,66)
            kernel.GetNamedPipeServerProcessId.side_effect=None
            kernel.GetNamedPipeServerProcessId.return_value=False
            with self.assertRaises(DesktopBridgeSafetyError):discovery.validate_pipe_server(55,44)

    def test_windows_job_uses_kill_on_close_and_releases_exact_handle(self):
        from codex_telegram_bridge.platforms.windows import processes
        kernel=Mock();kernel.CreateJobObjectW.return_value=51
        observed=[]
        def configure(handle,kind,limits,size):
            observed.append((handle,kind,limits._obj.BasicLimitInformation.LimitFlags));return True
        kernel.SetInformationJobObject.side_effect=configure
        with patch.object(processes,'os',SimpleNamespace(name='nt')), \
             patch.object(ctypes,'WinDLL',return_value=kernel,create=True):
            job=processes.ChildProcessJob();job.attach(SimpleNamespace(_handle=61));job.close();job.close()
        self.assertEqual(observed,[(51,9,0x2000)])
        kernel.AssignProcessToJobObject.assert_called_once_with(51,61)
        kernel.CloseHandle.assert_called_once_with(51)

    def test_interrupt_pins_original_turn_and_requires_matching_receipt(self):
        from test_desktop_bridge import FakeTransport
        from codex_telegram_bridge.codex import desktop as d
        reader, owner=FakeTransport(),FakeTransport()
        discover=Mock(return_value=d.DesktopEndpoint(44,r'\\.\pipe\codex-app-tools-test'))
        control=d.DesktopControl(FakeTransport,access=WindowsDesktopAccess())
        control.native=d.DesktopAppTools(lambda **kw:reader,discover=discover)
        control.transport_factory=lambda **kw:owner
        with patch.object(d,'validate_pipe_server'):
            owner.result_turn_id='old-turn'
            self.assertTrue(control.interrupt_turn('task','old-turn'))
            frames=[r for r in owner.requests if r['method']=='thread-follower-interrupt-turn']
            self.assertEqual(len(frames),1)
            self.assertEqual(frames[0]['params']['expectedTurnId'],'old-turn')
            owner.requests.clear();owner.result_turn_id='different-turn'
            with self.assertRaises(d.DesktopSubmissionUnknown):control.interrupt_turn('task','old-turn')
            self.assertEqual(len([r for r in owner.requests if r['method']=='thread-follower-interrupt-turn']),1)
            owner.requests.clear()
            with self.assertRaises(d.DesktopIpcInactive):control.interrupt_turn('task','ended-turn')
            self.assertFalse(owner.requests)

    def test_adapter_selection_and_platform_specific_recovery_policy(self):
        self.assertIsInstance(desktop_access('darwin'), MacOSDesktopAccess)
        self.assertIsInstance(desktop_access('win32'), WindowsDesktopAccess)
        self.assertIs(type(desktop_access('linux')), NativeDesktopAccess)
        self.assertTrue(desktop_access('darwin').recover_discovery_unavailable)
        self.assertFalse(desktop_access('win32').recover_discovery_unavailable)

    def test_dpapi_calls_use_user_scope_and_free_output_buffers(self):
        # This proves flags/lifetime, not native Windows DPAPI encryption.
        calls=[]; buffers=[]
        def api(name):
            def run(*args):
                incoming=args[0]._obj
                calls.append((name, ctypes.string_at(incoming.pbData,incoming.cbData), args[5]))
                value=b'opaque-fixture' if name=='CryptProtectData' else b'unit-secret'
                data=ctypes.create_string_buffer(value);buffers.append(data)
                output=args[-1]._obj
                output.cbData=len(value);output.pbData=ctypes.cast(data,ctypes.POINTER(ctypes.c_ubyte))
                return True
            return run
        with patch.object(credentials,'_dpapi_function',side_effect=api), \
             patch.object(credentials,'_local_free') as release:
            encoded=credentials.protect_secret('unit-secret')
            self.assertEqual(encoded,base64.b64encode(b'opaque-fixture').decode())
            self.assertEqual(credentials.unprotect_secret(encoded),'unit-secret')
        self.assertEqual(calls,[('CryptProtectData',b'unit-secret',0),('CryptUnprotectData',b'opaque-fixture',0)])
        self.assertEqual(release.call_count,2)

    def test_macos_denied_screen_permission_never_launches_capture_or_requests_bypass(self):
        quartz=Mock();quartz.CGPreflightScreenCaptureAccess.return_value=False
        with patch.dict(sys.modules,{'Quartz':quartz}), patch.object(mac.subprocess,'run') as run, \
             patch.object(mac,'capture_rect') as capture:
            with self.assertRaisesRegex(RuntimeError,'Screen Recording'):
                mac.capture({'kind':'monitor','rect':[0,0,10,10]})
        run.assert_not_called();capture.assert_not_called()
        self.assertEqual([c[0] for c in quartz.mock_calls],['CGPreflightScreenCaptureAccess'])

    def test_macos_changed_window_or_display_rejected_before_capture(self):
        quartz=Mock();quartz.CGPreflightScreenCaptureAccess.return_value=True
        with patch.dict(sys.modules,{'Quartz':quartz}), patch.object(mac.subprocess,'run') as run, \
             patch.object(mac,'capture_rect') as capture, patch.object(mac,'monitors',return_value=[]), \
             patch.object(mac,'codex_windows',return_value=[{'id':1,'pid':99}]):
            for target in ({'kind':'monitor','rect':[0,0,10,10]}, {'kind':'codex','id':1,'pid':2}):
                with self.assertRaisesRegex(RuntimeError,'changed'):mac.capture(target)
        run.assert_not_called();capture.assert_not_called()

    def test_wayland_uses_portal_and_propagates_denial_without_image(self):
        async def denied():raise RuntimeError('cancelled by desktop')
        with patch.dict(os.environ,WAYLAND_DISPLAY='isolated-fixture'), \
             patch.object(linux,'_portal',side_effect=denied) as portal, \
             patch.object(linux,'capture_rect') as capture:
            self.assertEqual(linux.codex_windows(),[])
            self.assertEqual(linux.monitors(),[{'id':1,'portal':True}])
            with self.assertRaisesRegex(RuntimeError,'cancelled'):linux.capture({'kind':'monitor'})
        portal.assert_called_once();capture.assert_not_called()

    def test_x11_changed_targets_and_invalid_png_cannot_return_fake_images(self):
        with patch.dict(os.environ,WAYLAND_DISPLAY=''), patch.object(linux,'codex_windows',return_value=[]), \
             patch.object(linux,'monitors',return_value=[]), patch.object(linux,'capture_rect') as capture:
            for target in ({'kind':'codex','id':1,'pid':2},{'kind':'monitor','rect':[0,0,2,2]}):
                with self.assertRaisesRegex(RuntimeError,'changed'):linux.capture(target)
        capture.assert_not_called()
        for data in (b'',b'not an image',b'\x89PNG\r\n\x1a\n'+b'\0'*16):
            with self.assertRaises(ValueError):image_result(data)

    def test_legacy_and_paginated_history_use_only_read_methods(self):
        app=Mock()
        old={'thread':{'id':'task','turns':[{'id':'old','items':[]}]}}
        app.request.return_value=old
        self.assertEqual(read_thread_history(app,'task'),old)
        app.request.assert_called_once_with('thread/read',{'threadId':'task','includeTurns':False},timeout=30)
        app.reset_mock()
        app.request.side_effect=[{'thread':{'id':'task','historyMode':'paginated'}},
            {'data':[{'id':'turn'}]}, {'data':[]}]
        self.assertEqual(read_thread_history(app,'task')['thread']['turns'][0]['id'],'turn')
        self.assertEqual([c.args[0] for c in app.request.call_args_list],
                         ['thread/read','thread/turns/list','thread/items/list'])

    def test_question_answers_require_explicit_typed_user_envelope(self):
        body='<send_user_message_question_reply>[{"questionItemId":"q","answer":"A & B"}]</send_user_message_question_reply>'
        self.assertEqual(question_answers([{'type':'userMessage','content':[{'type':'text','text':body}]}]),{'q':'A & B'})
        self.assertEqual(question_answers([{'type':'agentMessage','content':[{'type':'text','text':body}]}]),{})
        self.assertEqual(question_answers([{'type':'userMessage','content':[{'type':'text','text':'prefix '+body}]}]),{})

    def test_receipt_path_pins_full_thread_id_and_rejects_outside_profile(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);profile=root/'profile';sessions=profile/'sessions';sessions.mkdir(parents=True)
            tid='11111111-1111-4111-8111-111111111111'
            outside=root/('rollout-test-'+tid+'.jsonl');outside.write_text('outside')
            wrong=sessions/('rollout-test-'+tid+'-other.jsonl');wrong.write_text('other')
            with sqlite3.connect(profile/'state_5.sqlite') as db:
                db.execute('CREATE TABLE threads(id TEXT,rollout_path TEXT)')
                db.execute('INSERT INTO threads VALUES(?,?)',(tid,str(outside)))
            self.assertIsNone(receipts.session_path(tid,sessions))
            correct=sessions/('rollout-test-'+tid+'.jsonl');correct.write_text('correct')
            self.assertEqual(receipts.checkpoint(tid,sessions),(str(correct.resolve()),7))


if __name__=='__main__':unittest.main()
