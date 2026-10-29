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

    def test_owner_lost_after_recovery_does_not_mark_answer_submitted(self):
        row = self.observed()
        self.bot._desktop_follow_up.return_value = None
        with self.assertRaises(d.DesktopIpcUnavailable):
            self.manager.submit(row['key'], 'First')
        self.assertEqual(self.manager.get(row['key'])['status'], 'waiting')
        self.assertIsNone(self.manager.get(row['key'])['answer'])

    def test_question_card_uses_task_metadata_and_preserves_answer_layout(self):
        import sqlite3
        from contextlib import closing
        from codex_telegram_bridge.telegram import notifications as n
        root = self.manager.database.parent
        with closing(sqlite3.connect(root / 'state_5.sqlite')) as db, db:
            db.execute('CREATE TABLE threads(id TEXT,name TEXT,title TEXT,cwd TEXT)')
            db.execute('INSERT INTO threads VALUES(?,?,?,?)',
                       (self.thread, '排查 Codex 沙盒损坏', '旧标题', 'D:/workspace/codex'))
        with patch.object(n, 'CODEX_STATE_ROOT', root):
            row = self.observed()
            self.assertTrue(self.bot.send.call_args.args[0].startswith('✒️ <b>问题 · 排查 Codex 沙盒损坏</b> (待回答)'))
            row.update(status='answered', answer='保留原问题、我的回答和状态')
            self.manager.refresh_card(row)
        text = self.bot.enqueue_edit.call_args.args[1]
        self.assertTrue(text.startswith('✒️ <b>问题 · 排查 Codex 沙盒损坏</b> (已回答)'))
        self.assertIn('<code>codex</code>   ·   <code>task-123</code>', text)
        self.assertIn('<blockquote>Which version?</blockquote>', text)
        self.assertIn('<blockquote><b>保留原问题、我的回答和状态</b></blockquote>', text)
        self.assertEqual(self.bot.enqueue_edit.call_args.kwargs, {'parse_mode': 'HTML'})

    def test_question_card_metadata_fallback_does_not_use_robot_working_directory(self):
        from codex_telegram_bridge.telegram import notifications as n
        row = self.observed()
        with patch.object(n, 'CODEX_STATE_ROOT', self.manager.database.parent / 'missing'):
            heading = self.manager.card_heading(row, '已回答')
        self.assertIn('未知工作区', heading)
        self.assertIn('task-123', heading)

    def test_rich_card_escapes_user_content_and_bounds_visible_utf16_length(self):
        from xml.etree import ElementTree
        from codex_telegram_bridge.telegram import notifications as n
        row = self.observed()
        row.update(status='answered', title='<script>& "问题" 🥳' * 700,
                   answer='</blockquote><b>🥲&' * 900)
        with patch.object(n, 'conversation_title', return_value='<标题> & stuff'):
            text, full = self.manager.render_card(row)
        tree = ElementTree.fromstring('<root>' + text + '</root>')
        visible = ''.join(tree.itertext())
        self.assertLess(len(visible.encode('utf-16-le')) // 2, 4096)
        self.assertNotIn('<script>', text)
        self.assertIn('&lt;标题&gt; &amp; stuff', text)
        self.assertEqual(len(list(tree.iter('blockquote'))), 2)
        self.assertIn(row['title'], full)
        self.assertIn(row['answer'], full)

    def test_html_send_and_queued_edit_preserve_native_entities_and_buttons(self):
        from codex_telegram_bridge.telegram import notifications as n
        row = self.observed()
        row.update(status='answered', answer='Second & <literal>')
        text, _ = self.manager.render_card(row)
        keyboard = {'inline_keyboard': []}
        with patch.object(n, 'telegram_call', return_value={'message_id': 70}) as call:
            b.TelegramCodexBot.send(self.bot, text, keyboard, parse_mode='HTML')
            self.assertEqual(call.call_args.args[1], 'sendMessage')
            self.assertEqual(call.call_args.args[2]['parse_mode'], 'HTML')
            self.assertEqual(call.call_args.args[2]['text'], text)
            b.TelegramCodexBot.enqueue_edit(self.bot, 70, text, keyboard, parse_mode='HTML')
            self.bot.outbox.put(None)
            self.bot._outbox_worker()
            self.assertEqual(call.call_args.args[1], 'editMessageText')
            self.assertEqual(call.call_args.args[2]['parse_mode'], 'HTML')
            self.assertEqual(call.call_args.args[2]['text'], text)
            self.assertEqual(call.call_args.args[2]['reply_markup'], keyboard)

    def test_quiet_mode_suppresses_html_card_edits(self):
        row = self.observed()
        text, _ = self.manager.render_card(row)
        self.bot.state.update(quiet_mode=True)
        b.TelegramCodexBot.enqueue_edit(self.bot, 70, text, {'inline_keyboard': []}, parse_mode='HTML')
        self.assertTrue(self.bot.outbox.empty())

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
        self.assertIn('First &amp; Second', self.bot.enqueue_edit.call_args.args[1])
        self.assertIn('Which version?', self.bot.enqueue_edit.call_args.args[1])
        self.assertIn('</b> (已回答)', self.bot.enqueue_edit.call_args.args[1])
        self.assertEqual(self.bot.enqueue_edit.call_args.args[2], {'inline_keyboard': []})
        self.bot._desktop_follow_up.assert_not_called()

    def test_uncertain_submission_is_durable_and_never_retried(self):
        row = self.observed()
        self.bot._desktop_follow_up.side_effect = d.DesktopSubmissionUnknown('lost acknowledgement')
        with self.assertRaises(d.DesktopSubmissionUnknown):
            self.manager.submit(row['key'], 'First')
        restarted = inputs.AsyncInputs(self.bot, self.manager.database)
        self.assertEqual(restarted.get(row['key'])['status'], 'uncertain')
        self.assertEqual(restarted.get(row['key'])['answer'], 'First')
        self.assertIn('</b> (待确认)', self.bot.enqueue_edit.call_args.args[1])
        self.assertIn('First', self.bot.enqueue_edit.call_args.args[1])
        with self.assertRaises(ValueError):
            restarted.submit(row['key'], 'First')
        self.bot._desktop_follow_up.assert_called_once()

    def test_choice_answer_survives_restart_and_turn_end(self):
        row = self.observed()
        self.manager.callback(row['key'] + ':1')
        self.assertIn('Second', self.bot.enqueue_edit.call_args.args[1])
        self.assertIn('Which version?', self.bot.enqueue_edit.call_args.args[1])
        self.assertIn('</b> (已提交)', self.bot.enqueue_edit.call_args.args[1])
        restarted = inputs.AsyncInputs(self.bot, self.manager.database)
        self.assertEqual(restarted.get(row['key'])['answer'], 'Second')
        self.bot.desktop.native.read_thread.return_value['turns'][0]['status'] = 'completed'
        restarted.reconcile(self.thread, restarted.current_history(self.thread))
        self.assertIn('Second', self.bot.enqueue_edit.call_args.args[1])
        self.assertIn('</b> (已结束)', self.bot.enqueue_edit.call_args.args[1])
        self.assertEqual(self.bot.enqueue_edit.call_args.args[2], {'inline_keyboard': []})

    def test_ended_unanswered_question_keeps_title_without_inventing_answer(self):
        self.observed()
        self.bot.desktop.native.read_thread.return_value['turns'][0]['status'] = 'completed'
        self.manager.reconcile(self.thread, self.manager.current_history(self.thread))
        text = self.bot.enqueue_edit.call_args.args[1]
        self.assertIn('Which version?', text)
        self.assertIn('未记录到回答', text)
        self.assertNotIn('已回答', text)

    def test_native_receipt_can_replace_uncertain_local_answer(self):
        row = self.observed()
        self.bot._desktop_follow_up.side_effect = d.DesktopSubmissionUnknown('lost acknowledgement')
        with self.assertRaises(d.DesktopSubmissionUnknown):
            self.manager.submit(row['key'], 'First')
        answer = inputs.REPLY_OPEN + json.dumps([{'questionItemId': inputs.question_id(self.item, 0),
            'answer': 'Desktop choice'}]) + inputs.REPLY_CLOSE
        self.history['thread']['turns'][0]['items'].append({'type': 'userMessage',
            'content': [{'type': 'text', 'text': answer}]})
        self.manager.reconcile(self.thread, self.manager.current_history(self.thread))
        self.assertEqual(self.manager.get(row['key'])['answer'], 'Desktop choice')
        self.assertIn('</b> (已回答)', self.bot.enqueue_edit.call_args.args[1])
        self.bot._desktop_follow_up.assert_called_once()

    def test_long_answer_kept_in_database_and_full_reply_cache(self):
        from codex_telegram_bridge.telegram import notifications as n
        row = self.observed()
        answer = '我的回答\n' * 1000
        with patch.object(n, 'REPLY_CACHE_DIR', self.manager.database.parent / 'cache'):
            self.manager.submit(row['key'], answer)
            self.assertEqual(self.manager.get(row['key'])['answer'], answer)
            text, markup = self.bot.enqueue_edit.call_args.args[1:]
            self.assertLess(len(text), 3900)
            callback = markup['inline_keyboard'][0][0]['callback_data']
            cached = n.load_cached_reply(callback.split('|')[1])
            self.assertIn(answer.rstrip(), cached['text'])

    def test_clipped_emoji_answer_has_full_text_below_normal_cache_threshold(self):
        from codex_telegram_bridge.telegram import notifications as n
        row = self.observed()
        row.update(status='answered', answer='🥳' * 750)
        with patch.object(n, 'REPLY_CACHE_DIR', self.manager.database.parent / 'cache'):
            self.manager.refresh_card(row)
            text, markup = self.bot.enqueue_edit.call_args.args[1:]
            self.assertIn('…', text)
            callback = markup['inline_keyboard'][0][0]['callback_data']
            cached = n.load_cached_reply(callback.split('|')[1])
            self.assertLess(len(cached['text']), n.REPLY_PREVIEW_LIMIT)
            self.assertIn(row['answer'], cached['text'])

    def test_reaction_feedback_does_not_replace_answer_card_with_generic_text(self):
        row = self.observed()
        self.bot._reaction.return_value = True
        self.bot._ui_local.message_id = 70
        self.bot.edit_message = Mock()
        self.manager.submit(row['key'], 'Second')
        self.bot.edit_message.assert_not_called()
        self.assertEqual(self.bot._ui_local.message_id, 70)
        self.assertIn('Second', self.bot.enqueue_edit.call_args.args[1])

    def test_english_card_keeps_answer_in_original_language(self):
        from codex_telegram_bridge.i18n import using_language
        row = self.observed()
        with using_language('en'):
            self.manager.submit(row['key'], '保留中文回答')
        text = self.bot.enqueue_edit.call_args.args[1]
        self.assertIn('Question ·', text)
        self.assertIn('</b> (Submitted)', text)
        self.assertIn('User answer', text)
        self.assertIn('保留中文回答', text)

    def test_existing_database_migrates_without_losing_question_routes(self):
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(self.manager.database)) as db, db:
            db.execute('''CREATE TABLE questions(bot TEXT,chat TEXT,key TEXT,thread TEXT,turn TEXT,item TEXT,
                qindex INTEGER,title TEXT,options TEXT,status TEXT,message INTEGER,created REAL,
                PRIMARY KEY(bot,chat,thread,item,qindex),UNIQUE(bot,chat,key))''')
            db.execute('INSERT INTO questions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                ('111','123','existing',self.thread,self.turn,self.item,0,'Which version?','[]','publishing',70,inputs.time.time()))
        row = self.manager.get('existing')
        self.assertEqual(row['message'], 70)
        self.assertIsNone(row['answer'])
        self.manager.submit('existing', 'First')
        self.assertEqual(self.manager.get('existing')['answer'], 'First')

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

    def test_opening_async_text_input_does_not_complete_question_feedback(self):
        from types import SimpleNamespace
        from codex_telegram_bridge.storage.bridge import BridgeState
        from codex_telegram_bridge.telegram.feedback import Feedback
        from codex_telegram_bridge.telegram import notifications

        row = self.observed()
        self.bot._answer_callback = Mock()
        state = BridgeState(self.manager.database.parent / 'feedback.sqlite3')
        self.addCleanup(state.close)
        feedback = Feedback(state, self.bot.token, self.bot.chat_id, Mock(), Mock())
        for durable in (False, True):
            with self.subTest(durable=durable), patch.object(
                    notifications, 'set_task_reaction', return_value=True) as reaction:
                self.bot.delivery = SimpleNamespace(feedback=feedback) if durable else None
                sources = []
                self.bot.send.side_effect = lambda *a, **kw: sources.append(
                    self.bot._ui_local.source_message_id) or {'message_id': 71}
                self.bot.handle_update({'update_id': 91 + int(durable), 'callback_query': {
                    'id': 'open-text', 'from': {'id': 123, 'is_bot': False},
                    'data': 'asyncq|' + row['key'] + ':other',
                    'message': {'message_id': 70, 'chat': {'id': 123, 'type': 'private'}}}})
                self.assertEqual(sources, [70])
                self.assertEqual(self.bot.pending_free_text[self.bot.chat_id], 'async:' + row['key'])
                self.assertEqual(self.manager.get(row['key'])['status'], row['status'])
                self.assertEqual(state.feedback_records(), [])
                reaction.assert_not_called()
                self.bot._desktop_follow_up.assert_not_called()

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
