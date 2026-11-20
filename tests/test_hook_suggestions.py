"""Passive Desktop hook → approved completion card → one original-thread input."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing, redirect_stdout
from datetime import datetime, timezone
import io
import json
import os
import sqlite3
import time
import unittest
from unittest.mock import Mock, patch
from uuid import UUID

from codex_telegram_bridge.codex import composer_approval, hook_suggestions as hook, suggestion_source
from codex_telegram_bridge.codex.desktop import DesktopEndpoint
from codex_telegram_bridge.features import suggested_replies as cards
from codex_telegram_bridge.management import desktop_hook
from codex_telegram_bridge.telegram import notifications as notify
import test_completion_routing as routing_fixtures
import test_telegram_tables as notify_fixtures


PARENT = routing_fixtures.THREAD
TURN = routing_fixtures.TURN
CHILD = routing_fixtures.INTERNAL
OTHER = '018edf10-da27-7000-a000-000000000002'
TEXT = '保留原文 <&> [!]\n第二行 🥳'
TOKEN = '77:fixture'


class HookSuggestionTests(unittest.TestCase):
    def setUp(self):
        stack = self.enter = ExitStack()
        self.addCleanup(stack.close)
        self.fixture = routing_fixtures.CompletionRoutingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        old = notify_fixtures.PredictionRequestTests()
        old.setUp()
        self.addCleanup(old.doCleanups)
        self.profile, self.data = self.fixture.profile.resolve(), self.fixture.data
        self.config = {'chat_id': 77, 'include_message_preview': True}
        self.config_path = self.data / 'config.json'
        self.config_path.write_text(json.dumps(self.config))
        (self.profile/'auth.json').write_text('synthetic-account')
        generation = str(UUID(hex=f'{int(time.time()*1000):012x}7000a000000000000001'))
        self.endpoint = DesktopEndpoint(os.getppid(), None, self.profile)
        self.legacy = {**old.payload, 'thread-id': CHILD, 'turn-id': generation,
                       'last-assistant-message': json.dumps({'suggestion': TEXT})}
        self.stop = {'hook_event_name': 'Stop', 'session_id': CHILD, 'turn_id': generation,
                     'responsesapiClientMetadata': {'prediction_source_thread_id': PARENT,
                                                   'prediction_source_turn_id': TURN}}
        self.typed = {**self.stop, 'threadSource': 'composer_predictions',
                      'last_assistant_message': self.legacy['last-assistant-message']}
        stack.enter_context(patch.dict(os.environ, CODEX_HOME=str(self.profile), CODEX_TELEGRAM_DATA_DIR=str(self.data)))
        for name, path in (('CONFIG_PATH', self.config_path), ('BOT_STATE_PATH', self.data/'state'),
                           ('NATIVE_REPLY_DIR', self.data/'routes')):
            stack.enter_context(patch.object(notify, name, path))
        self.discovery = stack.enter_context(patch.object(hook, 'discover_desktop', return_value=self.endpoint))
        stack.enter_context(patch.object(composer_approval, 'log_roots', return_value=[self.data/'logs']))
        stack.enter_context(patch.object(notify, 'load_config', return_value=(self.config, TOKEN)))
        self.desktop = Mock()
        self.desktop.native.discover.return_value = self.endpoint
        self.desktop._reader.return_value.read_thread.return_value = {
            'thread': {'id': PARENT, 'hostId': 'local', 'status': {'type': 'idle'}},
            'turns': [{'id': TURN, 'status': 'completed'}]}
        stack.enter_context(patch.object(cards, 'DesktopControl', return_value=self.desktop))
        for name in ('forward_computer_use_notification', 'cache_full_reply', 'resolve_desktop_submissions',
                     'complete_payload_reactions', 'attach_task_reply', 'subagent_completion_context'):
            stack.enter_context(patch.object(notify, name, return_value=None))
        self.calls = []
        stack.enter_context(patch.object(notify, 'telegram_call', side_effect=self.telegram))
        self.fixture.task(turn=TURN, text='original')

    def telegram(self, token, method, payload, **kwargs):
        self.calls.append((method, payload))
        if method == 'deleteMessages':
            return True
        return {'message_id': len(self.calls), 'from': {'id': 77, 'is_bot': True}, 'chat': {'id': 77}}

    def approval(self, suppressed=False, *, thread=PARENT, turn=TURN, pid=None, prefix=''):
        now = datetime.now(timezone.utc)
        folder = self.data/'logs'/now.strftime('%Y/%m/%d')
        folder.mkdir(parents=True, exist_ok=True)
        file = folder/f'codex-desktop-test-{pid or self.endpoint.pid}-t0-i1-synthetic.log'
        with file.open('a', encoding='utf-8') as log:
            log.write(prefix + now.isoformat() + ' info [cumberbatch] cumberbatch_safety_decision '
                      + f'sourceThreadId={thread} sourceTurnId={turn} suppressed={str(suppressed).lower()}\n')

    def parent_completion(self):
        event = dict(self.fixture.payload, **{'last-assistant-message': 'original'})
        notify.send_desktop_notification(event, json.dumps(event))

    def lookup(self, thread=PARENT, turn=TURN):
        return hook.read_approved(self.endpoint, thread, turn).get('text')

    def row(self):
        with cards._database() as db:
            return dict(db.execute('SELECT * FROM cards').fetchone())

    def test_legacy_notify_then_stop_wakes_parked_card_and_clicks_once(self):
        self.parent_completion()
        self.assertTrue(self.row()['owner'].startswith('hook:'))
        cards.poll(self.config, TOKEN, self.desktop)
        self.assertGreater(self.row()['due'], self.row()['created'] + 3600)
        notify.send_desktop_notification(self.legacy, json.dumps(self.legacy))
        self.approval()
        self.assertIsNone(self.lookup())  # An orphan cannot claim the latest card.
        notify.send_hook_notification(self.stop)
        notify.send_hook_notification(self.typed)
        self.assertEqual(self.row()['due'], 0)
        cards.poll(self.config, TOKEN, self.desktop)
        self.assertEqual([m for m, _ in self.calls], ['sendRichMessage', 'deleteMessages', 'sendRichMessage'])
        self.assertEqual(self.calls[1][1]['message_ids'], [1])
        self.assertIn('第二行 🥳', self.calls[-1][1]['rich_message']['markdown'])
        key = self.row()['button']
        bot = Mock(token=TOKEN, desktop=self.desktop)
        received = []
        def followup(thread, text, cwd, *, expected_completed_turn_id, before_start):
            before_start(self.endpoint)
            received.append((thread, expected_completed_turn_id, text))
            return {'turn_id': OTHER}
        bot._desktop_follow_up.side_effect = followup
        message = {'message_id': 3, 'from': {'id': 77, 'is_bot': True}, 'chat': {'id': 77}}
        cards.handle_click(bot, message, key)
        with self.assertRaises(ValueError):
            cards.handle_click(bot, message, key)
        self.assertEqual(received, [(PARENT, TURN, TEXT)])

    def test_source_metadata_first_cannot_publish_until_legacy_provenance_arrives(self):
        self.approval()
        hook.capture(dict(self.stop, last_assistant_message=self.typed['last_assistant_message']))
        self.assertIsNone(self.lookup())
        with hook._journal() as db:
            self.assertIsNone(db.execute('SELECT text FROM samples').fetchone()[0])
        notify.send_desktop_notification(self.legacy, json.dumps(self.legacy))
        self.assertEqual(self.lookup(), TEXT)
        self.parent_completion()
        if self.row()['state'] == 'plain':  # Slow discovery is allowed to finish on the worker.
            cards.poll(self.config, TOKEN, self.desktop)
        self.assertEqual(self.row()['state'], 'ready')

    def test_temporary_journal_lock_backs_off_without_deleting_the_card(self):
        self.parent_completion()
        with patch.object(hook, 'has_target', side_effect=sqlite3.OperationalError('locked')):
            cards.poll(self.config, TOKEN, self.desktop)
        self.assertGreater(self.row()['due'], time.time())
        self.assertEqual(self.row()['state'], 'plain')
        self.assertEqual([method for method, _ in self.calls], ['sendRichMessage'])

    def test_inbox_ingress_collects_even_with_no_child_transcript(self):
        self.assertIsNone(desktop_hook.envelope(self.typed))
        with patch('sys.stdin', io.StringIO(json.dumps(self.typed))), redirect_stdout(io.StringIO()):
            desktop_hook.main()
        self.approval()
        self.assertEqual(self.lookup(), TEXT)
        self.assertFalse((self.data/'app-server-state.sqlite3').exists())
        self.assertEqual(self.calls, [])

    def test_wrong_source_and_verified_user_json_are_not_candidates(self):
        for event in (dict(self.typed, threadSource='ambient_suggestions'),
                      dict(self.typed, client='VS Code'), dict(self.typed, thread_source='thread_title'),
                      dict(self.typed, hostId='remote-host'),
                      dict(self.typed, hook_event_name='SubagentStop'),
                      dict(self.typed, transcript_path=str(self.data/'foreign.jsonl'))):
            hook.capture(event)
        copied = {**self.legacy, 'thread-id': PARENT, 'turn-id': TURN}
        notify.send_desktop_notification(copied, json.dumps(copied))
        self.assertFalse(hook._path().exists())
        self.assertEqual(len(self.calls), 1)
        self.assertIn('suggestion', self.calls[0][1]['rich_message']['markdown'])

    def test_partial_ids_join_only_within_the_same_generation(self):
        hook.capture(dict(self.typed, responsesapiClientMetadata={'prediction_source_thread_id': PARENT}))
        only_turn = dict(self.stop, responsesapiClientMetadata={'prediction_source_turn_id': TURN})
        hook.capture(dict(only_turn, turn_id=OTHER))
        self.approval()
        self.assertIsNone(self.lookup())
        # The other generation has a claim on the same turn; make its own parent explicit.
        hook.capture(dict(self.stop, turn_id=OTHER, responsesapiClientMetadata={
            'prediction_source_thread_id': OTHER, 'prediction_source_turn_id': TURN}))
        hook.capture(only_turn)
        self.assertEqual(self.lookup(), TEXT)

    def test_delayed_native_parent_can_complete_an_explicit_source_turn(self):
        hook.capture(dict(self.typed, responsesapiClientMetadata={'prediction_source_turn_id': TURN}))
        self.approval()
        self.assertIsNone(self.lookup())
        with closing(sqlite3.connect(self.profile/'state_5.sqlite')) as db, db:
            db.execute('CREATE TABLE thread_spawn_edges(parent_thread_id TEXT,child_thread_id TEXT PRIMARY KEY,status TEXT)')
            db.execute("INSERT INTO thread_spawn_edges VALUES (?,?,'active')", (PARENT, CHILD))
        self.assertEqual(self.lookup(), TEXT)
        with closing(sqlite3.connect(self.profile/'state_5.sqlite')) as db, db:
            db.execute('DELETE FROM thread_spawn_edges')
        self.assertIsNone(self.lookup())  # Revalidate this evidence before a button can send.

    def test_large_log_requires_unique_decision_even_before_the_last_megabyte(self):
        self.approval(suppressed=True)
        self.approval(prefix='x' * (1024 * 1024 + 2) + '\n')
        hook.capture(self.typed)
        self.assertIsNone(self.lookup())

    def test_unrelated_earlier_approval_cannot_bless_a_new_opaque_generation(self):
        self.approval()
        opaque = '44921c38-09ce-4023-87d3-b3cababcb046'
        notify.send_hook_notification(dict(self.typed, turn_id=opaque))
        self.assertIsNone(self.lookup())

    def test_duplicates_coalesce_and_a_second_generation_blocks_the_target(self):
        self.approval()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(hook.capture, [self.typed] * 8))
        self.assertEqual(self.lookup(), TEXT)
        hook.capture(dict(self.typed, turn_id=OTHER))
        self.assertIsNone(self.lookup())

    def test_conflicting_binding_quarantines_both_turns(self):
        self.approval()
        self.approval(turn=OTHER)
        hook.capture(self.typed)
        hook.capture(dict(self.typed, responsesapiClientMetadata={
            'prediction_source_thread_id': PARENT, 'prediction_source_turn_id': OTHER}))
        hook.capture(dict(self.typed, turn_id=OTHER, responsesapiClientMetadata={
            'prediction_source_thread_id': PARENT, 'prediction_source_turn_id': OTHER}))
        self.assertIsNone(self.lookup())
        self.assertIsNone(self.lookup(turn=OTHER))

    def test_suppression_wrong_turn_pid_or_absent_approval_keeps_candidate_private(self):
        hook.capture(self.typed)
        self.approval(pid=self.endpoint.pid + 1)
        self.approval(turn=OTHER)
        self.assertIsNone(self.lookup())
        self.approval(suppressed=True)
        self.approval()
        self.assertIsNone(self.lookup())

    def test_session_restart_quiet_generation_and_new_login_expire_the_candidate(self):
        hook.capture(self.typed)
        self.approval()
        self.assertEqual(self.lookup(), TEXT)
        with patch.object(notify, 'completion_generation', return_value=4):
            self.assertIsNone(self.lookup())
        with patch.object(hook.psutil.Process, 'create_time', return_value=1):
            self.assertIsNone(self.lookup())
        (self.profile/'auth.json').write_text('changed synthetic account')
        self.assertIsNone(self.lookup())

    def test_disabled_quiet_preview_and_non_desktop_processes_store_nothing(self):
        for config in (dict(self.config, desktop_suggestion_source=''), dict(self.config, include_message_preview=False),
                       dict(self.config, desktop_suggestion_source='custom')):
            self.config_path.write_text(json.dumps(config))
            hook.capture(self.typed)
        self.config_path.write_text(json.dumps(self.config))
        for context in (patch.object(hook.psutil.Process, 'parents', return_value=[]),
                        patch.object(notify, 'notifications_muted', return_value=True),
                        patch.dict(os.environ, CODEX_TELEGRAM_MANAGED_APP_SERVER='1'),
                        patch.dict(os.environ, CODEX_TELEGRAM_SIDE_CHAT='1')):
            with context:
                hook.capture(self.typed)
        self.assertFalse(hook._path().exists())

    def test_windows_profile_with_inactive_relay_stays_pinned_to_hook(self):
        self.config['desktop_suggestion_source'] = 'windows-desktop'
        hook.capture(self.typed)
        self.approval()
        relay = Mock(return_value=None)
        with patch.object(suggestion_source, 'reader_for', side_effect=lambda name:
                          relay if name == 'windows-desktop' else hook.read_approved):
            observation = suggestion_source.observe(self.desktop, 'windows-desktop', PARENT, TURN)
            self.assertEqual(observation.suggestion.text, TEXT)
            relay.return_value = {'principalEpoch': 'e'*32, 'hostId': 'local', 'threadId': PARENT,
                                  'sourceTurnId': TURN, 'responseId': 'relay', 'text': 'different'}
            current = suggestion_source.read_current(self.desktop, 'windows-desktop', PARENT, TURN,
                                                     owner=observation.owner)
            self.assertEqual(current, observation.suggestion)
            self.assertEqual(relay.call_count, 1)

    def test_conflicting_text_null_malformed_or_response_id_revoke(self):
        self.approval()
        for replacement in ('{"suggestion":null}', '{"suggestion":"other"}', '{"suggestion":"x","suggestion":"y"}',
                            '{"suggestions":[]}', '{"suggestion":123}', '{"suggestion":"\\ud800"}'):
            with self.subTest(replacement=replacement):
                hook.capture(self.typed)
                hook.capture(dict(self.typed, last_assistant_message=replacement), self.fixture.classify(self.typed))
                self.assertIsNone(self.lookup())
                hook._path().unlink()
        hook.capture(dict(self.typed, responseId='A'))
        hook.capture(dict(self.typed, responseId='B'))
        self.assertIsNone(self.lookup())

    def test_retention_removes_unbound_text_and_associations(self):
        hook.capture(self.legacy)
        hook.capture(self.typed)
        with hook._journal() as db:
            db.execute('UPDATE samples SET created=0')
        with patch.object(hook, '_next_cleanup', 0):
            cards.poll(self.config, TOKEN, self.desktop)  # No completion card or new hooks.
        with closing(sqlite3.connect(hook._path())) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM samples').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM targets').fetchone()[0], 0)
