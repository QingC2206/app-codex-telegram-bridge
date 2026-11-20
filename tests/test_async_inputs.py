"""Durable async-question routing and at-most-once submission regressions."""
import copy
from html import escape
import json
import os
from pathlib import Path
import sqlite3
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

    def rollout_fixture(self, suffix=''):
        root = self.manager.database.parent / 'sessions'
        root.mkdir()
        self.thread = '12345678-1234-1234-1234-123456789abc'
        self.history['thread']['id'] = self.thread
        self.bot.desktop.native.read_thread.return_value['thread']['id'] = self.thread
        self.bot._list_threads = Mock(side_effect=AssertionError('unexpected global task poll'))
        session_root = patch.object(b, 'SESSION_ROOT', root)
        session_root.start()
        self.addCleanup(session_root.stop)
        return root / f'rollout-fixture-{self.thread}{suffix}.jsonl'

    def rollout_event(self, kind, **fields):
        payload = dict(type=kind, thread_id=self.thread, turn_id=self.turn, **fields)
        return (json.dumps({'type': 'event_msg', 'payload': payload}) + '\n').encode()

    def rollout_question(self):
        return self.rollout_event('item_completed', item=dict(self.message, type='AgentMessage'))

    def test_old_rewritten_file_recovers_waiting_question_without_hook(self):
        path = self.rollout_fixture('_rewrite-generation')
        path.write_bytes(self.rollout_question())
        stamp = inputs.time.time() - 35 * 60
        os.utime(path, (stamp, stamp))
        self.manager.poll()
        inputs.AsyncInputs(self.bot, self.manager.database).poll()
        self.bot.send.assert_called_once()
        self.bot._list_threads.assert_not_called()

    def test_question_before_cold_tail_is_recovered_but_closed_archive_stays_idle(self):
        path = self.rollout_fixture()
        padding = self.rollout_event('other', text='x' * (300 * 1024))
        path.write_bytes(self.rollout_question() + padding + self.rollout_event('task_complete'))
        self.manager.poll()
        self.bot.desktop.native.read_thread.assert_not_called()
        self.bot.send.assert_not_called()
        path.write_bytes(self.rollout_question() + padding)
        self.manager.poll()
        self.bot.send.assert_called_once()

    def test_pending_read_survives_outage_restart_and_rollout_removal(self):
        path = self.rollout_fixture()
        path.write_bytes(self.rollout_question())
        clock = inputs.time.time()
        with patch.object(inputs.time, 'time', return_value=clock) as wall:
            self.manager.current_history = Mock(side_effect=TimeoutError)
            self.manager.poll()
            path.unlink()
            for elapsed in (5, 15, 35, 75):
                wall.return_value = clock + elapsed
                self.manager.poll()
            with self.manager.db() as db:
                row = db.execute('SELECT * FROM observations').fetchone()
                self.assertEqual((row['thread'], row['turn'], row['item']), (self.thread, self.turn, self.item))
                self.assertGreaterEqual(row['attempts'], 4)
            self.bot.send.assert_not_called()
            wall.return_value = clock + 245
            restarted = inputs.AsyncInputs(self.bot, self.manager.database)
            restarted.poll()
            restarted.poll()
            self.bot.send.assert_called_once()
            with restarted.db() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM observations').fetchone()[0], 0)

    def test_failed_journal_write_does_not_acknowledge_rollout(self):
        path = self.rollout_fixture()
        path.write_bytes(self.rollout_question())
        with patch.object(self.manager, '_queue_observation', side_effect=sqlite3.OperationalError):
            with self.assertRaises(sqlite3.OperationalError):
                self.manager.poll()
        self.bot.send.assert_not_called()
        self.assertNotIn(path, self.manager.rollouts.cursors)
        self.manager.poll()
        self.bot.send.assert_called_once()

    def test_delegated_answer_signal_retires_original_buttons_during_active_turn(self):
        path = self.rollout_fixture()
        path.write_bytes(self.rollout_question())
        self.manager.poll()
        answer = inputs.REPLY_OPEN + json.dumps([{'questionItemId': inputs.question_id(self.item, 0),
                                                  'answer': 'Second'}]) + inputs.REPLY_CLOSE
        receipt = dict(id='result', namespace='codex_app', name='send_message_to_thread',
                       output='<codex_delegation><input>' + escape(answer, quote=False) + '</input></codex_delegation>')
        self.history['thread']['turns'][0]['items'].append(dict(receipt, type='functionCallOutput'))
        with path.open('ab') as stream:
            stream.write((json.dumps({'type': 'response_item',
                                     'payload': dict(receipt, type='function_call_output')}) + '\n').encode())
        self.manager.poll()
        with self.manager.db() as db:
            self.assertEqual(tuple(db.execute('SELECT status,answer FROM questions').fetchone()), ('answered', 'Second'))
        self.assertEqual(self.bot.enqueue_edit.call_args.args[2], {'inline_keyboard': []})
        self.bot.send.assert_called_once()
        self.bot._desktop_follow_up.assert_not_called()

    def test_caught_up_observer_performs_no_history_or_file_reads_while_idle(self):
        path = self.rollout_fixture()
        path.write_bytes(self.rollout_event('task_complete'))
        self.manager.poll()
        now = inputs.time.monotonic()
        with patch.object(self.manager.rollouts, '_read', wraps=self.manager.rollouts._read) as read, \
                patch.object(inputs.time, 'monotonic', return_value=now) as clock:
            for _ in range(30):
                clock.return_value += 5
                self.manager.poll()
            read.assert_not_called()
        self.bot.desktop.native.read_thread.assert_not_called()
        self.bot._list_threads.assert_not_called()
        path.write_bytes(self.rollout_question())
        self.manager.poll()
        self.bot.send.assert_called_once()

    def test_answer_rechecks_older_pages_and_blocks_a_previously_answered_question(self):
        self.rollout_fixture()
        row = self.observed()
        latest = self.history['thread']['turns'][0]
        latest.update(items=[], _itemsTruncated=True, _olderItemsCursor='older')
        answer = inputs.REPLY_OPEN + json.dumps([{'questionItemId': inputs.question_id(self.item, 0),
                                                  'answer': 'First'}]) + inputs.REPLY_CLOSE
        receipt = {'id': 'answer', 'type': 'userMessage', 'content': [{'type': 'text', 'text': answer}]}
        self.bot.app.request.return_value = {'data': [{'turnId': self.turn, 'item': receipt},
                                                      {'turnId': self.turn, 'item': self.message}]}
        with self.assertRaises(ValueError):
            self.manager.submit(row['key'], 'Second')
        self.assertEqual(self.manager.get(row['key'])['status'], 'answered')
        self.bot._desktop_follow_up.assert_not_called()

    def test_cold_recovery_reads_older_typed_questions_from_the_verified_turn(self):
        path = self.rollout_fixture()
        path.write_bytes(self.rollout_question() + self.rollout_event('other', text='x' * 300000))
        self.history['thread']['turns'][0].update(items=[], _itemsTruncated=True, _olderItemsCursor='old')
        self.bot.app.request.return_value = {'data': [{'turnId': self.turn, 'item': self.message}]}
        self.manager.poll()
        self.bot.send.assert_called_once()
        params = self.bot.app.request.call_args.args[1]
        self.assertEqual((params['threadId'], params['turnId'], params['cursor']), (self.thread, self.turn, 'old'))

    def test_pre_tool_hint_retries_when_only_a_previous_question_is_visible(self):
        self.rollout_fixture()
        clock = inputs.time.time()
        with patch.object(inputs.time, 'time', return_value=clock) as wall:
            self.observed()
            wall.return_value = clock + 2
            with self.manager.db() as db:
                db.execute('INSERT INTO hints VALUES(?,?)', (self.thread, clock + 2))
            self.manager.poll()
            self.bot.send.assert_called_once()
            self.history['thread']['turns'][0]['items'].append(dict(self.message, id='new-question'))
            wall.return_value = clock + 10
            self.manager.poll()
            self.assertEqual(self.bot.send.call_count, 2)

    def test_lagging_turn_is_retried_and_proven_older_turn_is_retired(self):
        path = self.rollout_fixture()
        path.write_bytes(self.rollout_question())
        latest = self.history['thread']['turns'][0]
        latest.update(id='another-turn', items=[])
        self.bot.desktop.native.read_thread.return_value['turns'][0]['id'] = 'another-turn'
        self.bot.app.request.return_value = {'data': [{'id': 'another-turn'}]}
        clock = inputs.time.time()
        with patch.object(inputs.time, 'time', return_value=clock) as wall:
            self.manager.poll()
            with self.manager.db() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM observations').fetchone()[0], 1)
            self.bot.app.request.side_effect = [{'data': [{'id': 'another-turn'}], 'nextCursor': 'older'},
                                                {'data': [{'id': self.turn}]}]
            wall.return_value = clock + 10
            self.manager.poll()
            with self.manager.db() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM observations').fetchone()[0], 0)
        self.bot.send.assert_not_called()

    def test_partially_written_question_in_history_is_retried_with_the_same_id(self):
        path = self.rollout_fixture()
        path.write_bytes(self.rollout_question())
        latest = self.history['thread']['turns'][0]
        latest['items'] = [dict(self.message, questions=[])]
        clock = inputs.time.time()
        with patch.object(inputs.time, 'time', return_value=clock) as wall:
            self.manager.poll()
            self.bot.send.assert_not_called()
            latest['items'] = [self.message]
            wall.return_value = clock + 10
            self.manager.poll()
        self.bot.send.assert_called_once()

    def test_rollout_fallback_reads_only_new_questions_and_deduplicates(self):
        root = self.manager.database.parent / 'sessions'
        root.mkdir()
        thread = '12345678-1234-1234-1234-123456789abc'
        rollout = root / f'rollout-2026-11-17T01-00-00-{thread}.jsonl'
        ordinary = {"type": "event_msg", "payload": {"type": "item_completed", "thread_id": thread,
                    "turn_id": self.turn, "item": {"id": "ordinary", "type": "CommandExecution",
                    "questions": ["quoted by a tool"]}}}
        question = {"type": "event_msg", "payload": {"type": "item_completed", "thread_id": thread,
                    "turn_id": self.turn, "item": dict(self.message, type="AgentMessage")}}
        reply = {"type": "event_msg", "payload": {"type": "item_completed", "thread_id": thread,
                 "turn_id": self.turn, "item": {"id": "answer", "type": "UserMessage",
                 "content": [{"type": "text", "text": "<send_user_message_question_reply>"}]}}}
        def record(value):
            return json.dumps(value).encode("utf-8") + b"\n"

        self.history["thread"]["id"] = thread
        self.history["thread"]["turns"][0]["status"] = "inProgress"
        self.manager.current_history = Mock(return_value=self.history)
        self.bot._list_threads = Mock(side_effect=AssertionError("observer must not call thread/list"))
        rollout.write_bytes(record(ordinary))
        with patch.object(b, 'SESSION_ROOT', root):
            self.manager.poll()
            self.manager.current_history.assert_not_called()
            with rollout.open('ab') as stream:
                stream.write(record(ordinary) + record(question)[:-9])
            self.manager.poll()
            self.manager.current_history.assert_not_called()
            with rollout.open('ab') as stream:
                stream.write(record(question)[-9:])
            self.manager.poll()
            self.manager.current_history.assert_called_once_with(thread)
            self.bot.send.assert_called_once()
            self.manager.poll()
            with rollout.open('ab') as stream:
                stream.write(record(ordinary) + record(question))
            self.manager.poll()
            self.manager.current_history.assert_called_once_with(thread)
            self.bot.send.assert_called_once()
            with rollout.open('ab') as stream:
                stream.write(record(reply))
            self.manager.poll()
            self.assertEqual(self.manager.current_history.call_count, 2)
            self.bot.send.assert_called_once()

    def test_hook_hydrates_target_without_repeating_a_steady_hint(self):
        root = self.manager.database.parent / 'sessions'
        root.mkdir()
        with self.manager.db() as db:
            db.execute('INSERT INTO hints VALUES(?,?)', (self.thread, inputs.time.time()))
        self.manager.reconcile = Mock()
        self.manager.publish = Mock()
        self.bot._list_threads = Mock(side_effect=AssertionError('unexpected thread/list'))
        with patch.object(b, 'SESSION_ROOT', root):
            self.manager.poll()
            self.manager.poll()
        self.manager.reconcile.assert_called_once()
        self.assertEqual(self.manager.reconcile.call_args.args[0], self.thread)

    def test_inaccessible_rollout_does_not_block_healthy_threads_or_publish(self):
        root = self.manager.database.parent / 'sessions'
        root.mkdir()
        blocked_thread = '12345678-1234-1234-1234-123456789abc'
        healthy_thread = '12345678-1234-1234-1234-123456789def'
        blocked = root / f'rollout-{blocked_thread}.jsonl'
        healthy = root / f'rollout-{healthy_thread}.jsonl'
        for path, thread_id in ((blocked, blocked_thread), (healthy, healthy_thread)):
            event = {"type": "event_msg", "payload": {"type": "item_completed", "thread_id": thread_id,
                     "turn_id": self.turn, "item": dict(self.message, type="AgentMessage")}}
            path.write_text(json.dumps(event) + "\n", encoding="utf-8")
        self.history["thread"]["id"] = healthy_thread
        self.manager.current_history = Mock(return_value=self.history)
        self.manager.reconcile = Mock()
        self.manager.publish = Mock()
        original_stat = Path.stat

        def selective_stat(path, *args, **kwargs):
            if path == blocked:
                raise PermissionError('cannot stat rollout')
            return original_stat(path, *args, **kwargs)

        with patch.object(b, 'SESSION_ROOT', root), patch.object(Path, 'stat', selective_stat):
            self.manager.poll()
        self.manager.reconcile.assert_called_once()
        self.assertEqual(self.manager.reconcile.call_args.args[0], healthy_thread)
        self.manager.publish.assert_called_once()

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
