"""Interaction regressions. No Telegram sends or model/credit calls."""
import io
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
import telegram_bot as b
import telegram_notify as n


class InteractionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        reaction_db = patch.object(n, "REACTION_DB", Path(self.folder.name)/"reply-reactions.db")
        reaction_db.start(); self.addCleanup(reaction_db.stop)
        self.bot = b.TelegramCodexBot('111:secret', {'chat_id': '123'})
        self.bot.state = b.StateStore(Path(self.folder.name)/'state.json')
        self.bot.app = Mock()
        self.bot.desktop = Mock()
        self.bot.desktop.can_interrupt.return_value = True
        self.bot.sides.database = Path(self.folder.name)/'side.db'

    def test_task_detail_separates_fields_without_rewriting_reply_markdown(self):
        body = '| A | B |\n|---|---|\n| 1 | 2 |\n\n```python\na = 1\nb = 2\n```'
        self.bot.app.request.return_value = {'thread': {'id': 'task', 'name': '测试',
            'cwd': 'repo', 'status': {'type': 'idle'}, 'turns': [{'items': [
                {'type': 'agentMessage', 'text': body}]}]}}
        self.bot.send = Mock()
        self.bot.show_task('task')
        text = self.bot.send.call_args.args[0]
        self.assertIn('**\n\n**状态：**', text)
        self.assertIn('repo`\n\n**ID：**', text)
        self.assertIn('**Codex：**\n\n' + body, text)

    def test_task_lists_selection_and_progress_have_explicit_paragraphs(self):
        self.bot._list_threads = Mock(return_value=[{'id': 'task', 'name': '测试',
            'cwd': 'repo', 'status': {'type': 'idle'}}])
        self.bot.send = Mock()
        for render in (self.bot.show_task_list,
                       lambda: self.bot._prompt_task_selection('usage', '选择任务')):
            render()
            text = self.bot.send.call_args.args[0]
            self.assertNotRegex(text, r'(?<!\n)\n(?!\n)')
            self.assertNotIn('\n\n\n', text)
        self.assertNotRegex(self.bot._progress_card_locked('task'), r'(?<!\n)\n(?!\n)')

    def test_approval_and_input_prompts_separate_heading_fields_and_body(self):
        self.bot.enqueue = Mock()
        self.bot.on_server_request({'id': 1, 'method': 'item/commandExecution/requestApproval',
            'params': {'threadId': 'task', 'reason': '需要批准', 'command': 'echo test'}})
        self.assertNotRegex(self.bot.enqueue.call_args.args[0], r'(?<!\n)\n(?!\n)')
        question = {'header': '选择', 'question': '请选择', 'isSecret': True,
                    'options': [{'label': '第一项', 'description': '说明'}]}
        self.bot.pending_questions['q'] = {'questions': [question], 'index': 0}
        self.bot._enqueue_question('q')
        self.assertNotRegex(self.bot.enqueue.call_args.args[0], r'(?<!\n)\n(?!\n)')
        text = n.build_user_input_request_message({'thread_id': 'task',
            'tool_input': {'questions': [question]}})
        self.assertIn('**1. 选择**\n\n请选择\n\n1. 第一项', text)
        self.assertNotRegex(text, r'(?<!\n)\n(?!\n)')

    def test_diff_heading_separated_and_code_unchanged(self):
        diff = 'diff --git a/a.py b/a.py\n+++ b/a.py\n+one\n+two'
        self.bot.resolve_thread = Mock(return_value='task')
        self.bot.diffs['task'] = diff
        self.bot.send = Mock()
        self.bot.command_diff('task')
        text = self.bot.send.call_args.args[0]
        self.assertIn('**本轮 Diff**', text.replace('🧾 ', ''))
        self.assertIn('**\n\n**任务：**', text)
        self.assertIn('```diff\n' + diff + '\n```', text)

    def test_logs_preserve_lines_inside_code_and_redact_credentials(self):
        root = Path(self.folder.name)
        (root/'telegram_notify.log').write_text('one\ntwo\n111:secret\n```\n', encoding='utf-8')
        self.bot.send = Mock()
        with patch.object(b, 'HOOK_DIR', root), patch.object(b, 'LOG_PATH', root/'missing.log'):
            self.bot.command_logs()
        text = self.bot.send.call_args.args[0]
        self.assertIn('```text\n[telegram_notify.log]\none\ntwo\n', text)
        self.assertEqual(text.count('```'), 2)
        self.assertNotIn('111:secret', text)
        self.assertIn('one\ntwo\n', n.markdown_to_telegram_html(text))

    def test_usage_fields_are_separate_paragraphs(self):
        self.bot.resolve_thread = Mock(return_value='task')
        self.bot.app.request.return_value = {'thread': {'id': 'task'}}
        self.bot.token_usage['task'] = {'total': {'totalTokens': 123, 'inputTokens': 100}}
        self.bot.send = Mock()
        self.bot.command_usage('task')
        text = self.bot.send.call_args.args[0]
        self.assertNotRegex(text, r'(?<!\n)\n(?!\n)')
        self.assertNotIn('\n\n\n', text)

    def test_notification_uses_current_display_name_and_exact_task(self):
        import sqlite3
        root=Path(self.folder.name)
        with sqlite3.connect(root/'state_5.sqlite') as db:
            db.execute('CREATE TABLE threads(id TEXT, name TEXT, title TEXT)')
            db.executemany('INSERT INTO threads VALUES(?,?,?)', [
                ('task-one','主标题','第一条很长的用户消息'),('task-two','另一个任务','别的内容')])
        db.close()
        with patch.object(n,'CODEX_STATE_ROOT',root):
            self.assertEqual(n.conversation_title('task-one'),'主标题')
            with sqlite3.connect(root/'state_5.sqlite') as db:
                db.execute('UPDATE threads SET name=? WHERE id=?',('重命名之后','task-one'))
            db.close()
            self.assertEqual(n.conversation_title('task-one'),'重命名之后')
            self.assertEqual(n.conversation_title('task-two'),'另一个任务')
            self.assertEqual(n.conversation_title('missing-id'),'对话 missing-')

    def test_notification_lookup_missing_or_corrupt_db_does_not_block_send(self):
        root=Path(self.folder.name)
        with patch.object(n,'CODEX_STATE_ROOT',root):
            self.assertEqual(n.conversation_title('task-one'),'对话 task-one')
            self.assertFalse(list(root.glob('state_*.sqlite')))
            (root/'state_5.sqlite').write_bytes(b'not a sqlite database')
            text=n.build_message({'thread_id':'task-one','last_assistant_message':'正文'},True)
            self.assertTrue(text.startswith('**✅ 对话 task\\-one**\n\n'))
            self.assertIn('正文',text)

    def test_desktop_completion_passes_task_id_to_title_lookup(self):
        with patch.object(n,'forward_computer_use_notification'), \
             patch.object(n,'notifications_muted',return_value=False), \
             patch.object(n,'load_config',return_value=({'chat_id':'123'},'test-token')), \
             patch.object(n,'subagent_completion_context',return_value=None), \
             patch.object(n,'conversation_title',return_value='当前主标题') as title, \
             patch.object(n,'send_markdown_message') as send, \
             patch.object(n,'complete_payload_reactions'):
            n.send_desktop_notification({'type':'agent-turn-complete','thread-id':'task-one',
                                        'last-assistant-message':'答复'},'{}')
        title.assert_called_once_with('task-one')
        self.assertTrue(send.call_args.args[1]['text'].startswith('**✅ 当前主标题**\n\n'))

    def test_new_turn_remembers_telegram_source_without_reaction_dependency(self):
        self.bot._ui_local.source_message_id=42
        self.bot.app.request.return_value={'turn':{'id':'turn-new'}}
        self.assertEqual(self.bot._begin_turn('task-new','hello'),'turn-new')
        self.assertEqual(n.task_reply_source(self.bot.token,self.bot.chat_id,'task-new','turn-new'),42)

    def test_question_reply_is_associated_even_when_reactions_are_unsupported(self):
        self.bot._ui_local.source_message_id=42
        self.bot.pending_questions['1']={'id':1,'params':{'threadId':'task','turnId':'turn'},
                                        'questions':[{'id':'q'}],'index':0,'answers':{}}
        self.bot.enqueue=Mock()
        with patch.object(n,'set_task_reaction',return_value=False):
            self.bot._answer_question('1','answer')
        self.assertEqual(n.task_reply_source(self.bot.token,self.bot.chat_id,'task','turn'),42)

    def test_native_reply_source_isolated_by_bot_chat_task_and_turn(self):
        n.remember_task_reply_source('111:secret','123','task-a','turn-a',42)
        self.assertEqual(n.task_reply_source('111:secret','123','task-a','turn-a'),42)
        for args in [('222:other','123','task-a','turn-a'),('111:secret','456','task-a','turn-a'),
                     ('111:secret','123','task-b','turn-a'),('111:secret','123','task-a','turn-b'),
                     ('111:secret','123','task-a','')]:
            self.assertIsNone(n.task_reply_source(*args))

    def test_latest_same_turn_message_wins_and_late_old_binding_cannot_regress(self):
        for message in (42,44,41):
            n.remember_task_reply_source('111:secret','123','task','turn',message)
        self.assertEqual(n.task_reply_source('111:secret','123','task','turn'),44)

    def test_reply_association_survives_reaction_failure_and_completion_cleanup(self):
        self.bot._ui_local.source_message_id=42
        with patch.object(n,'telegram_call',side_effect=n.TelegramApiError(400,'reaction invalid')):
            self.assertFalse(self.bot._reaction('🤔','task','turn'))
        with patch.object(n,'telegram_call',return_value=True):
            n.complete_task_reactions(self.bot.token,'task','turn')
        self.assertEqual(n.task_reactions(self.bot.token),[])
        self.assertEqual(n.task_reply_source(self.bot.token,self.bot.chat_id,'task','turn'),42)

    def test_hook_and_desktop_results_reply_to_original_message(self):
        for desktop in (True,False):
            n.remember_task_reply_source('111:secret','123','task-a','turn-a',42)
            payload={'type':'agent-turn-complete','hook_event_name':'Stop',
                     'thread-id':'task-a','turn-id':'turn-a','last-assistant-message':'Done',
                     'thread_id':'task-a','turn_id':'turn-a','last_assistant_message':'Done'}
            with patch.object(n,'forward_computer_use_notification'), \
                 patch.object(n,'notifications_muted',return_value=False), \
                 patch.object(n,'load_config',return_value=({'chat_id':'123'},'111:secret')), \
                 patch.object(n,'subagent_completion_context',return_value=None), \
                 patch.object(n,'send_markdown_message') as send, \
                 patch.object(n,'complete_payload_reactions'):
                if desktop:n.send_desktop_notification(payload,'{}')
                else:n.send_hook_notification(payload)
            self.assertEqual(send.call_args.args[1]['reply_parameters'],
                             {'message_id':42,'allow_sending_without_reply':True})

    def test_managed_outbox_uses_exact_completion_source(self):
        n.remember_task_reply_source(self.bot.token,self.bot.chat_id,'task','old',42)
        n.remember_task_reply_source(self.bot.token,self.bot.chat_id,'task','new',45)
        self.bot.send=Mock()
        self.bot.enqueue('reply',completion=('task','old'))
        self.bot.outbox.put(None)
        with patch.object(n,'complete_task_reactions'):
            self.bot._outbox_worker()
        self.assertEqual(self.bot.send.call_args.kwargs['reply_to'],42)

    def test_immediate_response_uses_message_source_but_background_does_not(self):
        self.bot._ui_local.source_message_id=42
        with patch.object(b,'send_markdown_message',return_value={}) as send:
            self.bot.send('直接答复')
            self.assertEqual(send.call_args.args[1]['reply_parameters']['message_id'],42)
            self.bot.send('另一个聊天',chat_id='456')
            self.assertNotIn('reply_parameters',send.call_args.args[1])
            thread=threading.Thread(target=lambda:self.bot.send('后台通知'))
            thread.start();thread.join()
            self.assertNotIn('reply_parameters',send.call_args.args[1])

    def test_rich_reply_fallback_keeps_target_and_timeout_is_not_resent(self):
        payload={'chat_id':'123','text':'**答复**','reply_parameters':{'message_id':42,'allow_sending_without_reply':True}}
        with patch.object(n,'telegram_call',side_effect=[n.TelegramApiError(400,'rich messages are not supported'),{}]) as call:
            n._markdown_request('111:secret',payload,edit=False)
        self.assertEqual(call.call_count,2)
        self.assertTrue(all(x.args[2]['reply_parameters']==payload['reply_parameters'] for x in call.call_args_list))
        with patch.object(n,'telegram_call',side_effect=TimeoutError()) as call:
            with self.assertRaises(TimeoutError):n._markdown_request('111:secret',payload,edit=False)
            call.assert_called_once()

    def test_upgrade_recovers_current_turn_source_from_existing_reaction(self):
        with patch.object(n,'telegram_call',return_value=True):
            n.set_task_reaction('111:secret','123',42,'🤔','task','turn')
        self.assertEqual(n.task_reply_source('111:secret','123','task','turn'),42)
        self.assertIsNone(n.task_reply_source('111:secret','123','task','other'))

    def test_reaction_lifecycle_and_turn_isolation(self):
        with patch.object(n, 'REACTION_DB', Path(self.folder.name)/'reactions.db'), patch.object(n, 'telegram_call') as call:
            n.set_task_reaction('111:secret', '123', 9, '👀', 'task')
            n.set_task_reaction('111:secret', '123', 9, '🤔', 'task', 'new')
            n.complete_task_reactions('111:secret', 'task', 'old')
            self.assertEqual(len(n.task_reactions('111:secret')), 1)
            n.complete_task_reactions('111:secret', 'task', 'new')
            self.assertEqual(n.task_reactions('111:secret'), [])
            self.assertEqual([c.args[2]['reaction'] for c in call.call_args_list],
                             [[{'type': 'emoji', 'emoji': '👀'}], [{'type': 'emoji', 'emoji': '🤔'}], [{'type': 'emoji', 'emoji': '👌'}]])

    def test_completion_before_submission_feedback_does_not_leave_thinking(self):
        with patch.object(n, 'REACTION_DB', Path(self.folder.name)/'reactions.db'), patch.object(n, 'telegram_call') as call:
            n.complete_task_reactions('111:secret', 'task', 'turn')
            n.set_task_reaction('111:secret', '123', 9, '🤔', 'task', 'turn')
            self.assertEqual(call.call_args.args[2]['reaction'], [{'type': 'emoji', 'emoji': '👌'}])
            self.assertEqual(n.task_reactions('111:secret'), [])

    def test_failed_ok_is_persisted_for_retry(self):
        with patch.object(n, 'REACTION_DB', Path(self.folder.name)/'reactions.db'), patch.object(n, 'telegram_call') as call:
            n.set_task_reaction('111:secret', '123', 9, '🤔', 'task', 'turn')
            call.side_effect = RuntimeError('temporary network failure')
            n.complete_task_reactions('111:secret', 'task', 'turn')
            self.assertEqual(n.task_reactions('111:secret')[0][4], '👌')
            call.side_effect = None
            n.set_task_reaction('111:secret', '123', 9, '👌')
            self.assertEqual(n.task_reactions('111:secret'), [])
            self.assertEqual(call.call_args.args[2]['reaction'], [{'type': 'emoji', 'emoji': '👌'}])

    def test_direct_reply_uses_original_message_reactions_without_receipt(self):
        self.bot._ui_local.source_message_id = 42
        self.bot.send = Mock()
        self.bot._thread_activity = Mock(return_value=(False, ''))
        self.bot.start_turn = lambda thread, text: self.bot._submission_feedback(thread, 'turn', 'sent')
        with patch.object(n, 'set_task_reaction', return_value=True) as react:
            self.bot.submit_direct_reply('task', '继续')
        self.assertEqual([c.args[3] for c in react.call_args_list], ['👀', '🤔'])
        self.assertTrue(all(c.args[2] == 42 for c in react.call_args_list))
        self.bot.send.assert_not_called()

    def test_choice_keeps_received_until_submit_and_cancel_clears(self):
        self.bot._ui_local.source_message_id = 42
        self.bot.send = Mock()
        self.bot._thread_activity = Mock(return_value=(True, 'old'))
        with patch.object(n, 'set_task_reaction', return_value=True) as react:
            self.bot.submit_direct_reply('task', '继续')
            sequence = next(iter(self.bot.pending_direct_replies))
            self.bot._ui_local.source_message_id = None
            self.bot._handle_direct_reply_choice(sequence + ':cancel')
        self.assertEqual([c.args[3] for c in react.call_args_list], ['👀', ''])
        self.assertEqual(react.call_args.args[2], 42)
        self.assertIsNone(self.bot._ui_local.source_message_id)

    def test_duplicate_choice_submits_only_once(self):
        self.bot.pending_direct_replies['1'] = {'thread_id': 'task', 'turn_id': 'turn', 'text': '继续', 'source_message_id': 42}
        self.bot.steer_turn = Mock()
        self.bot._handle_direct_reply_choice('1:steer')
        with self.assertRaises(ValueError):
            self.bot._handle_direct_reply_choice('1:steer')
        self.bot.steer_turn.assert_called_once()

    def test_unsupported_reactions_fall_back_without_blocking_submission(self):
        self.bot._ui_local.source_message_id = 42
        self.bot.send = Mock(return_value={})
        self.bot._thread_activity = Mock(return_value=(False, ''))
        self.bot.start_turn = lambda thread, text: self.bot._submission_feedback(thread, 'turn', 'sent')
        with patch.object(n, 'set_task_reaction', return_value=False):
            self.bot.submit_direct_reply('task', '继续')
        self.assertEqual(self.bot.send.call_args.args[0], 'sent')

    def test_muted_completion_without_delivery_keeps_reactions(self):
        with patch.object(n, 'complete_payload_reactions') as done, patch.object(n, 'notifications_muted', return_value=True), patch.object(n, 'forward_computer_use_notification'):
            payload = {'type': 'agent-turn-complete', 'thread-id': 'task', 'turn-id': 'turn', 'last-assistant-message': 'Done'}
            n.send_desktop_notification(payload, '{}')
            done.assert_not_called()

    def test_unmanaged_completion_does_not_clear_before_notification_hook(self):
        with patch.object(n, 'complete_task_reactions') as done:
            self.bot.on_app_event({'method': 'turn/completed', 'params': {'threadId': 'task', 'turn': {'id': 'turn', 'status': 'completed'}}})
            done.assert_not_called()
            self.assertTrue(self.bot.outbox.empty())

    def test_question_answer_uses_reaction_and_suppresses_sent_text(self):
        self.bot._ui_local.source_message_id = 42
        self.bot.pending_questions['1'] = {'id': 1, 'params': {'threadId': 'task', 'turnId': 'turn'}, 'questions': [{'id': 'q'}], 'index': 0, 'answers': {}}
        self.bot.enqueue = Mock()
        with patch.object(n, 'set_task_reaction', return_value=True) as react:
            self.bot._answer_question('1', '同意')
        self.assertEqual([c.args[3] for c in react.call_args_list], ['👀', '🤔'])
        self.bot.enqueue.assert_not_called()
        self.bot.app.answer.assert_called_once()

    def test_background_reconciliation_requires_confirmed_delivery(self):
        self.bot.stop_event = Mock()
        self.bot.stop_event.wait.side_effect = [False, True]
        self.bot.app.request.return_value = {'thread': {'turns': [{'id': 'turn', 'status': 'completed'}]}}
        with patch.object(n, 'task_reactions', return_value=[('123', 42, 'task', 'turn', '🤔', 0)]), patch.object(n, 'complete_task_reactions') as done, patch.object(n, 'reaction_delivery_confirmed', return_value=False):
            self.bot._reaction_worker()
            done.assert_not_called()
        self.bot.app.request.assert_not_called()

    def test_outbox_removes_reaction_only_after_successful_reply(self):
        order = []
        self.bot.send = lambda *a, **kw: order.append('delivered')
        self.bot.enqueue('final answer', completion=('task', 'turn'))
        self.bot.outbox.put(None)
        with patch.object(n, 'complete_task_reactions', side_effect=lambda *a: order.append('cleared')):
            self.bot._outbox_worker()
        self.assertEqual(order, ['delivered', 'cleared'])

    def test_outbox_send_failure_retains_thinking(self):
        self.bot.send = Mock(side_effect=TimeoutError('not confirmed'))
        self.bot.enqueue('final answer', completion=('task', 'turn'))
        self.bot.outbox.put(None)
        with patch.object(n, 'complete_task_reactions') as done:
            self.bot._outbox_worker()
            done.assert_not_called()

    def test_desktop_and_stop_hooks_only_clear_after_delivery(self):
        for desktop in (True, False):
            for fails in (True, False):
                with self.subTest(desktop=desktop, fails=fails):
                    order = []
                    def send(*args):
                        order.append('send')
                        if fails: raise TimeoutError('not confirmed')
                        return {'message_id': 42}
                    payload = {'type': 'agent-turn-complete', 'hook_event_name': 'Stop', 'thread-id': 'task', 'turn-id': 'turn', 'last-assistant-message': 'Final answer', 'last_assistant_message': 'Final answer'}
                    with patch.object(n, 'load_config', return_value=({'chat_id': '123'}, '111:secret')), patch.object(n, 'notifications_muted', return_value=False), patch.object(n, 'subagent_completion_context', return_value=None), patch.object(n, 'forward_computer_use_notification'), patch.object(n, 'send_markdown_message', side_effect=send), patch.object(n, 'complete_payload_reactions', side_effect=lambda p: order.append('clear')):
                        if desktop: n.send_desktop_notification(payload, '{}')
                        else: n.send_hook_notification(payload)
                    self.assertEqual(order, ['send'] if fails else ['send','clear'])

    def test_old_completion_marker_is_not_delivery_proof(self):
        import sqlite3
        path = Path(self.folder.name)/'reactions.db'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE reaction_done (bot TEXT, thread TEXT, turn TEXT, updated REAL)')
            db.execute("INSERT INTO reaction_done VALUES ('111','task','turn',0)")
        db.close()
        with patch.object(n, 'REACTION_DB', path), patch.object(n, 'telegram_call') as call:
            self.assertFalse(n.reaction_delivery_confirmed('111:secret', 'task', 'turn'))
            n.set_task_reaction('111:secret', '123', 9, '🤔', 'task', 'turn')
            self.assertEqual(call.call_args.args[2]['reaction'], [{'type':'emoji','emoji':'🤔'}])

    def test_managed_completion_attaches_cleanup_to_final_reply(self):
        self.bot.state.update(managed_threads={'task': {}})
        self.bot.final_messages['task'] = 'Final result'
        with patch.object(b, 'cache_full_reply', return_value=None), patch.object(n, 'complete_task_reactions') as done:
            self.bot.on_app_event({'method': 'turn/completed', 'params': {'threadId': 'task', 'turn': {'id': 'turn', 'status': 'completed'}}})
            done.assert_not_called()
            item = self.bot.outbox.get_nowait()
            self.assertEqual(item['completion'], ('task', 'turn'))
            self.assertIn('Final result', item['text'])

    def test_reconciliation_retries_clear_after_confirmed_delivery(self):
        self.bot.stop_event = Mock()
        self.bot.stop_event.wait.side_effect = [False, True]
        with patch.object(n, 'task_reactions', return_value=[('123', 42, 'task', 'turn', '🤔', 0)]), patch.object(n, 'complete_task_reactions') as done, patch.object(n, 'reaction_delivery_confirmed', return_value=True):
            self.bot._reaction_worker()
            done.assert_called_once_with('111:secret', 'task', 'turn')

    def test_callback_navigation_edits_same_card_and_acknowledges(self):
        self.bot._list_threads = lambda **kw: []
        self.bot.edit_message = Mock()
        self.bot._answer_callback = Mock()
        with patch.object(b,'send_markdown_message',return_value={'message_id':80}) as send:
            self.bot.handle_callback({'id': 'q', 'from': {'id': 123}, 'data': 'tasklist|all:0', 'message': {'message_id': 8, 'chat': {'id': 123}}})
        self.assertEqual(send.call_args.args[1]['reply_parameters']['message_id'],8)
        self.assertEqual(self.bot.edit_message.call_args.args[0], 80)
        self.bot._answer_callback.assert_called_once()
        self.assertIsNone(getattr(self.bot._ui_local, 'message_id', None))

    def _nav_callback(self, panel, data='task|task-a'):
        return {'id':'callback','from':{'id':123},'data':data,'message':{'message_id':panel,'chat':{'id':123}}}

    def test_navigation_preserves_original_and_reuses_panel_across_pages_and_restart(self):
        self.bot._answer_callback=Mock()
        self.bot.show_task=lambda _:self.bot.send('详情',{'inline_keyboard':[]})
        self.bot.command_usage=lambda _:self.bot.send('用量')
        with patch.object(b,'send_markdown_message',return_value={'message_id':80}) as create, \
             patch.object(b,'edit_markdown_message',return_value={}) as edit:
            self.bot.handle_callback(self._nav_callback(8))
            self.bot.handle_callback(self._nav_callback(80,'usage|task-a'))
            self.bot.handle_callback(self._nav_callback(8))
            self.assertEqual(create.call_count,1)
            self.assertEqual({call.args[1]['message_id'] for call in edit.call_args_list},{80})
            self.assertTrue(all(any(button.get('callback_data')=='navclose|' for row in call.args[1]['reply_markup']['inline_keyboard'] for button in row) for call in edit.call_args_list))
            # New service instance can reopen the existing panel from SQLite.
            other=b.TelegramCodexBot(self.bot.token,{'chat_id':'123'})
            self.assertEqual(other._open_navigation({'message_id':8}),80)
            self.assertEqual(create.call_count,1)

    def test_close_navigation_deletes_only_panel_and_cancels_watch(self):
        n.reserve_navigation(self.bot.token,'123',8);n.bind_navigation(self.bot.token,'123',8,80)
        timer=Mock();self.bot.watches['task']={'message_id':80}
        self.bot.watch_timers['task']=timer;self.bot._answer_callback=Mock()
        with patch.object(b,'telegram_call') as api:
            self.bot.handle_callback(self._nav_callback(80,'navclose|'))
        self.assertEqual(api.call_args.args[1],'deleteMessage')
        self.assertEqual(api.call_args.args[2]['message_id'],80)
        self.assertEqual(n.navigation_view(self.bot.token,'123',80)['status'],'closed')
        self.assertNotIn('task',self.bot.watches);timer.cancel.assert_called_once()
        self.assertEqual(n.reserve_navigation(self.bot.token,'123',8),(True,None))

    def test_close_without_known_panel_cannot_delete_original(self):
        self.bot._answer_callback=Mock()
        with patch.object(b,'telegram_call') as api:
            self.bot.handle_callback(self._nav_callback(8,'navclose|'))
        api.assert_not_called()

    def test_navigation_timeout_does_not_duplicate_panel(self):
        with patch.object(b,'send_markdown_message',side_effect=TimeoutError()) as send:
            with self.assertRaises(TimeoutError):self.bot._open_navigation({'message_id':8})
            with self.assertRaises(ValueError):self.bot._open_navigation({'message_id':8})
        send.assert_called_once()

    def test_navigation_explicit_rejection_allows_new_create(self):
        with patch.object(b,'send_markdown_message',side_effect=[n.TelegramApiError(400,'rejected'),{'message_id':80}]):
            with self.assertRaises(n.TelegramApiError):self.bot._open_navigation({'message_id':8})
            self.assertEqual(self.bot._open_navigation({'message_id':8}),80)

    def test_old_queued_watch_edit_cannot_overwrite_new_page_or_closed_panel(self):
        n.reserve_navigation(self.bot.token,'123',8);n.bind_navigation(self.bot.token,'123',8,80)
        self.bot.enqueue_edit(80,'old watch')
        n.advance_navigation(self.bot.token,'123',80)
        self.bot.enqueue_edit(80,'new watch')
        n.advance_navigation(self.bot.token,'123',80,close=True)
        self.bot.outbox.put(None)
        self.bot.edit_message=Mock()
        self.bot._outbox_worker()
        self.bot.edit_message.assert_not_called()

    def test_progress_subpage_has_back_and_close_and_leaving_cancels_watch(self):
        n.reserve_navigation(self.bot.token,'123',8);n.bind_navigation(self.bot.token,'123',8,80)
        with patch.object(b,'edit_markdown_message',return_value={}) as edit:
            self.bot.edit_message(80,'progress',self.bot._watch_keyboard('task'))
        data={button.get('callback_data') for row in edit.call_args.args[1]['reply_markup']['inline_keyboard'] for button in row}
        self.assertIn('task|task',data);self.assertIn('navclose|',data)
        self.bot.watches['task']={'message_id':80}
        self.bot._answer_callback=Mock()
        self.bot.command_usage=lambda _:self.bot.send('usage')
        self.bot.edit_message=Mock()
        self.bot.handle_callback(self._nav_callback(80,'usage|task'))
        self.assertNotIn('task',self.bot.watches)
        self.assertEqual(self.bot.edit_message.call_args.args[0],80)

    def test_navigation_bot_chat_and_original_are_isolated(self):
        n.reserve_navigation(self.bot.token,'123',8);n.bind_navigation(self.bot.token,'123',8,80)
        self.assertIsNone(n.navigation_view('222:other','123',80))
        self.assertIsNone(n.navigation_view(self.bot.token,'456',80))
        self.assertEqual(n.reserve_navigation(self.bot.token,'123',9),(True,None))

    def test_manual_panel_deletion_allows_reopening_from_original(self):
        n.reserve_navigation(self.bot.token,'123',8);n.bind_navigation(self.bot.token,'123',8,80)
        with patch.object(b,'edit_markdown_message',side_effect=n.TelegramApiError(400,'message to edit not found')):
            with self.assertRaises(n.TelegramApiError):self.bot.edit_message(80,'详情')
        self.assertEqual(n.reserve_navigation(self.bot.token,'123',8),(True,None))

    def test_notification_levels_and_global_silent(self):
        with patch.object(b, 'send_markdown_message', return_value={}) as send:
            self.bot.send('查询结果')
            self.assertTrue(send.call_args.args[1]['disable_notification'])
            self.bot.send('需要处理', silent=False)
            self.assertFalse(send.call_args.args[1]['disable_notification'])
            self.bot.silent = True
            self.bot.send('需要处理', silent=False)
            self.assertTrue(send.call_args.args[1]['disable_notification'])

    def test_task_filters_and_exact_completed_status(self):
        self.bot._list_threads = lambda **kw: [
            {'id': 'run', 'name': 'Running', 'status': {'type': 'active'}},
            {'id': 'wait', 'name': 'Waiting', 'status': {'type': 'active', 'activeFlags': ['waitingOnApproval']}},
            {'id': 'done', 'name': 'Done', 'status': {'type': 'idle'}},
            {'id': 'empty', 'name': 'Empty', 'status': {'type': 'idle'}},
        ]
        self.bot.app.request.side_effect = lambda method, params, **kw: {'thread': {'turns': [{'status': 'completed'}] if params['threadId'] == 'done' else []}}
        self.bot.send = Mock()
        self.bot.show_task_list('waiting')
        self.assertEqual(self.bot.state.snapshot()['task_aliases'], ['wait'])
        self.bot.show_task_list('done')
        self.assertEqual(self.bot.state.snapshot()['task_aliases'], ['done'])
        from running_tasks import RunningTasks
        with patch.object(b, 'collect_running_tasks', return_value=RunningTasks(self.bot._list_threads()[:2], [])):
            self.bot.show_task_list('active')
        self.assertEqual(self.bot.state.snapshot()['task_aliases'], ['run', 'wait'])

    def test_idle_task_has_no_stop_button(self):
        self.bot.app.request.return_value = {'thread': {'id': 'a', 'name': 'A', 'status': {'type': 'idle'}}}
        self.bot.send = Mock()
        self.bot.show_task('a')
        self.assertNotIn('stop|a', str(self.bot.send.call_args))
        self.bot.active_turns['a'] = 't'
        self.bot.show_task('a')
        self.assertIn('stop|a', str(self.bot.send.call_args))

    def test_receipt_becomes_waiting_choice_without_extra_send(self):
        self.bot.task_names['a'] = '任务 A'
        self.bot._thread_activity = lambda *a, **k: (True, 'turn')
        self.bot.edit_message = Mock()
        with patch.object(b, 'send_markdown_message', return_value={'message_id': 77}) as send:
            self.bot.submit_direct_reply('a', '继续')
        self.assertEqual(send.call_count, 1)
        self.assertIn('任务 A', send.call_args.args[1]['text'])
        self.assertEqual(self.bot.edit_message.call_args.args[0], 77)
        self.assertIn('尚未发送', self.bot.edit_message.call_args.args[1])

    def test_old_cancel_does_not_cancel_new_input(self):
        self.bot.send = Mock(return_value={'message_id': 1})
        self.bot._request_task_message('a', 'send')
        old = self.bot.input_selection['nonce']
        self.bot._request_task_message('a', 'send')
        self.bot.handle_callback({'data': 'cancelinput|' + old, 'from': {'id': 123}, 'message': {'chat': {'id': 123}, 'message_id': 1}})
        self.assertEqual(self.bot.awaiting_continue['123'], 'a')

    def test_plain_panel_edit_removes_native_task_mapping(self):
        with patch.object(n, 'NATIVE_REPLY_DIR', Path(self.folder.name)/'routes'):
            result = {'message_id': 42}
            n.remember_task_message('111:secret', {'chat_id': '123', 'reply_markup': {'inline_keyboard': [[{'callback_data': 'direct|a'}]]}}, result)
            n.remember_task_message('111:secret', {'chat_id': '123', 'reply_markup': {'inline_keyboard': []}}, result)
            self.assertIsNone(n.lookup_task_message('111:secret', '123', 42))

    def test_download_is_bounded_and_does_not_trust_filename(self):
        with patch.object(n, 'ATTACHMENT_DIR', Path(self.folder.name)/'files'), \
             patch.object(n, 'telegram_call', return_value={'file_path': 'documents/file.txt'}), \
             patch.object(n, 'urlopen', return_value=io.BytesIO(b'hello')):
            path = n.download_telegram_attachment('111:secret', {'file_id': 'x', 'file_name': '../../evil.txt'})
            self.assertEqual(path.read_bytes(), b'hello')
            self.assertTrue(path.is_relative_to(Path(self.folder.name)))
            self.assertEqual(path.name, 'file-evil.txt')

    def test_download_rejects_oversize_and_hides_token_on_failure(self):
        with patch.object(n, 'telegram_call') as call:
            with self.assertRaises(ValueError):
                n.download_telegram_attachment('111:secret', {'file_id': 'x', 'file_size': n.ATTACHMENT_LIMIT + 1})
            call.assert_not_called()
        with patch.object(n, 'ATTACHMENT_DIR', Path(self.folder.name)/'files'), \
             patch.object(n, 'telegram_call', return_value={'file_path': 'doc/a'}), \
             patch.object(n, 'urlopen', side_effect=OSError('https://example/111:secret')):
            with self.assertRaises(ValueError) as caught:
                n.download_telegram_attachment('111:secret', {'file_id': 'x'})
            self.assertNotIn('secret', str(caught.exception))

    def test_photo_reply_passes_local_file_and_caption_to_selected_task(self):
        self.bot.submit_direct_reply = Mock()
        self.bot.send = Mock()
        with patch.object(b, 'lookup_task_message', return_value='target'), \
             patch.object(b, 'download_telegram_attachment', return_value=Path('C:/attachments/image.jpg')):
            self.bot.handle_update({'message': {'chat': {'id': 123}, 'from': {'id': 123}, 'caption': '看这里',
                'photo': [{'file_id': 'small'}, {'file_id': 'large'}], 'reply_to_message': {'message_id': 22}}})
        self.assertEqual(self.bot.submit_direct_reply.call_args.args[0], 'target')
        self.assertIn('看这里', self.bot.submit_direct_reply.call_args.args[1])
        self.assertIn('image.jpg', self.bot.submit_direct_reply.call_args.args[1])

    def test_unbound_file_never_downloads(self):
        self.bot.send = Mock()
        with patch.object(b, 'download_telegram_attachment') as download:
            self.bot.handle_update({'message': {'chat': {'id': 123}, 'from': {'id': 123}, 'document': {'file_id': 'x'}}})
        download.assert_not_called()

    def test_reply_to_new_card_without_visible_direct_button_keeps_route(self):
        with patch.object(n, 'NATIVE_REPLY_DIR', Path(self.folder.name)/'routes'):
            n.remember_task_message('111:secret', {'chat_id': '123', 'reply_markup': {'inline_keyboard': [[{'callback_data': 'direct|task-a'}]]}}, {'message_id': 9})
            self.bot.submit_direct_reply = Mock()
            self.bot.handle_update({'message': {'chat': {'id': 123}, 'from': {'id': 123}, 'text': '继续',
                'reply_to_message': {'chat': {'id': 123}, 'from': {'id': 111}, 'message_id': 9,
                                     'reply_markup': {'inline_keyboard': [[{'callback_data': 'task|task-a'}]]}}}})
            self.bot.submit_direct_reply.assert_called_once_with('task-a', '继续')

    def test_ui_context_does_not_capture_background_notification(self):
        self.bot._ui_local.message_id = 5
        self.bot.edit_message = Mock()
        with patch.object(b, 'send_markdown_message', return_value={}) as send:
            worker = threading.Thread(target=lambda: self.bot.send('完成', silent=False))
            worker.start()
            worker.join()
        send.assert_called_once()
        self.bot.edit_message.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
