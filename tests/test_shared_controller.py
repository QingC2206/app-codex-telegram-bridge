"""User journeys through the production configured-runtime entry point.

The original Windows suites exercise core.bot through its compatibility alias.
These tests exercise the same rules with the macOS deployment's actual profile,
including migration and old Telegram messages, without live network writes.
"""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from codex_telegram_bridge.core import bot as core
from codex_telegram_bridge.backends.app_server.runtime import AppServerBot
from codex_telegram_bridge.backends.app_server.compatibility import translate_callback
from codex_telegram_bridge.storage.bridge import BridgeState
from codex_telegram_bridge.storage.migration import migrate_controller
from codex_telegram_bridge.telegram import notifications as n


class SharedControllerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.legacy = BridgeState(self.root/'legacy.db'); self.addCleanup(self.legacy.close)
        self.rpc = Mock()
        self.bot = AppServerBot({'codex_executable': sys.executable, 'cwd': str(self.root)},
            token='111:fake', credential_config={'chat_id': 42}, legacy=self.legacy,
            app_factory=lambda *args: self.rpc)
        self.bot.state = core.StateStore(self.root/'state.json')
        self.bot.sides.database = self.root/'sides.db'
        self.bot.async_inputs.database = self.root/'async.db'
        self.bot.desktop = Mock()
        self.sent = []
        self.bot.send = Mock(side_effect=lambda text, *args, **kwargs: self.sent.append((text, args, kwargs)) or {'message_id': 401})
        self.bot.edit_message = Mock()
        self.bot._answer_callback = Mock()
        self.bot._refresh_watch = Mock()
        self.bot.plugins.command = Mock(return_value=False)
        for name, path in [('REACTION_DB', self.root/'reactions.db'), ('NATIVE_REPLY_DIR', self.root/'routes'),
                           ('REPLY_CACHE_DIR', self.root/'replies')]:
            p = patch.object(n, name, path); p.start(); self.addCleanup(p.stop)
        p = patch.object(n, 'telegram_call', return_value=True)
        self.api = p.start(); self.addCleanup(p.stop)
        self.migrate()

    def migrate(self):
        self.bot.state.update(controller_schema=0)
        migrate_controller(self.bot, self.legacy, self.bot.settings)

    def update(self, text, *, message_id=10, update_id=None, reply=None, sender=42, chat=42):
        message = {'text': text, 'date': int(time.time()), 'message_id': message_id,
            'from': {'id': sender}, 'chat': {'id': chat, 'type': 'private' if chat>0 else 'group'}}
        if reply is not None: message['reply_to_message'] = {'message_id': reply}
        self.bot.handle_update({'update_id': message_id if update_id is None else update_id, 'message': message})

    def owned(self):
        self.bot.state.update(managed_threads={'owned-thread': {'origin': 'bot_created', 'cwd': str(self.root)}})
        self.bot.active_turns['owned-thread'] = 'turn-one'

    def request(self, method, **extra):
        return {'id': 7, 'method': method, 'params': {'threadId': 'owned-thread', 'turnId': 'turn-one', 'itemId': 'item-one', **extra}}

    def test_major_business_rules_are_the_same_implementation(self):
        for name in ('handle_text', 'command_new', 'command_cwd', 'submit_direct_reply', 'start_turn',
                     'steer_turn', 'stop_turn', 'command_quota', 'command_settings', 'on_app_event',
                     '_handle_callback_inner', 'on_server_request', '_reaction_worker'):
            self.assertIs(getattr(AppServerBot, name), getattr(core.TelegramCodexBot, name), name)

    def test_plain_text_never_uses_historical_selection_or_creates_task(self):
        selected = self.legacy.new_conversation(str(self.root))
        self.legacy.bind(selected['id'], 'owned-thread'); self.migrate()
        self.update('unqualified task input')
        self.rpc.request.assert_not_called()
        self.bot.desktop.send_follow_up.assert_not_called()
        self.assertIn('没有指定任务', self.sent[-1][0])

    def test_cwd_only_changes_default_and_not_selection_or_ownership(self):
        before = self.bot.state.snapshot()['managed_threads']
        folder = self.root/'folder with spaces'; folder.mkdir()
        self.update('/cwd '+str(folder))
        self.assertEqual(self.bot.state.snapshot()['default_cwd'], str(folder.resolve()))
        self.assertEqual(self.bot.state.snapshot()['managed_threads'], before)
        self.rpc.request.assert_not_called()

    def test_group_allowlist_reuses_canonical_authorization(self):
        self.bot.chat_id = '-100'; self.bot.allowed_user_ids = {'42'}
        self.update('/cwd '+str(self.root), chat=-100, sender=99)
        self.assertFalse(self.sent)
        self.update('/cwd '+str(self.root), chat=-100, sender=42)
        self.assertTrue(self.sent)

    def test_old_reply_keeps_original_thread_after_selection_changes(self):
        first = self.legacy.new_conversation(str(self.root)); self.legacy.bind(first['id'], 'original-thread')
        with self.legacy.db:
            self.legacy.db.execute('INSERT INTO message_routes VALUES (?,?)', (77, json.dumps({'conversation': first['id'], 'thread_id': 'original-thread'})))
        self.legacy.new_conversation(str(self.root)); self.migrate()
        self.bot.submit_direct_reply = Mock()
        self.update('continue original', reply=77)
        self.bot.submit_direct_reply.assert_called_once_with('original-thread', 'continue original')

    def test_running_reply_shows_choice_without_steering_or_interrupting(self):
        self.bot._thread_activity = Mock(return_value=(True, 'turn-one'))
        self.bot._can_interrupt = Mock(return_value=True)
        self.bot.submit_direct_reply('original-thread', 'new instruction')
        self.bot.desktop.send_follow_up.assert_not_called()
        self.bot.desktop.interrupt_turn.assert_not_called()
        self.assertTrue(self.bot.pending_direct_replies)
        self.assertIn('追加', self.sent[-1][0])

    def test_quiet_blocks_old_button_and_text_but_keeps_queries(self):
        row = self.legacy.new_conversation(str(self.root)); self.legacy.bind(row['id'], 'original-thread')
        self.bot.state.update(quiet_mode=True)
        self.bot.stop_turn = Mock()
        self.bot.handle_update({'callback_query': {'id': 'cb', 'from': {'id': 42},
            'message': {'message_id': 77, 'chat': {'id': 42}}, 'data': 'ux|stop|'+row['id']}})
        self.bot.stop_turn.assert_not_called()
        self.update('/new must not execute')
        self.rpc.request.assert_not_called()
        self.update('/help')
        self.assertIn('/side', self.sent[-1][0])

    def test_windows_callback_protocol_is_accepted_by_mac_profile(self):
        self.bot.show_task = Mock()
        with patch.object(self.bot, '_open_navigation', return_value=71), patch.object(n, 'navigation_view', return_value=None):
            self.bot.handle_update({'callback_query': {'id': 'cb', 'from': {'id': 42},
                'message': {'message_id': 70, 'chat': {'id': 42}}, 'data': 'task|original-thread'}})
        self.bot.show_task.assert_called_once_with('original-thread')

    def test_upgrade_old_new_task_button_is_read_only(self):
        translated = translate_callback(self.bot, {'data': 'ux|continue|24'})
        self.assertEqual(translated['data'], 'legacydraft|24')
        self.rpc.request.assert_not_called()

    def test_import_is_idempotent_and_does_not_replay_jobs(self):
        row = self.legacy.new_conversation(str(self.root)); self.legacy.bind(row['id'], 'owned-thread')
        self.legacy.enqueue(22, row['id'], 'do not replay')
        self.legacy.set('offset', 300)
        self.migrate(); migrate_controller(self.bot, self.legacy, self.bot.settings)
        self.assertEqual(self.bot.state.snapshot()['update_offset'], 300)
        self.assertTrue(self.bot._owns_thread('owned-thread'))
        self.assertEqual(self.legacy.job(22)['status'], 'queued')
        self.rpc.request.assert_not_called()

    def test_import_covers_more_than_recent_hundred_conversations(self):
        for index in range(103):
            row = self.legacy.new_conversation(str(self.root)); self.legacy.bind(row['id'], 'thread-'+str(index))
        self.migrate()
        self.assertEqual(len(self.bot.state.snapshot()['managed_threads']), 103)

    def test_foreign_and_old_turn_requests_are_rejected(self):
        self.owned()
        for tid, turn in [('foreign-thread', 'turn-one'), ('owned-thread', 'old-turn')]:
            self.bot.on_server_request(self.request('item/commandExecution/requestApproval', threadId=tid, turnId=turn, command='echo test'))
            self.assertEqual(self.rpc.answer.call_args.args[1], {'decision': 'decline'})
        self.assertFalse(self.bot.pending_approvals)

    def test_command_requires_action_and_file_change_requires_reviewable_diff(self):
        self.owned()
        for method in ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval'):
            self.bot.on_server_request(self.request(method))
            self.assertFalse(self.bot.pending_approvals)
            self.assertEqual(self.rpc.answer.call_args.args[1], {'decision': 'decline'})

    def test_network_only_request_shows_protocol_and_target(self):
        self.owned()
        self.bot.on_server_request(self.request('item/commandExecution/requestApproval',
            networkApprovalContext={'host': 'example.com', 'protocol': 'https'}))
        queued = self.bot.outbox.get_nowait()
        self.assertIn('example.com', queued['text']); self.assertIn('https', queued['text'])
        buttons = queued['reply_markup']['inline_keyboard'][0]
        self.assertTrue(any(':session' in b['callback_data'] for b in buttons))

    def test_permission_request_never_grants_unknown_decision_or_expands_scope(self):
        self.owned()
        permissions = {'network': {'enabled': True}}
        self.bot.on_server_request(self.request('item/permissions/requestApproval', permissions=permissions))
        key = next(iter(self.bot.pending_approvals))
        for choice in ('garbage', 'accept-everything'):
            with self.assertRaises(ValueError): self.bot._handle_approval_callback(key+':'+choice)
        self.rpc.answer.assert_not_called()
        self.bot._handle_approval_callback(key+':once')
        self.assertEqual(self.rpc.answer.call_args.args[1], {'permissions': permissions, 'scope': 'turn'})
        with self.assertRaises(ValueError): self.bot._handle_approval_callback(key+':once')
        self.assertEqual(self.rpc.answer.call_count, 1)

    def test_fast_completion_before_start_ack_does_not_resurrect_turn(self):
        self.owned(); self.bot.active_turns.clear()
        def request(method, params, **kwargs):
            self.bot.on_app_event({'method': 'item/completed', 'params': {'threadId': 'owned-thread', 'turnId': 'fast-turn',
                'item': {'type': 'agentMessage', 'id': 'answer', 'text': 'final', 'phase': 'final_answer'}}})
            self.bot.on_app_event({'method': 'turn/completed', 'params': {'threadId': 'owned-thread',
                'turn': {'id': 'fast-turn', 'status': 'completed'}}})
            return {'turn': {'id': 'fast-turn'}}
        self.rpc.request.side_effect = request
        self.bot._begin_turn('owned-thread', 'work')
        self.assertNotIn('owned-thread', self.bot.active_turns)
        self.assertIn('final', self.bot.outbox.get_nowait()['text'])

    def test_old_turn_completion_cannot_clear_new_turn(self):
        self.owned()
        self.bot.on_app_event({'method': 'turn/completed', 'params': {'threadId': 'owned-thread', 'turn': {'id': 'old-turn'}}})
        self.assertEqual(self.bot.active_turns['owned-thread'], 'turn-one')

    def test_failed_completion_carries_failure_reaction(self):
        self.owned()
        self.bot.on_app_event({'method': 'turn/completed', 'params': {'threadId': 'owned-thread',
            'turn': {'id': 'turn-one', 'status': 'failed'}}})
        self.assertEqual(self.bot.outbox.get_nowait()['outcome'], '😢')

    def test_failed_delivery_cannot_later_become_success_when_binding_arrives(self):
        n.complete_task_reactions(self.bot.token, 'owned-thread', 'failed-turn', outcome='😢')
        n.set_task_reaction(self.bot.token, self.bot.chat_id, 8, '🤔', 'owned-thread', 'failed-turn')
        self.assertEqual(self.api.call_args.args[2]['reaction'], [{'type': 'emoji', 'emoji': '😢'}])
        n.complete_task_reactions(self.bot.token, 'owned-thread', 'failed-turn')
        self.assertEqual(self.api.call_args.args[2]['reaction'], [{'type': 'emoji', 'emoji': '😢'}])

    def test_stale_question_cannot_submit_after_turn_changes(self):
        self.owned()
        self.bot.on_server_request(self.request('item/tool/requestUserInput', questions=[{'id': 'q', 'question': 'choose'}]))
        key = next(iter(self.bot.pending_questions))
        self.bot.active_turns['owned-thread'] = 'new-turn'
        with self.assertRaises(ValueError): self.bot._answer_question(key, 'answer')
        self.rpc.answer.assert_not_called()

    def test_mcp_url_requires_explicit_callback_and_unsafe_url_declines(self):
        self.owned()
        self.bot.on_server_request(self.request('mcpServer/elicitation/request', mode='url', url='http://example.com'))
        self.assertEqual(self.rpc.answer.call_args.args[1]['action'], 'decline')
        self.rpc.answer.reset_mock()
        self.bot.on_server_request(self.request('mcpServer/elicitation/request', mode='url', url='https://example.com', turnId=None))
        self.rpc.answer.assert_not_called()
        key = next(iter(self.bot.pending_approvals))
        self.bot._handle_approval_callback(key+':once')
        self.assertEqual(self.rpc.answer.call_args.args[1], {'action': 'accept', 'content': None})

class PollingRecoveryTests(unittest.TestCase):
    setUp = SharedControllerTests.setUp
    migrate = SharedControllerTests.migrate
    def test_polling_commits_offset_before_action_and_never_replays_duplicates(self):
        self.bot.state.update(update_offset=5)
        updates = [{'update_id': i, 'message': {'date': int(time.time()), 'message_id': i,
            'text': '/help', 'from': {'id': 42}, 'chat': {'id': 42, 'type': 'private'}}} for i in (4, 5, 5, 6)]
        handled = []
        def dispatch(update):
            handled.append((update['update_id'], self.bot.state.snapshot()['update_offset']))
        def poll(*args, **kwargs):
            self.bot.stop_event.set()
            return updates
        # Host exclusion has dedicated tests; this fixture only exercises polling.
        with patch.object(core, 'SingleInstance'), \
                patch.object(core.threading, 'Thread'), patch.object(core, 'telegram_call', side_effect=poll), \
                patch.object(self.bot, 'handle_update', side_effect=dispatch), \
                patch.object(self.bot, '_set_commands'), patch.object(self.bot, 'shutdown'), \
                patch.object(self.bot.plugins, 'start'), patch.object(self.bot.quota_resets, 'startup'), \
                patch.object(self.bot.async_inputs, 'start'), patch.object(self.bot, '_send_unreported_credit_baseline'):
            # A single polling iteration exits after consuming its batch.
            self.bot.stop_event.clear()
            core.TelegramCodexBot.run(self.bot, quiet_start=True)
        self.assertEqual(handled, [(5, 6), (6, 7)])
        self.assertEqual(self.bot.state.snapshot()['update_offset'], 7)
