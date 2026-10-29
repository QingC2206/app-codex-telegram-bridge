"""Approved R10/R12/R13/R14 regressions; all RPC and Telegram are fakes."""
import copy
import time
import unittest
from unittest.mock import Mock

import test_controller_delivery as delivery_fixture
import test_interactions as interaction_fixture
import test_telegram_quota as quota_fixture
from codex_telegram_bridge.core import bot as core
from codex_telegram_bridge.telegram import notifications as n
from codex_telegram_bridge.telegram.formatting import format_token_count


class DeliveryFixTests(unittest.TestCase):
    setUp = delivery_fixture.ControllerDeliveryTests.setUp
    migrate = delivery_fixture.ControllerDeliveryTests.migrate
    owned = delivery_fixture.ControllerDeliveryTests.owned
    update = delivery_fixture.ControllerDeliveryTests.update
    ready = delivery_fixture.ControllerDeliveryTests.ready
    emojis = delivery_fixture.ControllerDeliveryTests.emojis

    def question(self, blocking=True):
        self.owned()
        self.bot.on_server_request({'id': 72, 'method': 'item/tool/requestUserInput', 'params': {
            'threadId':'owned-thread', 'turnId':'turn-one', 'itemId':'item-one',
            'isBlocking':blocking, 'autoResolutionMs':120000,
            'questions':[{'id':'one','header':'One','question':'Pick one',
                          'options':[{'label':'A','description':'first'}]}]}})
        self.assertEqual(len(self.bot.pending_questions),1)
        return next(iter(self.bot.pending_questions))

    def test_expired_rate_limited_question_is_never_sent(self):
        seq=self.question()
        self.delivery_error=n.TelegramApiError(429,'retry after 5')
        self.bot.delivery.tick()
        self.bot.pending_questions[seq]['expires_at']=time.time()-60
        self.ready(); self.calls.clear(); self.bot.delivery.tick()
        self.assertFalse(self.calls)
        self.assertEqual(self.legacy.db.execute('SELECT status FROM outbox').fetchone()[0],'expired')
        self.assertNotIn(seq,self.bot.pending_questions)
        self.rpc.answer.assert_not_called()

    def test_changed_process_cannot_deliver_queued_question(self):
        seq=self.question()
        self.bot.app.process=object()
        self.bot.delivery.tick()
        self.assertFalse(self.calls)
        self.assertNotIn(seq,self.bot.pending_questions)

    def test_nonblocking_question_survives_continuing_task(self):
        seq=self.question(blocking=False)
        self.bot.active_turns['owned-thread']='new-turn'
        self.bot.delivery.tick()
        self.assertTrue(any(m=='sendRichMessage' for m,p in self.calls))
        self.bot._check_pending_interaction(seq,self.bot.pending_questions[seq])

    def test_expired_displayed_card_is_retired(self):
        seq=self.question();self.bot.delivery.tick()
        self.bot.pending_questions[seq]['expires_at']=time.time()-1
        self.calls.clear();self.bot.delivery.tick()
        edits=[p for m,p in self.calls if m=='editMessageText']
        self.assertEqual(len(edits),1)
        self.assertEqual(edits[0]['reply_markup'],{'inline_keyboard':[]})

    def test_windows_memory_queue_uses_same_expiry_check(self):
        self.bot.delivery=None
        seq=self.question()
        self.bot.pending_questions[seq]['expires_at']=time.time()-1
        self.bot.send=Mock();self.bot.outbox.put(None)
        self.bot._outbox_worker()
        self.bot.send.assert_not_called()
        self.assertNotIn(seq,self.bot.pending_questions)

    def test_unknown_side_page_keeps_unconfirmed_tracking(self):
        first=self.legacy.queue_message('sendMessage',{'text':'one'},{'controller':True})
        second=self.legacy.queue_message('sendMessage',{'text':'two'},{'controller':True})
        self.legacy.message_done(first,'sent',message_id=501)
        self.legacy.message_done(second,'unknown')
        self.bot.delivery.defer_reaction([first,second],10,0)
        self.bot.delivery.finish_deferred_reactions()
        self.assertEqual(self.emojis()[-1],'👀')
        self.assertEqual(self.legacy.db.execute("SELECT count(*) FROM meta WHERE key LIKE 'delivery-reaction:%'").fetchone()[0],1)
        self.bot.delivery.tick();self.rpc.request.assert_not_called()
        self.assertNotIn('😢',self.emojis());self.assertNotIn('🎉',self.emojis())

    def test_unknown_side_page_wins_over_other_failed_page(self):
        ids=[self.legacy.queue_message('sendMessage',{'text':'page'}) for _ in range(2)]
        self.legacy.message_done(ids[0],'failed');self.legacy.message_done(ids[1],'unknown')
        self.bot.delivery.defer_reaction(ids,10,0);self.bot.delivery.finish_deferred_reactions()
        self.assertEqual(self.emojis()[-1],'👀')

    def test_known_failed_side_page_is_failed(self):
        msg=self.legacy.queue_message('sendMessage',{'text':'page'})
        self.legacy.message_done(msg,'failed')
        self.bot.delivery.defer_reaction([msg],10,0);self.bot.delivery.finish_deferred_reactions()
        self.assertEqual(self.emojis()[-1],'😢')
        self.assertEqual(self.legacy.db.execute("SELECT count(*) FROM meta WHERE key LIKE 'delivery-reaction:%'").fetchone()[0],0)

    def test_unknown_retention_expires_without_claiming_failure(self):
        msg=self.legacy.queue_message('sendMessage',{'text':'page'})
        self.legacy.message_done(msg,'unknown')
        self.bot.delivery.defer_reaction([msg],10,0)
        key='delivery-reaction:10:'+str(msg)
        record=self.legacy.get(key);record['created']=time.time()-8*86400;self.legacy.set(key,record)
        self.bot.delivery.finish_deferred_reactions()
        self.assertIsNone(self.legacy.get(key));self.assertEqual(self.emojis()[-1],'👀')


class UsageFixTests(unittest.TestCase):
    setUp=interaction_fixture.InteractionTests.setUp

    def usage(self,value,account=None):
        self.bot.token_usage['task']=value
        self.bot.resolve_thread=lambda _: 'task'
        self.bot.app.request.side_effect=lambda method,*args,**kwargs: {'thread':{'id':'task'}} if method=='thread/read' else (account or {})
        self.bot.send=Mock();self.bot.command_usage('task')
        return self.bot.send.call_args.args[0]

    def test_missing_subcounts_are_unknown_but_zero_stays_zero(self):
        text=self.usage({'total':{'totalTokens':123,'inputTokens':0}})
        self.assertIn('总 Token：123',text);self.assertIn('输入：0',text)
        for field in ('缓存输入','输出','推理输出'):self.assertIn(field+'：未知',text)

    def test_invalid_counts_are_not_zero_or_truncated(self):
        for value in (None,True,False,-1,1.5,'invalid','',float('nan'),float('inf')):
            with self.subTest(value=value):self.assertEqual(format_token_count(value),'未知')
        self.assertEqual(format_token_count('1234'),'1,234');self.assertEqual(format_token_count(0),'0')

    def test_context_occupancy_missing_does_not_claim_zero_percent(self):
        text=self.usage({'last':{'inputTokens':2},'modelContextWindow':200000})
        self.assertIn('占用未知',text);self.assertNotIn('0.0%',text)

    def test_partial_server_groups_do_not_claim_complete_sum(self):
        text=self.usage({}, {'threadUsage':{'groups':[{'totalTokens':100},{'model':'fixture'}]}})
        self.assertIn('服务端估算 Token：** 未知',text)

    def test_malformed_server_group_is_not_silently_excluded(self):
        text=self.usage({}, {'threadUsage':{'groups':[{'totalTokens':100},None]}})
        self.assertIn('服务端估算 Token：** 未知',text)


class CreditFixTests(unittest.TestCase):
    setUp=quota_fixture.QuotaTests.setUp
    send=quota_fixture.QuotaTests.send
    observe=quota_fixture.QuotaTests.observe
    button=quota_fixture.QuotaTests.button
    click=quota_fixture.QuotaTests.click

    def test_malformed_metadata_blocks_confirmed_consumption(self):
        variants=[]
        for count in (True,1.5,-1,float('nan'),float('inf'),'2'):
            value=quota_fixture.quota(5);value['rateLimitResetCredits']['availableCount']=count;variants.append(value)
        for expiry in ('not-a-timestamp',True,-1,float('nan'),float('inf'),1.2):
            value=quota_fixture.quota(5);value['rateLimitResetCredits']['credits'][0]['expiresAt']=expiry;variants.append(value)
        for identifier in ('',42,'card-2'):
            value=quota_fixture.quota(5);value['rateLimitResetCredits']['credits'][0]['id']=identifier;variants.append(value)
        for value in variants:
            with self.subTest(value=value['rateLimitResetCredits']):
                self.observe(5);self.click('立即重置');confirm=self.button('确认使用 1 张重置卡')
                self.app.latest=value
                with self.assertRaises(ValueError):self.ctl.handle(copy.deepcopy(confirm))
                self.assertEqual(self.app.consumed(),[])

    def test_incomplete_card_details_cannot_authorize(self):
        for details in (None, [],[{'id':'card-1'}]):
            value=quota_fixture.quota(5)
            value['rateLimitResetCredits']['credits']=details
            with self.subTest(details=details):
                with self.assertRaises(ValueError):core.reset_credit_gate(value)
                with self.assertRaises(ValueError):self.ctl._select_credit(value)

    def test_missing_expiry_is_valid_and_normal_confirmation_consumes_once(self):
        self.observe(5)
        self.app.latest['rateLimitResetCredits']['credits'][0].pop('expiresAt')
        self.app.latest['rateLimitResetCredits']['credits'][1]['expiresAt']=None
        self.click('立即重置');confirm=self.button('确认使用 1 张重置卡')
        self.ctl.handle(confirm)
        self.assertEqual(len(self.app.consumed()),1)
        with self.assertRaises(ValueError):self.ctl.handle(confirm)
        self.assertEqual(len(self.app.consumed()),1)
