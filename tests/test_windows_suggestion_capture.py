"""Offline protocol fixtures; the Windows tests also exercise actual DPAPI and named pipes."""
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import psutil

from codex_telegram_bridge.codex.desktop import DesktopEndpoint
from codex_telegram_bridge.platforms.windows import composer_capture as capture
from codex_telegram_bridge.platforms.windows import desktop_launch as launch, desktop_proxy as proxy


THREAD = '00000000-0000-4000-8000-000000000001'
TURN = '00000000-0000-4000-8000-000000000002'
OTHER = '00000000-0000-4000-8000-000000000003'
TEXT = '把 [A] 和 B 对比一下。\nPreserve <&> 😀'


def request(rid):
    return dict(id=rid, method='thread/sample', params=dict(threadSource='composer_predictions',
        threadId=THREAD, expectedTurnId=TURN, turnTrigger='composer_predictions',
        responsesapiClientMetadata=dict(prediction_source_thread_id=THREAD, prediction_source_turn_id=TURN)))


def response(rid):
    return dict(id=rid, result=dict(sourceTurnId=TURN, responseId='fixture-candidate',
                                    text=json.dumps({'suggestion': TEXT}, ensure_ascii=False)))


def observe(tracker, msg, incoming):
    tracker.consume(json.dumps(msg, ensure_ascii=False).encode(), incoming=incoming)


class CaptureTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.profile = Path(tmp.name)
        (self.profile/'auth.json').write_text('fixture')
        self.check = Mock(return_value=True)
        self.tracker = capture.Capture(self.profile, 123, decision=self.check)

    def approved(self):
        return self.tracker.lookup(THREAD, TURN)

    def publish(self, rid='thread/sample:synthetic'):
        observe(self.tracker, request(rid), True)
        observe(self.tracker, response(rid), False)

    def test_only_approved_exact_text_is_published(self):
        self.check.return_value = False
        initial = self.approved()
        self.publish()
        self.assertEqual(self.approved(), initial)
        self.check.return_value = True
        result = self.approved()
        self.assertEqual(result['text'], TEXT)
        self.assertEqual(result['responseId'], 'fixture-candidate')
        self.assertEqual(result['principalEpoch'], initial['principalEpoch'])
        self.assertNotIn('text', self.tracker.lookup(THREAD, OTHER))

    def test_wrong_ids_sources_missing_text_and_ordinary_json_stay_private(self):
        for change in ('wrong method','wrong source','metadata','rpc','turn','missing response',
                       'error','ordinary JSON','null','non-string','boolean id'):
            with self.subTest(change):
                self.tracker.reset()
                req, result = request(41), response(41)
                if change == 'wrong method': req['method'] = 'thread/read'
                if change == 'wrong source': req['params']['threadSource'] = 'user'
                if change == 'metadata': req['params']['responsesapiClientMetadata']['prediction_source_turn_id'] = OTHER
                if change == 'rpc': result['id'] = 42
                if change == 'turn': result['result']['sourceTurnId'] = OTHER
                if change == 'missing response': del result['result']['responseId']
                if change == 'error': result['error'] = {}
                if change == 'ordinary JSON': result['method'] = 'item/completed'
                if change == 'null': result['result']['text'] = '{"suggestion":null}'
                if change == 'non-string': result['result']['text'] = '{"suggestion":42}'
                if change == 'boolean id': req['id'] = result['id'] = True
                observe(self.tracker, req, True)
                observe(self.tracker, result, False)
                self.assertNotIn('text', self.approved())

    def test_integer_ids_and_conflicting_candidates(self):
        self.publish(42)
        self.assertEqual(self.approved()['text'], TEXT)
        self.publish(43)
        self.assertNotIn('text', self.approved())

    def test_reused_rpc_id_and_incomplete_terminal_response_cannot_claim_later_text(self):
        for frame, incoming in (({'id': 41, 'method': 'thread/read'}, True),
                                ({'id': 41, 'result': {}}, False)):
            self.tracker.reset()
            observe(self.tracker, request(41), True)
            observe(self.tracker, frame, incoming)
            observe(self.tracker, response(41), False)
            self.assertNotIn('text', self.approved())

    def test_switch_and_external_auth_file_replacement_revoke(self):
        self.publish()
        first = self.approved()['principalEpoch']
        observe(self.tracker, dict(method='account/updated', params={}), False)
        after = self.approved()
        self.assertNotEqual(after['principalEpoch'], first)
        self.assertNotIn('text', after)
        self.publish(2)
        (self.profile/'auth.json').write_text('different fixture session')
        self.assertNotIn('text', self.approved())
        self.assertNotEqual(self.approved()['principalEpoch'], after['principalEpoch'])
        (self.profile/'auth.json').unlink()
        self.assertIsNone(self.approved())

    def test_session_change_during_approval_revokes(self):
        self.publish()
        self.check.side_effect = lambda *_: (self.tracker.reset() or True)
        self.assertIsNone(self.approved())

    def test_log_matches_exact_parent_time_and_turn_and_refuses_conflicts(self):
        now = datetime.now(timezone.utc)
        folder = self.profile/'Codex/Logs'/now.strftime('%Y/%m/%d')
        folder.mkdir(parents=True)
        logfile = folder/'codex-desktop-test-123-t0-i1-test.log'
        prefix = (now.isoformat() + ' info [cumberbatch] cumberbatch_safety_decision ' +
                  f'sourceThreadId={THREAD} sourceTurnId={TURN} suppressed=')
        with patch.dict(os.environ, LOCALAPPDATA=str(self.profile)):
            def ok(pid=123, turn=TURN, when=None):
                return capture.approved_in_log(pid, (THREAD, turn), when or time.time()-20)
            self.assertFalse(ok())
            logfile.write_text(prefix+'false\n')
            self.assertTrue(ok())
            self.assertFalse(ok(122))
            self.assertFalse(ok(turn=OTHER))
            self.assertFalse(ok(when=time.time()+60))
            for last in ('true','false'):
                logfile.write_text(prefix+'false\n'+prefix+last+'\n')
                self.assertFalse(ok())

    def test_binary_pump_forwards_split_and_oversized_frames_verbatim(self):
        class Stream(io.BytesIO):
            def read1(self, size):
                return self.read(min(3, size))
        payload = b'\0x\n' + bytes(range(50, 100)) + b'\nOK\npartial'
        frames, sink, reset = [], io.BytesIO(), Mock()
        with patch.object(proxy, 'MAX_FRAME', 12):
            proxy.pump(Stream(payload), sink, frames.append, reset)
        self.assertEqual(sink.getvalue(), payload)
        self.assertEqual(frames, [b'\0x', b'OK'])
        reset.assert_called_once()

    def test_observer_error_cannot_modify_traffic(self):
        sink, reset = io.BytesIO(), Mock()
        payload = b'1\n2\n\xff'
        proxy.pump(io.BytesIO(payload), sink, Mock(side_effect=ValueError()), reset)
        self.assertEqual(sink.getvalue(), payload)
        reset.assert_called_once()

    def test_launch_environment_is_local_and_precisely_pinned(self):
        exe = self.profile/proxy.PROXY
        with self.assertRaises(FileNotFoundError):
            launch.launch_env(self.profile, self.profile/'data', exe)
        exe.touch()
        before = os.environ.get('CODEX_CLI_PATH')
        env = launch.launch_env(self.profile, self.profile/'data', exe)
        self.assertEqual(env['CODEX_CLI_PATH'], str(exe))
        self.assertEqual(env['CODEX_HOME'], env[proxy.PROFILE_MARKER])
        self.assertEqual(os.environ.get('CODEX_CLI_PATH'), before)

    def test_launcher_refuses_already_running_or_shared_profile(self):
        for home in (self.profile, self.profile/'other'):
            shared = self.profile/'app-data'
            with (patch.object(launch, 'enabled', return_value=True),
                  patch.object(launch, 'codex_home', return_value=self.profile),
                  patch.object(launch, '_profiles', return_value=[(home, shared, 1)]),
                  patch.object(launch.subprocess, 'Popen') as spawn):
                with self.assertRaises(RuntimeError):
                    launch.start(shared)
                spawn.assert_not_called()

    def test_packaged_parent_requires_system_registration_and_matching_binary(self):
        package = self.profile/'OpenAI.Codex_26.1.2.3_x64__2p2nqsd0c76g0'
        app = package/'app/ChatGPT.exe'
        backend = app.parent/'resources/codex.exe'
        backend.parent.mkdir(parents=True)
        backend.touch()
        app.touch()
        with patch.object(proxy, '_registered_package_path', return_value=None):
            self.assertIsNone(proxy.bundled_runtime(app))
        with patch.object(proxy, '_registered_package_path', return_value=package):
            self.assertEqual(proxy.bundled_runtime(app), backend)
            self.assertIsNone(proxy.bundled_runtime(backend))


@unittest.skipUnless(os.name == 'nt', 'Windows named pipe and user-scoped DPAPI')
class PipeTests(unittest.TestCase):
    def test_live_ipc_is_pinned_and_rejects_wrong_keys_profiles_and_multiple_relays(self):
        from codex_telegram_bridge.platforms.windows import suggestion_pipe as pipe
        from codex_telegram_bridge.platforms.windows.discovery import validate_pipe_server
        from codex_telegram_bridge.codex.errors import DesktopBridgeSafetyError
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, CODEX_HOME=tmp,
                                                                CODEX_TELEGRAM_DATA_DIR=tmp):
            profile = Path(tmp).resolve()
            (profile/'auth.json').write_text('fixture')
            pid = psutil.Process().ppid()
            endpoint = DesktopEndpoint(pid, None, profile)
            tracker = capture.Capture(profile, pid, decision=lambda *_: True)
            sender = pipe.Publisher(tracker)
            try:
                observe(tracker, request(5), True)
                observe(tracker, response(5), False)
                self.assertEqual(pipe.read_approved(endpoint, THREAD, TURN)['text'], TEXT)
                self.assertIsNone(pipe.read_approved(DesktopEndpoint(pid,None,profile/'other'), THREAD, TURN))
                self.assertIsNone(pipe.read_approved(endpoint, THREAD, 'short'))
                desc = sender.path.read_text()
                self.assertNotIn(TEXT, desc)
                self.assertNotIn(sender.secret, desc)
                with pipe._connect(sender.address) as conn:
                    with self.assertRaises(DesktopBridgeSafetyError):
                        validate_pipe_server(conn.fileno(), pid)
                    conn.send_bytes(json.dumps(dict(thread=THREAD, turn=TURN, key='wrong')).encode())
                    self.assertIsNone(json.loads(conn.recv_bytes(128)))
                second = pipe.Publisher(tracker)
                try:
                    self.assertIsNone(pipe.read_approved(endpoint, THREAD, TURN))
                finally:
                    second.close()
                self.assertEqual(pipe.read_approved(endpoint, THREAD, TURN)['text'], TEXT)
            finally:
                sender.close()
            self.assertFalse(sender.path.exists())
            self.assertIsNone(pipe.read_approved(endpoint, THREAD, TURN))
