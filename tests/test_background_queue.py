"""Exercise Telegram entry points, the durable send journal, and strict Desktop ownership."""
from concurrent.futures import ThreadPoolExecutor
import base64
from contextlib import closing, nullcontext
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from codex_telegram_bridge.codex import background_queue, desktop, receipts
from codex_telegram_bridge.codex.errors import (
    DesktopBridgeSafetyError, DesktopFollowerMissing, DesktopIpcUnavailable, DesktopSubmissionUnknown,
)
from codex_telegram_bridge.core import bot as core
from codex_telegram_bridge.platforms.windows.desktop_access import WindowsDesktopAccess
from codex_telegram_bridge.storage import desktop_queue as journal
from codex_telegram_bridge.storage import desktop_inbox as inbox
from codex_telegram_bridge.features import desktop_delivery as delivery
from codex_telegram_bridge.storage.state import StateStore
from codex_telegram_bridge.telegram import notifications as notify


class QueueTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.profile = Path(folder.name).resolve()
        self.thread = '01a00000-0000-7000-a000-000000000001'
        sessions = self.profile / 'sessions'
        sessions.mkdir()
        self.log = sessions / ('rollout-fixture-' + self.thread + '.jsonl')
        self.record('session_meta', {'id': self.thread, 'originator': 'Codex Desktop', 'source': 'vscode'})
        self.metadata = self.profile / 'state_5.sqlite'
        with closing(sqlite3.connect(self.metadata)) as db, db:
            db.execute('CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT, archived INTEGER)')
            db.execute('INSERT INTO threads VALUES(?,?,0)', (self.thread, str(self.log)))
        self.patch(notify, 'SESSION_ROOT', sessions)
        self.patch(notify, 'REACTION_DB', self.profile / 'bot.sqlite')
        self.patch(notify, 'set_task_reaction', Mock(return_value=True))
        self.bot = core.TelegramCodexBot('111:fixture', {'chat_id': '123'})
        self.bot.state = StateStore(self.profile / 'state.json')
        self.bot.send = Mock(return_value={'message_id': 99})
        self.bot.edit_message = Mock()
        self.bot._reaction = Mock(return_value=True)
        self.bot.async_inputs.reply_to = Mock(return_value=False)
        self.bot.sides.lookup = Mock(return_value=None)
        self.bot._answer_question_from_prompt = Mock(return_value=False)
        self.patch(core, 'lookup_task_message', Mock(return_value=self.thread))
        self.bot.resolve_thread = Mock(return_value=self.thread)
        self.bot.app = Mock()
        self.bot.desktop = Mock()
        self.bot.desktop.access = WindowsDesktopAccess()
        self.bot.desktop.timeout = 10
        self.endpoint = desktop.DesktopEndpoint(7, 'fixture', self.profile)
        self.bot.desktop.native.discover.return_value = self.endpoint
        self.owner = self.bot.desktop._legacy_for_current_desktop.return_value
        self.owner.find_thread_owner.side_effect = DesktopFollowerMissing('absent')
        self.live = {'id': self.thread, 'hostId': 'local', 'status': {'type': 'idle'}}
        self.bot.desktop._reader.return_value.read_thread.return_value = {'thread': self.live}
        self.client = Mock()
        self.client._request.side_effect = self.rpc
        self.channel = self.patch(background_queue, 'open_queue', Mock(return_value=nullcontext(self.client)))
        self.adds = []
        self.source = journal.key(self.bot.token, self.bot.chat_id, 70)

    def patch(self, owner, name, value):
        p = patch.object(owner, name, value)
        self.addCleanup(p.stop)
        return p.start()

    def enable_inbox_fixture(self):
        from codex_telegram_bridge.platforms.windows import credentials
        self.scope = self.patch(delivery, 'account_binding', Mock(return_value='fixture-account'))
        self.patch(credentials, 'protect_secret', lambda text: base64.b64encode(text.encode()).decode())
        self.patch(credentials, 'unprotect_secret', lambda body: base64.b64decode(body).decode())

    def admit_message(self, *, number=70, update=100, kind='send', date=None, sender=123):
        message = {'message_id': number, 'date': int(date or time.time()), 'chat': {'id': 123, 'type': 'private'},
                   'from': {'id': sender, 'is_bot': False}, 'text': '/send id same' if kind=='send' else 'same'}
        if kind == 'reply':
            message['reply_to_message'] = {'message_id': 15}
        incoming = {'update_id': update, 'message': message}
        self.bot._ui_local.poll_update = incoming
        self.bot.handle_update(incoming)
        self.bot._ui_local.poll_update = None

    def inbox_row(self, message=70):
        with closing(inbox.connect()) as db:
            return db.execute('SELECT * FROM desktop_inbox WHERE message=?', (message,)).fetchone()

    def record(self, kind, payload):
        with self.log.open('a', encoding='utf8') as stream:
            stream.write(json.dumps({'type': kind, 'payload': payload}) + '\n')

    def user_item(self, client, *, thread=None, body='same', item='item1', turn='turn1'):
        self.record('event_msg', {'type': 'item_completed', 'thread_id': thread or self.thread, 'turn_id': turn,
                    'item': {'type': 'UserMessage', 'id': item, 'client_id': client,
                             'content': [{'type': 'text', 'text': body}]}})

    def rpc(self, method, params, *, timeout, ensure):
        self.assertFalse(ensure)
        self.assertEqual(params['threadId'], self.thread)
        if method == 'thread/queue/add':
            self.assertEqual(timeout, 10)
            entry = {**params, 'id': 'queue-' + str(len(self.adds))}
            self.adds.append(entry)
            return {'queuedSubmission': entry}
        self.assertEqual(method, 'thread/queue/list')
        return {'data': self.adds}

    def update(self, text, *, message=70, sender=123, reply=False):
        data = {'message_id': message, 'text': text, 'chat': {'id': 123, 'type': 'private'},
                'from': {'id': sender, 'is_bot': False}}
        if reply:
            data['reply_to_message'] = {'message_id': 15}
        self.bot.handle_update({'message': data})

    def test_direct_reply_selected_send_and_inline_send(self):
        self.update('idle', reply=True)
        self.live['status']['type'] = 'active'
        self.bot.awaiting_continue['123'] = self.thread
        self.update('busy', message=71)
        self.live['status']['type'] = 'notLoaded'
        self.update('/send id cold', message=72)
        self.assertEqual([entry['input'][0]['text'] for entry in self.adds], ['idle', 'busy', 'cold'])
        self.assertEqual(len({entry['clientUserMessageId'] for entry in self.adds}), 3)
        for word, call in zip(('等待原任务接收', '当前轮结束后', '加载原任务后'), self.bot.send.call_args_list):
            self.assertIn(word, call.args[0])
            self.assertEqual(call.args[1]['inline_keyboard'][0][0]['callback_data'], 'task|' + self.thread)
        self.bot.app.request.assert_not_called()
        self.bot.desktop._recover_thread_owner.assert_not_called()
        self.owner.send_follow_up.assert_not_called()

    def test_replayed_update_after_restart_or_owner_change_is_never_resent(self):
        self.update('same', reply=True)
        self.owner.find_thread_owner.side_effect = None
        self.owner.find_thread_owner.return_value = True
        self.bot.start_turn = Mock()
        self.update('same', reply=True)
        self.assertEqual(len(self.adds), 1)
        self.bot.start_turn.assert_not_called()
        with self.assertRaises(DesktopBridgeSafetyError):
            self.update('modified', reply=True)

    def test_lost_ack_uses_list_only_then_exact_rollout_after_consumption(self):
        def lost(method, params, **options):
            result = self.rpc(method, params, **options)
            if method == 'thread/queue/add':
                raise DesktopSubmissionUnknown('wire closed')
            return result
        self.client._request.side_effect = lost
        self.update('same', reply=True)
        self.assertEqual(journal.existing(self.source, self.thread, 'same'), 'queued')
        self.assertEqual([c.args[0] for c in self.client._request.call_args_list], ['thread/queue/add', 'thread/queue/list'])
        with patch.object(self.client, '_request', return_value={'data': []}):
            self.update('same', message=71, reply=True)
            self.assertIn('待核实', self.bot.send.call_args.args[0])
            self.update('same', message=71, reply=True)
            self.assertEqual(self.client._request.call_count, 2)

    def test_receipt_needs_thread_and_client_identity_even_for_identical_text(self):
        self.update('same', reply=True)
        self.update('same', message=71, reply=True)
        first, second = [x['clientUserMessageId'] for x in self.adds]
        self.user_item(first, thread='wrong-thread')
        self.user_item(first, body='wrong body')
        self.record('response_item', {'type': 'message', 'role': 'user', 'id': 'by-hand',
                    'content': [{'type': 'input_text', 'text': 'same'}],
                    'internal_chat_message_metadata_passthrough': {'turn_id': 'manual', 'create_time': time.time()+0.1}})
        self.user_item(second, item='item2', turn='turn2')
        notify.resolve_desktop_submissions(self.bot.token, update_reactions=False)
        self.assertEqual(journal.existing(self.source, self.thread, 'same'), 'queued')
        self.assertFalse(notify.bind_desktop_submission(self.bot.token, '123', 70, self.thread, 'wrong', 'wrong'))
        self.assertEqual(notify.desktop_submission_receipt(self.bot.token, '123', 71, self.thread),
                         {'item_id': 'item2', 'turn_id': 'turn2'})
        self.user_item(first)
        notify.resolve_desktop_submissions(self.bot.token, update_reactions=False)
        self.assertEqual(journal.existing(self.source, self.thread, 'same'), 'received')
        with patch.object(journal.time, 'time', return_value=time.time()+10):
            journal.update_cards(self.bot)
            journal.update_cards(self.bot)
        self.assertEqual(self.bot.edit_message.call_count, 2)

    def test_concurrent_claims_and_old_unconfirmed_claim(self):
        barrier = threading.Barrier(2)
        def contender(_):
            barrier.wait()
            return journal.claim(self.source, self.thread, 'same')
        with ThreadPoolExecutor(2) as pool:
            claims = list(pool.map(contender, (1, 2)))
        self.assertEqual(sum(bool(v) for v in claims), 1)
        with closing(notify._reaction_db()) as db, db:
            db.execute('UPDATE desktop_submissions SET created=0')
        notify.remember_desktop_submission(self.bot.token, '123', 75, self.thread, 'different')
        notify.forget_desktop_submission(self.bot.token, '123', 70, self.thread)
        self.update('same', reply=True)
        self.assertEqual(self.adds, [])
        self.user_item(next(filter(None, claims)))
        notify.resolve_desktop_submissions(self.bot.token, update_reactions=False)
        self.assertEqual(journal.existing(self.source, self.thread, 'same'), 'received')

    def test_restart_after_claim_or_lost_telegram_card_recovers_feedback_only(self):
        self.bot.send.side_effect = TimeoutError('status delivery uncertain')
        self.update('same', reply=True)
        self.assertEqual(journal.existing(self.source, self.thread, 'same'), 'queued')
        journal.claim(journal.key(self.bot.token, '123', 71), self.thread, 'other')
        self.bot.send.side_effect = None
        self.bot.send.reset_mock()
        with patch.object(journal.time, 'time', return_value=time.time()+95):
            journal.update_cards(self.bot)
            journal.update_cards(self.bot)
        self.assertEqual(len(self.adds), 1)
        self.assertEqual([c.kwargs['reply_to'] for c in self.bot.send.call_args_list], [70, 71])
        self.assertIn('已排队', self.bot.send.call_args_list[0].args[0])
        self.assertIn('待核实', self.bot.send.call_args_list[1].args[0])

    def test_pre_send_instance_change_releases_only_an_unwritten_reservation(self):
        self.bot.desktop._check_instance.side_effect = [None, DesktopBridgeSafetyError('restarted')]
        with self.assertRaises(DesktopBridgeSafetyError):
            self.update('same', reply=True)
        self.assertIsNone(journal.existing(self.source, self.thread, 'same'))
        self.assertEqual(self.adds, [])

    def test_claim_blocks_legacy_writer_racing_the_queue(self):
        journal.claim(self.source, self.thread, 'same')
        with self.assertRaises(DesktopSubmissionUnknown):
            notify.remember_desktop_submission(self.bot.token, '123', 70, self.thread, 'same')
        prior = journal.key(self.bot.token, '123', 71)
        notify.remember_desktop_submission(self.bot.token, '123', 71, self.thread, 'same')
        with self.assertRaises(DesktopSubmissionUnknown):
            journal.claim(prior, self.thread, 'same')

    def test_unauthorized_quiet_and_managed_tasks_never_enter_queue(self):
        self.update('message', sender=456, reply=True)
        self.bot.state.update(quiet_mode=True)
        self.update('message', reply=True)
        self.bot.state.update(quiet_mode=False, managed_threads={self.thread: {'origin': 'bot_created'}})
        self.bot.start_turn = Mock()
        self.update('/send id message')
        self.bot.start_turn.assert_called_once()
        self.channel.assert_not_called()

    def test_arbitrary_pipe_failure_and_wrong_live_identity_fail_before_add(self):
        for exc in (DesktopBridgeSafetyError('wrong owner'), DesktopIpcUnavailable('unverified')):
            with self.subTest(exc=exc), self.assertRaises(type(exc)):
                self.owner.find_thread_owner.side_effect = exc
                self.update('message', reply=True)
        self.owner.find_thread_owner.side_effect = DesktopFollowerMissing('absent')
        for field, value in [('id', 'different'), ('hostId', 'remote'), ('status', {'type': 'unknown'})]:
            with self.subTest(field=field), patch.dict(self.live, {field: value}), self.assertRaises(DesktopBridgeSafetyError):
                self.update('message', reply=True)
        with closing(sqlite3.connect(self.metadata)) as db, db:
            db.execute('UPDATE threads SET archived=1')
        with self.assertRaises(DesktopBridgeSafetyError):
            self.update('message', reply=True)
        self.channel.assert_not_called()

    def test_live_owner_and_explicit_steer_do_not_enqueue(self):
        self.owner.find_thread_owner.side_effect = None
        self.owner.find_thread_owner.return_value = True
        self.bot.start_turn = Mock(side_effect=DesktopSubmissionUnknown('lost legacy acknowledgement'))
        with self.assertRaises(DesktopSubmissionUnknown):
            self.update('/send id same')
        self.bot.steer_turn = Mock()
        self.bot.awaiting_steer['123'] = self.thread
        self.update('strict', message=71)
        self.bot.steer_turn.assert_called_once_with(self.thread, 'strict')
        self.channel.assert_not_called()

    def test_durable_receive_and_offset_rollback_together_then_recover_once(self):
        self.enable_inbox_fixture()
        with closing(inbox.connect()) as db, db:
            db.execute("CREATE TRIGGER disk_error BEFORE INSERT ON desktop_poll_offsets BEGIN SELECT RAISE(ABORT,'disk error'); END")
        with self.assertRaises(delivery.IntakeUnavailable):
            self.admit_message()
        self.assertIsNone(self.inbox_row())
        self.assertEqual(inbox.offset('111'), 0)
        with closing(inbox.connect()) as db, db:
            db.execute('DROP TRIGGER disk_error')
        self.admit_message()
        self.assertEqual(inbox.offset('111'),101)
        self.assertNotEqual(self.inbox_row()['body'], 'same')
        self.assertEqual(self.adds, [])
        inbox.recover('111','123')
        delivery.drain(self.bot)
        self.admit_message()
        delivery.drain(self.bot)
        self.assertEqual(len(self.adds), 1)
        self.assertEqual((self.inbox_row()['phase'],self.inbox_row()['body']), ('done',''))

    def test_service_restarts_either_side_of_the_real_polling_boundary(self):
        self.enable_inbox_fixture()
        for name in ('_start_service_worker', '_set_commands', 'shutdown'):
            setattr(self.bot, name, Mock())
        self.bot.plugins.start = self.bot.quota_resets.startup = self.bot.async_inputs.start = Mock()
        self.bot.state.update(update_offset=100)
        payload = {'update_id':100,'message':{'message_id':70,'text':'/send id same','chat':{'id':123},
                                              'from':{'id':123},'date':int(time.time())}}
        def poll(token, method, args, **options):
            self.assertEqual(method, 'getUpdates')
            if args['offset'] <= 100:
                return [payload]
            self.bot.stop_event.set()
            return []
        with patch.object(core, 'telegram_call', side_effect=poll):
            with patch.object(self.bot, 'handle_update', side_effect=KeyboardInterrupt):
                self.bot._run_service(quiet_start=True)
            self.assertEqual(self.bot.state.snapshot()['update_offset'],100)
            self.assertIsNone(self.inbox_row())
            original=inbox.receive
            def crash_after_commit(*args):
                original(*args)
                raise KeyboardInterrupt
            self.bot.stop_event.clear()
            with patch.object(inbox, 'receive', side_effect=crash_after_commit):
                self.bot._run_service(quiet_start=True)
            self.assertEqual(inbox.offset('111'),101)
            self.assertEqual(self.adds,[])
            self.bot.stop_event.clear()
            self.bot._run_service(quiet_start=True)
            self.assertEqual(len(self.adds),1)

    def test_offline_retry_survives_restart_without_reordering_same_chat(self):
        self.enable_inbox_fixture()
        self.admit_message(kind='reply')
        self.admit_message(number=71,update=101)
        self.bot.desktop.native.discover.side_effect=DesktopIpcUnavailable('offline')
        delivery.drain(self.bot)
        self.assertEqual(self.inbox_row()['phase'],'ready')
        self.assertEqual(self.inbox_row(71)['attempts'],0)
        self.bot.desktop.native.discover.side_effect=None
        with patch.object(inbox.time,'time',return_value=time.time()+8):
            inbox.recover('111','123')
            delivery.drain(self.bot)
            delivery.drain(self.bot)
        self.assertEqual(len(self.adds),2)
        with closing(notify._reaction_db()) as db:
            ordered=db.execute('SELECT message FROM desktop_queue_sends ORDER BY rowid').fetchall()
        self.assertEqual(ordered, [(70,),(71,)])

    def test_expired_inbox_needs_sender_confirmation_and_cancel_is_final(self):
        self.enable_inbox_fixture()
        self.admit_message(date=time.time()-620)
        self.admit_message(number=71,update=101,date=time.time()-620)
        delivery.drain(self.bot)
        delivery.drain(self.bot)
        first=self.inbox_row()
        self.assertEqual(first['phase'],'confirm')
        self.assertEqual(self.adds,[])
        self.bot._ui_local.sender_id='456'
        with self.assertRaises(ValueError):
            delivery.confirm(self.bot,first['nonce'],True)
        self.bot._ui_local.sender_id='123'
        delivery.confirm(self.bot,first['nonce'],True)
        with self.assertRaises(ValueError):
            delivery.confirm(self.bot,first['nonce'],True)
        delivery.confirm(self.bot,self.inbox_row(71)['nonce'],False)
        delivery.drain(self.bot)
        self.assertEqual(len(self.adds),1)
        self.assertEqual(self.inbox_row(71)['body'],'')

    def test_account_change_quiet_generation_and_stale_dispatch_cannot_send(self):
        self.enable_inbox_fixture()
        self.admit_message()
        self.scope.return_value='different'
        delivery.drain(self.bot)
        self.assertEqual(self.inbox_row()['body'],'')
        self.admit_message(number=71,update=101)
        self.bot.state.update(quiet_generation=1)
        delivery.drain(self.bot)
        self.admit_message(number=72,update=102)
        inbox.writing(journal.key(self.bot.token,'123',72))
        inbox.recover('111','123')
        delivery.drain(self.bot)
        self.assertEqual(self.inbox_row(72)['phase'],'done')
        self.assertEqual(self.inbox_row(71)['phase'],'done')
        self.assertEqual(self.adds,[])

    def test_owner_disappears_before_write_enters_queue_once(self):
        self.enable_inbox_fixture()
        self.admit_message()
        self.owner.find_thread_owner.side_effect=None
        self.owner.find_thread_owner.return_value=True
        self.bot.app.request.return_value={'thread':{'id':self.thread,'cwd':'fixture','status':{'type':'idle'}}}
        self.bot.desktop.send_follow_up.side_effect=DesktopFollowerMissing('before first write')
        delivery.drain(self.bot)
        self.assertEqual(len(self.adds),1)
        self.assertEqual(self.inbox_row()['phase'],'done')

    def test_legacy_unknown_after_durable_guard_never_changes_route(self):
        self.enable_inbox_fixture()
        self.admit_message()
        self.owner.find_thread_owner.side_effect=None
        self.owner.find_thread_owner.return_value=True
        self.bot.app.request.return_value={'thread':{'id':self.thread,'cwd':'fixture','status':{'type':'idle'}}}
        def lost(*args, before_start, **kwargs):
            before_start(self.endpoint)
            self.assertEqual(self.inbox_row()['phase'],'sending')
            raise DesktopSubmissionUnknown('possibly written')
        self.bot.desktop.send_follow_up.side_effect=lost
        self.bot._reconcile_desktop_submission=Mock(return_value=None)
        delivery.drain(self.bot)
        inbox.recover('111','123')
        delivery.drain(self.bot)
        self.assertEqual(self.bot.desktop.send_follow_up.call_count,1)
        self.assertEqual(self.adds,[])

    def test_fresh_read_only_recheck_pages_and_edits_existing_unknown_card(self):
        self.enable_inbox_fixture()
        self.admit_message()
        self.client._request.side_effect=DesktopSubmissionUnknown('original connection lost')
        delivery.drain(self.bot)
        with closing(notify._reaction_db()) as db:
            cid=db.execute('SELECT client FROM desktop_queue_sends').fetchone()[0]
        fresh=Mock()
        hit={'id':'q1','clientUserMessageId':cid,'input':[{'type':'text','text':'same'}]}
        fresh._request.side_effect=[{'data':[],'nextCursor':'page2'},{'data':[hit],'nextCursor':None}]
        self.channel.return_value=nullcontext(fresh)
        with patch.object(inbox.time,'time',return_value=time.time()+15):
            journal.recheck_uncertain(self.bot)
            journal.update_cards(self.bot)
            journal.update_cards(self.bot)
        self.assertEqual(fresh._request.call_args_list[1].args[1]['cursor'],'page2')
        self.assertTrue(all(c.args[0]=='thread/queue/list' for c in fresh._request.call_args_list))
        self.assertEqual(journal.existing(self.source,self.thread,'same'),'queued')
        self.bot.edit_message.assert_called_once()
        self.assertIn('已排队',self.bot.edit_message.call_args.args[1])

    def test_ambiguous_two_page_match_and_cursor_cycle_never_acknowledge(self):
        first={'id':'one','clientUserMessageId':'cid','input':[{'type':'text','text':'same'}]}
        for page2 in ({'data':[{**first,'id':'two'}]}, {'data':[],'nextCursor':'repeat'},
                      {'data':[{**first,'input':[{'type':'text','text':'other'}]}]},
                      {'data':[{**first,'threadId':'wrong-task'}]}):
            with self.subTest(second=page2):
                client=Mock()
                client._request.side_effect=[{'data':[first],'nextCursor':'repeat'},page2]
                self.assertIsNone(background_queue.lookup_entry(client,self.thread,'cid',receipts.prompt_digest(self.thread,'same')))
                self.assertEqual(client._request.call_count,2)

    def test_rate_limit_retries_only_status_after_requested_wait(self):
        self.update('same',reply=True)
        with closing(notify._reaction_db()) as db, db:
            db.execute('UPDATE desktop_queue_sends SET card=NULL')
        self.bot.send.reset_mock()
        self.bot.send.side_effect=notify.TelegramApiError(429,'limited',{'retry_after':7})
        now=time.time()+95
        with patch.object(inbox.time,'time',return_value=now):
            journal.update_cards(self.bot)
        self.bot.send.side_effect=None
        with patch.object(inbox.time,'time',return_value=now+6):
            journal.update_cards(self.bot)
        self.assertEqual(self.bot.send.call_count,1)
        with patch.object(inbox.time,'time',return_value=now+8):
            journal.update_cards(self.bot)
            journal.update_cards(self.bot)
        self.assertEqual(self.bot.send.call_count,2)
        self.assertEqual(len(self.adds),1)

    @unittest.skipUnless(os.name == 'nt', 'Windows drive paths')
    def test_extended_path_still_verifies_original_file(self):
        with closing(sqlite3.connect(self.metadata)) as db, db:
            db.execute('UPDATE threads SET rollout_path=?', ('\\\\?\\' + str(self.log),))
        background_queue.validate_local_desktop_thread(self.endpoint, self.thread)
        self.assertEqual(receipts.session_path(self.thread, self.profile/'sessions'), self.log)

    @unittest.skipUnless(os.name == 'nt', 'Windows current-user DPAPI')
    def test_process_exit_during_transaction_and_immediately_after_commit(self):
        from codex_telegram_bridge.platforms.windows.credentials import unprotect_secret
        marker = 'unique crash-test plaintext'
        script = """
import os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from codex_telegram_bridge.storage import desktop_inbox as inbox
from codex_telegram_bridge.platforms.windows.credentials import protect_secret
inbox.notify.REACTION_DB = Path(sys.argv[2])
original = inbox.connect
def connect():
    db = original()
    def interrupt(statement):
        if statement.startswith('INSERT INTO desktop_poll_offsets') and sys.argv[3] == 'during':
            os._exit(73)
    db.set_trace_callback(interrupt)
    return db
inbox.connect = connect
inbox.receive(('111','123',70), 100, '123', sys.argv[4], 'send', protect_secret(sys.argv[5]), 'scope', 0, time.time())
os._exit(74)
"""
        for stage, code in [('during', 73), ('after', 74)]:
            process = subprocess.run([sys.executable, '-I', '-B', '-c', script,
                                      str(Path(inbox.__file__).resolve().parents[2]), str(notify.REACTION_DB),
                                      stage, self.thread, marker], timeout=20, capture_output=True)
            self.assertEqual(process.returncode, code, process.stderr[-500:])
            inbox.recover('111', '123')
            if stage == 'during':
                self.assertEqual(inbox.offset('111'), 0)
                self.assertIsNone(self.inbox_row())
            else:
                rows = inbox.due('111', '123')
                self.assertEqual(len(rows), 1)
                self.assertEqual((inbox.offset('111'), unprotect_secret(rows[0]['body'])), (101, marker))
                self.assertTrue(all(marker.encode() not in file.read_bytes() for file in self.profile.glob('bot.sqlite*')))

    def test_group_selection_survives_restart_belongs_to_sender_and_is_consumed_once(self):
        self.enable_inbox_fixture()
        self.bot.chat_id = '-999'
        self.bot.allowed_user_ids = {'123', '456'}
        def select(sender):
            self.bot._ui_local.sender_id = sender
            self.bot._request_task_message(self.thread, 'send')
            return self.bot.input_selection['nonce']
        def text_update(sender, number):
            message = {'message_id': number, 'text': 'same', 'chat': {'id': -999}, 'from': {'id': int(sender)}}
            update = {'update_id': 200 + number, 'message': message}
            self.bot._ui_local.poll_update = update
            try:
                self.bot.handle_update(update)
            finally:
                self.bot._ui_local.poll_update = None
        first = select('123')
        second = select('456')
        self.bot.awaiting_continue.clear()
        self.bot.input_selection = {}
        self.assertNotEqual(first, second)
        self.assertIsNone(inbox.selection(('111', '-998', '123')))
        inbox.clear_selection(('111', '-999', '456'), first)
        self.assertEqual(inbox.selection(('111', '-999', '456'))['nonce'], second)
        text_update('123', 70)
        self.assertIsNone(inbox.selection(('111', '-999', '123')))
        self.assertEqual(inbox.selection(('111', '-999', '456'))['nonce'], second)
        delivery.drain(self.bot)
        text_update('123', 71)
        self.assertIsNone(self.inbox_row(71))
        self.assertEqual(len(self.adds), 1)
        select('123')
        self.scope.return_value = 'different-account'
        text_update('123', 72)
        self.assertEqual(self.inbox_row(72)['phase'], 'done')
        self.assertEqual(self.inbox_row(72)['body'], '')
        with patch.object(inbox.time, 'time', return_value=time.time() + inbox.AUTO_SECONDS + 1):
            self.assertIsNone(inbox.selection(('111', '-999', '456')))
            text_update('456', 73)
        self.assertIsNone(self.inbox_row(73))
        self.assertEqual(len(self.adds), 1)

    def test_unverified_account_does_not_block_later_telegram_updates(self):
        self.enable_inbox_fixture()
        self.scope.return_value = ''
        self.admit_message()
        self.assertEqual((self.inbox_row()['body'], self.inbox_row()['notice']), ('', 'unbound'))
        self.assertEqual(inbox.offset('111'), 101)
        self.scope.return_value = 'valid'
        self.admit_message(number=71, update=101)
        delivery.drain(self.bot)
        self.assertEqual(inbox.offset('111'), 102)
        self.assertEqual(len(self.adds), 1)

    def test_rate_limited_card_edit_keeps_card_id_and_only_retries_feedback(self):
        self.update('same', reply=True)
        with closing(notify._reaction_db()) as db, db:
            db.execute("UPDATE desktop_submissions SET turn='verified-turn',item='verified-item'")
        self.bot.edit_message.side_effect = notify.TelegramApiError(429, 'limited', {'retry_after': 20})
        now = time.time() + 5
        with patch.object(inbox.time, 'time', return_value=now):
            journal.update_cards(self.bot)
        self.bot.edit_message.side_effect = None
        with patch.object(inbox.time, 'time', return_value=now + 18):
            journal.update_cards(self.bot)
        self.assertEqual(self.bot.edit_message.call_count, 1)
        with patch.object(inbox.time, 'time', return_value=now + 21):
            journal.update_cards(self.bot)
            journal.update_cards(self.bot)
        self.assertEqual([call.args[0] for call in self.bot.edit_message.call_args_list], [99, 99])
        self.assertEqual(len(self.adds), 1)

    def test_queue_runtime_requires_same_registered_desktop_package_and_store(self):
        from codex_telegram_bridge.platforms.windows import runtime, discovery
        pkg = self.profile/'OpenAI.Codex_1.2.3.4_x64__2p2nqsd0c76g0'
        executable = pkg/'app/resources/codex.exe'
        executable.parent.mkdir(parents=True)
        executable.touch()
        proc = Mock()
        proc.exe.return_value = str(pkg/'app/chatgpt.exe')
        with patch('psutil.Process',return_value=proc), patch.object(runtime, '_registered_package_path', return_value=pkg) as registered, \
                patch.object(discovery, '_process_environment', return_value={}) as environment:
            access = WindowsDesktopAccess()
            self.assertEqual(access.queue_runtime(self.endpoint)[0], executable)
            environment.return_value = {'CODEX_SQLITE_HOME': str(self.profile/'other')}
            with self.assertRaises(DesktopBridgeSafetyError):
                access.queue_runtime(self.endpoint)
            environment.return_value = {}
            registered.return_value = None
            with self.assertRaises(DesktopBridgeSafetyError):
                access.queue_runtime(self.endpoint)
            proc.exe.return_value = str(pkg/'app/Code.exe')
            with self.assertRaises(DesktopBridgeSafetyError):
                access.queue_runtime(self.endpoint)
