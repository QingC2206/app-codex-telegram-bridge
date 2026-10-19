"""Quiet mode keeps explicit queries usable and suppresses task interaction."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from codex_telegram_bridge import app as b, i18n
from codex_telegram_bridge.telegram import notifications as n


class QuietModeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bot = b.TelegramCodexBot('111:fake', {'chat_id': '123'})
        self.bot.state = b.StateStore(self.root / 'state.json')
        self.bot.async_inputs.database = self.root / 'async.sqlite3'
        self.bot.send = Mock(return_value={'message_id': 8})
        self.bot.edit_message = Mock()
        self.bot._answer_callback = Mock()
        self.bot._reaction = Mock(return_value=True)
        self.bot.app = Mock()
        self.bot.desktop = Mock()
        self.bot.plugins.command = Mock()
        self.bot.plugins.callback = Mock()

    def callback(self, value, user=123):
        with patch.object(n, 'navigation_view', return_value=None):
            self.bot.handle_callback({'id': 'cb', 'data': value, 'from': {'id': user},
                                     'message': {'message_id': 8, 'chat': {'id': 123}}})

    def test_persisted_mode_keeps_mute_and_routing_preferences(self):
        self.bot.state.update(muted=True, task_aliases=['task-1'], default_cwd=str(self.root))
        self.bot.awaiting_continue['123'] = 'task-1'
        self.callback('settings|quiet:on', user=999)
        self.assertFalse(self.bot.quiet_mode_enabled())
        self.callback('settings|quiet:on')
        self.callback('settings|quiet:on')
        saved = b.StateStore(self.bot.state.path).snapshot()
        self.assertTrue(saved['quiet_mode'])
        self.assertEqual(saved['quiet_generation'], 1)
        self.callback('settings|quiet:off')
        self.assertFalse(self.bot.quiet_mode_enabled())
        self.assertTrue(self.bot.state.snapshot()['muted'])
        self.assertEqual(self.bot.state.snapshot()['task_aliases'], ['task-1'])
        self.assertEqual(self.bot.awaiting_continue, {'123': 'task-1'})
        self.bot.app.request.assert_not_called()
        self.bot.desktop.interrupt_turn.assert_not_called()

    def test_text_and_old_buttons_cannot_submit_or_run_plugins(self):
        self.bot._set_quiet_mode(True)
        self.bot.start_turn = Mock()
        self.bot.submit_direct_reply = Mock()
        self.bot.command_new = Mock()
        self.bot.command_stop = Mock()
        self.bot.sides.command = Mock()
        self.bot.async_inputs.reply_to = Mock()
        for text in ['/send task-1 go', '/new go', '/steer task-1 go', '/stop task-1',
                     '/side task-1 why', '/watch task-1', '/screenshot', 'go']:
            self.bot.handle_text(text, reply_to_message_id=77)
            self.assertIn('静默模式', self.bot.send.call_args.args[0])
        for action in ['continue', 'direct', 'directchoice', 'steerpick', 'stop', 'side',
                       'approval', 'question', 'asyncq', 'screenshot', 'plug', 'watch',
                       'reservequota', 'reservehours', 'resetnow', 'confirmquota']:
            self.callback(action + '|old')
        self.bot.start_turn.assert_not_called()
        self.bot.submit_direct_reply.assert_not_called()
        self.bot.command_new.assert_not_called()
        self.bot.command_stop.assert_not_called()
        self.bot.sides.command.assert_not_called()
        self.bot.async_inputs.reply_to.assert_not_called()
        self.bot.plugins.command.assert_not_called()
        self.bot.plugins.callback.assert_not_called()
        self.bot.app.request.assert_not_called()
        self.bot.app.answer.assert_not_called()

    def test_attachment_is_rejected_before_download(self):
        self.bot._set_quiet_mode(True)
        with patch.object(b, 'download_telegram_attachment') as download:
            self.bot._handle_update_inner({'message': {'chat': {'id': 123},
                'document': {'file_id': 'fixture'}, 'caption': 'look'}})
        download.assert_not_called()
        self.assertIn('静默模式', self.bot.send.call_args.args[0])

    def test_queries_and_settings_still_dispatch(self):
        self.bot._set_quiet_mode(True)
        for command, method in [('quota', 'command_quota'), ('tasks', 'command_tasks'),
                                ('running', 'command_running'), ('task', 'command_task'),
                                ('diff', 'command_diff'), ('usage', 'command_usage'),
                                ('logs', 'command_logs'), ('help', 'command_help')]:
            with patch.object(self.bot, method) as handler:
                self.bot.handle_text('/' + command)
                handler.assert_called_once_with('')
        self.bot.command_settings('quiet')
        self.assertIn('已开启', self.bot.send.call_args.args[0])
        with i18n.using_language('en'):
            self.bot.command_settings('quiet')
        self.assertIn('Quiet mode: On', self.bot.send.call_args.args[0])
        with patch.object(self.bot, 'command_quota') as handler:
            self.callback('quotaview|refresh')
            handler.assert_called_once()

    def test_enable_cancels_watch_and_reservation_without_interrupting_task(self):
        timer = Mock()
        self.bot.watch_timers['task-1'] = timer
        self.bot.watches['task-1'] = {'message_id': 7}
        self.bot.state.update(quota_reset_reservation={'id': 'r', 'status': 'armed'},
                              quota_reset_confirmation={'token': 'old'})
        self.bot._set_quiet_mode(True)
        timer.cancel.assert_called_once()
        self.assertFalse(self.bot.watches)
        self.assertEqual(self.bot.state.snapshot()['quota_reset_reservation']['status'], 'cancelled')
        self.assertFalse(self.bot.state.snapshot()['quota_reset_confirmation'])
        self.bot.quota_resets.maybe_execute()
        self.bot.app.request.assert_not_called()
        self.bot.desktop.interrupt_turn.assert_not_called()

    def test_queued_and_quiet_period_events_are_not_replayed(self):
        self.bot.enqueue('queued before quiet')
        self.bot._set_quiet_mode(True)
        self.bot.enqueue('suppressed')
        self.bot.enqueue_edit(7, 'suppressed edit')
        self.bot._set_quiet_mode(False)
        self.bot.enqueue('new event')
        self.bot.outbox.put(None)
        self.bot._outbox_worker()
        self.bot.send.assert_called_once_with('new event', None, silent=False)
        self.bot.edit_message.assert_not_called()

    def test_hook_processes_read_quiet_mode_before_credentials_or_network(self):
        self.bot._set_quiet_mode(True)
        with patch.object(n, 'BOT_STATE_PATH', self.bot.state.path), patch.object(n, 'load_config') as load, \
                patch.object(n, 'forward_computer_use_notification'):
            self.assertTrue(n.notifications_muted())
            payload = {'hook_event_name': 'Stop', 'type': 'agent-turn-complete',
                       'thread_id': 'task-123456', 'thread-id': 'task-123456',
                       'last_assistant_message': 'reply', 'last-assistant-message': 'reply'}
            n.send_hook_notification(payload)
            n.send_desktop_notification(payload, json.dumps(payload))
            n.send_user_input_request_notification(payload)
            load.assert_not_called()
        self.bot._set_quiet_mode(False)
        with patch.object(n, 'BOT_STATE_PATH', self.bot.state.path):
            self.assertFalse(n.notifications_muted())

    def test_managed_completion_and_blocking_question_are_silent(self):
        self.bot.state.update(managed_threads={'task-1': {}})
        self.bot.active_turns['task-1'] = 'turn-1'
        self.bot.final_messages['task-1'] = 'do not forward'
        self.bot._set_quiet_mode(True)
        self.bot.on_app_event({'method': 'turn/completed', 'params': {
            'threadId': 'task-1', 'turn': {'id': 'turn-1', 'status': 'completed'}}})
        self.bot.on_server_request({'id': 9, 'method': 'item/tool/requestUserInput',
            'params': {'threadId': 'task-1', 'questions': []}})
        self.assertNotIn('task-1', self.bot.active_turns)
        self.assertTrue(self.bot.outbox.empty())
        self.bot.app.answer.assert_not_called()
        self.bot.send.assert_not_called()

    def test_async_questions_observed_during_quiet_are_not_replayed(self):
        manager = self.bot.async_inputs
        item = {'type': 'agentMessage', 'id': 'q1', 'questions': [{'title': 'Before', 'options': ['Yes']}]}
        manager.observe('task-1', 'turn-1', item)
        self.bot._set_quiet_mode(True)
        manager.observe('task-1', 'turn-1', {**item, 'id': 'q2'})
        manager.publish()
        self.bot._set_quiet_mode(False)
        manager.observe('task-1', 'turn-1', item)
        manager.publish()
        self.bot.send.assert_not_called()
        with manager.db() as db:
            self.assertEqual([r['status'] for r in db.execute('SELECT status FROM questions')], ['suppressed', 'suppressed'])

    def test_execution_boundaries_reject_work_even_without_command_dispatch(self):
        self.bot._set_quiet_mode(True)
        for call in [lambda: self.bot.command_new('go'), lambda: self.bot.start_turn('task', 'go'),
                     lambda: self.bot.steer_turn('task', 'go'), lambda: self.bot.stop_turn('task'),
                     lambda: self.bot._handle_approval_callback('old:once'),
                     lambda: self.bot._handle_question_callback('old:0'),
                     lambda: self.bot._answer_question('old', 'answer'),
                     lambda: self.bot.async_inputs.submit('old', 'answer'),
                     lambda: self.bot.quota_resets._execute({}, False)]:
            with self.assertRaisesRegex(ValueError, '静默模式'):
                call()
        self.bot.app.request.assert_not_called()
        self.bot.app.answer.assert_not_called()

    def test_background_generation_invalidates_late_side_replies(self):
        generation = self.bot.state.snapshot()['quiet_generation']
        self.bot._set_quiet_mode(True)
        self.bot._set_quiet_mode(False)
        self.assertFalse(self.bot._background_allowed(generation))
        self.assertTrue(self.bot._background_allowed(self.bot.state.snapshot()['quiet_generation']))


if __name__ == '__main__':
    unittest.main()
