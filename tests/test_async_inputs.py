"""Durable async-question routing and at-most-once submission regressions."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import codex_telegram_bridge.features.async_inputs as inputs
import codex_telegram_bridge.codex.desktop as d
import codex_telegram_bridge.app as b
class AsyncInputTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.bot = b.TelegramCodexBot('111:fixture', {'chat_id': '123'})
        self.bot.state = b.StateStore(Path(temp.name) / 'state.json')
        self.bot.app = Mock()
        self.bot.desktop = Mock()
        self.bot.send = Mock(return_value={'message_id': 70})
        self.bot.enqueue_edit = Mock()
        self.bot._reaction = Mock(return_value=False)
        self.bot._desktop_follow_up = Mock(return_value={'route': 'desktop-user-input', 'turn_id': ''})
        self.thread, self.turn, self.item = 'task-123456', 'turn-123456', 'question-item'
        self.message = {'type': 'agentMessage', 'id': self.item, 'delivery': 'async',
                        'phase': 'commentary', 'text': '',
                        'questions': [{'title': 'Which version?', 'options': ['First', 'Second']}]}
        self.history = {'thread': {'id': self.thread, 'historyMode': 'paginated', 'turns': [
            {'id': self.turn, 'status': 'interrupted', 'items': [self.message]}]}}
        self.bot.desktop.native.read_thread.return_value = {'thread': {'id': self.thread},
            'turns': [{'id': self.turn, 'status': 'inProgress'}]}
        self.bot._owns_thread = Mock(return_value=False)
        self.manager = inputs.AsyncInputs(self.bot, Path(temp.name) / 'async.sqlite3')
        self.bot.async_inputs = self.manager
        history = patch.object(inputs, 'read_thread_history', side_effect=lambda *a, **kw: copy.deepcopy(self.history))
        history.start()
        self.addCleanup(history.stop)

    def observed(self):
        self.manager.observe(self.thread, self.turn, self.message)
        self.manager.publish()
        with self.manager.db() as db:
            return dict(db.execute('SELECT * FROM questions').fetchone())

    def test_question_id_matches_desktop_canonical_serialization(self):
        self.assertEqual(inputs.question_id('item', 0), '["request_user_input_async","item",0]')

    def test_duplicate_events_and_restart_do_not_resend_question(self):
        self.observed()
        self.manager.observe(self.thread, self.turn, self.message)
        self.manager.publish()
        inputs.AsyncInputs(self.bot, self.manager.database).publish()
        self.bot.send.assert_called_once()

    def test_unconfirmed_telegram_delivery_is_not_retried(self):
        self.bot.send.side_effect = TimeoutError('lost acknowledgement')
        self.manager.observe(self.thread, self.turn, self.message)
        self.manager.publish()
        self.manager.publish()
        self.bot.send.assert_called_once()

    def test_desktop_runtime_overrides_other_readers_interrupted_status(self):
        history = self.manager.current_history(self.thread)
        self.assertEqual(history['thread']['turns'][-1]['status'], 'inProgress')
        self.bot.app.request.assert_not_called()

    def test_direct_reply_is_bound_to_the_original_question_and_sent_once(self):
        row = self.observed()
        self.assertTrue(self.manager.reply_to(70, 'Second'))
        args = self.bot._desktop_follow_up.call_args
        self.assertEqual(args.args[0], self.thread)
        self.assertFalse(args.kwargs['start_if_inactive'])
        self.assertEqual(args.kwargs['expected_turn_id'], self.turn)
        text = args.args[1]
        parsed = json.loads(text[len(inputs.REPLY_OPEN):-len(inputs.REPLY_CLOSE)])
        self.assertEqual(parsed, [{'questionItemId': inputs.question_id(self.item, 0),
                                  'question': 'Which version?', 'answer': 'Second'}])
        with self.assertRaises(ValueError):
            self.manager.callback(row['key'] + ':1')
        self.bot._desktop_follow_up.assert_called_once()
        self.bot.app.request.assert_not_called()

    def test_other_bot_or_chat_cannot_use_the_question_key(self):
        row = self.observed()
        other = b.TelegramCodexBot('222:fixture', {'chat_id': '123'})
        isolated = inputs.AsyncInputs(other, self.manager.database)
        self.assertIsNone(isolated.get(row['key']))
        with self.assertRaises(ValueError):
            isolated.callback(row['key'] + ':0')
        self.bot._desktop_follow_up.assert_not_called()

    def test_changed_or_ended_turn_never_receives_an_answer(self):
        row = self.observed()
        self.bot.desktop.native.read_thread.return_value['turns'][0]['status'] = 'completed'
        with self.assertRaises(ValueError):
            self.manager.submit(row['key'], 'First')
        self.assertEqual(self.manager.get(row['key'])['status'], 'expired')
        self.bot._desktop_follow_up.assert_not_called()

    def test_question_content_change_is_rejected(self):
        row = self.observed()
        self.history['thread']['turns'][0]['items'][0]['questions'][0]['title'] = 'Different question'
        with self.assertRaises(ValueError):
            self.manager.submit(row['key'], 'First')
        self.bot._desktop_follow_up.assert_not_called()

    def test_answered_in_desktop_is_not_submitted_again(self):
        row = self.observed()
        answer = inputs.REPLY_OPEN + json.dumps([{'questionItemId': inputs.question_id(self.item, 0),
            'question': 'Which version?', 'answer': 'First'}]) + inputs.REPLY_CLOSE
        self.history['thread']['turns'][0]['items'].append({'id': 'user-answer', 'type': 'userMessage',
            'content': [{'type': 'text', 'text': answer}]})
        with self.assertRaises(ValueError):
            self.manager.submit(row['key'], 'Second')
        self.assertEqual(self.manager.get(row['key'])['status'], 'answered')
        self.bot._desktop_follow_up.assert_not_called()

    def test_escaped_native_answer_is_resolved_and_disables_old_card(self):
        row = self.observed()
        answer = inputs.REPLY_OPEN + json.dumps([{'questionItemId': inputs.question_id(self.item, 0),
            'question': 'Which version?', 'answer': 'First & Second'}]) + inputs.REPLY_CLOSE
        escaped = answer.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        self.history['thread']['turns'][0]['items'].append({'id': 'delegated-answer',
            'type': 'functionCallOutput', 'namespace': 'codex_app', 'name': 'send_message_to_thread',
            'output': '<codex_delegation>\n  <input>' + escaped + '</input>\n</codex_delegation>'})
        self.manager.reconcile(self.thread, self.manager.current_history(self.thread))
        self.assertEqual(self.manager.get(row['key'])['status'], 'answered')
        self.bot.enqueue_edit.assert_called_with(70, '该异步问题已回答或结束。', {'inline_keyboard': []})
        self.bot._desktop_follow_up.assert_not_called()

    def test_uncertain_submission_is_durable_and_never_retried(self):
        row = self.observed()
        self.bot._desktop_follow_up.side_effect = d.DesktopSubmissionUnknown('lost acknowledgement')
        with self.assertRaises(d.DesktopSubmissionUnknown):
            self.manager.submit(row['key'], 'First')
        restarted = inputs.AsyncInputs(self.bot, self.manager.database)
        self.assertEqual(restarted.get(row['key'])['status'], 'uncertain')
        with self.assertRaises(ValueError):
            restarted.submit(row['key'], 'First')
        self.bot._desktop_follow_up.assert_called_once()

    def test_telegram_feedback_failure_does_not_turn_accepted_answer_into_failure(self):
        row = self.observed()
        self.bot.send.side_effect = TimeoutError('lost feedback acknowledgement')
        with patch.object(b, 'log'):
            self.manager.submit(row['key'], 'First')
        self.assertEqual(self.manager.get(row['key'])['status'], 'submitted')
        self.bot._desktop_follow_up.assert_called_once()

    def test_real_callback_shape_preserves_question_message_as_reply_source(self):
        row = self.observed()
        captured = []
        self.bot._desktop_follow_up.side_effect = lambda *a, **kw: captured.append(
            self.bot._ui_local.source_message_id) or {'route': 'desktop-user-input'}
        self.bot._operation_reaction = Mock()
        self.bot._finish_feedback = Mock()
        self.bot._answer_callback = Mock()
        self.bot.handle_update({'update_id': 1, 'callback_query': {'id': 'callback-fixture',
            'from': {'id': 123, 'is_bot': False}, 'data': 'asyncq|' + row['key'] + ':1',
            'message': {'message_id': 70, 'chat': {'id': 123, 'type': 'private'},
                        'from': {'id': 111, 'is_bot': True}}}})
        self.assertEqual(captured, [70])

    def test_bot_owned_turn_uses_its_existing_server_without_resuming(self):
        row = self.observed()
        self.bot._owns_thread.return_value = True
        self.bot.active_turns[self.thread] = self.turn
        self.manager.submit(row['key'], 'First')
        self.bot._desktop_follow_up.assert_not_called()
        self.bot.app.request.assert_called_once()
        args = self.bot.app.request.call_args
        self.assertEqual(args.args[0], 'turn/steer')
        self.assertEqual(args.args[1]['expectedTurnId'], self.turn)

    def test_reply_to_expired_card_does_not_fall_through_to_normal_task_message(self):
        row = self.observed()
        with self.manager.db() as db:
            db.execute("UPDATE questions SET status='expired' WHERE key=?", (row['key'],))
        self.bot.submit_direct_reply = Mock()
        with self.assertRaises(ValueError):
            self.bot.handle_text('answer', reply_to_message_id=70)
        self.bot.submit_direct_reply.assert_not_called()


if __name__ == '__main__':
    unittest.main()
