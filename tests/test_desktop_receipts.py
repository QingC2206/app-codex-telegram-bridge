"""Offline tests for native receipts, Telegram feedback, and hook delivery."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import codex_telegram_bridge.codex.receipts as receipts
import codex_telegram_bridge.telegram.notifications as n
import codex_telegram_bridge.app as b
class ReceiptTests(unittest.TestCase):
    def test_digest_matches_desktop_xml_text_escaping(self):
        import hashlib
        expected = ('<codex_delegation>\n  <source_thread_id>task-123456</source_thread_id>\n'
                    '  <input>&lt;answer&gt;A &amp; B "quoted"&lt;/answer&gt;</input>\n</codex_delegation>')
        self.assertEqual(receipts.prompt_digest('task-123456', '<answer>A & B "quoted"</answer>', origin='delegation'),
                         hashlib.sha256(expected.encode('utf-8')).hexdigest())

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sessions = self.root / 'sessions'
        self.sessions.mkdir()
        self.thread = 'thread-123456'
        self.path = self.sessions / ('rollout-test-' + self.thread + '.jsonl')
        self.path.write_text('', encoding='utf-8')
        self.token, self.chat, self.message = '111:fixture', '123', 42
        for context in (patch.object(n, 'REACTION_DB', self.root / 'reactions.db'),
                        patch.object(n, 'SESSION_ROOT', self.sessions),
                        patch.object(n, 'telegram_call'), patch.object(n, 'log_error')):
            value = context.start(); self.addCleanup(context.stop)
            if context.attribute == 'telegram_call': self.api = value
        self.bot = b.TelegramCodexBot(self.token, {'chat_id': self.chat})
        self.bot.state = b.StateStore(self.root / 'state.json')
        self.bot._ui_local.source_message_id = self.message
        self.bot.send = Mock()
        self.bot.desktop = Mock()

    def append(self, record):
        with self.path.open('a', encoding='utf-8', newline='\n') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')

    def submitted(self, text='hello', turn='turn-new', item='item-new', created=None, origin='user'):
        payload = {'type': 'message', 'role': 'user', 'id': item,
            'content': [{'type': 'input_text', 'text': text}],
            'internal_chat_message_metadata_passthrough': {'turn_id': turn,
                'create_time': time.time() if created is None else created}}
        if origin == 'delegation':
            escaped = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            output = ('<codex_delegation>\n  <source_thread_id>' + self.thread
                      + '</source_thread_id>\n  <input>' + escaped + '</input>\n</codex_delegation>')
            payload.update(type='function_call_output', namespace='codex_app', name='send_message_to_thread', output=output)
        self.append({'type': 'response_item', 'payload': payload})

    def test_user_receipt_preserves_json_and_xml_as_typed_input(self):
        text = '{"suggestion":"<answer>A & B</answer>"}'
        self.submitted(text)
        entries, _ = receipts.read_receipts(self.path, 0)
        self.assertEqual(entries[0]['origin'], 'user')
        self.assertEqual(entries[0]['digest'], receipts.prompt_digest(self.thread, text))
        self.assertNotEqual(entries[0]['digest'], receipts.prompt_digest(self.thread, text, origin='delegation'))

    def test_old_delegation_journal_still_reconciles_after_upgrade(self):
        self.track()
        db = n._reaction_db()
        with db:
            db.execute('UPDATE desktop_submissions SET digest=?',
                       (receipts.prompt_digest(self.thread, 'hello', origin='delegation'),))
        db.close()
        self.submitted(origin='delegation')
        n.resolve_desktop_submissions(self.token)
        self.assertEqual(n.desktop_submission_turn(self.token, self.chat, 42, self.thread), 'turn-new')

    def test_assistant_body_is_never_a_user_receipt(self):
        self.append({'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant',
            'id': 'assistant', 'content': [{'type': 'input_text', 'text': 'hello'}],
            'internal_chat_message_metadata_passthrough': {'turn_id': 'turn-new', 'create_time': time.time()}}})
        self.assertEqual(receipts.read_receipts(self.path, 0)[0], [])

    def complete(self, turn='turn-new', final='answer'):
        self.append({'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': turn, 'last_agent_message': final}})

    def track(self, text='hello', message=None):
        message = message or self.message
        n.set_task_reaction(self.token, self.chat, message, '👀', self.thread)
        n.remember_desktop_submission(self.token, self.chat, message, self.thread, text)

    def emojis(self):
        return [c.args[2]['reaction'][0]['emoji'] if c.args[2]['reaction'] else '' for c in self.api.call_args_list]

    def notify(self, final='answer', fails=False):
        response = Mock(side_effect=TimeoutError('Telegram disconnected')) if fails else Mock(return_value={'message_id': 70})
        with patch.object(n, 'load_config', return_value=({'chat_id': self.chat}, self.token)), \
             patch.object(n, 'notifications_muted', return_value=False), \
             patch.object(n, 'subagent_completion_context', return_value=None), \
             patch.object(n, 'forward_computer_use_notification'), \
             patch.object(n, 'send_markdown_message', response):
            n.send_desktop_notification({'type': 'agent-turn-complete', 'thread-id': self.thread, 'last-assistant-message': final}, '{}')
        return response

    def test_native_without_returned_turn_id_restores_all_reaction_stages(self):
        def accept(*args, **kwargs):
            self.submitted()
            return {'mode': 'steer', 'turn_id': '', 'route': 'desktop-user-input'}
        self.bot.desktop.send_follow_up.side_effect = accept
        result = self.bot._desktop_follow_up(self.thread, 'hello', '')
        self.bot._announce_desktop_follow_up(self.thread, result)
        self.complete()
        response = self.notify()
        self.assertEqual(self.emojis(), ['👀', '🤔', '👌'])
        self.assertEqual(response.call_args.args[1]['reply_parameters']['message_id'], self.message)
        self.assertFalse(n.task_reactions(self.token))
        self.bot.desktop.send_follow_up.assert_called_once()

    def test_delayed_receipt_keeps_thinking_and_binds_later(self):
        self.bot.desktop.send_follow_up.return_value = {'mode': 'steer', 'turn_id': '', 'route': 'desktop-user-input'}
        result = self.bot._desktop_follow_up(self.thread, 'hello', '')
        self.bot._announce_desktop_follow_up(self.thread, result)
        self.assertEqual(self.emojis(), ['👀'])
        self.assertEqual(n.task_reactions(self.token)[0][3], '')
        self.submitted()
        n.resolve_desktop_submissions(self.token)
        self.assertEqual(self.emojis(), ['👀', '🤔'])
        self.assertEqual(n.task_reply_source(self.token, self.chat, self.thread, 'turn-new'), self.message)
        self.assertEqual(n.task_reactions(self.token)[0][3], 'turn-new')

    def test_completion_before_ack_cannot_revert_ok_to_thinking(self):
        self.track(); self.submitted(); self.complete()
        self.notify()
        self.bot._submission_feedback(self.thread, 'turn-new', 'sent')
        self.assertEqual(self.emojis()[:3], ['👀', '🤔', '👌'])
        self.assertNotIn('🤔', self.emojis()[3:])
        self.assertEqual(self.emojis()[-1], '👌')

    def test_delivery_failure_does_not_mark_ok(self):
        self.track(); self.submitted(); self.complete()
        self.notify(fails=True)
        self.assertEqual(self.emojis(), ['👀', '🤔'])
        self.assertFalse(n.reaction_delivery_confirmed(self.token, self.thread, 'turn-new'))

    def test_old_identical_prompt_before_checkpoint_is_not_bound(self):
        self.submitted(turn='turn-old', item='item-old')
        self.track()
        n.resolve_desktop_submissions(self.token)
        self.assertEqual(n.desktop_submission_turn(self.token, self.chat, self.message, self.thread), '')
        self.submitted()
        n.resolve_desktop_submissions(self.token)
        self.assertEqual(n.desktop_submission_turn(self.token, self.chat, self.message, self.thread), 'turn-new')

    def test_identical_queued_prompts_claim_distinct_receipts_in_order(self):
        self.track(); self.track(message=43)
        self.submitted(turn='turn-one', item='item-one')
        self.submitted(turn='turn-two', item='item-two')
        n.resolve_desktop_submissions(self.token)
        self.assertEqual(n.desktop_submission_turn(self.token, self.chat, 42, self.thread), 'turn-one')
        self.assertEqual(n.desktop_submission_turn(self.token, self.chat, 43, self.thread), 'turn-two')

    def test_receipt_without_explicit_metadata_never_guesses_active_turn(self):
        self.track()
        self.append({'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'unrelated'}})
        self.append({'type': 'response_item', 'payload': {'type': 'function_call_output', 'namespace': 'codex_app',
            'name': 'send_message_to_thread', 'id': 'no-metadata', 'output': 'hello'}})
        n.resolve_desktop_submissions(self.token)
        self.assertEqual(n.desktop_submission_turn(self.token, self.chat, 42, self.thread), '')

    def test_same_final_text_in_different_turns_is_ambiguous(self):
        self.complete('one'); self.complete('two')
        self.assertEqual(receipts.completion_turn(self.thread, 'answer', self.sessions), '')

    def test_restart_reconciles_saved_receipt_without_resending(self):
        self.track(); self.submitted()
        self.bot.stop_event = Mock()
        self.bot.stop_event.wait.side_effect = [True]
        self.bot._reaction_worker()
        self.assertEqual(self.emojis(), ['👀', '🤔'])
        self.bot.desktop.send_follow_up.assert_not_called()

    def test_reaction_network_failure_is_persisted_and_retried(self):
        self.api.side_effect = TimeoutError('network')
        self.assertFalse(n.set_task_reaction(self.token, self.chat, 42, '👀', self.thread))
        self.assertEqual(n.task_reactions(self.token)[0][4], '👀')
        self.api.side_effect = None
        n.retry_task_reactions(self.token)
        self.assertEqual(self.emojis(), ['👀', '👀'])
        before = self.api.call_count
        n.retry_task_reactions(self.token)
        self.assertEqual(self.api.call_count, before)

    def test_stale_retry_cannot_overwrite_successfully_delivered_ok(self):
        n.set_task_reaction(self.token, self.chat, 42, '🤔', self.thread, 'turn-new')
        n.complete_task_reactions(self.token, self.thread, 'turn-new')
        before = self.api.call_count
        n.set_task_reaction(self.token, self.chat, 42, '🤔', self.thread, 'turn-new', _retry=True)
        self.assertEqual(self.api.call_count, before)
        self.assertEqual(self.emojis()[-1], '👌')

    def test_uncertain_submission_is_reconciled_without_any_retry(self):
        self.bot.desktop.send_follow_up.side_effect = b.DesktopSubmissionUnknown('lost reply')
        with self.assertRaises(b.DesktopSubmissionUnknown):
            self.bot._desktop_follow_up(self.thread, 'hello', '')
        self.submitted()
        n.resolve_desktop_submissions(self.token)
        self.assertEqual(n.desktop_submission_turn(self.token, self.chat, 42, self.thread), 'turn-new')
        self.bot.desktop.send_follow_up.assert_called_once()

    def test_known_rejection_removes_pending_receipt(self):
        self.bot.desktop.send_follow_up.side_effect = b.DesktopIpcError('rejected')
        with self.assertRaises(b.DesktopIpcError):
            self.bot._desktop_follow_up(self.thread, 'hello', '')
        self.assertIsNone(n.desktop_submission_turn(self.token, self.chat, 42, self.thread))

    def test_partial_rollout_line_is_retained_until_complete(self):
        self.track()
        record = json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
            'id': 'partial-item', 'content': [{'type': 'input_text', 'text': 'hello'}],
            'internal_chat_message_metadata_passthrough': {'turn_id': 'turn-new', 'create_time': time.time()}}})
        self.path.write_text(record[:len(record)//2], encoding='utf-8')
        n.resolve_desktop_submissions(self.token)
        self.assertEqual(n.desktop_submission_turn(self.token, self.chat, 42, self.thread), '')
        with self.path.open('a', encoding='utf-8') as f:f.write(record[len(record)//2:]+'\n')
        n.resolve_desktop_submissions(self.token)
        self.assertEqual(n.desktop_submission_turn(self.token, self.chat, 42, self.thread), 'turn-new')

    def test_moved_rollout_advances_cursor_without_binding_old_history(self):
        for _ in range(40):
            self.append({'type': 'event_msg', 'payload': {'type': 'other', 'padding': 'x'*80}})
        self.track()
        new_folder = self.sessions / 'unarchived'; new_folder.mkdir()
        moved = new_folder / self.path.name
        self.path.rename(moved); self.path = moved
        self.submitted()
        with patch.object(receipts, 'READ_LIMIT', 1024):
            for _ in range(15):
                n.resolve_desktop_submissions(self.token)
        self.assertEqual(n.desktop_submission_turn(self.token, self.chat, 42, self.thread), 'turn-new')


if __name__ == '__main__':
    unittest.main(verbosity=2)
