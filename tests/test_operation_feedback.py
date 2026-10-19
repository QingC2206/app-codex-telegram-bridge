"""Offline checks for query, error, callback and asynchronous reaction feedback."""
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import codex_telegram_bridge.app as b
import codex_telegram_bridge.telegram.notifications as n
import codex_telegram_bridge.features.side_chat as side_chat
class OperationFeedbackTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        for owner, name, value in ((n, 'REACTION_DB', self.root / 'reactions.db'),
                                   (n, 'LOG_PATH', self.root / 'notify.log'),
                                   (b, 'LOG_PATH', self.root / 'bot.log')):
            context = patch.object(owner, name, value)
            context.start()
            self.addCleanup(context.stop)
        reaction = patch.object(n, 'set_task_reaction', return_value=True)
        self.reaction_patch = reaction
        self.react = reaction.start()
        self.addCleanup(reaction.stop)
        for name in ('send_markdown_message', 'edit_markdown_message'):
            context = patch.object(b, name, return_value={'message_id': 301})
            mock = context.start()
            self.addCleanup(context.stop)
            setattr(self, name, mock)
        self.bot = b.TelegramCodexBot('111:secret', {'chat_id': '123'})
        self.bot.state = b.StateStore(self.root / 'state.json')
        self.bot.app = Mock()
        self.bot.plugins.command = Mock(return_value=False)
        self.bot.sides.database = self.root / 'sides.db'
        self.bot.sides.lookup = Mock(return_value=None)
        self.bot._refresh_quota_app_server_if_outdated = Mock()
        self.bot._observe_quota = Mock()
        self.bot.quota_resets.buttons = Mock(return_value={'inline_keyboard': []})
        self.bot._answer_callback = Mock()
        self.bot.app.request.return_value = {'rateLimits': {
            'primary': {'usedPercent': 20, 'windowDurationMins': 300, 'resetsAt': time.time() + 3600},
            'secondary': {'usedPercent': 30, 'windowDurationMins': 10080, 'resetsAt': time.time() + 86400}}}

    def update(self, text, *, message_id=42, **extra):
        self.bot.handle_update({'message': dict({'message_id': message_id, 'text': text,
            'chat': {'id': 123, 'type': 'private'}, 'from': {'id': 123, 'is_bot': False}}, **extra)})

    def emojis(self):
        return [c.args[3] for c in self.react.call_args_list]

    def test_quota_query_has_received_processing_success(self):
        self.update('/quota')
        self.assertEqual(self.emojis(), ['👀', '🤔', '👌'])
        self.bot.app.request.assert_called_once_with('account/rateLimits/read', {}, timeout=45)
        self.assertIn('剩余额度', self.edit_markdown_message.call_args.args[1]['text'])
        self.assertTrue(all(c.args[2] == 42 and c.args[4] == b.BOT_OPERATION_THREAD for c in self.react.call_args_list))
        self.assertIsNone(self.bot._ui_local.source_message_id)
        self.assertIsNone(self.bot._ui_local.feedback)

    def test_other_regular_commands_share_feedback(self):
        for command, method in (('/help', 'command_help'), ('/tasks', 'command_tasks'),
                                ('/running', 'command_running'),
                                ('/cwd', 'command_cwd'), ('/logs', 'command_logs')):
            with self.subTest(command=command), patch.object(self.bot, method, side_effect=lambda _: self.bot.send('查询结果')):
                self.react.reset_mock()
                self.update(command)
                self.assertEqual(self.emojis(), ['👀', '🤔', '👌'])

    def test_unqualified_text_gets_error_and_task_selection_guidance(self):
        self.bot.start_turn = Mock()
        self.bot.submit_direct_reply = Mock()
        self.update('继续帮我修改一下')
        self.assertEqual(self.emojis(), ['👀', '👎'])
        self.assertIn('/send', self.send_markdown_message.call_args.args[1]['text'])
        self.bot.start_turn.assert_not_called()
        self.bot.submit_direct_reply.assert_not_called()
        self.bot.app.request.assert_not_called()

    def test_unknown_command_gets_error(self):
        self.update('/not-a-command')
        self.assertEqual(self.emojis(), ['👀', '👎'])
        self.assertIn('无法识别', self.send_markdown_message.call_args.args[1]['text'])

    def test_unknown_quoted_message_gets_error_without_task_submission(self):
        with patch.object(b, 'lookup_task_message', return_value=None):
            self.update('继续', reply_to_message={'message_id': 99})
        self.assertEqual(self.emojis(), ['👀', '👎'])
        self.bot.app.request.assert_not_called()

    def test_attachment_without_task_gets_error_before_download(self):
        with patch.object(b, 'download_telegram_attachment') as download:
            self.update('', document={'file_id': 'not-downloaded'})
        self.assertEqual(self.emojis(), ['👀', '👎'])
        download.assert_not_called()

    def test_quota_failure_never_sets_success(self):
        self.bot.app.request.side_effect = RuntimeError('quota unavailable')
        self.update('/quota')
        self.assertEqual(self.emojis(), ['👀', '🤔', '👎'])
        self.assertIn('查询失败', self.edit_markdown_message.call_args.args[1]['text'])

    def test_unhandled_command_failure_sets_error_and_preserves_exception(self):
        with patch.object(self.bot, 'command_tasks', side_effect=RuntimeError('failed')):
            with self.assertRaisesRegex(RuntimeError, 'failed'):
                self.update('/tasks')
        self.assertEqual(self.emojis(), ['👀', '🤔', '👎'])
        self.assertIsNone(self.bot._ui_local.feedback)

    def test_failure_to_send_query_reply_never_sets_success(self):
        self.send_markdown_message.side_effect = TimeoutError('send uncertain')
        with self.assertRaises(TimeoutError):
            self.update('/help')
        self.assertEqual(self.emojis(), ['👀', '🤔', '👎'])

    def test_unknown_submission_neither_claims_failure_nor_success(self):
        self.bot.awaiting_continue['123'] = 'task-a'
        self.bot.start_turn = Mock(side_effect=b.DesktopSubmissionUnknown('not confirmed'))
        with self.assertRaises(b.DesktopSubmissionUnknown):
            self.update('continue')
        self.assertNotIn('👎', self.emojis())
        self.assertNotIn('👌', self.emojis())
        self.assertTrue(self.bot._command_error_text(b.DesktopSubmissionUnknown('not confirmed')).startswith('⚠️'))
        self.assertTrue(self.bot._command_error_text(RuntimeError('rejected')).startswith('❌'))

    def test_unknown_callback_submission_shows_warning(self):
        self.bot._handle_direct_reply_choice = Mock(side_effect=b.DesktopSubmissionUnknown('not confirmed'))
        self.bot.handle_update({'callback_query': {'id': 'cb', 'from': {'id': 123},
            'data': 'directchoice|1:steer', 'message': {'message_id': 55, 'chat': {'id': 123, 'type': 'private'}}}})
        texts = [call.args[1]['text'] for call in self.send_markdown_message.call_args_list + self.edit_markdown_message.call_args_list]
        self.assertTrue(any(text.startswith('⚠️') for text in texts))
        self.assertNotIn('👎', self.emojis())
        self.assertNotIn('👌', self.emojis())

    def test_error_reply_is_detected_even_when_handler_returns_normally(self):
        with patch.object(self.bot, 'command_tasks', side_effect=lambda _: self.bot.send('❌ 查询失败')):
            self.update('/tasks')
        self.assertEqual(self.emojis(), ['👀', '🤔', '👎'])

    def test_quota_refresh_button_reacts_on_clicked_card(self):
        self.bot.handle_update({'callback_query': {'id': 'cb', 'from': {'id': 123},
            'data': 'quotaview|refresh', 'message': {'message_id': 55, 'chat': {'id': 123, 'type': 'private'}}}})
        self.assertEqual(self.emojis(), ['👀', '🤔', '👌'])
        self.assertTrue(all(c.args[2] == 55 for c in self.react.call_args_list))
        self.assertEqual(self.edit_markdown_message.call_args.args[1]['message_id'], 55)

    def test_failed_quota_refresh_button_ends_in_error(self):
        self.bot.app.request.side_effect = RuntimeError('quota unavailable')
        self.bot.handle_update({'callback_query': {'id': 'cb', 'from': {'id': 123},
            'data': 'quotaview|refresh', 'message': {'message_id': 55, 'chat': {'id': 123, 'type': 'private'}}}})
        self.assertEqual(self.emojis(), ['👀', '🤔', '👎'])

    def test_task_submission_does_not_finish_on_acceptance(self):
        self.bot.awaiting_continue['123'] = 'task-a'
        self.bot.start_turn = lambda thread, text: self.bot._submission_feedback(thread, 'turn-a', 'accepted')
        self.update('继续处理')
        self.assertEqual(self.emojis()[-1], '🤔')
        self.assertNotIn('👌', self.emojis())
        self.assertEqual(self.react.call_args.args[4:6], ('task-a', 'turn-a'))

    def test_new_task_submission_does_not_finish_on_acceptance(self):
        with patch.object(self.bot, 'command_new', side_effect=lambda _: self.bot._submission_feedback('task-a', 'turn-a', 'accepted')):
            self.update('/new 做个任务')
        self.assertEqual(self.emojis(), ['👀', '🤔'])
        self.assertEqual(self.react.call_args.args[4:6], ('task-a', 'turn-a'))

    def test_side_chat_defers_generic_success(self):
        self.bot.sides.lookup.return_value = {'kind': 'parent', 'target': 'task-a', 'parent': 'task-a'}
        worker = Mock()
        self.bot.sides.cleanup_expired = Mock()
        with patch.object(side_chat.threading, 'Thread', return_value=worker):
            self.update('侧边问题', reply_to_message={'message_id': 99})
        worker.start.assert_called_once()
        self.assertNotIn('👌', self.emojis())

    def test_async_plugin_defers_generic_success(self):
        def plugin(command, argument):
            self.bot._defer_feedback()
            return True
        self.bot.plugins.command.side_effect = plugin
        self.update('/demo run')
        self.assertNotIn('👌', self.emojis())

    def test_leading_space_command_does_not_become_pending_task_input(self):
        self.bot.awaiting_continue['123'] = 'task-a'
        self.bot.start_turn = Mock()
        self.update('  /quota')
        self.bot.start_turn.assert_not_called()
        self.assertEqual(self.bot.awaiting_continue['123'], 'task-a')
        self.assertEqual(self.emojis(), ['👀', '🤔', '👌'])

    def test_pending_direct_reply_does_not_claim_unquoted_text(self):
        self.bot.awaiting_direct_reply[99] = 'task-a'
        self.update('没有引用的文字')
        self.assertEqual(self.emojis(), ['👀', '👎'])
        self.assertEqual(self.bot.awaiting_direct_reply[99], 'task-a')

    def test_unsupported_reactions_do_not_block_query_delivery(self):
        self.react.return_value = False
        self.update('/quota')
        self.assertEqual(self.emojis(), ['👀', '🤔', '👌'])
        self.assertIn('剩余额度', self.edit_markdown_message.call_args.args[1]['text'])

    def test_unauthorized_message_and_callback_receive_no_reaction(self):
        message = {'message_id': 42, 'chat': {'id': 123}, 'from': {'id': 999}, 'text': '/quota'}
        self.bot.handle_update({'message': message})
        self.bot.handle_update({'callback_query': {'id': 'cb', 'from': {'id': 999}, 'data': 'quotaview|refresh', 'message': message}})
        self.react.assert_not_called()
        self.bot.app.request.assert_not_called()

    def test_restart_marks_old_operation_error_without_replaying_query(self):
        self.bot.stop_event = Mock()
        self.bot.stop_event.wait.return_value = True
        with patch.object(n, 'task_reactions', return_value=[('123', 42, b.BOT_OPERATION_THREAD, '', '🤔', 0)]):
            self.bot._reaction_worker()
        self.assertEqual(self.emojis(), ['👎'])
        self.bot.app.request.assert_not_called()

    def test_startup_cleanup_does_not_touch_new_operation(self):
        self.bot.stop_event = Mock()
        self.bot.stop_event.wait.return_value = True
        with patch.object(n, 'task_reactions', return_value=[('123', 42, b.BOT_OPERATION_THREAD, '', '🤔', self.bot.reaction_started_at + 1)]):
            self.bot._reaction_worker()
        self.react.assert_not_called()

    def test_error_reaction_network_retry_is_retired_after_success(self):
        # Exercise the actual persistence layer rather than the dispatch mock.
        self.reaction_patch.stop()
        with patch.object(n, 'telegram_call', side_effect=TimeoutError('timeout')):
            self.assertFalse(n.set_task_reaction('111:secret', '123', 42, '👎', b.BOT_OPERATION_THREAD))
        self.assertEqual(n.task_reactions('111:secret')[0][4], '👎')
        with patch.object(n, 'telegram_call', return_value=True) as api:
            n.retry_task_reactions('111:secret')
        self.assertEqual(api.call_count, 1)
        self.assertEqual(n.task_reactions('111:secret'), [])
        self.assertIsNone(n.task_reply_source('111:secret', '123', b.BOT_OPERATION_THREAD, ''))


if __name__ == '__main__':
    unittest.main(verbosity=2)
