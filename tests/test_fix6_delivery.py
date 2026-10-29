"""R17/R19: no Telegram, accounts, Desktop, or production state access."""
import json
from contextlib import closing, ExitStack
import io
import os
import socket
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from urllib.error import HTTPError
from unittest.mock import Mock, patch

import test_controller_delivery as fixtures
import test_completion_routing as completion_fixture
from codex_telegram_bridge.core import bot as core
from codex_telegram_bridge.storage.bridge import BridgeState
from codex_telegram_bridge.platforms import locking
from codex_telegram_bridge.telegram import notifications as n
from codex_telegram_bridge.telegram.notifications import telegram_call as call_json
from codex_telegram_bridge.telegram import pending_completions as pending
from codex_telegram_bridge.telegram import notification_diagnostics as diagnostics
from codex_telegram_bridge.telegram.controller_delivery import ControllerDelivery
from codex_telegram_bridge.telegram.delivery import TelegramDeliveryUnknown, delivery_error


class DefaultDocumentTests(unittest.TestCase):
    def send(self):
        return n.send_document('111:fake', chat_id='42', filename='reply.md', content=b'original')

    def test_uncertain_upload_is_never_repeated(self):
        for error in (TimeoutError('response lost'), RuntimeError('connection lost'), n.TelegramApiError(502, 'gateway')):
            with self.subTest(error=type(error).__name__), patch.object(n, 'telegram_multipart_call', side_effect=error) as upload, patch.object(n.time, 'sleep') as sleep:
                with self.assertRaises(TelegramDeliveryUnknown):self.send()
                self.assertEqual(upload.call_count, 1);sleep.assert_not_called()

    def test_invalid_success_receipt_is_unknown_once(self):
        for result in (None, {}, {'message_id': True}, {'message_id': 0}, {'message_id': -1}, {'message_id': '7'}):
            with self.subTest(result=result), patch.object(n, 'telegram_multipart_call', return_value=result) as upload:
                with self.assertRaises(TelegramDeliveryUnknown):self.send()
                self.assertEqual(upload.call_count, 1)

    def test_explicit_429_uses_server_delays_and_same_bytes(self):
        errors=[n.TelegramApiError(429, 'limited', {'retry_after': 7}), n.TelegramApiError(429, 'retry after 11'), {'message_id': 7}]
        with patch.object(n, 'telegram_multipart_call', side_effect=errors) as upload, patch.object(n.time, 'sleep') as sleep:
            self.assertEqual(self.send(), {'message_id': 7})
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [7, 11])
            self.assertEqual([call.kwargs['content'] for call in upload.call_args_list], [b'original']*3)

    def test_long_rate_limit_does_not_block_or_retry_early(self):
        with patch.object(n, 'telegram_multipart_call', side_effect=n.TelegramApiError(429, 'limited', {'retry_after': 90})) as upload, patch.object(n.time, 'sleep') as sleep:
            with self.assertRaises(n.TelegramApiError) as error:self.send()
            self.assertEqual(error.exception.retry_after, 90)
            self.assertEqual(upload.call_count, 1);sleep.assert_not_called()

    def test_default_wait_budget_and_attempts_are_bounded(self):
        with patch.object(n, 'telegram_multipart_call', side_effect=n.TelegramApiError(429, 'limited', {'retry_after': 20})) as upload, patch.object(n.time, 'sleep') as sleep:
            with self.assertRaises(n.TelegramApiError):self.send()
            self.assertEqual(upload.call_count, 2)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [20])

    def test_definite_rejection_is_not_retried(self):
        with patch.object(n, 'telegram_multipart_call', side_effect=n.TelegramApiError(403, 'forbidden')) as upload:
            with self.assertRaises(n.TelegramApiError):self.send()
            self.assertEqual(upload.call_count, 1)

    def test_malformed_upload_response_is_unknown(self):
        for raw in (b'not json', b'[]', b'{"ok": false, "error_code": "bad"}'):
            with self.subTest(raw=raw), patch.object(n, 'urlopen') as urlopen:
                urlopen.return_value.__enter__.return_value.read.return_value=raw
                with self.assertRaises(TelegramDeliveryUnknown):self.send()
                self.assertEqual(urlopen.call_count, 1)

    def test_shared_policy_does_not_shorten_long_server_delay(self):
        self.assertEqual(delivery_error(n.TelegramApiError(429, 'limited', {'retry_after': 100000})), ('retry', 100000))
        for invalid in ('bad', float('nan'), float('inf'), -1):
            self.assertEqual(delivery_error(n.TelegramApiError(429, 'limited', {'retry_after': invalid})), ('retry', 30))


class DurableDeliveryTests(unittest.TestCase):
    setUp=fixtures.ControllerDeliveryTests.setUp
    migrate=fixtures.ControllerDeliveryTests.migrate
    ready=fixtures.ControllerDeliveryTests.ready
    emojis=fixtures.ControllerDeliveryTests.emojis

    def rows(self):
        return [dict(row) for row in self.legacy.db.execute('SELECT * FROM outbox ORDER BY id')]

    def watch(self, thread='task', message=70, version='v1', chat='42'):
        self.bot.watches[thread]={'message_id':message,'delivery_version':version,'chat_id':chat}
        return {'thread':thread,'version':version}

    def progress(self, text, thread='task', message=70, version='v1', chat='42'):
        self.bot.enqueue_edit(message, text, chat_id=chat, progress={'thread':thread,'version':version})
        return self.rows()[-1]['id']

    def test_document_snapshot_survives_restart_and_cache_change(self):
        upload=Mock(side_effect=[n.TelegramApiError(429, 'limited', {'retry_after':60}), {'message_id':701}])
        with patch.object(n, 'telegram_multipart_call', upload), patch.object(core, 'load_cached_reply', return_value={'text':'original\n😀','workspace':'test'}) as cache:
            self.bot.send_cached_reply_file('cached')
            identifier=self.rows()[0]['id']
            self.assertEqual(self.legacy.message_upload(identifier)['content'], 'original\n😀'.encode())
            self.legacy.close()
            self.legacy=BridgeState(self.root/'legacy.db');self.addCleanup(self.legacy.close)
            self.bot.delivery=ControllerDelivery(self.bot,self.legacy)
            cache.return_value={'text':'changed','workspace':'different'}
            self.ready();self.bot.delivery.tick()
        self.assertEqual(upload.call_count,2)
        self.assertEqual([call.kwargs['content'] for call in upload.call_args_list], ['original\n😀'.encode()]*2)
        self.assertEqual(cache.call_count,1)
        self.assertIsNone(self.legacy.message_upload(identifier))

    def test_document_timeout_has_uncertain_feedback_and_no_retry(self):
        with patch.object(n, 'telegram_multipart_call', side_effect=TimeoutError('response lost')) as upload:
            with self.assertRaises(TelegramDeliveryUnknown):
                with self.bot.delivery.feedback.operation(10,10):
                    self.bot.delivery.send_document(filename='reply.md',content=b'original')
            self.bot.delivery.tick(limit=8)
        self.assertEqual(upload.call_count,1)
        self.assertEqual(self.rows()[0]['status'],'unknown')
        self.assertEqual(self.emojis()[-1],'👀');self.assertNotIn('😢',self.emojis());self.assertNotIn('🎉',self.emojis())

    def test_default_controller_download_keeps_uncertain_feedback_without_durable_queue(self):
        self.bot.delivery=None
        callback={'update_id':10,'callback_query':{'id':'callback','from':{'id':42},'data':'replyfile|cache',
                  'message':{'message_id':10,'chat':{'id':42}}}}
        with patch.object(n,'telegram_multipart_call',side_effect=TimeoutError('unknown')) as upload, patch.object(core,'load_cached_reply',return_value={'text':'original','workspace':'test'}):
            self.bot.handle_update(callback)
        self.assertEqual(upload.call_count,1);self.assertEqual(self.rows(),[])
        self.assertEqual(self.emojis()[-1],'👀');self.assertNotIn('😢',self.emojis());self.assertNotIn('🎉',self.emojis())
        self.rpc.request.assert_not_called()

    def test_restart_does_not_replay_inflight_document(self):
        identifier=self.legacy.queue_message('sendDocument',{'chat_id':'42','_upload':True},{'controller':True},
                    upload={'filename':'reply.md','content_type':'text/markdown','content':b'original'})
        self.legacy.take_message(identifier)
        self.bot.delivery=ControllerDelivery(self.bot,self.legacy)
        with patch.object(n,'telegram_multipart_call') as upload:self.bot.delivery.tick(limit=8)
        upload.assert_not_called();self.assertEqual(self.rows()[0]['status'],'unknown')
        self.assertIsNone(self.legacy.message_upload(identifier))

    def test_document_429_blocks_progress_and_final_until_deadline_then_unknown_remains_unknown(self):
        self.watch()
        with patch.object(n, 'telegram_multipart_call', side_effect=[n.TelegramApiError(429,'limited',{'retry_after':10}),TimeoutError('unknown')]) as upload:
            with self.bot.delivery.feedback.operation(10,10):
                self.bot.delivery.send_document(filename='reply.md',content=b'original')
            for i in range(8):self.progress(str(i))
            self.bot.enqueue('final')
            self.bot.delivery.tick(limit=8)
            self.assertEqual(upload.call_count,1)
            self.assertFalse([m for m,p in self.calls if m!='setMessageReaction'])
            self.ready();self.bot.delivery.tick(limit=8)
            self.bot.delivery.tick(limit=8)
        self.assertEqual(upload.call_count,2)
        self.assertEqual(self.emojis()[-1],'👀');self.assertNotIn('😢',self.emojis());self.assertNotIn('🎉',self.emojis())
        self.assertEqual([r['status'] for r in self.rows()],['unknown','sent','sent'])

    def test_document_receipt_is_validated_in_durable_path(self):
        with patch.object(n,'telegram_multipart_call',return_value={'message_id':True}) as upload:
            with self.assertRaises(TelegramDeliveryUnknown):self.bot.delivery.send_document(filename='reply.md',content=b'x')
        self.assertEqual(upload.call_count,1);self.assertEqual(self.rows()[0]['status'],'unknown')

    def test_two_panels_coalesce_and_final_question_fifo_precedes_progress(self):
        self.watch();self.watch('other',71)
        for i in range(20):
            self.progress('a'+str(i));self.progress('b'+str(i),'other',71)
        self.assertEqual(len(self.rows()),2)
        for text in ('final-one','question','final-two'):self.bot.enqueue(text)
        self.bot.delivery.tick(limit=8)
        sent=[p.get('text') or p.get('rich_message',{}).get('markdown') for m,p in self.calls if m!='setMessageReaction']
        self.assertEqual(sent,['final-one','question','final-two','a19','b19'])

    def test_progress_scope_does_not_merge_other_versions_destinations_or_generations(self):
        self.progress('first')
        self.progress('other chat',chat='99')
        self.progress('other panel',message=71)
        self.progress('other watch version',version='v2')
        self.bot.state.update(quiet_generation=1)
        self.progress('other quiet generation')
        with patch.object(n,'navigation_view',return_value={'status':'open','revision':9}):self.progress('other navigation version')
        self.assertEqual(len(self.rows()),6)

    def test_only_pending_progress_can_be_replaced(self):
        first=self.progress('in flight');self.legacy.take_message(first)
        second=self.progress('next')
        self.legacy.message_done(first,'unknown')
        self.progress('latest')
        self.assertEqual([(r['status'],json.loads(r['payload'])['text']) for r in self.rows()],[('unknown','in flight'),('pending','latest')])
        self.assertNotEqual(first,second)

    def test_429_progress_merge_preserves_retry_deadline(self):
        self.watch();identifier=self.progress('old')
        self.delivery_error=n.TelegramApiError(429,'limited',{'retry_after':60})
        self.bot.delivery.tick();before=self.rows()[0]
        self.progress('new');after=self.rows()[0]
        self.assertEqual((after['id'],after['due'],after['attempts'],after['status']),(identifier,before['due'],1,'retry'))
        self.assertEqual(json.loads(after['payload'])['text'],'new')
        self.delivery_error=None;self.bot.delivery.tick(limit=8)
        self.assertEqual(len([m for m,p in self.calls if m!='setMessageReaction']),1)

    def test_closed_watch_and_old_quiet_generation_are_retired(self):
        self.watch();self.progress('closed');self.bot.watches.clear()
        self.bot.delivery.tick(limit=8)
        self.watch();self.progress('muted');self.bot.state.update(quiet_generation=1)
        self.bot.delivery.tick(limit=8)
        self.assertEqual([r['status'] for r in self.rows()],['expired','expired'])
        self.assertFalse(self.calls)

    def test_closed_navigation_cannot_be_overwritten_by_progress(self):
        self.watch()
        with patch.object(n,'navigation_view',return_value={'status':'open','revision':2}):self.progress('old')
        with patch.object(n,'navigation_view',return_value={'status':'closed','revision':3}):self.bot.delivery.tick(limit=8)
        self.assertEqual(self.rows()[0]['status'],'expired');self.assertFalse(self.calls)

    def test_expired_question_protection_survives_batch(self):
        self.watch();self.progress('latest progress')
        self.bot.pending_questions['q']={'id':7,'process':self.bot.app.process,'expires_at':time.time()-1,
                                         'params':{'threadId':'task','turnId':'old'}}
        self.bot.enqueue('expired question',interaction=('question','q'))
        self.bot.enqueue('final')
        self.bot.delivery.tick(limit=8)
        self.assertNotIn('q',self.bot.pending_questions)
        self.assertEqual([p.get('text') or p.get('rich_message',{}).get('markdown') for m,p in self.calls if m!='setMessageReaction'],['final','latest progress'])

    def test_batch_has_a_hard_message_count_bound(self):
        for i in range(10):self.bot.enqueue(str(i))
        self.assertEqual(self.bot.delivery.tick(limit=3),3)
        self.assertEqual(sum(r['status']=='pending' for r in self.rows()),7)

    def test_queued_output_wakes_waiting_worker(self):
        waiting=threading.Event();delivered=threading.Event();event=threading.Event()
        class ObservedEvent:
            def clear(self):event.clear()
            def set(self):event.set()
            def wait(self,timeout):waiting.set();return event.wait(timeout)
        self.legacy.outbox_event=ObservedEvent()
        original=self.api.side_effect
        def api(token,method,payload,timeout=30):
            result=original(token,method,payload,timeout)
            if (payload.get('text') or payload.get('rich_message',{}).get('markdown'))=='wake':delivered.set();self.bot.stop_event.set()
            return result
        self.api.side_effect=api
        thread=threading.Thread(target=self.bot.delivery.run,args=(self.bot.stop_event,))
        thread.start()
        try:
            self.assertTrue(waiting.wait(1));self.bot.enqueue('wake')
            self.assertTrue(delivered.wait(1),'worker waited for polling instead of queue wakeup')
        finally:
            self.bot.stop_event.set();event.set();thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_hook_scanning_does_not_wait_for_delivery_worker(self):
        sending=threading.Event();scanned=threading.Event()
        def send(stop):sending.set();stop.wait(2)
        def hooks():
            self.assertTrue(sending.wait(1));scanned.set();self.bot.stop_event.set()
        with patch.object(self.bot.delivery,'run',side_effect=send),patch.object(self.bot,'process_hooks',side_effect=hooks):
            self.bot._hook_loop()
        self.assertTrue(scanned.is_set())

    def test_shutdown_holds_machine_lock_and_database_until_upload_finishes(self):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',0));endpoint=probe.getsockname()
        uploading=threading.Event();finish=threading.Event();stopping=threading.Event()
        results=[];errors=[]
        original=self.api.side_effect
        def api(token,method,payload=None,timeout=30):
            if method=='getMe':return {'id':111}
            if method=='getWebhookInfo':return {}
            return original(token,method,payload,timeout)
        self.api.side_effect=api
        close=Mock(wraps=self.legacy.close);self.legacy.close=close
        def upload(*args,**kwargs):
            uploading.set()
            if not finish.wait(10):raise TimeoutError('test upload not released')
            return {'message_id':701}
        def service(bot,**kwargs):
            bot.delivery.state.queue_message('sendDocument',{'chat_id':'42','_upload':True},{'controller':True},
                upload={'filename':'reply.md','content_type':'text/markdown','content':b'original'})
            if not uploading.wait(5):raise AssertionError('upload did not start')
            return 0
        def shutdown():self.bot.stop_event.set();stopping.set()
        def run():
            try:results.append(self.bot.run(quiet_start=True))
            except BaseException as exc:errors.append(exc)
        with patch.object(locking,'_MACHINE_ENDPOINT',endpoint),patch.object(n,'telegram_multipart_call',side_effect=upload),patch.object(core.TelegramCodexBot,'_run_service',service),patch.object(self.bot,'shutdown',side_effect=shutdown):
            runner=threading.Thread(target=run);runner.start()
            try:
                self.assertTrue(stopping.wait(5));self.assertTrue(runner.is_alive())
                other=locking.SingleInstance('another-profile')
                try:
                    acquired=other.acquire()
                    self.assertFalse(acquired,{'endpoint':endpoint,'runner_alive':runner.is_alive(),
                        'hook_alive':bool(self.bot.hook_worker and self.bot.hook_worker.is_alive()),
                        'finish_set':finish.is_set(),'errors':[str(error) for error in errors],
                        'held_endpoints':[instance.handle.getsockname() for instance in list(locking._held) if instance.handle]})
                finally:other.close()
                close.assert_not_called()
                self.assertEqual(self.legacy.db.execute('SELECT status FROM outbox').fetchone()[0],'sending')
            finally:finish.set();runner.join(10)
            self.assertFalse(runner.is_alive());self.assertEqual(errors,[]);self.assertEqual(results,[0])
            close.assert_called_once()
            with self.assertRaises(sqlite3.ProgrammingError):self.legacy.db.execute('SELECT 1')
            with locking.SingleInstance() as next_instance:self.assertIsNotNone(next_instance.handle)

    def test_watch_refresh_marks_only_progress_edits(self):
        self.watch()
        with patch.object(self.bot,'_progress_card_locked',return_value='updated'):
            core.TelegramCodexBot._refresh_watch(self.bot,'task',force=True)
        self.assertIsNotNone(self.rows()[0]['progress_key'])
        self.bot.stop_watch_tracking('task')
        self.assertIsNone(self.rows()[-1]['progress_key'])
        self.bot.delivery.tick(limit=8)
        self.assertEqual([r['status'] for r in self.rows()],['expired','sent'])

    def test_http_requests_keep_verified_tls_bounded_timeouts_and_upload_type(self):
        context=ssl._create_default_https_context()
        self.assertEqual(context.verify_mode,ssl.CERT_REQUIRED);self.assertTrue(context.check_hostname)
        with patch.object(n,'urlopen',return_value=io.BytesIO(b'{"ok":true,"result":{"id":111}}')) as opened:
            self.assertEqual(call_json('111:fake','getMe'),{'id':111})
            self.assertTrue(opened.call_args.args[0].full_url.startswith('https://api.telegram.org/'))
            self.assertEqual(opened.call_args.kwargs,{'timeout':20})
        with patch.object(n,'urlopen',return_value=io.BytesIO(b'{"ok":true,"result":{"message_id":7}}')) as opened:
            n.send_document('111:fake',chat_id='42',filename='reply.md',content=b'unchanged bytes')
            request=opened.call_args.args[0]
            self.assertTrue(request.full_url.startswith('https://api.telegram.org/'))
            self.assertEqual(opened.call_args.kwargs,{'timeout':45})
            self.assertTrue(request.get_header('Content-type').startswith('multipart/form-data; boundary='))
            self.assertIn(b'Content-Type: text/markdown; charset=utf-8',request.data)
            self.assertIn(b'unchanged bytes',request.data)

    def test_http_rate_limit_preserves_code_and_delay(self):
        for call in (lambda:call_json('111:fake','getMe'),lambda:n.telegram_multipart_call('111:fake','sendDocument',{},file_field='document',content=b'x')):
            error=HTTPError('https://api.telegram.org/bot111:fake/sendDocument',429,'limit',{},
                            io.BytesIO(b'{"ok":false,"description":"limited","parameters":{"retry_after":17}}'))
            with patch.object(n,'urlopen',side_effect=error),self.assertRaises(n.TelegramApiError) as caught:call()
            self.assertEqual((caught.exception.code,caught.exception.retry_after),(429,17))
            self.assertNotIn('111:fake',str(caught.exception))

    def test_document_size_and_type_limits_match_both_entrypoints(self):
        with patch.object(n,'MAX_CACHED_REPLY_BYTES',2),patch.object(n,'telegram_multipart_call') as upload:
            for content in (b'123','not bytes'):
                with self.assertRaises(ValueError):n.send_document('111:fake',chat_id='42',filename='x.md',content=content)
                with self.assertRaises(ValueError):self.bot.delivery.send_document(filename='x.md',content=content)
            upload.assert_not_called();self.assertEqual(self.rows(),[])

    def test_pending_completion_uses_actual_200_limit_24_hour_expiry_and_seven_day_reservation(self):
        profile=self.root/'profile';profile.mkdir();database=self.root/'pending.sqlite3'
        decision=SimpleNamespace(reason='unverified-local-task',requires_turn_evidence=False,thread_source=None,turn_trigger=None)
        payload={'thread-id':'11111111-1111-4111-8111-111111111111','last-assistant-message':'body'}
        now=1000000
        with patch.object(pending.time,'time',return_value=now):
            for index in range(200):
                self.assertTrue(pending.defer(dict(payload,**{'turn-id':'pending-'+str(index)}),decision,profile,database))
            self.assertFalse(pending.defer(dict(payload,**{'turn-id':'overflow'}),decision,profile,database))
        with patch.object(pending.time,'time',return_value=now+86400-1):pending.ready(profile,database)
        with closing(sqlite3.connect(database)) as db:self.assertEqual(db.execute("SELECT count(*) FROM completions WHERE state='pending'").fetchone()[0],200)
        with patch.object(pending.time,'time',return_value=now+86400+1):pending.ready(profile,database)
        with closing(sqlite3.connect(database)) as db:self.assertEqual(db.execute("SELECT count(*) FROM completions WHERE state='expired'").fetchone()[0],200)
        reservation=self.root/'reserve.sqlite3';same=dict(payload,**{'turn-id':'same-turn'})
        with patch.object(pending.time,'time',return_value=now):self.assertTrue(pending.reserve(same,profile,reservation))
        with patch.object(pending.time,'time',return_value=now+7*86400-1):self.assertFalse(pending.reserve(same,profile,reservation))
        with patch.object(pending.time,'time',return_value=now+7*86400+1):self.assertTrue(pending.reserve(same,profile,reservation))

    def test_diagnostic_samples_use_actual_100_limit_dedup_and_seven_day_boundary(self):
        database=self.root/'diagnostics.sqlite3';now=1000000
        def record(index):
            return diagnostics.record({'thread-id':str(index),'last-assistant-message':'sensitive body'},database,
                                      stored_status='missing',stored_source=None,composer_match=False)
        with patch.object(diagnostics.time,'time',return_value=now):
            first=record(0)
            for index in range(1,101):record(index)
            last=record(100)
        with closing(sqlite3.connect(database)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM samples').fetchone()[0],100)
            self.assertIsNone(db.execute('SELECT id FROM samples WHERE substr(id,1,12)=?',(first,)).fetchone())
            self.assertEqual(db.execute('SELECT occurrences FROM samples WHERE substr(id,1,12)=?',(last,)).fetchone()[0],2)
        with patch.object(diagnostics.time,'time',return_value=now+7*86400-1):record('near-boundary')
        with closing(sqlite3.connect(database)) as db:self.assertEqual(db.execute('SELECT count(*) FROM samples').fetchone()[0],100)
        with patch.object(diagnostics.time,'time',return_value=now+7*86400+1):record('after-boundary')
        with closing(sqlite3.connect(database)) as db:self.assertEqual(db.execute('SELECT count(*) FROM samples').fetchone()[0],2)

    def test_cache_identity_expiry_empty_and_page_boundaries_without_task_execution(self):
        original='  original **reply**\n'+('😀'*2000)+'\n'
        cache=n.cache_full_reply({'last_assistant_message':original},force=True)
        other=n.cache_full_reply({'last_assistant_message':original},force=True)
        self.assertRegex(cache,r'^[0-9a-f]{16}$');self.assertNotEqual(cache,other)
        self.assertEqual(n.load_cached_reply(cache)['text'],original)
        with self.assertRaises(ValueError):n.load_cached_reply('../credentials')
        with patch.object(self.bot,'send') as send:
            for page in (-1,999):
                with self.assertRaisesRegex(ValueError,'页码'):self.bot.show_cached_reply(cache,page)
            self.bot.show_cached_reply(cache,0);send.assert_called_once()
        with patch.object(self.bot.delivery,'send_document') as send:
            self.bot.send_cached_reply_file(cache)
            self.assertEqual(send.call_args.kwargs['content'],original.encode())
            self.assertRegex(send.call_args.kwargs['filename'],r'^[A-Za-z0-9._-]+\.md$')
        empty=n.cache_full_reply({'last_assistant_message':''},force=True)
        with self.assertRaisesRegex(ValueError,'为空'):self.bot.show_cached_reply(empty,0)
        path=n.REPLY_CACHE_DIR/(cache+'.json');now=time.time();os.utime(path,(now,now))
        n.cleanup_reply_cache(now+7*86400-1);self.assertTrue(path.exists())
        n.cleanup_reply_cache(now+7*86400+1);self.assertFalse(path.exists())
        with self.assertRaisesRegex(FileNotFoundError,'过期'):n.load_cached_reply(cache)
        self.rpc.request.assert_not_called()

    def test_navigation_creation_version_and_closed_buttons_are_guarded(self):
        self.assertEqual(n.reserve_navigation(self.bot.token,'42',10),(True,None))
        self.assertEqual(n.reserve_navigation(self.bot.token,'42',10),(False,None))
        n.bind_navigation(self.bot.token,'42',10,80)
        revisions=[n.navigation_view(self.bot.token,'42',80)['revision']]
        for _ in range(2):
            n.advance_navigation(self.bot.token,'42',80)
            revisions.append(n.navigation_view(self.bot.token,'42',80)['revision'])
        self.assertEqual(revisions,[0,1,2])
        n.advance_navigation(self.bot.token,'42',80,close=True)
        self.bot.show_task=Mock()
        self.bot.handle_callback({'id':'cb','from':{'id':42},'data':'task|task',
                                  'message':{'message_id':80,'chat':{'id':42}}})
        self.bot.show_task.assert_not_called();self.rpc.request.assert_not_called()

    def test_trusted_old_buttons_restore_full_id_but_forwarded_or_body_only_messages_do_not(self):
        full='11111111-1111-4111-8111-111111111111'
        self.bot.submit_direct_reply=Mock();self.bot._input_error=Mock()
        old={'from':{'id':111},'chat':{'id':42},'text':'task '+full,
             'reply_markup':{'inline_keyboard':[[{'callback_data':'direct|'+full}]]}}
        cases=[old,{**old,'forward_origin':{'type':'user','sender_user':{'id':111}}},
               {**old,'from':{'id':999}},{**old,'reply_markup':{}},{**old,'chat':{'id':99}}]
        for index,reply in enumerate(cases):
            self.bot.handle_update({'update_id':index+100,'message':{'message_id':index+100,'date':int(time.time()),
                'from':{'id':42},'chat':{'id':42},'text':'continue','reply_to_message':dict(reply,message_id=index+10)}})
        self.bot.submit_direct_reply.assert_called_once_with(full,'continue')
        self.assertEqual(n.lookup_task_message(self.bot.token,'42',10),full)
        for message in range(11,15):self.assertIsNone(n.lookup_task_message(self.bot.token,'42',message))

    def test_notification_metadata_optional_fields_empty_preview_and_subagent_default(self):
        payload={'cwd':'C:/work/project','model':' model-x ','last_assistant_message':'  '}
        text=n.build_message(payload,True,display_title='Title',completed_at='2026-10-28 12:34:56')
        for value in ('Title','project','model-x','2026-10-28 12:34:56'):self.assertIn(value,text)
        self.assertNotIn('最后回复',text)
        text=n.build_message({'cwd':'/work/project','model':None},True,display_title='Title',completed_at='fixed')
        self.assertNotIn('模型',text);self.assertNotIn('最后回复',text)
        for config in ({},{'include_subagent_notifications':False},{'include_subagent_notifications':'true'}):
            self.assertFalse(n.subagent_notifications_enabled(config))
        self.assertTrue(n.subagent_notifications_enabled({'include_subagent_notifications':True}))

    def test_question_hook_checks_event_and_async_only_hints_observer(self):
        with patch.object(n,'load_config') as credentials,patch.object(n,'notifications_muted',return_value=False),patch.object(n,'log_error'):
            for payload in ({},{'hook_event_name':'PostToolUse','tool_name':'request_user_input'},
                            {'hook_event_name':'PreToolUse','tool_name':'other'}):
                n.send_user_input_request_notification(payload)
            credentials.assert_not_called()
        with patch('codex_telegram_bridge.features.async_inputs.hint_async_thread') as hint,patch.object(n,'load_config') as credentials:
            n.send_hook_notification({'hook_event_name':'PreToolUse','tool_name':'request_user_input_async','session_id':'original'})
            hint.assert_called_once_with('original',n.HOOK_DIR);credentials.assert_not_called()

    def test_manual_test_requires_explicit_entrypoint(self):
        with patch.object(n,'send_manual_test',return_value=0) as manual,patch.object(n,'send_hook_notification') as hook,patch.object(n,'send_desktop_notification') as notify:
            with patch.object(n.sys,'argv',['notify']),patch.object(n.sys,'stdin',io.StringIO('{}')):n.main()
            manual.assert_not_called();hook.assert_called_once_with({})
            with patch.object(n.sys,'argv',['notify','{}']):n.main()
            manual.assert_not_called();notify.assert_called_once_with({},'{}')
            with patch.object(n.sys,'argv',['notify','--test']):self.assertEqual(n.main(),0)
            manual.assert_called_once()

    def test_reaction_api_writes_are_serialized_between_processes(self):
        events=self.root/'reaction-events';go=self.root/'go'
        code='''import sys,time
from pathlib import Path
from codex_telegram_bridge.telegram import notifications as n
n.REACTION_DB=Path(sys.argv[1])
n._reaction_db().close()
events,go,label=Path(sys.argv[2]),Path(sys.argv[3]),sys.argv[4]
print('ready',flush=True)
while not go.exists():time.sleep(.005)
def api(*args,**kwargs):
 with events.open('a') as f:f.write('start-'+label+'\\n')
 time.sleep(.1)
 with events.open('a') as f:f.write('end-'+label+'\\n')
 return True
assert n.set_task_reaction('111:fake','42',int(label),'👀','task',api=api)
'''
        children=[subprocess.Popen([sys.executable,'-u','-c',code,str(n.REACTION_DB),str(events),str(go),str(i)],
                                   stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for i in (80,81)]
        try:
            for child in children:self.assertEqual(child.stdout.readline().strip(),'ready')
            go.touch()
            for child in children:
                _,error=child.communicate(timeout=5);self.assertEqual(child.returncode,0,error)
        finally:
            for child in children:
                if child.poll() is None:child.kill()
                child.communicate()
        lines=events.read_text().splitlines()
        self.assertEqual(len(lines),4)
        self.assertTrue(lines[0].startswith('start-'));self.assertEqual(lines[1],lines[0].replace('start-','end-'))
        self.assertTrue(lines[2].startswith('start-'));self.assertEqual(lines[3],lines[2].replace('start-','end-'))
        self.assertNotEqual(lines[0],lines[2]);self.assertEqual(len(n.task_reactions('111:fake')),2)

    def test_submission_journal_persists_digest_checkpoint_and_source_before_binding(self):
        body='private original prompt not stored as text'
        with patch.object(n.desktop_receipts,'checkpoint',return_value=('rollout-file',77)) as checkpoint:
            self.assertTrue(n.remember_desktop_submission('111:fake','42',10,'task-identity',body))
        checkpoint.assert_called_once_with('task-identity',n.SESSION_ROOT)
        with closing(n._reaction_db()) as db:
            row=db.execute('SELECT bot,chat,message,thread,path,offset,digest,turn FROM desktop_submissions').fetchone()
        self.assertEqual(row[:6],('111','42',10,'task-identity','rollout-file',77))
        self.assertEqual(len(row[6]),64);self.assertEqual(row[7],'')
        self.assertNotIn(body.encode(),n.REACTION_DB.read_bytes())
        self.assertTrue(n.bind_desktop_submission('111:fake','42',10,'task-identity','turn-one',update_reaction=False))
        self.assertEqual(n.task_reply_source('111:fake','42','task-identity','turn-one'),10)

    def test_download_checks_id_path_exact_limit_official_url_and_stream_growth(self):
        self.assertEqual(n.ATTACHMENT_LIMIT,20*1024*1024)
        with patch.object(n,'telegram_call') as api:
            for attachment in ({},{'file_id':''},{'file_id':42},{'file_id':'x','file_size':20*1024*1024+1}):
                with self.assertRaises(ValueError):n.download_telegram_attachment('111:fake',attachment)
            api.assert_not_called()
        with patch.object(n,'ATTACHMENT_DIR',self.root/'downloads'):
            for path in ('/absolute','../escape','a\\b'):
                with patch.object(n,'telegram_call',return_value={'file_path':path}),patch.object(n,'urlopen') as opened:
                    with self.assertRaises(ValueError):n.download_telegram_attachment('111:fake',{'file_id':'x'})
                    opened.assert_not_called()
            with patch.object(n,'telegram_call',return_value={'file_path':'documents/safe file.txt'}),patch.object(n,'urlopen',return_value=io.BytesIO(b'hello')) as opened:
                path=n.download_telegram_attachment('111:fake',{'file_id':'x','file_name':'../../evil.txt'})
                self.assertEqual(path.read_bytes(),b'hello')
                self.assertEqual(opened.call_args.args[0],'https://api.telegram.org/file/bot111:fake/documents/safe%20file.txt')
            with patch.object(n,'ATTACHMENT_LIMIT',4),patch.object(n,'telegram_call',return_value={'file_path':'documents/file'}),patch.object(n,'urlopen',return_value=io.BytesIO(b'12345')):
                with self.assertRaises(ValueError):n.download_telegram_attachment('111:fake',{'file_id':'x'})

    def test_failed_attachment_download_never_submits_task(self):
        n.remember_task_message(self.bot.token,{'chat_id':'42','reply_markup':n.build_notification_keyboard('original-task')},{'message_id':10})
        self.bot.submit_direct_reply=Mock()
        message={'message_id':50,'date':int(time.time()),'from':{'id':42},'chat':{'id':42},
                 'document':{'file_id':'file','file_name':'info.txt'},'reply_to_message':{'message_id':10}}
        with patch.object(core,'download_telegram_attachment',side_effect=ValueError('unavailable')) as download:
            with self.assertRaises(ValueError):self.bot.handle_update({'update_id':50,'message':message})
            download.assert_called_once()
        self.bot.submit_direct_reply.assert_not_called();self.rpc.request.assert_not_called()

    def test_setup_uses_interactive_chat_selection_and_rejects_invalid_bot_identity(self):
        updates=[{'message':{'date':2,'chat':{'id':42,'type':'private','first_name':'owner'}}},
                 {'message':{'date':1,'chat':{'id':-100,'type':'group','title':'group'}}}]
        with patch.object(n,'telegram_call',return_value=updates) as api,patch('builtins.input',side_effect=['','','2']),patch('builtins.print'):
            self.assertEqual(n.choose_chat_id('111:fake'),'-100')
            api.assert_called_once_with('111:fake','getUpdates',{'limit':100,'timeout':0})
        with patch.object(n,'HOOK_DIR',self.root),patch.object(n,'CONFIG_PATH',self.root/'credentials.json'),patch.object(n.getpass,'getpass',return_value='111:fake'),patch.object(n,'telegram_call',return_value={'username':'no-id'}) as api,patch.object(n,'store_token') as store,patch('builtins.print'):
            self.assertEqual(n.setup(),1);api.assert_called_once_with('111:fake','getMe')
            store.assert_not_called();self.assertFalse((self.root/'credentials.json').exists())

    def test_inline_links_escape_markup_and_code_pages_preserve_complete_lines(self):
        text=n.markdown_to_telegram_html('**<Bold>** `<x>&y` [<Click>](https://example.test/?a=1&b="two")')
        self.assertIn('<b>&lt;Bold&gt;</b>',text);self.assertIn('<code>&lt;x&gt;&amp;y</code>',text)
        self.assertIn('<a href="https://example.test/?a=1&amp;b=&quot;two&quot;">&lt;Click&gt;</a>',text)
        source='```python\n'+('x="😀"\n'*60)+'```'
        pages=n.split_markdown_pages(source,limit=256)
        self.assertGreater(len(pages),1)
        self.assertEqual(sum(page.count('x="😀"') for page in pages),60)
        for page in pages:
            self.assertLessEqual(len(page.encode('utf-16-le'))//2,256)
            self.assertEqual(sum(line.startswith('```') for line in page.splitlines()),2)

    def test_managed_completion_cards_distinguish_success_and_failure(self):
        self.bot.state.update(managed_threads={'task':{'origin':'bot_created','cwd':str(self.root)}})
        with patch.object(n,'conversation_title',return_value='Visible title'),patch.object(self.bot,'enqueue') as enqueue:
            for status,icon,reaction in [('completed','🥳','🎉'),('failed','🥲','😢')]:
                self.bot.final_messages['task']='original answer'
                self.bot.on_app_event({'method':'turn/completed','params':{'threadId':'task','turn':{'id':'turn-'+status,'status':status}}})
                self.assertTrue(enqueue.call_args.args[0].startswith('**'+icon+' Visible title**'))
                self.assertIn('original answer',enqueue.call_args.args[0])
                self.assertEqual(enqueue.call_args.kwargs['outcome'],reaction)

    def test_subagent_stop_and_notify_share_exact_child_deduplication(self):
        case=completion_fixture.CompletionRoutingTests();case.setUp();self.addCleanup(case.doCleanups)
        case.task(source='subagent',text=case.payload['last-assistant-message'])
        with ExitStack() as stack:
            send=case.delivery_patches(stack)
            stack.enter_context(patch.object(n,'load_config',return_value=({'chat_id':'42','include_message_preview':True,'include_subagent_notifications':True},'111:fake')))
            n.send_hook_notification({'hook_event_name':'SubagentStop','session_id':completion_fixture.INTERNAL,
                'agent_id':completion_fixture.THREAD,'turn_id':completion_fixture.TURN,'last_assistant_message':case.payload['last-assistant-message']})
            n.send_desktop_notification(case.payload,'original')
            send.assert_called_once()
            self.assertIn('direct|'+completion_fixture.THREAD,str(send.call_args.args[1]['reply_markup']))
            self.assertNotIn(completion_fixture.INTERNAL,str(send.call_args.args[1]['reply_markup']))

    def test_notification_muted_combines_quiet_and_mute_without_credentials(self):
        path=self.root/'preferences.json'
        with patch.object(n,'BOT_STATE_PATH',path),patch.object(n,'load_config') as load,patch.object(n,'forward_computer_use_notification'):
            for muted,quiet,expected in [(False,False,False),(True,False,True),(False,True,True),(True,True,True)]:
                path.write_text(json.dumps({'muted':muted,'quiet_mode':quiet}))
                self.assertEqual(n.notifications_muted(),expected)
                if expected:
                    n.send_hook_notification({'hook_event_name':'Stop','session_id':'task','turn_id':'turn-1'})
                    n.send_desktop_notification({'thread-id':'task','turn-id':'turn-1'},'original')
                    n.send_user_input_request_notification({'hook_event_name':'PreToolUse','tool_name':'request_user_input','session_id':'task','turn_id':'turn-1'})
            load.assert_not_called()

    def test_bot_owned_lifecycle_does_not_duplicate_controller_completion(self):
        case=completion_fixture.CompletionRoutingTests();case.setUp();self.addCleanup(case.doCleanups)
        case.task(origin='codex_telegram_bot',text=case.payload['last-assistant-message'])
        self.bot.state.update(managed_threads={completion_fixture.THREAD:{'origin':'bot_created','cwd':str(self.root)}})
        with ExitStack() as stack:
            send=case.delivery_patches(stack)
            enqueue=stack.enter_context(patch.object(self.bot,'enqueue'))
            self.bot.final_messages[completion_fixture.THREAD]=case.payload['last-assistant-message']
            self.bot.on_app_event({'method':'turn/completed','params':{'threadId':completion_fixture.THREAD,
                'turn':{'id':completion_fixture.TURN,'status':'completed'}}})
            n.send_hook_notification({'hook_event_name':'Stop','session_id':completion_fixture.THREAD,
                'turn_id':completion_fixture.TURN,'last_assistant_message':case.payload['last-assistant-message']})
            n.send_desktop_notification(case.payload,'original')
            enqueue.assert_called_once();send.assert_not_called()
            self.assertEqual(enqueue.call_args.kwargs['completion'],(completion_fixture.THREAD,completion_fixture.TURN))
