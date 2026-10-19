"""Focused protocol regressions. External communication is mocked."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import codex_telegram_bridge.codex.desktop as d
import codex_telegram_bridge.app as b
import codex_telegram_bridge.telegram.notifications as n
from codex_telegram_bridge.features.elicitation import form_questions, form_content
from codex_telegram_bridge.codex.history import read_thread_history


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bot = b.TelegramCodexBot('111:fixture', {'chat_id': '123'})
        self.bot.app = Mock()
        self.bot.send = Mock()
        self.bot.enqueue = Mock()
        self.bot.enqueue_edit = Mock()
        self.bot.async_inputs.database = Path(self.temp.name) / 'async.sqlite3'

    def test_history_uses_pages_and_preserves_chronological_order(self):
        app = Mock()
        def request(method, params, **kwargs):
            self.assertFalse(params.get('includeTurns'))
            if method == 'thread/read':
                return {'thread': {'id': 'task', 'historyMode': 'paginated'}}
            if method == 'thread/turns/list':
                return {'data': [{'id': 'turn-2'}, {'id': 'turn-1'}]}
            return {'data': [{'turnId': params['turnId'], 'item': {'id': params['turnId']+'-item'}}]}
        app.request.side_effect = request
        result = read_thread_history(app, 'task')['thread']
        self.assertEqual([turn['id'] for turn in result['turns']], ['turn-1', 'turn-2'])
        self.assertEqual(result['turns'][-1]['items'][0]['id'], 'turn-2-item')

    def test_wrong_turn_item_and_duplicate_cursor_are_rejected(self):
        for pages in (
            [{'data': [{'turnId': 'wrong', 'item': {}}]}],
            [{'data': [], 'nextCursor': 'same'}, {'data': [], 'nextCursor': 'same'}],
        ):
            app = Mock()
            app.request.side_effect = [{'thread': {'id': 'task', 'historyMode': 'paginated'}},
                                       {'data': [{'id': 'turn'}]}, *pages]
            with self.assertRaises(ValueError):
                read_thread_history(app, 'task')

    def test_bounded_history_reports_truncation(self):
        app = Mock()
        app.request.side_effect = [{'thread': {'historyMode': 'paginated'}},
            {'data': [{'id': 'turn'}]},
            {'data': [{'turnId': 'turn', 'item': {'id': 'last'}}], 'nextCursor': 'more'}]
        result = read_thread_history(app, 'task', item_limit=1)['thread']
        self.assertTrue(result['_historyTruncated'])
        self.assertEqual(app.request.call_count, 3)

    def test_normal_json_is_preserved_even_with_internal_shape(self):
        with patch.object(n, '_stored_thread_purpose', return_value=('stored', 'user')):
            for body in ('{"title":"Valid data","description":"Requested JSON"}', '{"exclude":[]}'):
                payload = {'thread-id': 'user-task', 'threadSource': 'user', 'last-assistant-message': body}
                self.assertFalse(n.is_internal_task_metadata_completion(payload))
                self.assertFalse(n.is_empty_internal_exclusion_completion(payload))
                self.assertFalse(n.is_internal_suggestions_completion(payload))

    def test_attributed_internal_output_still_skips_without_body_heuristics(self):
        self.assertTrue(n.is_internal_task_metadata_completion({'threadSource': 'ambient_suggestions'}))
        self.assertTrue(n.is_empty_internal_exclusion_completion({'threadSource': 'ambient_suggestion_safety'}))

    def test_resolved_request_only_removes_matching_thread_and_request(self):
        self.bot.pending_questions = {
            'one': {'id': 7, 'params': {'threadId': 'task'}, 'message_id': 40},
            'other': {'id': 7, 'params': {'threadId': 'other-task'}},
            'still': {'id': 8, 'params': {'threadId': 'task'}}}
        self.bot.pending_free_text['123'] = 'one'
        self.bot.on_app_event({'method': 'serverRequest/resolved', 'params': {'threadId': 'task', 'requestId': 7}})
        self.assertEqual(set(self.bot.pending_questions), {'other', 'still'})
        self.assertNotIn('123', self.bot.pending_free_text)
        self.bot.enqueue_edit.assert_called_once_with(40, '该提问或审批已结束。', {'inline_keyboard': []})

    def test_old_transport_prompt_cannot_answer_new_process(self):
        self.bot.pending_questions['q'] = {'id': 1, 'params': {'threadId': 'task'}, 'process': object()}
        with self.assertRaises(ValueError):
            self.bot._handle_question_callback('q:0')
        self.bot.app.answer.assert_not_called()

    def test_nonblocking_prompt_is_labelled_and_expiry_rejects_answer(self):
        self.bot.on_server_request({'id': 1, 'method': 'item/tool/requestUserInput', 'params': {
            'threadId': 'task', 'turnId': 'turn', 'isBlocking': False,
            'questions': [{'id': 'q', 'header': 'Choose', 'question': 'Pick one', 'options': []}]}})
        self.assertIn('任务仍可继续', self.bot.enqueue.call_args.args[0])
        seq = next(iter(self.bot.pending_questions))
        self.bot.pending_questions[seq]['expires_at'] = 0
        with self.assertRaises(ValueError):
            self.bot._answer_question(seq, 'answer')
        self.bot.app.answer.assert_not_called()

    def test_restart_nonce_does_not_reuse_old_callback_sequence(self):
        other = b.TelegramCodexBot('111:fixture', {'chat_id': '123'})
        self.assertNotEqual(self.bot._new_sequence(), other._new_sequence())

    def test_time_request_and_unsupported_verification_have_typed_responses(self):
        with patch.object(b.time, 'time', return_value=123456):
            self.bot.on_server_request({'id': 3, 'method': 'currentTime/read', 'params': {'threadId': 'task'}})
        self.bot.app.answer.assert_called_with(3, {'currentTimeAt': 123456})
        self.bot.on_server_request({'id': 4, 'method': 'mcpServer/elicitation/request',
                                   'params': {'threadId': 'task', 'mode': 'openai/userVerification'}})
        self.bot.app.answer.assert_called_with(4, {'action': 'decline', 'content': None})
        self.bot.app.answer_error.assert_not_called()

    def test_scalar_form_rejects_invalid_answer_before_advancing(self):
        params = {'threadId': 'task', 'mode': 'form', 'requestedSchema': {'type': 'object',
            'properties': {'count': {'type': 'integer', 'minimum': 1, 'maximum': 5}}, 'required': ['count']}}
        self.bot.on_server_request({'id': 5, 'method': 'mcpServer/elicitation/request', 'params': params})
        seq = next(iter(self.bot.pending_questions))
        with self.assertRaises(ValueError):
            self.bot._answer_question(seq, '99')
        self.assertEqual(self.bot.pending_questions[seq]['index'], 0)
        self.bot._reaction = Mock()
        self.bot._answer_question(seq, '3')
        self.bot.app.answer.assert_called_once_with(5, {'action': 'accept', 'content': {'count': 3}})

    def test_stop_requires_the_exact_confirmed_interrupted_turn(self):
        ipc = b.CodexDesktopIpc()
        ipc._open = Mock(return_value=55)
        ipc._close = Mock()
        for result, error in (({'ok': True, 'interruptedTurnId': None}, d.DesktopIpcInactive),
                              ({'ok': True}, d.DesktopSubmissionUnknown),
                              ({'interruptedTurnId': 'other-turn'}, d.DesktopSubmissionUnknown)):
            with self.subTest(result=result):
                ipc._request = Mock(side_effect=[{'result': {'clientId': 'client'}},
                    {'handledByClientId': 'owner'}, {'result': result}])
                with self.assertRaises(error):
                    ipc.interrupt_turn('task', 'expected-turn')
                self.assertEqual(ipc._request.call_count, 3)
        ipc._request = Mock(side_effect=[{'result': {'clientId': 'client'}},
            {'handledByClientId': 'owner'}, {'result': {'interruptedTurnId': 'expected-turn'}}])
        self.assertTrue(ipc.interrupt_turn('task', 'expected-turn'))

    def test_unavailable_stop_is_not_offered_and_no_backend_is_resumed(self):
        self.bot.desktop = Mock()
        self.bot.desktop.can_interrupt.return_value = False
        self.bot._queue_direct_reply_choice('task', 'continue', 'turn')
        buttons = self.bot.send.call_args.args[1]['inline_keyboard']
        self.assertFalse(any(button['callback_data'].endswith(':interrupt') for row in buttons for button in row))
        self.bot.app.request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
