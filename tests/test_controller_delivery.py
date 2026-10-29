"""Delivery/reaction contracts through the controller used by the service."""
import json
from types import MethodType
import unittest
from unittest.mock import Mock

import test_shared_controller as fixtures
from codex_telegram_bridge.core import bot as core
from codex_telegram_bridge.telegram.controller_delivery import ControllerDelivery
from codex_telegram_bridge.telegram import notifications as n


class ControllerDeliveryTests(unittest.TestCase):
    migrate = fixtures.SharedControllerTests.migrate
    update = fixtures.SharedControllerTests.update
    owned = fixtures.SharedControllerTests.owned

    def setUp(self):
        fixtures.SharedControllerTests.setUp(self)
        self.calls = []
        self.delivery_error = self.reaction_error = None
        def api(token, method, payload, timeout=30):
            self.calls.append((method, payload))
            if method == 'setMessageReaction':
                if self.reaction_error: raise self.reaction_error
                return True
            if self.delivery_error: raise self.delivery_error
            return {'message_id': payload.get('message_id', 500+len(self.calls))}
        self.api.side_effect = api
        self.bot.delivery = ControllerDelivery(self.bot, self.legacy)
        self.bot.send = MethodType(core.TelegramCodexBot.send, self.bot)
        self.bot.edit_message = MethodType(core.TelegramCodexBot.edit_message, self.bot)

    def emojis(self, source=10):
        return [p['reaction'][0]['emoji'] if p['reaction'] else '' for m,p in self.calls
                if m == 'setMessageReaction' and p['message_id'] == source]

    def ready(self):
        self.delivery_error = None
        self.legacy.set('telegram_retry_at', 0)
        with self.legacy.db:
            self.legacy.db.execute("UPDATE outbox SET due=0 WHERE status='retry'")

    def test_background_navigation_refresh_keeps_return_and_close_controls(self):
        n.reserve_navigation(self.bot.token, self.bot.chat_id, 8)
        n.bind_navigation(self.bot.token, self.bot.chat_id, 8, 80)
        self.bot.watches['owned-thread'] = {'message_id': 80, 'delivery_version': 'v1'}
        progress = {'thread': 'owned-thread', 'version': 'v1'}
        self.bot.enqueue_edit(80, 'live progress', self.bot._watch_keyboard('owned-thread'), progress=progress)
        self.bot.delivery.tick()
        edits = [p for method, p in self.calls if method == 'editMessageText']
        self.assertEqual([p['message_id'] for p in edits], [80])
        buttons = [b['callback_data'] for row in edits[0]['reply_markup']['inline_keyboard'] for b in row]
        self.assertEqual(buttons.count('navclose|'), 1)
        self.assertEqual(buttons.count('task|owned-thread'), 1)
        self.bot.enqueue_edit(80, 'obsolete progress', self.bot._watch_keyboard('owned-thread'), progress=progress)
        n.advance_navigation(self.bot.token, self.bot.chat_id, 80)
        self.bot.delivery.tick()
        n.advance_navigation(self.bot.token, self.bot.chat_id, 80, close=True)
        self.bot.enqueue_edit(80, 'closed progress', self.bot._watch_keyboard('owned-thread'), progress=progress)
        self.bot.delivery.tick()
        self.assertEqual(len([p for method, p in self.calls if method == 'editMessageText']), 1)

    def test_legacy_background_navigation_adds_controls_once(self):
        n.reserve_navigation(self.bot.token, self.bot.chat_id, 8)
        n.bind_navigation(self.bot.token, self.bot.chat_id, 8, 80)
        self.bot.delivery = None
        self.bot.enqueue_edit(80, 'progress', self.bot._watch_keyboard('owned-thread'))
        self.bot.outbox.put(None)
        self.bot._outbox_worker()
        edits = [p for method, p in self.calls if method == 'editMessageText']
        self.assertEqual([p['message_id'] for p in edits], [80])
        buttons = [b['callback_data'] for row in edits[0]['reply_markup']['inline_keyboard'] for b in row]
        self.assertEqual(buttons.count('navclose|'), 1)
        self.assertEqual(buttons.count('task|owned-thread'), 1)

    def test_query_received_processing_delivered_success(self):
        self.update('/help')
        self.assertEqual(self.emojis(), ['👀', '🤔', '🎉'])
        self.assertFalse(self.legacy.feedback_records())
        self.rpc.request.assert_not_called()

    def test_429_defers_success_then_retries_only_output(self):
        self.delivery_error = n.TelegramApiError(429, 'retry after 5')
        self.update('/help')
        self.assertNotIn('🎉', self.emojis())
        self.assertEqual(self.legacy.feedback_delivery('update:10'), 'retry')
        self.ready(); self.bot.delivery.tick()
        self.assertEqual(self.emojis()[-1], '🎉')
        self.rpc.request.assert_not_called()

    def test_unknown_query_delivery_is_not_retried_or_celebrated(self):
        self.delivery_error = TimeoutError('unknown')
        with self.assertRaises(RuntimeError): self.update('/help')
        count = len([m for m,p in self.calls if m != 'setMessageReaction'])
        self.delivery_error = None; self.bot.delivery.tick()
        self.assertEqual(len([m for m,p in self.calls if m != 'setMessageReaction']), count)
        self.assertNotIn('🎉', self.emojis())

    def test_unknown_task_submission_stays_unknown_and_never_replays(self):
        self.rpc.request.side_effect = core.AppServerSubmissionUnknown('unknown')
        with self.assertRaises(core.AppServerSubmissionUnknown): self.update('/new test task')
        self.bot.delivery.tick()
        self.assertEqual(self.rpc.request.call_count, 1)
        self.assertNotIn('🎉', self.emojis()); self.assertNotIn('😢', self.emojis())

    def test_reaction_retry_does_not_repeat_query_output(self):
        self.reaction_error = TimeoutError('reaction')
        self.update('/help')
        count = len([m for m,p in self.calls if m != 'setMessageReaction'])
        self.reaction_error = None; self.bot.delivery.tick()
        self.assertEqual(len([m for m,p in self.calls if m != 'setMessageReaction']), count)
        self.assertEqual(self.emojis()[-1], '🎉')

    def test_unqualified_input_has_failure_and_no_task(self):
        self.update('unqualified input')
        self.assertEqual(self.emojis()[-1], '😢')
        self.rpc.request.assert_not_called()

    def test_new_operation_on_same_card_supersedes_old_pending_reaction(self):
        self.delivery_error = n.TelegramApiError(429, 'retry after 5')
        self.update('/help')
        self.ready(); self.update('/unknown', message_id=10, update_id=11)
        self.bot.delivery.tick()
        self.assertEqual(self.emojis()[-1], '😢')

    def test_background_completion_persists_before_worker_and_waits_for_delivery(self):
        self.bot._ui_local.source_message_id = 10
        with self.bot.delivery.feedback.operation(10, 10):
            self.bot._reaction('🤔', 'owned-thread', 'turn-one')
        self.delivery_error = n.TelegramApiError(429, 'retry after 5')
        self.bot.enqueue('final', completion=('owned-thread', 'turn-one'), outcome='🎉')
        self.assertEqual(self.bot.outbox.qsize(), 0)
        self.assertEqual(self.legacy.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 1)
        self.bot.delivery.tick()
        self.assertFalse(n.reaction_delivery_confirmed(self.bot.token, 'owned-thread', 'turn-one'))
        self.ready(); self.bot.delivery.tick()
        self.assertTrue(n.reaction_delivery_confirmed(self.bot.token, 'owned-thread', 'turn-one'))
        self.assertEqual(self.emojis()[-1], '🎉')

    def test_completed_failure_is_not_success_after_delivery(self):
        self.bot._ui_local.source_message_id = 10
        with self.bot.delivery.feedback.operation(10, 10):
            self.bot._reaction('🤔', 'owned-thread', 'failed-turn')
        self.bot.enqueue('failed', completion=('owned-thread', 'failed-turn'), outcome='😢')
        self.bot.delivery.tick()
        self.assertEqual(self.emojis()[-1], '😢')
        self.assertNotIn('🎉', self.emojis())

    def test_quiet_generation_prevents_backlog_replay_after_unmute(self):
        self.bot.enqueue('must not replay', completion=('owned-thread', 'turn-one'))
        self.bot.state.update(quiet_mode=True, quiet_generation=1)
        self.bot.state.update(quiet_mode=False)
        self.bot.delivery.tick()
        self.assertFalse([m for m,p in self.calls if m != 'setMessageReaction'])
        self.assertEqual(self.legacy.db.execute('SELECT status FROM outbox').fetchone()[0], 'expired')

    def test_expired_question_is_not_delivered_after_rate_limit(self):
        self.bot.pending_questions['q'] = {'id': 3}
        self.bot.enqueue('question', interaction=('question', 'q'))
        self.delivery_error = n.TelegramApiError(429, 'retry after 5'); self.bot.delivery.tick()
        self.bot.pending_questions.clear()
        self.ready(); before = len(self.calls); self.bot.delivery.tick()
        self.assertEqual(len(self.calls), before)
        self.assertEqual(self.legacy.db.execute('SELECT status FROM outbox').fetchone()[0], 'expired')

    def test_restart_keeps_pending_outputs_but_never_replays_sending(self):
        pending = self.legacy.queue_message('sendMessage', {'text': 'pending'}, {'controller': True})
        unknown = self.legacy.queue_message('sendMessage', {'text': 'unknown'}, {'controller': True})
        self.legacy.take_message(unknown)
        self.bot.delivery = ControllerDelivery(self.bot, self.legacy)
        self.assertEqual(self.legacy.message_status(unknown), 'unknown')
        self.bot.delivery.tick()
        self.assertEqual(self.legacy.message_status(pending), 'sent')
        self.assertEqual(self.legacy.message_status(unknown), 'unknown')

    def test_pending_progress_is_replaced_with_final_content(self):
        self.delivery_error = n.TelegramApiError(429, 'retry after 5')
        progress = self.bot.send('progress')
        self.bot.replace_progress_message(progress, 'final')
        self.ready(); self.bot.delivery.tick()
        final = [p for m,p in self.calls if m == 'sendRichMessage'][-1]
        self.assertEqual(final['rich_message']['markdown'], 'final')

    def test_already_delivered_progress_is_edited_without_second_send(self):
        self.delivery_error = n.TelegramApiError(429, 'retry after 5')
        progress = self.bot.send('progress')
        self.ready(); self.bot.delivery.tick()
        count = len([m for m,p in self.calls if m == 'sendRichMessage'])
        self.bot.replace_progress_message(progress, 'final')
        self.assertEqual(len([m for m,p in self.calls if m == 'sendRichMessage']), count)
        self.assertEqual(self.calls[-1][0], 'editMessageText')

    def test_unknown_completion_stays_received_and_does_not_replay_task(self):
        self.bot._ui_local.source_message_id = 10
        with self.bot.delivery.feedback.operation(10, 10):
            self.bot._reaction('🤔', 'owned-thread', 'turn-one')
        self.bot.enqueue('final', completion=('owned-thread', 'turn-one'))
        self.delivery_error = TimeoutError('delivery unknown')
        self.bot.delivery.tick()
        self.assertEqual(self.emojis()[-1], '👀')
        before = len(self.calls); self.bot.delivery.tick()
        self.assertEqual(len(self.calls), before)
        self.rpc.request.assert_not_called()

    def test_old_messages_do_not_react_or_submit_after_migration(self):
        self.legacy.set('accept_after', 200)
        self.bot.handle_update({'update_id': 1, 'message': {'date': 100, 'message_id': 1,
            'text': '/new stale', 'chat': {'id': 42, 'type': 'private'}, 'from': {'id': 42}}})
        self.assertFalse(self.calls); self.rpc.request.assert_not_called()

    def test_delayed_reply_prompt_registers_original_thread_after_delivery(self):
        self.delivery_error = n.TelegramApiError(429, 'retry after 5')
        self.bot.request_direct_reply('original-thread')
        self.assertFalse(self.bot.awaiting_direct_reply)
        self.ready(); self.bot.delivery.tick()
        message = next(iter(self.bot.awaiting_direct_reply))
        self.assertEqual(self.bot.awaiting_direct_reply[message], 'original-thread')
        self.assertEqual(n.lookup_task_message(self.bot.token, self.bot.chat_id, message), 'original-thread')

    def test_foreign_user_cannot_create_delivery_or_feedback(self):
        self.update('/help', sender=999)
        self.assertFalse(self.calls)
        self.assertFalse(self.legacy.feedback_records())

    def test_delayed_async_card_keeps_original_question_reply_route(self):
        self.bot.async_inputs.observe('original-thread', 'turn-one', {'id': 'question-item', 'type': 'agentMessage',
            'questions': [{'title': 'Choose', 'options': ['Yes', 'No']}]})
        self.delivery_error = n.TelegramApiError(429, 'retry after 5')
        self.bot.async_inputs.publish()
        with self.bot.async_inputs.db() as db:
            self.assertIsNone(db.execute('SELECT message FROM questions').fetchone()[0])
        self.ready(); self.bot.delivery.tick()
        with self.bot.async_inputs.db() as db:
            saved = db.execute('SELECT thread,turn,message FROM questions').fetchone()
        self.assertEqual((saved['thread'], saved['turn']), ('original-thread', 'turn-one'))
        self.assertIsInstance(saved['message'], int)
        self.rpc.request.assert_not_called()

    def test_delayed_side_prompt_can_be_quoted_after_delivery(self):
        self.delivery_error = n.TelegramApiError(429, 'retry after 5')
        self.bot.sides.prompt('original-thread')
        self.ready(); self.bot.delivery.tick()
        message = self.legacy.db.execute('SELECT message_id FROM outbox').fetchone()[0]
        self.assertEqual(self.bot.sides.lookup(message),
            {'kind': 'parent', 'target': 'original-thread', 'parent': 'original-thread'})
        self.rpc.request.assert_not_called()

    def test_delayed_confirmation_binds_only_the_same_unexpired_nonce(self):
        import time
        self.bot._ui_local.quota_scope = self.bot.quota_state.activate('fixture-account', self.bot.quota_state.connection())
        self.bot.quota_state.update(quota_reset_confirmation={'nonce': 'original', 'expires_at': time.time()+120})
        self.delivery_error = n.TelegramApiError(429, 'retry after 5')
        sent = self.bot.send('confirm')
        self.bot.delivery.after_delivery(sent['_pending_outbox'], 'state',
            {'key': 'quota_reset_confirmation', 'identity_key': 'nonce', 'identity': 'original'})
        self.ready(); self.bot.delivery.tick()
        self.assertIsInstance(self.bot.quota_state.snapshot()['quota_reset_confirmation']['message_id'], int)
        self.rpc.request.assert_not_called()

    def test_replaced_confirmation_is_not_sent_after_rate_limit(self):
        import time
        self.bot._ui_local.quota_scope = self.bot.quota_state.activate('fixture-account', self.bot.quota_state.connection())
        self.bot.quota_state.update(quota_reset_confirmation={'nonce': 'old', 'expires_at': time.time()+120})
        self.delivery_error = n.TelegramApiError(429, 'retry after 5')
        sent = self.bot.send('confirm')
        self.bot.delivery.after_delivery(sent['_pending_outbox'], 'state',
            {'key': 'quota_reset_confirmation', 'identity_key': 'nonce', 'identity': 'old'})
        self.bot.quota_state.update(quota_reset_confirmation={'nonce': 'new', 'expires_at': time.time()+120})
        count = len(self.calls); self.ready(); self.bot.delivery.tick()
        self.assertEqual(len(self.calls), count)
        self.assertNotIn('message_id', self.bot.quota_state.snapshot()['quota_reset_confirmation'])

    def test_side_result_reaction_waits_for_every_page(self):
        first = self.legacy.queue_message('sendMessage', {'text': 'one'}, {'controller': True})
        second = self.legacy.queue_message('sendMessage', {'text': 'two'}, {'controller': True})
        self.bot.delivery.defer_reaction([first, second], 10, 0)
        self.bot.delivery.tick(); self.assertNotIn('🎉', self.emojis())
        self.bot.delivery.tick(); self.assertEqual(self.emojis()[-1], '🎉')

    def test_shared_markdown_pagination_bounds_non_bmp_text_without_loss(self):
        body = '😀'*4100
        pages = n.split_markdown_pages(body, limit=3000)
        self.assertEqual(''.join(p.replace('\n', '') for p in pages), body)
        self.assertTrue(all(len(p.encode('utf-16-le'))//2 <= 3000 for p in pages))
        self.assertLessEqual(len(n.markdown_preview(body, 3900).encode('utf-16-le'))//2, 3900)

    def test_retried_draft_feedback_stays_on_original_input_not_clicked_card(self):
        from codex_telegram_bridge.backends.app_server.compatibility import handle_callback
        row = self.legacy.desktop_conversation('original-thread', str(self.root), 'Original')
        self.legacy.enqueue(20, row['id'], 'retry', kind='desktop')
        self.legacy.update_job(20, status='failed')
        self.legacy.set('input_message:20', 10)
        self.bot._ui_local.source_message_id = 99
        self.bot.submit_direct_reply = lambda tid, text: self.bot._reaction('🤔', tid, 'retry-turn')
        handle_callback(self.bot, 'legacyretry', '20')
        self.assertEqual(self.emojis(10)[-1], '🤔')
        self.assertFalse(self.emojis(99))
        self.assertEqual(self.bot._ui_local.source_message_id, 99)
        self.bot.enqueue('done', completion=('original-thread', 'retry-turn'))
        self.bot.delivery.tick()
        self.assertEqual(self.emojis(10)[-1], '🎉')

    def test_migration_recovers_in_flight_feedback_without_job_replay(self):
        row = self.legacy.desktop_conversation('original-thread', str(self.root), 'Original')
        self.legacy.enqueue(20, row['id'], 'pending', kind='desktop')
        self.legacy.update_job(20, status='submitted', turn_id='pending-turn')
        self.legacy.set('input_message:20', 10)
        self.migrate()
        self.bot.delivery.tick()
        self.assertEqual(self.emojis(10)[-1], '🤔')
        self.assertEqual(self.legacy.job(20)['status'], 'submitted')
        self.rpc.request.assert_not_called()

    def test_upgrade_pending_legacy_completion_keeps_reply_route_and_delivery_gate(self):
        row = self.legacy.desktop_conversation('original-thread', str(self.root), 'Original')
        route = {'conversation': row['id'], 'origin': 'desktop', 'thread_id': 'original-thread',
                 'turn_id': 'old-turn', 'notice': True, 'delivery_key': 'legacy-completion'}
        self.legacy.queue_message('sendMessage', {'text': 'first'}, route, 'legacy-completion:0')
        self.legacy.queue_message('sendMessage', {'text': 'last'}, route, 'legacy-completion:1')
        self.migrate(); self.bot.delivery.tick()
        self.assertFalse(n.reaction_delivery_confirmed(self.bot.token, 'original-thread', 'old-turn'))
        self.bot.delivery.tick()
        self.assertTrue(n.reaction_delivery_confirmed(self.bot.token, 'original-thread', 'old-turn'))
        with self.legacy.lock:
            ids = [r[0] for r in self.legacy.db.execute('SELECT message_id FROM outbox')]
        self.assertTrue(all(n.lookup_task_message(self.bot.token, self.bot.chat_id, i) == 'original-thread' for i in ids))
        self.rpc.request.assert_not_called()

    def test_upgrade_pending_notification_respects_completion_mute(self):
        self.legacy.set('completion_notifications', False)
        row = self.legacy.desktop_conversation('original-thread', str(self.root), 'Original')
        self.legacy.queue_message('sendMessage', {'text': 'must stay muted'}, {
            'conversation': row['id'], 'thread_id': 'original-thread', 'turn_id': 'old-turn', 'notice': True})
        self.migrate(); self.bot.delivery.tick()
        self.assertFalse(self.calls)
        self.assertEqual(self.legacy.db.execute('SELECT status FROM outbox').fetchone()[0], 'expired')

    def test_legacy_pending_notice_cannot_cross_a_new_quiet_generation(self):
        self.bot.state.update(quiet_mode=False, quiet_generation=2)
        self.legacy.queue_message('sendMessage', {'text': 'old'}, {
            'thread_id': 'original-thread', 'turn_id': 'old-turn', 'notice': True})
        self.migrate(); self.bot.delivery.tick()
        self.assertFalse(self.calls)
        self.assertEqual(self.legacy.db.execute('SELECT status FROM outbox').fetchone()[0], 'expired')
