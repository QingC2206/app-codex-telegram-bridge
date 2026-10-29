"""iOS-started local turns with no renderer owner; offline, no task writes."""
import unittest
from unittest.mock import Mock, patch

import test_desktop_bridge as fixtures
import codex_telegram_bridge.app as b
import codex_telegram_bridge.codex.desktop as d
import codex_telegram_bridge.features.async_inputs as inputs
from pathlib import Path


class UnloadedControlTests(unittest.TestCase):
    setUp = fixtures.BridgeTests.setUp
    enable_recovery = fixtures.BridgeTests.enable_recovery
    navigations = fixtures.BridgeTests.navigations
    mutations = fixtures.BridgeTests.mutations

    def interrupts(self):
        return [r for r in self.owner.requests if r['method'] == 'thread-follower-interrupt-turn']

    def test_interrupt_loads_once_and_pins_original_turn_in_protocol_v4(self):
        self.enable_recovery()
        self.owner.result_turn_id = 'old-turn'
        self.assertTrue(self.control.interrupt_turn('task', recover_owner=True))
        self.assertEqual(len(self.navigations()), 1)
        self.assertEqual(len(self.interrupts()), 1)
        frame = self.interrupts()[0]
        self.assertEqual(frame['params']['expectedTurnId'], 'old-turn')
        self.assertEqual(frame['version'], 4)
        self.assertGreater(frame['timeoutMs'], 0)
        self.assertLessEqual(frame['timeoutMs'], 10000)
        self.assertFalse(self.mutations())

    def test_interrupt_checks_turn_again_after_navigation(self):
        self.enable_recovery()
        def navigate():
            self.owner.owner_present = True
            self.transport.turn_id = 'new-ios-turn'
        self.transport.on_navigate = navigate
        with self.assertRaises(d.DesktopIpcInactive):
            self.control.interrupt_turn('task', 'old-turn', recover_owner=True)
        self.assertEqual(len(self.navigations()), 1)
        self.assertFalse(self.interrupts())

    def test_stale_or_missing_turn_never_navigates_or_interrupts(self):
        for status, current, requested in [('idle', 'old-turn', ''), ('active', '', ''),
                                            ('active', 'new-turn', 'old-turn')]:
            with self.subTest(status=status, current=current):
                self.enable_recovery()
                self.transport.status, self.transport.turn_id = status, current
                with self.assertRaises(d.DesktopIpcInactive):
                    self.control.interrupt_turn('task', requested, recover_owner=True)
                self.assertFalse(self.navigations())
                self.assertFalse(self.interrupts())

    def test_instance_restart_during_recovery_cannot_interrupt(self):
        self.enable_recovery()
        def navigate():
            self.owner.owner_present = True
            self.discover.return_value = d.DesktopEndpoint(66, 'different-pipe')
        self.transport.on_navigate = navigate
        with self.assertRaises(d.DesktopBridgeSafetyError):
            self.control.interrupt_turn('task', recover_owner=True)
        self.assertFalse(self.interrupts())

    def test_lost_interrupt_ack_never_repeats_or_sends_input(self):
        self.enable_recovery()
        self.owner.fail_read_call = True
        with self.assertRaises(d.DesktopSubmissionUnknown):
            self.control.interrupt_turn('task', recover_owner=True)
        self.assertEqual(len(self.navigations()), 1)
        self.assertEqual(len(self.interrupts()), 1)
        self.assertFalse(self.mutations())

    def test_capability_without_navigation_tool_fails_closed(self):
        self.owner.owner_present = False
        self.assertFalse(self.control.can_interrupt('task'))
        self.assertFalse(self.navigations())
        self.assertFalse(self.interrupts())

    def test_cross_instance_broker_failure_never_attempts_recovery(self):
        self.enable_recovery()
        with patch.object(self.owner, '_open', side_effect=d.DesktopBridgeSafetyError('wrong broker')):
            self.assertFalse(self.control.can_interrupt('task'))
            with self.assertRaises(d.DesktopBridgeSafetyError):
                self.control.interrupt_turn('task', recover_owner=True)
        self.assertFalse(self.navigations())
        self.assertFalse(self.interrupts())

    def test_unknown_native_state_is_not_idle(self):
        with self.assertRaises(d.DesktopIpcUnavailable):
            d.thread_activity({'thread': {'id': 'task', 'status': {'type': 'notLoaded'}}}, 'task')


class UnloadedBotTests(unittest.TestCase):
    def setUp(self):
        fixtures.BotEntryTests.setUp(self)
        del self.bot._thread_activity
        self.owner.owner_present = False
        self.transport.catalog_names.add('navigate_to_codex_page')
        self.transport.on_navigate = lambda: setattr(self.owner, 'owner_present', True)
        self.bot._submission_feedback = Mock()

    def inputs(self):
        return [r for r in self.owner.requests if r['method'] in
                {'thread-follower-steer-turn', 'thread-follower-start-turn'}]

    def assert_no_takeover(self):
        self.assertFalse(any(c.args[0] in {'thread/resume', 'turn/start', 'turn/steer', 'turn/interrupt'}
                             for c in self.bot.app.request.call_args_list))

    def test_native_activity_overrides_stale_bot_and_rollout_state(self):
        self.bot.active_turns['task'] = 'stale-local-turn'
        self.transport.status = 'idle'
        with patch.object(b, 'persisted_thread_activity', return_value=(True, 'stale-rollout')):
            self.assertEqual(self.bot._thread_activity('task'), (False, ''))
        self.transport.status = 'active'
        self.assertEqual(self.bot._thread_activity('task'), (True, 'old-turn'))
        self.assertFalse(self.inputs())
        self.assertFalse(any(r.get('params', {}).get('tool') == 'navigate_to_codex_page'
                             for r in self.transport.requests))

    def test_direct_reply_to_unloaded_active_task_prompts_then_appends_once(self):
        # A successful steer must acknowledge the existing turn, not a new one.
        self.owner.result_turn_id = 'old-turn'
        with patch.object(b, 'lookup_task_message', return_value='task'):
            self.bot.handle_text('继续工作', reply_to_message_id=7)
        seq, pending = next(iter(self.bot.pending_direct_replies.items()))
        self.assertEqual(pending['turn_id'], 'old-turn')
        self.assertIn(':interrupt', str(self.bot.send.call_args))
        self.assertFalse(self.inputs())
        self.bot._direct_reply_choice_inner(seq + ':steer')
        self.assertEqual(len(self.inputs()), 1)
        self.assert_no_takeover()

    def test_unloaded_steer_mismatched_ack_is_unknown_and_not_replayed(self):
        with patch.object(b, 'lookup_task_message', return_value='task'):
            self.bot.handle_text('继续工作', reply_to_message_id=7)
        seq = next(iter(self.bot.pending_direct_replies))
        self.owner.result_turn_id = 'different-turn'
        with self.assertRaises(d.DesktopSubmissionUnknown):
            self.bot._direct_reply_choice_inner(seq + ':steer')
        self.assertEqual(len(self.inputs()), 1)
        self.assert_no_takeover()

    def test_send_selection_loads_unopened_idle_task_and_starts_once(self):
        self.transport.status = 'idle'
        self.owner.steer_inactive = True
        self.bot.awaiting_continue['123'] = 'task'
        self.bot.handle_text('继续工作')
        self.assertEqual([r['method'] for r in self.inputs()],
                         ['thread-follower-steer-turn', 'thread-follower-start-turn'])
        self.assertEqual(len({r['params'].get('clientUserMessageId') or
                              r['params']['turnStart']['request']['clientUserMessageId'] for r in self.inputs()}), 1)
        self.assert_no_takeover()

    def test_steer_prompt_recognizes_unloaded_active_task(self):
        self.bot._request_task_message('task', 'steer')
        self.assertEqual(self.bot.awaiting_steer['123'], 'task')
        self.assertFalse(self.inputs())

    def test_stop_recovers_owner_without_backend_interrupt(self):
        self.owner.result_turn_id = 'old-turn'
        self.bot.stop_turn('task')
        frames = [r for r in self.owner.requests if r['method'] == 'thread-follower-interrupt-turn']
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]['params']['expectedTurnId'], 'old-turn')
        self.assert_no_takeover()

    def test_interrupt_unknown_prevents_follow_up(self):
        self.owner.fail_read_call = True
        with self.assertRaises(d.DesktopSubmissionUnknown):
            self.bot.interrupt_and_send('task', 'must not send', 'old-turn')
        self.assertFalse(self.inputs())
        self.assert_no_takeover()

    def test_unverified_idle_after_stop_prevents_follow_up(self):
        self.bot.stop_turn = Mock()
        self.transport.status = 'notLoaded'
        with self.assertRaises(d.DesktopIpcUnavailable):
            self.bot.interrupt_and_send('task', 'must not send', 'old-turn')
        self.assertFalse(self.inputs())

    def test_quiet_mode_blocks_interrupt_before_navigation(self):
        self.bot.state.update(quiet_mode=True)
        with self.assertRaises(ValueError):
            self.bot.stop_turn('task')
        self.assertFalse(self.owner.requests)
        self.assertFalse(self.transport.requests)

    def test_async_answer_recovers_owner_and_keeps_question_and_turn_identity(self):
        self.owner.result_turn_id = 'old-turn'
        manager = inputs.AsyncInputs(self.bot, Path(self.folder.name) / 'async.sqlite3')
        question = {'type': 'agentMessage', 'id': 'q-item', 'delivery': 'async',
                    'questions': [{'title': 'Choose?', 'options': ['One', 'Two']}]}
        history = {'thread': {'id': 'task', 'historyMode': 'paginated', 'turns': [
            {'id': 'old-turn', 'status': 'interrupted', 'items': [question]}]}}
        self.bot.send.return_value = {'message_id': 70}
        manager.observe('task', 'old-turn', question)
        with patch.object(inputs, 'read_thread_history', return_value=history):
            manager.publish()
            with manager.db() as db:
                key = db.execute('SELECT key FROM questions').fetchone()[0]
            manager.submit(key, 'One')
        self.assertEqual(len(self.inputs()), 1)
        prompt = self.inputs()[0]['params']['input'][0]['text']
        self.assertIn('send_user_message_question_reply', prompt)
        self.assertIn('q-item', prompt)
        self.assertEqual(manager.get(key)['status'], 'submitted')
        self.assert_no_takeover()

    def test_watch_reads_active_ios_state_even_without_local_lifecycle_event(self):
        with patch.object(b, 'persisted_thread_is_active', return_value=False), \
                patch.object(b, 'persisted_thread_token_usage', return_value={}):
            self.assertTrue(self.bot._hydrate_external_watch('task', {'turns': []}))
        self.assertEqual(self.bot.runtime_status['task']['type'], 'active')
        self.assertFalse(self.inputs())

    def test_confirmed_interrupt_then_idle_allows_one_follow_up(self):
        self.owner.result_turn_id = 'old-turn'
        original = self.owner._next_message
        def receive(handle, deadline):
            frame = self.owner.requests[-1]
            result = original(handle, deadline)
            if frame['method'] == 'thread-follower-interrupt-turn':
                self.transport.status = 'idle'
                self.owner.steer_inactive = True
                self.owner.result_turn_id = 'new-turn'
            return result
        with patch.object(self.owner, '_next_message', side_effect=receive):
            self.bot.interrupt_and_send('task', 'next instruction', 'old-turn')
        self.assertEqual(len([r for r in self.inputs() if r['method'] == 'thread-follower-start-turn']), 1)
        self.assert_no_takeover()

    def test_task_detail_from_notification_uses_live_status_and_stop_button(self):
        self.bot.show_task('task')
        self.assertIn('状态：** 运行中', self.bot.send.call_args.args[0])
        self.assertIn('stop|task', str(self.bot.send.call_args))
        self.assert_no_takeover()


if __name__ == '__main__':
    unittest.main()
