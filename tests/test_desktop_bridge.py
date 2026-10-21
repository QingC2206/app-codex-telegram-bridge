"""Offline Desktop routing regressions. No Telegram/model/task writes."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import codex_telegram_bridge.codex.desktop as d
import codex_telegram_bridge.app as b
import codex_telegram_bridge.telegram.notifications as n
class FakeTransport(b.CodexDesktopIpc):
    """Recorded follower frames; native tools may read or open an existing UI."""
    def __init__(self, timeout=35):
        self.timeout = timeout
        self.requests, self.closed = [], []
        self.expected_server_pid = None
        self.fail_write = self.fail_read_call = False
        self.catalog_names = {"read_thread", "list_threads"}
        self.sidebar = {'threads': [], 'pinnedThreads': []}
        self.status, self.thread_id = 'active', 'task'
        self.turn_id, self.result_turn_id = 'old-turn', 'new-turn'
        self.owner_present = True
        self.steer_inactive = False
        self.mutation_error = None
        self.on_navigate = lambda: None
        self.navigation_result = {'navigated': True}
        self.lose_navigation_reply = False

    def _open(self):
        return 55

    def _close(self, handle):
        self.closed.append(handle)

    def _write(self, handle, request):
        self.requests.append(request)
        if self.fail_write and request['method'] in ('thread-follower-steer-turn', 'thread-follower-start-turn'):
            raise OSError('partial write')

    def _next_message(self, handle, deadline):
        request = self.requests[-1]
        method = request['method']
        if request.get('type') == 'request':
            result = {}
            error = None
            if method == 'initialize':
                result = {'clientId': 'client'}
            elif method == 'thread-owner-discovery':
                if not self.owner_present: error = 'no-client-found'
            elif method == 'thread-follower-steer-turn':
                if self.fail_read_call: raise TimeoutError('lost acknowledgement')
                if self.steer_inactive: error = 'SteerTurnInactiveError: no active turn to steer'
                elif self.mutation_error: error = self.mutation_error
                else: result = {'result': {'turnId': self.result_turn_id}}
            elif method == 'thread-follower-start-turn':
                result = {'result': {'turn': {'id': self.result_turn_id}}}
            else:
                raise AssertionError('Unexpected follower method: ' + method)
            return {'type': 'response', 'requestId': request['requestId'], 'method': method,
                    'handledByClientId': 'owner', 'resultType': 'error' if error else 'success',
                    'result': result, 'error': error}
        if method == 'tools/list':
            result = {'tools': [{'name': name, 'namespace': 'codex_app'} for name in self.catalog_names]}
        else:
            assert request['params']['tool'] in {'read_thread', 'list_threads', 'navigate_to_codex_page'}, 'Agent delegation must never submit human input'
            content = self.sidebar if request['params']['tool'] == 'list_threads' else {
                'thread': {'id': self.thread_id, 'status': {'type': self.status}},
                'turns': [{'id': self.turn_id, 'status': 'inProgress'}]}
            if request['params']['tool'] == 'navigate_to_codex_page':
                self.on_navigate()
                if self.lose_navigation_reply:
                    raise TimeoutError('lost navigation acknowledgement')
                content = self.navigation_result
            result = {'success': True, 'contentItems': [{'type': 'inputText', 'text': json.dumps(content)}]}
        return {'id': request['id'], 'result': result}


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.transport = FakeTransport()
        self.owner = FakeTransport()
        self.discover = Mock(return_value=d.DesktopEndpoint(44, r"\\.\pipe\codex-app-tools-test"))
        self.native = d.DesktopAppTools(lambda **kw: self.transport, discover=self.discover)
        self.control = d.DesktopControl(FakeTransport)
        self.control.native = self.native
        self.control.transport_factory = lambda **kw: self.owner
        verify = patch.object(d, 'validate_pipe_server')
        self.verify = verify.start(); self.addCleanup(verify.stop)

    def mutations(self):
        return [r for r in self.owner.requests if r['method'] in
                ('thread-follower-steer-turn', 'thread-follower-start-turn')]

    def test_default_desktop_timeout_is_ten_seconds_in_follower_frame(self):
        self.assertEqual(d.DesktopControl(FakeTransport).native.timeout, 10)
        def factory(**kwargs):
            self.owner.timeout = kwargs['timeout']
            return self.owner
        self.control.transport_factory = factory
        self.control.send_follow_up('task', 'hello', '')
        self.assertEqual(self.mutations()[0]['timeoutMs'], 10000)

    def test_human_input_is_a_follower_frame_without_delegation_or_settings_override(self):
        result = self.control.send_follow_up('task', '继续工作 <answer>&', r'D:\repo')
        self.assertEqual((result['route'], result['turn_id']), ('desktop-user-input', 'new-turn'))
        frames = self.mutations()
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]['method'], 'thread-follower-steer-turn')
        self.assertEqual(frames[0]['targetClientId'], 'owner')
        self.assertEqual(frames[0]['params']['input'],
                         [{'type': 'text', 'text': '继续工作 <answer>&', 'text_elements': []}])
        self.assertNotIn('toolOutput', frames[0]['params'])
        self.assertEqual(self.owner.expected_server_pid, 44)
        self.assertFalse(hasattr(self.native, 'send_follow_up'))
        self.assertEqual([r['params']['tool'] for r in self.transport.requests if r['method'] == 'tools/call'], ['read_thread'])

    def test_read_only_probe_never_sends_a_follow_up(self):
        self.assertEqual(self.native.read_thread('task')['thread']['id'], 'task')
        self.assertFalse(self.mutations())

    def test_receipt_history_read_is_bounded_and_read_only(self):
        self.native.read_submission_history('task', timeout=3)
        calls = [r for r in self.transport.requests if r['method'] == 'tools/call']
        self.assertEqual(calls[0]['params']['tool'], 'read_thread')
        self.assertEqual(calls[0]['params']['arguments'], {'threadId': 'task', 'hostId': 'local',
            'turnLimit': 3, 'includeOutputs': False, 'maxOutputCharsPerItem': 20000})
        self.assertFalse(self.mutations())
        self.assertEqual(self.transport.closed, [55])

    def test_receipt_history_deadline_covers_catalog_and_history(self):
        original = self.transport._next_message
        elapsed = [0.0]
        def receive(handle, deadline):
            self.assertEqual(deadline, 3.0)
            elapsed[0] += 2
            return original(handle, deadline)
        with patch.object(d.time, 'monotonic', side_effect=lambda: elapsed[0]), \
                patch.object(self.transport, '_next_message', side_effect=receive):
            with self.assertRaises(TimeoutError):
                self.native.read_submission_history('task', timeout=3)
        self.assertEqual(self.transport.closed, [55])

    def test_live_list_uses_supported_limit_and_real_task_context(self):
        self.assertEqual(self.native.list_threads('actual-task'), self.transport.sidebar)
        params = self.transport.requests[-1]['params']
        self.assertEqual(params['tool'], 'list_threads')
        self.assertEqual(params['threadId'], 'actual-task')
        self.assertEqual(params['arguments'], {'limit': 50})
        self.assertFalse(self.mutations())

    def test_malformed_native_list_is_not_accepted_as_empty(self):
        self.transport.sidebar = {}
        with self.assertRaises(d.DesktopIpcUnavailable): self.native.list_threads('actual-task')

    def test_read_batch_rejects_restarted_desktop_instead_of_switching_instances(self):
        scoped = self.native.for_current_instance()
        self.discover.return_value = d.DesktopEndpoint(66, r'\\.\pipe\codex-app-tools-new')
        def verify(handle, expected):
            if expected != 66:
                raise d.DesktopBridgeSafetyError('pipe now belongs to a different process')
        self.verify.side_effect = verify
        with self.assertRaises(d.DesktopBridgeSafetyError): scoped.read_thread('task')
        self.assertFalse(self.transport.requests)

    def test_missing_owner_returns_without_agent_or_backend_fallback(self):
        self.owner.owner_present = False
        self.assertIsNone(self.control.send_follow_up('task', '继续', ''))
        self.assertFalse(self.mutations())
        self.assertFalse(self.transport.requests)

    def enable_recovery(self):
        self.owner.owner_present = False
        self.transport.catalog_names.add('navigate_to_codex_page')
        self.transport.on_navigate = lambda: setattr(self.owner, 'owner_present', True)

    def navigations(self):
        return [r for r in self.transport.requests if r['method'] == 'tools/call'
                and r['params']['tool'] == 'navigate_to_codex_page']

    def test_recovery_opens_exact_task_then_submits_once_to_original_instance(self):
        self.enable_recovery()
        result = self.control.send_follow_up('task', 'hello', '', recover_owner=True)
        self.assertTrue(result['owner_recovered'])
        self.assertEqual(len(self.mutations()), 1)
        self.assertEqual(len(self.navigations()), 1)
        self.assertEqual(self.navigations()[0]['params']['arguments'], {'threadId': 'task'})
        self.assertEqual(self.navigations()[0]['params']['threadId'], 'task')
        self.assertEqual([r['params']['tool'] for r in self.transport.requests if r['method'] == 'tools/call'],
                         ['read_thread', 'navigate_to_codex_page', 'read_thread'])
        self.assertEqual(self.owner.expected_server_pid, 44)
        self.assertTrue(all(call.args[1] == 44 for call in self.verify.call_args_list))

    def test_existing_owner_never_navigates(self):
        self.transport.catalog_names.add('navigate_to_codex_page')
        self.control.send_follow_up('task', 'hello', '', recover_owner=True)
        self.assertFalse(self.navigations())
        self.assertEqual(len(self.mutations()), 1)

    def test_missing_navigation_capability_stops_before_input(self):
        self.enable_recovery()
        self.transport.catalog_names.remove('navigate_to_codex_page')
        with self.assertRaises(d.DesktopIpcUnavailable):
            self.control.send_follow_up('task', 'hello', '', recover_owner=True)
        self.assertFalse(self.navigations())
        self.assertFalse(self.mutations())

    def test_wrong_task_stops_before_navigation_and_input(self):
        self.enable_recovery()
        self.transport.thread_id = 'another-task'
        with self.assertRaises(d.DesktopBridgeSafetyError):
            self.control.send_follow_up('task', 'hello', '', recover_owner=True)
        self.assertFalse(self.navigations())
        self.assertFalse(self.mutations())

    def test_restart_before_recovery_does_not_open_or_submit(self):
        self.enable_recovery()
        self.discover.side_effect = [d.DesktopEndpoint(44, 'old'), d.DesktopEndpoint(66, 'new')]
        with self.assertRaises(d.DesktopBridgeSafetyError):
            self.control.send_follow_up('task', 'hello', '', recover_owner=True)
        self.assertFalse(self.navigations())
        self.assertFalse(self.mutations())

    def test_restart_after_navigation_never_switches_instance(self):
        self.enable_recovery()
        self.transport.on_navigate = lambda: setattr(self.discover, 'return_value', d.DesktopEndpoint(66, 'new'))
        with self.assertRaises(d.DesktopBridgeSafetyError):
            self.control.send_follow_up('task', 'hello', '', recover_owner=True)
        self.assertEqual(len(self.navigations()), 1)
        self.assertFalse(self.mutations())

    def test_ambiguous_profile_during_recovery_stops_before_navigation(self):
        self.enable_recovery()
        with patch.object(d, 'validate_thread_profile', side_effect=[None, d.DesktopBridgeSafetyError('duplicate')]):
            with self.assertRaises(d.DesktopBridgeSafetyError):
                self.control.send_follow_up('task', 'hello', '', recover_owner=True)
        self.assertFalse(self.navigations())
        self.assertFalse(self.mutations())

    def test_navigation_must_acknowledge_success_before_input(self):
        for response in ({}, {'navigated': False}, {'navigated': 'true'}):
            with self.subTest(response=response):
                self.enable_recovery()
                self.transport.navigation_result = response
                with self.assertRaises(d.DesktopIpcUnavailable):
                    self.control.send_follow_up('task', 'hello', '', recover_owner=True)
                self.assertFalse(self.mutations())

    def test_lost_navigation_ack_is_not_an_unknown_user_submission(self):
        self.enable_recovery()
        self.transport.lose_navigation_reply = True
        with self.assertRaises(d.DesktopIpcUnavailable):
            self.control.send_follow_up('task', 'hello', '', recover_owner=True)
        self.assertEqual(len(self.navigations()), 1)
        self.assertFalse(self.mutations())

    def test_no_owner_after_navigation_is_bounded_and_never_submits(self):
        self.enable_recovery()
        self.transport.on_navigate = lambda: None
        elapsed = [0.0]
        def sleep(seconds):
            elapsed[0] += seconds
        with patch.object(d.time, 'monotonic', side_effect=lambda: elapsed[0]), \
                patch.object(d.time, 'sleep', side_effect=sleep):
            with self.assertRaises(d.DesktopIpcUnavailable):
                self.control.send_follow_up('task', 'hello', '', recover_owner=True)
        self.assertEqual(elapsed[0], 10)
        self.assertEqual(len(self.navigations()), 1)
        self.assertFalse(self.mutations())

    def test_recovery_budget_covers_native_navigation_and_broker_queries(self):
        self.enable_recovery()
        self.transport.on_navigate = lambda: None
        elapsed = [0.0]
        original = self.owner._next_message
        def receive(handle, deadline):
            if self.navigations():
                self.assertLessEqual(deadline, 10.0)
                elapsed[0] += 4
            return original(handle, deadline)
        # Model time spent in discovery after navigation, without a 10s sleep.
        with patch.object(d.time, 'monotonic', side_effect=lambda: elapsed[0]), \
                patch.object(d.time, 'sleep', side_effect=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)), \
                patch.object(self.owner, '_next_message', side_effect=receive):
            with self.assertRaises(d.DesktopIpcUnavailable):
                self.control.send_follow_up('task', 'hello', '', recover_owner=True)
        self.assertFalse(self.mutations())
        self.assertEqual(len(self.navigations()), 1)

    def test_delayed_owner_is_probed_without_repeating_navigation(self):
        self.enable_recovery()
        self.transport.on_navigate = lambda: None
        with patch.object(d.time, 'sleep', side_effect=lambda _: setattr(self.owner, 'owner_present', True)):
            self.control.send_follow_up('task', 'hello', '', recover_owner=True)
        self.assertEqual(len(self.navigations()), 1)
        self.assertEqual(len(self.mutations()), 1)

    def test_recovered_owner_still_requires_expected_active_turn(self):
        self.enable_recovery()
        with self.assertRaises(d.DesktopIpcInactive):
            self.control.send_follow_up('task', 'answer', '', expected_turn_id='changed-turn', recover_owner=True)
        self.assertFalse(self.mutations())

    def test_dispatched_unknown_submission_never_reopens_or_retries(self):
        self.enable_recovery()
        self.owner.fail_read_call = True
        with self.assertRaises(d.DesktopSubmissionUnknown):
            self.control.send_follow_up('task', 'hello', '', recover_owner=True)
        self.assertEqual(len(self.navigations()), 1)
        self.assertEqual(len(self.mutations()), 1)

    def test_capability_probe_never_opens_task(self):
        self.enable_recovery()
        self.assertFalse(self.control.can_interrupt('task'))
        self.assertFalse(self.navigations())
        self.assertFalse(self.mutations())

    def test_owner_lost_again_after_recovery_does_not_loop(self):
        self.enable_recovery()
        original = self.owner.find_thread_owner
        def probe(thread):
            found = original(thread)
            self.owner.owner_present = False
            return found
        with patch.object(self.owner, 'find_thread_owner', side_effect=probe):
            self.assertIsNone(self.control.send_follow_up('task', 'hello', '', recover_owner=True))
        self.assertEqual(len(self.navigations()), 1)
        self.assertFalse(self.mutations())

    def test_owner_probes_and_submissions_use_separate_receive_buffers(self):
        control = d.DesktopControl(b.CodexDesktopIpc)
        control.native.discover = self.discover
        first = control._legacy_for_current_desktop('task')
        second = control._legacy_for_current_desktop('task')
        self.assertIsNot(first, second)
        self.assertIsNot(first._buffer, second._buffer)

    def test_desktop_restart_during_owner_lookup_stops_before_input(self):
        self.discover.side_effect = [d.DesktopEndpoint(44, None), d.DesktopEndpoint(66, None)]
        with self.assertRaises(d.DesktopBridgeSafetyError):
            self.control.send_follow_up('task', '继续', '')
        self.assertFalse(self.mutations())

    def test_pipe_server_mismatch_stops_before_any_input(self):
        self.verify.side_effect = d.DesktopBridgeSafetyError('wrong Desktop process')
        with self.assertRaises(d.DesktopBridgeSafetyError):
            self.control.send_follow_up('task', '继续', '')
        self.assertFalse(self.mutations())
        self.assertEqual(self.transport.closed, [55])

    def test_missing_read_capability_stops_before_input(self):
        self.transport.catalog_names = set()
        with self.assertRaises(d.DesktopIpcUnavailable):
            self.control.send_follow_up('task', '继续', '')
        self.assertFalse(self.mutations())

    def test_wrong_profile_task_stops_before_input(self):
        self.transport.thread_id = 'another-task'
        with self.assertRaises(d.DesktopBridgeSafetyError):
            self.control.send_follow_up('task', '继续', '')
        self.assertFalse(self.mutations())

    def test_partial_write_or_lost_ack_is_uncertain_and_never_retried(self):
        for option in ('fail_write', 'fail_read_call'):
            with self.subTest(option=option):
                self.owner.requests.clear(); setattr(self.owner, option, True)
                with self.assertRaises(d.DesktopSubmissionUnknown):
                    self.control.send_follow_up('task', '继续', '')
                self.assertEqual(len(self.mutations()), 1)
                setattr(self.owner, option, False)

    def test_generic_error_after_dispatch_stays_uncertain(self):
        self.owner.mutation_error = 'client-disconnected'
        with self.assertRaises(d.DesktopSubmissionUnknown):
            self.control.send_follow_up('task', '继续', '')
        self.assertEqual(len(self.mutations()), 1)

    def test_explicit_inactive_error_allows_one_desktop_owned_start(self):
        self.transport.status = 'idle'; self.owner.steer_inactive = True
        result = self.control.send_follow_up('task', '继续', '')
        self.assertEqual(result['mode'], 'start')
        self.assertEqual([r['method'] for r in self.mutations()],
                         ['thread-follower-steer-turn', 'thread-follower-start-turn'])
        request = self.mutations()[1]['params']['turnStart']
        self.assertEqual(request['context'], {'inheritThreadSettings': True})
        self.assertEqual(set(request['request']), {'threadId', 'clientUserMessageId', 'input'})

    def test_strict_append_rejects_inactive_without_input(self):
        self.transport.status = 'idle'
        with self.assertRaises(d.DesktopIpcInactive):
            self.control.send_follow_up('task', '追加', '', start_if_inactive=False)
        self.assertFalse(self.mutations())

    def test_question_answer_rejects_changed_turn_before_input(self):
        with self.assertRaises(d.DesktopIpcInactive):
            self.control.send_follow_up('task', 'answer', '', expected_turn_id='different-turn')
        self.assertFalse(self.mutations())

    def test_turn_ending_after_preflight_never_starts_answer_in_new_turn(self):
        self.owner.steer_inactive = True
        with self.assertRaises(d.DesktopIpcInactive):
            self.control.send_follow_up('task', 'answer', '', expected_turn_id='old-turn')
        self.assertEqual(len(self.mutations()), 1)

    def test_question_ack_for_different_turn_is_uncertain_without_retry(self):
        with self.assertRaises(d.DesktopSubmissionUnknown):
            self.control.send_follow_up('task', 'answer', '', expected_turn_id='old-turn')
        self.assertEqual(len(self.mutations()), 1)

    def test_empty_input_does_not_connect(self):
        with self.assertRaises(ValueError): self.control.send_follow_up('task', ' ', '')
        self.discover.assert_not_called()


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.home = Path(self.folder.name).resolve()
        self.roots = patch.object(d, "_desktop_roots", return_value=[{"ProcessId": 44}])
        self.env = patch.object(d, "_process_environment", return_value={"CODEX_HOME": str(self.home)})
        self.roots_mock = self.roots.start(); self.addCleanup(self.roots.stop)
        self.env_mock = self.env.start(); self.addCleanup(self.env.stop)
        from codex_telegram_bridge.platforms.discovery import valid_windows_endpoint
        grammar = patch.object(d, 'valid_native_endpoint', side_effect=valid_windows_endpoint)
        grammar.start(); self.addCleanup(grammar.stop)

    def test_current_desktop_environment_is_rediscovered_after_restart(self):
        self.env_mock.return_value["CODEX_APP_TOOLS_PIPE_PATH"] = r"\\.\pipe\codex-browser-use-first"
        first = d.discover_desktop(self.home)
        self.roots_mock.return_value = [{"ProcessId": 66}]
        self.env_mock.return_value["CODEX_APP_TOOLS_PIPE_PATH"] = r"\\.\pipe\codex-browser-use-second"
        second = d.discover_desktop(self.home)
        self.assertNotEqual(first, second)

    def test_modern_app_tools_pipe_is_accepted(self):
        pipe = r"\\.\pipe\codex-app-tools-12345678-1234-1234-1234-123456789abc"
        self.env_mock.return_value["CODEX_APP_TOOLS_PIPE_PATH"] = pipe
        self.assertEqual(d.discover_desktop(self.home), d.DesktopEndpoint(44, pipe))

    def test_legacy_browser_use_pipe_remains_supported(self):
        pipe = r"\\.\pipe\codex-browser-use-12345678-1234-1234-1234-123456789abc"
        self.env_mock.return_value["CODEX_APP_TOOLS_PIPE_PATH"] = pipe
        self.assertEqual(d.discover_desktop(self.home), d.DesktopEndpoint(44, pipe))

    def test_known_pipe_names_do_not_allow_paths_control_characters_or_empty_suffix(self):
        for prefix in (r"\\.\pipe\codex-app-tools-", r"\\.\pipe\codex-browser-use-"):
            for suffix in ('', '../another', '..\\another', 'name\\another', 'name/another',
                           'name\x00other', 'name\nother', 'name other', 'name:other', 'a' * 129):
                with self.subTest(prefix=prefix, suffix=suffix):
                    self.env_mock.return_value['CODEX_APP_TOOLS_PIPE_PATH'] = prefix + suffix
                    with self.assertRaises(d.DesktopBridgeSafetyError):
                        d.discover_desktop(self.home)

    def test_modern_pipe_selection_stays_with_the_correct_subscription(self):
        self.roots_mock.return_value = [{'ProcessId': 44}, {'ProcessId': 66}]
        pipe = r'\\.\pipe\codex-app-tools-primary'
        self.env_mock.side_effect = [
            {'CODEX_HOME': str(self.home), 'CODEX_APP_TOOLS_PIPE_PATH': pipe},
            {'CODEX_HOME': str(self.home / 'other'), 'CODEX_APP_TOOLS_PIPE_PATH': r'\\.\pipe\codex-app-tools-isolated'},
        ]
        self.assertEqual(d.discover_desktop(self.home), d.DesktopEndpoint(44, pipe))

    def test_cockpit_other_profile_is_never_used(self):
        self.env_mock.return_value["CODEX_HOME"] = str(self.home / "other-subscription")
        with self.assertRaises(d.DesktopBridgeSafetyError):
            d.discover_desktop(self.home)

    def test_isolated_second_subscription_does_not_block_correct_profile(self):
        self.roots_mock.return_value = [{"ProcessId": 44}, {"ProcessId": 66}]
        self.env_mock.side_effect = [{"CODEX_HOME": str(self.home)}, {"CODEX_HOME": str(self.home / "isolated")}]
        self.assertEqual(d.discover_desktop(self.home).pid, 44)

    def test_two_roots_sharing_profile_are_rejected(self):
        self.roots_mock.return_value = [{"ProcessId": 44}, {"ProcessId": 66}]
        with self.assertRaises(d.DesktopBridgeSafetyError):
            d.discover_desktop(self.home)

    def test_unreadable_instance_cannot_be_silently_assumed_safe(self):
        self.env_mock.side_effect = d.DesktopBridgeSafetyError("read denied")
        with self.assertRaises(d.DesktopBridgeSafetyError):
            d.discover_desktop(self.home)

    def test_untrusted_endpoint_path_is_rejected(self):
        self.env_mock.return_value["CODEX_APP_TOOLS_PIPE_PATH"] = r"\\remote-host\pipe\unknown"
        with self.assertRaises(d.DesktopBridgeSafetyError):
            d.discover_desktop(self.home)

    def test_no_desktop_is_unavailable(self):
        self.roots_mock.return_value = []
        with self.assertRaises(d.DesktopIpcUnavailable):
            d.discover_desktop(self.home)

    def test_relative_profile_is_not_resolved_using_bot_cwd(self):
        self.env_mock.return_value["CODEX_HOME"] = "relative-home"
        with self.assertRaises(d.DesktopBridgeSafetyError):
            d.discover_desktop(self.home)

    def test_default_profile_uses_desktop_user_directory(self):
        self.env_mock.return_value = {"USERPROFILE": str(self.home)}
        self.assertEqual(d.discover_desktop(self.home / ".codex").pid, 44)

    def test_isolated_userprofile_cannot_be_replaced_with_bot_home(self):
        self.env_mock.return_value = {"USERPROFILE": str(self.home / "other-subscription")}
        with self.assertRaises(d.DesktopBridgeSafetyError):
            d.discover_desktop(self.home / ".codex")

    def test_conflicting_home_hints_fail_closed(self):
        self.env_mock.return_value = {"USERPROFILE": str(self.home), "HOME": str(self.home / "other")}
        with self.assertRaises(d.DesktopBridgeSafetyError):
            d.discover_desktop(self.home / ".codex")

    @unittest.skipUnless(os.name == "nt", "Windows process metadata; POSIX checked separately")
    def test_process_inventory_excludes_vscode_and_electron_children(self):
        self.roots.stop()
        app = self.home / "WindowsApps/OpenAI.Codex_test/app/ChatGPT.exe"
        app.parent.mkdir(parents=True); app.write_bytes(b"fixture")
        records = [{"ProcessId": 44, "ExecutablePath": str(app), "CommandLine": str(app)},
                   {"ProcessId": 66, "ExecutablePath": str(app), "CommandLine": str(app) + " --type=renderer"},
                   {"ProcessId": 88, "ExecutablePath": str(self.home / "vscode/ChatGPT.exe")}]
        import psutil
        processes=[Mock(info={'pid':r['ProcessId'],'exe':r['ExecutablePath'],'cmdline':[r['ExecutablePath']]+(['--type=renderer'] if r['ProcessId']==66 else [])}) for r in records]
        with patch.object(psutil, "process_iter", return_value=processes):
            self.assertEqual([r["ProcessId"] for r in d._desktop_roots()], [44])


class ProfileCollisionTests(unittest.TestCase):
    def test_copied_thread_in_other_running_profile_is_rejected(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as folder:
            profile = Path(folder)
            with sqlite3.connect(profile / 'state_9.sqlite') as db:
                db.execute('CREATE TABLE threads (id TEXT)')
                db.execute("INSERT INTO threads VALUES ('task')")
            db.close()
            endpoint = d.DesktopEndpoint(44, None, other_profiles=(profile,))
            with self.assertRaises(d.DesktopBridgeSafetyError): d.validate_thread_profile(endpoint, 'task')
            d.validate_thread_profile(endpoint, 'another-task')

    def test_unreadable_other_profile_state_is_not_assumed_empty(self):
        with tempfile.TemporaryDirectory() as folder:
            profile = Path(folder); (profile / 'state_9.sqlite').write_bytes(b'invalid sqlite')
            endpoint = d.DesktopEndpoint(44, None, other_profiles=(profile,))
            with self.assertRaises(d.DesktopBridgeSafetyError): d.validate_thread_profile(endpoint, 'task')


class BotEntryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        self.bot = b.TelegramCodexBot("111:fixture", {"chat_id": "123"})
        self.bot.state = b.StateStore(Path(self.folder.name) / "state.json")
        self.bot.app = Mock()
        self.bot.app.request.return_value = {"thread": {"id": "task", "cwd": "repo", "status": {"type": "notLoaded"}}}
        self.bot.send = Mock(return_value={})
        self.bot._reaction = Mock(return_value=True)
        self.transport = FakeTransport()
        self.owner = FakeTransport()
        self.bot.desktop = d.DesktopControl(lambda **kw: self.transport)
        self.bot.desktop.transport_factory = lambda **kw: self.owner
        self.bot.desktop.native.discover = Mock(return_value=d.DesktopEndpoint(44, r"\\.\pipe\codex-browser-use-test"))
        self.bot._thread_activity = Mock(return_value=(False, ""))
        for context in (patch.object(d, "validate_pipe_server"), patch.object(b, "persisted_thread_is_active", return_value=False)):
            context.start(); self.addCleanup(context.stop)

    def assert_single_desktop_message_no_backend_takeover(self):
        calls = [r for r in self.transport.requests if r["method"] == "tools/call"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["params"]["tool"], "read_thread")
        inputs = [r for r in self.owner.requests if r['method'] == 'thread-follower-steer-turn']
        self.assertEqual(len(inputs), 1)
        self.assertEqual(inputs[0]['params']['input'][0]['text'], '继续工作')
        self.assertEqual([c.args[0] for c in self.bot.app.request.call_args_list], ["thread/read"])

    def test_reply_to_notification_reaches_native_desktop(self):
        with patch.object(b, "lookup_task_message", return_value="task"):
            self.bot.handle_text("继续工作", reply_to_message_id=7)
        self.assert_single_desktop_message_no_backend_takeover()

    def test_reply_to_notification_uses_discovered_modern_pipe(self):
        profile = Path(self.folder.name).resolve()
        pipe = r'\\.\pipe\codex-app-tools-modern' if os.name == 'nt' else '/tmp/codex-app-tools/modern.sock'
        self.bot.desktop.native.discover = lambda: d.discover_desktop(profile)
        with patch.object(d, '_desktop_roots', return_value=[{'ProcessId': 44}]), \
                patch.object(d, '_process_environment', return_value={
                    'CODEX_HOME': str(profile), 'CODEX_APP_TOOLS_PIPE_PATH': pipe}), \
                patch.object(b, 'lookup_task_message', return_value='task'):
            self.bot.handle_text('继续工作', reply_to_message_id=7)
        self.assertEqual(self.transport.PIPE_PATH, pipe)
        self.assert_single_desktop_message_no_backend_takeover()

    def test_send_selected_task_uses_discovered_modern_pipe(self):
        profile = Path(self.folder.name).resolve()
        pipe = r'\\.\pipe\codex-app-tools-modern' if os.name == 'nt' else '/tmp/codex-app-tools/modern.sock'
        self.bot.desktop.native.discover = lambda: d.discover_desktop(profile)
        self.bot.awaiting_continue['123'] = 'task'
        with patch.object(d, '_desktop_roots', return_value=[{'ProcessId': 44}]), \
                patch.object(d, '_process_environment', return_value={
                    'CODEX_HOME': str(profile), 'CODEX_APP_TOOLS_PIPE_PATH': pipe}):
            self.bot.handle_text('继续工作')
        self.assertEqual(self.transport.PIPE_PATH, pipe)
        self.assert_single_desktop_message_no_backend_takeover()
        self.assertNotIn('123', self.bot.awaiting_continue)

    def test_send_selected_task_reaches_native_desktop(self):
        self.bot.awaiting_continue["123"] = "task"
        self.bot.handle_text("继续工作")
        self.assert_single_desktop_message_no_backend_takeover()
        self.assertNotIn("123", self.bot.awaiting_continue)

    def test_direct_send_command_reaches_native_desktop(self):
        self.bot.resolve_thread = Mock(return_value="task")
        self.bot.command_send("task 继续工作")
        self.assert_single_desktop_message_no_backend_takeover()

    def test_receipt_network_failure_does_not_prevent_actual_submission(self):
        self.bot._reaction.return_value = False
        self.bot.send.side_effect = TimeoutError("Telegram offline")
        with patch.object(b, "log"):
            self.bot.submit_direct_reply("task", "继续工作")
        self.assert_single_desktop_message_no_backend_takeover()

    def test_definite_failure_is_not_mislabeled_unknown_or_reported_twice(self):
        self.bot.desktop = Mock()
        self.bot.desktop.send_follow_up.side_effect = d.DesktopIpcError("Desktop explicitly rejected")
        with self.assertRaisesRegex(d.DesktopIpcError, "explicitly rejected"):
            self.bot.submit_direct_reply("task", "继续工作")
        self.bot.send.assert_not_called()  # update handler supplies one error

    def test_lost_user_input_ack_is_never_resumed_or_submitted_again(self):
        self.owner.fail_read_call = True
        self.bot.awaiting_continue["123"] = "task"
        with self.assertRaises(d.DesktopSubmissionUnknown):
            self.bot.handle_text("继续工作")
        self.assert_single_desktop_message_no_backend_takeover()
        self.assertNotIn("123", self.bot.awaiting_continue)

    def test_force_reply_target_survives_known_failure(self):
        self.bot.awaiting_direct_reply[7] = "task"
        self.bot.start_turn = Mock(side_effect=d.DesktopIpcError("rejected"))
        with self.assertRaises(d.DesktopIpcError):
            self.bot.handle_text("继续工作", reply_to_message_id=7)
        self.assertEqual(self.bot.awaiting_direct_reply[7], "task")

    def test_explicit_missing_desktop_retains_send_selection(self):
        self.bot.start_turn = Mock(side_effect=d.DesktopIpcUnavailable("Desktop not running"))
        self.bot.awaiting_continue["123"] = "task"
        with self.assertRaises(d.DesktopIpcUnavailable):
            self.bot.handle_text("继续工作")
        self.assertEqual(self.bot.awaiting_continue["123"], "task")

    def _run_poll_fixture(self, poll):
        self.bot.plugins.start = Mock()
        self.bot._set_commands = Mock()
        self.bot.quota_resets.startup = Mock()
        self.bot._send_unreported_credit_baseline = Mock()
        self.bot.shutdown = Mock()
        with patch.object(b.threading, "Thread"), patch.object(b, "telegram_call", side_effect=poll), patch.object(b, "log"):
            self.bot.run(quiet_start=True)

    def test_service_crash_after_dispatch_cannot_replay_consumed_message(self):
        self.bot.state.update(update_offset=100)
        submitted = []
        def submit_then_crash(update):
            self.assertEqual(self.bot.state.snapshot()["update_offset"], 101)
            submitted.append(update["update_id"])
            raise KeyboardInterrupt()
        self.bot.handle_update = Mock(side_effect=submit_then_crash)
        self._run_poll_fixture(lambda *a, **kw: [{"update_id": 100}])
        self.bot.state = b.StateStore(self.bot.state.path)  # fresh on-disk read after crash
        self.bot.handle_update = Mock()
        polls = []
        def restart_poll(*a, **kw):
            polls.append(kw)
            if len(polls) == 1:
                return [{"update_id": 100}]
            self.bot.stop_event.set()
            return []
        self._run_poll_fixture(restart_poll)
        self.assertEqual(submitted, [100])
        self.bot.handle_update.assert_not_called()

    def test_failed_durable_offset_write_prevents_dispatch(self):
        self.bot.state.update(update_offset=100)
        self.bot.state.update = Mock(side_effect=OSError("state disk unavailable"))
        self.bot.handle_update = Mock()
        with patch.object(b.time, "sleep", side_effect=lambda seconds: self.bot.stop_event.set()):
            self._run_poll_fixture(lambda *a, **kw: [{"update_id": 100}])
        self.bot.handle_update.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
