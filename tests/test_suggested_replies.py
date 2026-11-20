"""A Desktop adapter and Telegram transport are isolated; no real prompt is sent."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from copy import deepcopy
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import Mock, patch

from codex_telegram_bridge.codex import suggestion_source as source
from codex_telegram_bridge.codex.desktop import DesktopControl, DesktopEndpoint, DesktopSubmissionUnknown
from codex_telegram_bridge.core.bot import TelegramCodexBot
from codex_telegram_bridge.features import suggested_replies as cards
from codex_telegram_bridge.telegram import notifications as notify
from codex_telegram_bridge.telegram.completion_routing import Decision
from test_desktop_bridge import FakeTransport
from test_local_images import PNG


THREAD = '00000000-0000-4000-8000-000000000001'
TURN = '00000000-0000-4000-8000-000000000002'
TOKEN = '111:fixture'
PROMPT = 'Compare [A](https://example.org) & B.\n> **Exact** 😀'


class SuggestedReplyTests(unittest.TestCase):
    def setUp(self):
        stack = self.enter = ExitStack()
        self.addCleanup(stack.close)
        root = Path(stack.enter_context(TemporaryDirectory())).resolve()
        for attr, path in [('HOOK_DIR', root), ('SOURCE_STATE_ROOT', root),
                           ('BOT_STATE_PATH', root / 'state'), ('NATIVE_REPLY_DIR', root / 'routes')]:
            stack.enter_context(patch.object(notify, attr, path))
        self.config = {'chat_id': '7', 'desktop_suggestion_source': 'fixture', 'include_message_preview': True}
        stack.enter_context(patch.object(notify, 'load_config', return_value=(self.config, TOKEN)))
        stack.enter_context(patch.object(source, 'codex_home', return_value=root))
        self.endpoint = DesktopEndpoint(61, 'fixture', root)
        self.desktop = Mock()
        self.desktop.native.discover.return_value = self.endpoint
        self.live = {'thread': {'id': THREAD, 'hostId': 'local', 'status': {'type': 'idle'}},
                     'turns': [{'id': TURN, 'status': 'completed'}]}
        self.desktop._reader.return_value.read_thread.return_value = self.live
        stack.enter_context(patch.object(cards, 'DesktopControl', return_value=self.desktop))
        self.approved = {'accountId': 'account-A', 'userId': 'A', 'hostId': 'local', 'threadId': THREAD,
                         'sourceTurnId': TURN, 'text': PROMPT, 'responseId': 'candidate-A'}
        self.reader = Mock(return_value=self.approved)
        stack.enter_context(patch.object(source, 'installed', return_value=True))
        stack.enter_context(patch.object(source, 'reader_for', return_value=self.reader))
        self.calls = []
        self.api = stack.enter_context(patch.object(notify, 'telegram_call', side_effect=self.rpc))
        self.bot = Mock(token=TOKEN, chat_id='7', desktop=self.desktop)
        self.inputs = []
        def submit(thread, text, cwd, *, expected_completed_turn_id, before_start):
            before_start(self.endpoint)
            self.inputs.append((thread, expected_completed_turn_id, text))
            return {'turn_id': 'next'}
        self.bot._desktop_follow_up.side_effect = submit
        self.request = {'chat_id': '7', 'text': '**Original answer**', 'message_thread_id': 17,
                        'reply_parameters': {'message_id': 29}, 'disable_notification': True,
                        'protect_content': True, 'reply_markup': notify.build_notification_keyboard(THREAD)}
        self.decision = Decision('conversation', 'verified', THREAD, TURN)

    def rpc(self, token, method, payload, **kwargs):
        self.calls.append((method, deepcopy(payload)))
        if method == 'deleteMessages':
            return True
        return {'message_id': 100 + len(self.calls), 'chat': {'id': int(payload['chat_id'])},
                'from': {'id': 111, 'is_bot': True}, 'message_thread_id': payload.get('message_thread_id')}

    def methods(self):
        return [method for method, payload in self.calls]

    def row(self):
        with cards._database() as db:
            return dict(db.execute('SELECT * FROM cards').fetchone())

    def send(self):
        return cards.send_completion(self.config, TOKEN, self.request, self.decision, TURN)

    def make_late(self):
        self.reader.return_value = dict(self.approved, text=None, responseId=None)
        self.send()
        self.reader.return_value = self.approved

    def poll(self):
        cards.poll(self.config, TOKEN, self.desktop)

    def callback(self, msg_id=None):
        row = self.row()
        key = row['button']
        return {'chat': {'id': 7}, 'from': {'is_bot': True, 'id': 111}, 'message_thread_id': 17,
                'message_id': msg_id or row['new_id'], 'reply_markup': {'inline_keyboard': [[
                    {'text': 'Send', 'callback_data': 'suggest|' + key}]]}}

    def click(self, message=None):
        cards.handle_click(self.bot, message or self.callback(), self.row()['button'])

    def due(self):
        with cards._database() as db:
            db.execute('UPDATE cards SET due=0')

    def test_early_one_card_click_exact_literal_in_original_turn(self):
        self.send()
        self.assertEqual(self.methods(), ['sendRichMessage'])
        card = self.calls[0][1]
        self.assertIn('> ', card['rich_message']['markdown'])
        self.assertIn('**Original answer**', card['rich_message']['markdown'])
        self.assertNotIn('_suggestion_html', card)
        buttons = [b['callback_data'] for row in card['reply_markup']['inline_keyboard'] for b in row]
        self.assertEqual(buttons, ['task|' + THREAD, 'suggest|' + self.row()['button']])
        self.click()
        with self.assertRaises(ValueError):
            self.click()
        self.assertEqual(self.inputs, [(THREAD, TURN, PROMPT)])

    @unittest.skipUnless(os.name == 'nt', 'Windows packaged suggestion source')
    def test_windows_sample_through_real_pipe_replaces_card_and_submits_once(self):
        import psutil
        from test_windows_suggestion_capture import request, response, observe, TEXT
        from codex_telegram_bridge.platforms.windows import composer_capture, suggestion_pipe
        root = notify.HOOK_DIR.resolve()
        self.enter.enter_context(patch.dict(os.environ, CODEX_HOME=str(root), CODEX_TELEGRAM_DATA_DIR=str(root)))
        (root/'auth.json').write_text('fixture')
        self.endpoint = DesktopEndpoint(psutil.Process().ppid(), None, root)
        self.desktop.native.discover.return_value = self.endpoint
        self.config['desktop_suggestion_source'] = composer_capture.SOURCE
        decision = Mock(return_value=False)
        tracker = composer_capture.Capture(root, self.endpoint.pid, decision=decision)
        sender = suggestion_pipe.Publisher(tracker)
        self.enter.callback(sender.close)
        self.reader.side_effect = suggestion_pipe.read_approved
        self.send()
        observe(tracker, request(73), True)
        observe(tracker, response(73), False)
        self.poll()
        self.assertEqual(self.methods(), ['sendRichMessage'])
        decision.return_value = True
        self.due()
        self.poll()
        self.assertEqual(self.methods(), ['sendRichMessage', 'deleteMessages', 'sendRichMessage'])
        self.click()
        with self.assertRaises(ValueError):
            self.click()
        self.assertEqual(self.inputs, [(THREAD, TURN, TEXT)])

    def test_late_deletes_old_text_before_repost_and_keeps_both_routes(self):
        self.make_late()
        self.poll()
        self.poll()
        self.assertEqual(self.methods(), ['sendRichMessage', 'deleteMessages', 'sendRichMessage'])
        self.assertEqual(self.calls[1][1], {'chat_id': '7', 'message_ids': [101]})
        for key in ('reply_parameters', 'protect_content', 'message_thread_id', 'disable_notification'):
            self.assertEqual(self.calls[2][1][key], self.request[key])
        for identifier in (101, 103):
            self.assertEqual(notify.lookup_task_message(TOKEN, '7', identifier), THREAD)
        with self.assertRaises(ValueError):
            self.click(self.callback(101))

    def test_late_repost_never_repeats_media_or_parses_paths_in_suggestion(self):
        photo = notify.HOOK_DIR / 'preview.png'
        photo.write_bytes(PNG)
        self.request['text'] += '\n\n![preview](' + photo.as_posix() + ')'
        self.approved['text'] += '\n![do not upload](' + photo.as_posix() + ')'
        with patch.object(notify, 'telegram_multipart_call', side_effect=self.rpc):
            self.make_late()
            self.poll()
        self.assertEqual(self.methods(), ['sendPhoto','sendRichMessage','deleteMessages','sendRichMessage'])
        self.assertEqual(self.calls[2][1]['message_ids'], [102])
        for identifier in (101, 102, 104):
            self.assertEqual(notify.lookup_task_message(TOKEN, '7', identifier), THREAD)

    def test_normal_reply_before_late_suggestion_leaves_original_card(self):
        self.make_late()
        cards.note_user_input(TOKEN, '7', THREAD)
        self.poll()
        self.assertEqual(self.methods(), ['sendRichMessage'])

    def test_new_turn_or_wrong_instance_prevents_replacement(self):
        self.make_late()
        self.live['turns'][0]['id'] = 'newer'
        self.poll()
        self.assertEqual(self.methods(), ['sendRichMessage'])
        self.assertEqual(self.row()['state'], 'expired')

    def test_unloaded_owner_can_be_rechecked_without_navigation(self):
        self.make_late()
        self.live['thread']['status']['type'] = 'notLoaded'
        self.poll()
        self.assertEqual(self.row()['state'], 'plain')
        self.live['thread']['status']['type'] = 'idle'
        self.due()
        self.poll()
        self.assertEqual(self.row()['state'], 'ready')

    def test_original_task_can_be_loaded_after_the_first_card(self):
        self.live['thread']['status']['type'] = 'notLoaded'
        self.send()
        self.assertEqual(self.row()['state'], 'plain')
        self.live['thread']['status']['type'] = 'idle'
        self.poll()
        self.assertEqual(self.row()['state'], 'ready')

    def test_changed_account_candidate_or_text_blocks_click(self):
        self.send()
        for field in ('accountId', 'userId', 'responseId', 'sourceTurnId', 'threadId', 'hostId', 'text'):
            self.reader.return_value = dict(self.approved, **{field: 'changed'})
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.click()
        self.assertFalse(self.inputs)

    def test_wrong_bot_chat_topic_and_message_cannot_replay(self):
        self.send()
        for fields in ({'from': {'id': 3,'is_bot': True}}, {'chat': {'id': 8}},
                       {'message_thread_id': 18}, {'message_id': 1000}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.click(dict(self.callback(), **fields))
        self.assertFalse(self.inputs)

    def test_duplicate_clicks_compete_for_one_submission(self):
        self.send()
        barrier = threading.Barrier(5)
        message = self.callback()
        def click(i):
            barrier.wait()
            try:
                self.click(message)
            except ValueError:
                pass
        with ThreadPoolExecutor(max_workers=5) as executor:
            list(executor.map(click, range(5)))
        self.assertEqual(len(self.inputs), 1)

    def test_pre_dispatch_read_timeout_can_retry_unknown_write_cannot(self):
        self.send()
        self.reader.side_effect = TimeoutError('read')
        with self.assertRaises(TimeoutError):
            self.click()
        self.reader.side_effect = None
        def lose_ack(thread, text, cwd, *, expected_completed_turn_id, before_start):
            before_start(self.endpoint)
            self.inputs.append(text)
            raise DesktopSubmissionUnknown('uncertain')
        self.bot._desktop_follow_up.side_effect = lose_ack
        with self.assertRaises(DesktopSubmissionUnknown):
            self.click()
        with self.assertRaises(ValueError):
            self.click()
        self.assertEqual(self.inputs, [PROMPT])

    def test_delete_retry_does_not_duplicate_repost_after_unknown_send(self):
        self.make_late()
        def fail_once(token, method, payload, **kwargs):
            result = self.rpc(token, method, payload)
            if len(self.calls) in {2, 4}:
                raise TimeoutError('lost receipt')
            return result
        self.api.side_effect = fail_once
        self.poll()
        self.assertEqual(self.row()['state'], 'deleting')
        self.due()
        self.poll()
        self.poll()
        self.assertEqual(self.methods(), ['sendRichMessage', 'deleteMessages', 'deleteMessages', 'sendRichMessage'])
        self.assertEqual(self.row()['state'], 'uncertain')
        self.click(self.callback(104))
        self.assertEqual(notify.lookup_task_message(TOKEN, '7', 104), THREAD)
        self.assertEqual(self.inputs, [(THREAD, TURN, PROMPT)])

    def test_repost_429_resumes_from_confirmed_deletion(self):
        self.make_late()
        def rate_limit(token, method, payload, **kw):
            result = self.rpc(token, method, payload)
            if len(self.calls) == 3:
                raise notify.TelegramApiError(429, 'retry after 10', {'retry_after': 10})
            return result
        self.api.side_effect = rate_limit
        self.poll()
        self.poll()
        self.assertGreater(self.row()['due'], time.time())
        self.due()
        self.poll()
        self.assertEqual(self.methods(), ['sendRichMessage', 'deleteMessages', 'sendRichMessage', 'sendRichMessage'])
        self.assertEqual(self.row()['state'], 'ready')

    def test_explicit_delete_rejection_keeps_old_card(self):
        self.make_late()
        self.api.side_effect = notify.TelegramApiError(400, 'cannot delete')
        self.poll()
        self.assertEqual(self.row()['state'], 'expired')
        self.assertEqual(self.api.call_count, 2)
        self.assertEqual(notify.lookup_task_message(TOKEN, '7', 101), THREAD)

    def test_new_input_during_deletion_restores_original_without_button(self):
        self.make_late()
        def advance(token, method, payload, **kw):
            if method == 'deleteMessages':
                self.live['turns'][0]['id'] = 'newer'
            return self.rpc(token, method, payload)
        self.api.side_effect = advance
        self.poll()
        self.assertEqual(self.row()['state'], 'restored')
        self.assertEqual(self.calls[0][1], self.calls[2][1])

    def test_muted_or_quiet_generation_changed_before_replacement(self):
        self.make_late()
        with patch.object(notify, 'completion_generation', return_value=1):
            self.poll()
        self.assertEqual(self.methods(), ['sendRichMessage'])

    def test_oversize_text_or_raw_model_object_cannot_get_a_button(self):
        self.make_late()
        for value in ({'suggestion': PROMPT}, dict(self.approved, text='😀' * 3000), None):
            self.reader.return_value = value
            self.due()
            self.poll()
        self.assertEqual(self.methods(), ['sendRichMessage'])

    def test_default_hook_without_login_uses_ordinary_sender_and_stores_nothing(self):
        self.config.pop('desktop_suggestion_source')
        self.request['text'] = '{"suggestion":"Ordinary user-requested JSON"}'
        self.send()
        self.assertFalse(cards._path().exists())
        self.reader.assert_not_called()
        self.assertEqual(self.calls[0][1]['rich_message']['markdown'], self.request['text'])

    def test_without_original_principal_a_later_login_cannot_claim_the_card(self):
        self.reader.return_value = None
        self.send()
        self.reader.return_value = self.approved
        self.poll()
        self.assertEqual(self.methods(), ['sendRichMessage'])
        self.assertEqual(self.row()['state'], 'expired')

    def test_account_change_between_initial_send_and_late_result_keeps_original(self):
        self.make_late()
        self.reader.return_value = dict(self.approved, accountId='another-account')
        self.poll()
        self.assertEqual(self.methods(), ['sendRichMessage'])

    def test_missing_provider_or_disabled_preview_does_not_register_a_card(self):
        with patch.object(source, 'installed', return_value=None):
            self.send()
        self.config['include_message_preview'] = False
        self.send()
        self.assertFalse(cards._path().exists())
        self.reader.assert_not_called()

    def test_concurrent_polls_delete_and_repost_only_once(self):
        self.make_late()
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(lambda _: self.poll(), range(4)))
        self.assertEqual(self.methods(), ['sendRichMessage', 'deleteMessages', 'sendRichMessage'])

    def test_rate_limited_repost_restores_plain_card_if_candidate_was_revoked(self):
        self.make_late()
        def reject(token, method, payload, **kw):
            result = self.rpc(token, method, payload)
            if len(self.calls) == 3:
                raise notify.TelegramApiError(429, 'retry after 1')
            return result
        self.api.side_effect = reject
        self.poll()
        self.reader.return_value = None
        self.due()
        self.poll()
        self.assertEqual(self.row()['state'], 'restored')
        self.assertEqual(self.calls[0][1], self.calls[-1][1])
        self.assertIsNone(self.row()['fingerprint'])

    def _public_notification(self, first):
        from test_completion_routing import CompletionRoutingTests
        from codex_telegram_bridge.telegram import pending_completions
        fixture = CompletionRoutingTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        for name in ('forward_computer_use_notification', 'cache_full_reply', 'resolve_desktop_submissions',
                     'complete_payload_reactions', 'attach_task_reply', 'subagent_completion_context'):
            self.enter.enter_context(patch.object(notify, name, return_value=None))
        self.reader.return_value = dict(self.approved, text=None)
        body = 'A real user reply can be JSON: {"suggestion":"keep this"}'
        payload = dict(fixture.payload, **{'thread-id': THREAD, 'turn-id': TURN, 'last-assistant-message': body})
        hook = {'hook_event_name': 'Stop', 'session_id': THREAD, 'turn_id': TURN, 'last_assistant_message': body}
        if first == 'deferred':
            payload.update(client='Fixture iOS controller', turnTrigger='remote_ios')
            notify.send_desktop_notification(payload, '{}')
            self.assertFalse(self.calls)
        fixture.task(thread=THREAD, turn=TURN, text=body, trigger='remote_ios' if first == 'deferred' else 'user')
        if first == 'deferred':
            with patch.object(pending_completions.time, 'time', return_value=time.time() + 4):
                notify.retry_pending_completions()
        elif first == 'hook':
            notify.send_hook_notification(hook)
            notify.send_desktop_notification(payload, '{}')
        else:
            notify.send_desktop_notification(payload, '{}')
            notify.send_hook_notification(hook)
        self.assertEqual(self.methods(), ['sendRichMessage'])
        self.reader.return_value = self.approved
        self.due()
        self.poll()
        self.assertEqual(self.methods(), ['sendRichMessage', 'deleteMessages', 'sendRichMessage'])
        self.assertIn(body, self.calls[-1][1]['rich_message']['markdown'])

    def test_hook_first_joins_the_late_suggestion(self):
        self._public_notification('hook')

    def test_notify_first_joins_the_late_suggestion(self):
        self._public_notification('notify')

    def test_deferred_mobile_completion_joins_the_late_suggestion(self):
        self._public_notification('deferred')

    def test_html_fallback_uses_literal_quote(self):
        def fallback(token, method, payload, **kw):
            if method == 'sendRichMessage':
                raise notify.TelegramApiError(404, 'method not found')
            return self.rpc(token, method, payload)
        self.api.side_effect = fallback
        self.send()
        self.assertIn('<blockquote>Compare [A](https://example.org) &amp; B.', self.calls[0][1]['text'])
        self.assertNotIn('_suggestion_plain', self.calls[0][1])

    def test_start_only_uses_owned_start_frame_and_never_steers(self):
        owner = FakeTransport()
        result = owner.send_follow_up(THREAD, PROMPT, start_only=True)
        methods = [req['method'] for req in owner.requests]
        self.assertEqual(result['mode'], 'start')
        self.assertEqual(methods, ['initialize', 'thread-owner-discovery', 'thread-follower-start-turn'])
        self.assertEqual(owner.requests[-1]['params']['turnStart']['request']['input'][0]['text'], PROMPT)

    def test_authenticated_update_reaches_owner_and_records_receipt_on_new_card(self):
        self.make_late()
        self.poll()
        transport = FakeTransport()
        transport.result_turn_id = '00000000-0000-4000-8000-000000000003'
        control = DesktopControl(lambda **kw: transport)
        control.native.discover = Mock(return_value=self.endpoint)
        control._reader = self.desktop._reader
        bot = TelegramCodexBot(TOKEN, self.config)
        bot.desktop = control
        bot._owns_thread = Mock(return_value=False)
        bot._answer_callback = Mock()
        bot.send = Mock()
        for method in ('_operation_reaction', '_reaction', '_submission_feedback'):
            setattr(bot, method, Mock())
        self.enter.enter_context(patch.object(notify, 'REACTION_DB', notify.HOOK_DIR / 'reactions.sqlite'))
        msg = self.callback()
        key = self.row()['button']
        callback = {'id': 'id', 'data': 'suggest|' + key, 'message': msg, 'from': {'id': 900}}
        bot.handle_update({'callback_query': callback})
        self.assertFalse(transport.requests)
        callback['from'] = {'id': 7}
        bot.handle_update({'callback_query': callback})
        starts = [v for v in transport.requests if v['method'] == 'thread-follower-start-turn']
        self.assertEqual(len(starts), 1, bot.send.call_args_list)
        self.assertEqual(starts[0]['params']['turnStart']['request']['input'][0]['text'], PROMPT)
        self.assertEqual(self.row()['state'], 'submitted')
        self.assertEqual(notify.desktop_submission_turn(TOKEN, '7', msg['message_id'], THREAD),
                         transport.result_turn_id)
        bot.handle_update({'callback_query': callback})
        self.assertEqual(len([v for v in transport.requests if v['method']=='thread-follower-start-turn']), 1)
