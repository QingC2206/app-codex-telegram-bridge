"""Offline failure tests for the live acceptance gate, not live-link evidence."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import codex_telegram_bridge.management.connection_check as check
import codex_telegram_bridge.codex.desktop as desktop
import codex_telegram_bridge.app as bot
class AcceptanceGateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.credentials = self.root / 'credentials.json'
        self.credentials.write_text(json.dumps({'chat_id': '123', 'token_dpapi': 'encrypted-fixture'}))
        self.rollout = self.root / 'rollout-test.jsonl'
        self.rollout.write_text('')
        self.thread = 'task-123456'
        self.client = Mock()
        self.client.native._connect.return_value = (Mock(), 55)
        self.client._legacy_for_current_desktop.return_value.find_thread_owner.return_value = True
        self.client.native.read_thread.return_value = {'thread': {'id': self.thread, 'status': {'type': 'active'}},
                                                     'turns': [{'id': 'received-turn', 'status': 'inProgress'}]}
        self.client.send_follow_up.side_effect = self.accept
        contexts = (
            patch.object(check, 'load_token', return_value='fixture-token'),
            patch.object(check.notify, 'telegram_call', side_effect=self.telegram),
            patch.object(check.desktop, 'discover_desktop', return_value=desktop.DesktopEndpoint(44, 'fixture')),
            patch.object(check.desktop, 'DesktopControl', return_value=self.client),
            patch.object(check.receipts, 'checkpoint', return_value=(str(self.rollout), 0)),
        )
        self.mocks = []
        for context in contexts:
            self.mocks.append(context.start())
            self.addCleanup(context.stop)
        self.api = self.mocks[1]

    def telegram(self, token, method, *args, **kwargs):
        if method == 'getMe':
            return {'is_bot': True, 'id': 99}
        if method == 'sendMessage':
            return {'message_id': 70, 'chat': {'id': 123}}
        raise AssertionError('Unexpected API call: ' + method)

    def accept(self, thread, prompt, cwd, **kwargs):
        # Exercise the production receipt parser against an appended record.
        record = {'type': 'response_item', 'payload': {
            'type': 'message', 'role': 'user', 'id': 'received-item',
            'content': [{'type': 'input_text', 'text': prompt}],
            'internal_chat_message_metadata_passthrough': {'turn_id': 'received-turn'}}}
        with self.rollout.open('a', encoding='utf-8') as f:
            f.write(json.dumps(record) + '\n')
        return {'route': 'desktop-user-input', 'mode': 'steer', 'turn_id': 'received-turn'}

    def run_check(self, **kwargs):
        options = dict(thread_id=self.thread, send_probe=True, codex_home=self.root,
                       credentials=self.credentials, receipt_timeout=0.001)
        options.update(kwargs)
        return check.check_connection(**options)

    def test_read_only_check_does_not_claim_or_attempt_task_delivery(self):
        report = self.run_check(thread_id='', send_probe=False)
        self.assertTrue(report['success'])
        self.assertEqual(report['mode'], 'read-only')
        self.assertEqual(report['checks']['desktop_delivery'], 'not_run')
        self.assertEqual(report['telegram_inbound'], 'not_exercised')
        self.client.send_follow_up.assert_not_called()
        self.assertEqual([c.args[1] for c in self.api.call_args_list], ['getMe'])

    def test_no_explicit_task_stops_before_any_io(self):
        self.assertFalse(self.run_check(thread_id='')['success'])
        self.api.assert_not_called()
        self.client.send_follow_up.assert_not_called()

    def test_telegram_success_cannot_hide_desktop_discovery_failure(self):
        self.mocks[2].side_effect = desktop.DesktopBridgeSafetyError('invalid native endpoint')
        report = self.run_check()
        self.assertFalse(report['success'])
        self.assertEqual(report['failure_stage'], 'desktop_connection')
        self.client.send_follow_up.assert_not_called()

    def test_wrong_or_inactive_target_is_not_sent_to(self):
        for thread in ({'id': 'other-task', 'status': {'type': 'active'}},
                       {'id': self.thread, 'status': {'type': 'idle'}}):
            with self.subTest(thread=thread):
                self.client.native.read_thread.return_value = {'thread': thread}
                self.assertFalse(self.run_check()['success'])
                self.client.send_follow_up.assert_not_called()

    def test_accepted_submission_without_receipt_does_not_pass_or_retry(self):
        self.client.send_follow_up.side_effect = None
        self.client.send_follow_up.return_value = {'route': 'desktop-user-input', 'turn_id': 'received-turn'}
        report = self.run_check()
        self.assertFalse(report['success'])
        self.assertEqual(report['checks']['desktop_delivery'], 'unconfirmed')
        self.client.send_follow_up.assert_called_once()
        self.assertEqual([c.args[1] for c in self.api.call_args_list], ['getMe'])

    def test_receipt_for_another_turn_cannot_pass(self):
        def wrong_turn(*args, **kwargs):
            result = self.accept(*args, **kwargs)
            result['turn_id'] = 'other-turn'
            return result
        self.client.send_follow_up.side_effect = wrong_turn
        self.assertFalse(self.run_check()['success'])
        self.client.send_follow_up.assert_called_once()

    def test_delegation_receipt_cannot_pass_as_human_input(self):
        def delegated(thread, prompt, cwd, **kwargs):
            escaped = prompt.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            envelope = ('<codex_delegation>\n  <source_thread_id>' + thread
                        + '</source_thread_id>\n  <input>' + escaped + '</input>\n</codex_delegation>')
            record = {'type': 'response_item', 'payload': {'type': 'function_call_output',
                'namespace': 'codex_app', 'name': 'send_message_to_thread', 'id': 'agent-item',
                'output': envelope, 'internal_chat_message_metadata_passthrough': {'turn_id': 'received-turn'}}}
            self.rollout.write_text(json.dumps(record) + '\n', encoding='utf-8')
            return {'route': 'desktop-user-input', 'turn_id': 'received-turn'}
        self.client.send_follow_up.side_effect = delegated
        self.assertFalse(self.run_check()['success'])
        self.client.send_follow_up.assert_called_once()

    def test_absent_owner_uses_guarded_recovery_for_explicit_probe(self):
        self.client._legacy_for_current_desktop.return_value.find_thread_owner.return_value = False
        report = self.run_check()
        self.assertTrue(report['success'])
        self.assertEqual(report['desktop_attempts'], 1)
        self.assertTrue(self.client.send_follow_up.call_args.kwargs['recover_owner'])
        self.assertEqual(self.client.send_follow_up.call_args.kwargs['expected_turn_id'], 'received-turn')

    def test_missing_turn_id_stops_before_explicit_probe(self):
        self.client.native.read_thread.return_value.pop('turns')
        self.assertFalse(self.run_check()['success'])
        self.client.send_follow_up.assert_not_called()

    def test_exact_receipt_and_confirmed_telegram_message_are_both_required(self):
        report = self.run_check()
        self.assertTrue(report['success'])
        self.assertEqual(report['checks']['desktop_delivery'], 'passed')
        self.assertEqual(report['checks']['telegram_delivery'], 'passed')
        self.assertEqual(report['receipt_turn_id'], 'received-turn')
        self.assertEqual(report['receipt_origin'], 'user')
        self.assertEqual(report['desktop_route'], 'desktop-user-input')
        self.assertEqual((report['desktop_attempts'], report['telegram_attempts']), (1, 1))
        self.assertEqual(report['telegram_inbound'], 'not_exercised')
        self.assertFalse(self.client.send_follow_up.call_args.kwargs['start_if_inactive'])
        self.assertEqual([c.args[1] for c in self.api.call_args_list], ['getMe', 'sendMessage'])

    def test_uncertain_submission_is_never_retried(self):
        self.client.send_follow_up.side_effect = desktop.DesktopSubmissionUnknown('lost acknowledgement')
        report = self.run_check()
        self.assertFalse(report['success'])
        self.assertEqual(report['desktop_attempts'], 1)
        self.client.send_follow_up.assert_called_once()

    def test_wrong_telegram_chat_is_not_reported_as_delivery(self):
        self.api.side_effect = [self.telegram('', 'getMe'), {'message_id': 70, 'chat': {'id': 456}}]
        report = self.run_check()
        self.assertFalse(report['success'])
        self.assertEqual(report['checks']['desktop_delivery'], 'passed')
        self.assertEqual(report['checks']['telegram_delivery'], 'unconfirmed')

    def test_legacy_install_check_uses_live_desktop_checker_without_starting_backend(self):
        with patch.object(check, 'check_connection', return_value={'success': True}) as live, \
             patch.object(check, 'print_result'), patch.object(bot, 'CodexAppServer') as backend:
            self.assertEqual(bot.check_install(False), 0)
        live.assert_called_once_with(send_telegram=False)
        backend.assert_not_called()


if __name__ == '__main__':
    unittest.main()
