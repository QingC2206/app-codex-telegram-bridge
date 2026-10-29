"""Account-bound quota regression: fake RPCs and isolated state only."""
import copy
import json
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_telegram_quota as fixture
import test_controller_delivery as delivery_fixture
from codex_telegram_bridge.core import bot as core
from codex_telegram_bridge.features.quota_state import QuotaState
from codex_telegram_bridge.telegram.controller_delivery import ControllerDelivery


class QuotaAccountTests(unittest.TestCase):
    setUp = fixture.QuotaTests.setUp
    send = fixture.QuotaTests.send
    observe = fixture.QuotaTests.observe
    button = fixture.QuotaTests.button
    click = fixture.QuotaTests.click
    reserve = fixture.QuotaTests.reserve

    def account(self, name, remaining=2, reset_at=None):
        self.app.email = name+'@example.invalid'
        self.observe(remaining, reset_at)
        return self.bot.quota_state.scope()['account']

    def bucket(self, key):
        return self.bot.state.snapshot()['quota_accounts']['accounts'][key]

    def test_identical_deadlines_do_not_share_thresholds_or_restore_messages(self):
        deadline = int(time.time())+86400
        first = self.account('a', 3, deadline)
        second = self.account('b', 2, deadline)
        self.assertNotEqual(first, second)
        low = [text for text, _ in self.alerts if '额度低于' in text]
        self.assertEqual(len(low), 2)
        self.assertEqual(self.bucket(first)['quota_weekly_observation']['used_percent'], 97)
        self.assertEqual(self.bucket(second)['quota_weekly_observation']['used_percent'], 98)
        self.account('c', 100, deadline)
        self.assertFalse(any('提前恢复' in text or '已自然重置' in text for text, _ in self.alerts))

    def test_card_baselines_are_account_owned_and_switch_back_is_not_a_new_grant(self):
        a = self.account('a', 80)
        self.app.email = 'b@example.invalid'
        self.app.latest = fixture.quota(80)
        self.app.latest['rateLimitResetCredits']['credits'][0]['id'] = 'b-card'
        self.bot._observe_quota(self.bot.quota_state.fetch())
        b = self.bot.quota_state.scope()['account']
        self.assertIn('card-1', self.bucket(a)['quota_reset_credits_observation']['credit_ids'])
        self.assertIn('b-card', self.bucket(b)['quota_reset_credits_observation']['credit_ids'])
        self.assertEqual(sum('首次建立监控基线' in text for text, _ in self.alerts), 2)
        count = len(self.alerts)
        self.account('a', 80)
        self.assertEqual(len(self.alerts), count)

    def test_switch_back_preserves_history_but_never_revives_authorization(self):
        self.reserve(3)
        scope = self.bot.quota_state.scope()
        callback = self.button('立即重置')
        cycle = self.ctl.state()['quota_reset_cycle']['id']
        self.account('b', 80)
        self.app.email = 'test@example.invalid'; self.observe(3)
        self.assertEqual(self.ctl.state()['quota_reset_cycle']['id'], cycle)
        self.assertEqual(self.ctl.state()['quota_reset_reservation']['status'], 'cancelled')
        self.assertNotEqual(self.bot.quota_state.scope()['epoch'], scope['epoch'])
        with self.assertRaises(ValueError): self.ctl.handle(callback)
        self.assertFalse(self.app.consumed())

    def test_other_account_cannot_recover_or_inherit_an_unconfirmed_attempt(self):
        self.observe(3)
        a = self.bot.quota_state.scope()['account']
        attempt = {'cycle':self.ctl.state()['quota_reset_cycle']['id'], 'account':a,
                   'request_sent':True, 'status':'uncertain', 'recovery_observed':False,
                   'resets_at':int(time.time())+86400, 'used_percent':97}
        self.bot.quota_state.update(quota_reset_attempt=attempt)
        self.account('b', 2, int(time.time())+200000)
        self.ctl._check_unused(self.ctl.state()['quota_reset_cycle']['id'])
        self.assertEqual(self.bucket(a)['quota_reset_attempt'], attempt)
        self.app.email = 'test@example.invalid'; self.observe(2)
        with self.assertRaises(ValueError): self.ctl._check_unused(self.ctl.state()['quota_reset_cycle']['id'])

    def test_successful_attempt_cannot_use_another_accounts_recovery(self):
        self.observe(3)
        a = self.bot.quota_state.scope()['account']
        attempt = {'cycle':self.ctl.state()['quota_reset_cycle']['id'], 'request_sent':True,
                   'status':'reset', 'recovery_observed':False, 'resets_at':int(time.time())+86400,
                   'used_percent':97}
        self.bot.quota_state.update(quota_reset_attempt=attempt)
        self.account('b', 100, int(time.time())+200000)
        self.assertFalse(self.bucket(a)['quota_reset_attempt']['recovery_observed'])
        self.app.email = 'test@example.invalid'; self.observe(2)
        with self.assertRaises(ValueError): self.ctl._check_unused(self.ctl.state()['quota_reset_cycle']['id'])

    def test_unassigned_legacy_attempt_survives_restart_and_blocks_redemption(self):
        for status in ('consuming', 'uncertain', 'reset'):
            with self.subTest(status=status):
                self.bot.state = core.StateStore(Path(self.tmp.name)/(status+'.json'))
                attempt = {'status':status, 'request_sent':True, 'recovery_observed':False,
                           'idempotency_key':'preserve-this', 'credit_id':'old-card'}
                legacy = {'quota_reset_attempt':attempt, 'quota_weekly_observation':{'used_percent':99},
                          'quota_reset_reservation':{'status':'armed','id':'old'}}
                self.bot.state.update(**legacy, user_option='keep')
                self.ctl.startup(); self.bot.quota_state.fetch()
                root = self.bot.state.snapshot()
                for key in legacy: self.assertNotIn(key, root)
                self.assertEqual(root['quota_accounts']['legacy_unassigned'], legacy | {'quota_reset_credits_observation':{}})
                self.assertEqual(root['user_option'], 'keep')
                with self.assertRaisesRegex(ValueError, '归属'): self.ctl._check_unused('new')
                self.bot.state = core.StateStore(self.bot.state.path)
                self.ctl.startup(); self.account('b')
                self.assertEqual(self.bot.state.snapshot()['quota_accounts']['legacy_unassigned']['quota_reset_attempt'], attempt)
                with self.assertRaises(ValueError): self.ctl._check_unused('another')
        self.assertFalse(self.app.consumed())

    def test_legacy_root_writes_and_mutate_bypasses_are_rejected_atomically(self):
        before = self.bot.state.path.read_bytes()
        for change in (lambda:self.bot.state.update(quota_reset_attempt={}),
                       lambda:self.bot.state.update(quota_accounts={}),
                       lambda:self.bot.state.mutate(lambda state:state.update(quota_reset_attempt={})),
                       lambda:self.bot.state.mutate(lambda state:state.pop('quota_accounts'))):
            with self.assertRaises(ValueError): change()
            self.assertEqual(self.bot.state.path.read_bytes(), before)
        reloaded = core.StateStore(self.bot.state.path).snapshot()
        self.assertNotIn('quota_weekly_observation', reloaded)
        conflict = {**reloaded, 'quota_reset_attempt':{'status':'uncertain','request_sent':True}}
        self.bot.state.path.write_text(json.dumps(conflict))
        raw = self.bot.state.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'conflicts'): core.StateStore(self.bot.state.path)
        self.assertEqual(self.bot.state.path.read_bytes(), raw)

    def test_identity_is_stable_across_plan_metadata_but_not_email(self):
        a = {'type':'chatgpt','email':'A@example.invalid','planType':'plus'}
        self.assertEqual(QuotaState.account_key(a), QuotaState.account_key({**a,'planType':'pro'}))
        self.assertNotEqual(QuotaState.account_key(a), QuotaState.account_key({**a,'email':'b@example.invalid'}))
        with self.assertRaises(ValueError): QuotaState.account_key({'type':'apiKey'})

    def test_failed_account_switch_save_preserves_history_and_revokes_old_authority(self):
        self.reserve(3)
        scope=self.bot.quota_state.scope()
        before=self.bot.state.path.read_bytes()
        snapshot=self.bot.state.snapshot()
        self.app.email='b@example.invalid'
        with patch('codex_telegram_bridge.storage.state.os.replace', side_effect=OSError('fixture disk failure')):
            with self.assertRaises(OSError): self.bot.quota_state.fetch()
        self.assertEqual(self.bot.state.path.read_bytes(), before)
        self.assertEqual(self.bot.state.snapshot(), snapshot)
        self.assertFalse(self.bot.quota_state.valid(scope))
        self.assertIsNone(self.bot.quota_state.scope())
        self.assertFalse(self.app.consumed())

    def test_account_or_connection_change_during_read_rejects_snapshot(self):
        for kind in ('account', 'connection'):
            with self.subTest(kind=kind):
                self.app.email='test@example.invalid'; self.app.connection_generation=1
                self.bot.quota_state.fetch()
                def change(method):
                    if method == 'account/rateLimits/read':
                        if kind == 'account': self.app.email='b@example.invalid'
                        else: self.app.connection_generation += 1
                self.app.hook=change
                with self.assertRaises(ValueError): self.bot.quota_state.fetch()
                self.assertIsNone(self.bot.quota_state.scope())
                self.app.hook=None
        self.assertFalse(self.app.consumed())

    def test_identity_event_immediately_revokes_scope_without_reader_rpc(self):
        self.reserve(3)
        self.app.connection_generation=1; self.bot.quota_state.fetch()
        scope=self.bot.quota_state.scope(); calls=len(self.app.calls)
        self.bot.on_app_event({'method':'account/updated', 'params':{}, '_bridge_connection_generation':0})
        self.assertTrue(self.bot.quota_state.valid(scope))
        self.assertTrue(self.bot.quota_events.empty())
        self.bot.on_app_event({'method':'account/updated', 'params':{}, '_bridge_connection_generation':1})
        self.assertFalse(self.bot.quota_state.valid(scope))
        self.assertEqual(len(self.app.calls), calls)
        self.bot.quota_state.fetch()
        self.assertFalse(self.bot.quota_state.valid(scope))

    def test_identity_event_during_scope_publication_cannot_restore_old_authority(self):
        self.bot.quota_state.note_identity_change()
        original = self.bot.state.mutate
        def interrupt_publication(callback):
            original(callback)
            self.bot.quota_state.note_identity_change()
        with patch.object(self.bot.state, 'mutate', side_effect=interrupt_publication):
            with self.assertRaises(ValueError): self.bot.quota_state.fetch()
        self.assertIsNone(self.bot.quota_state.scope())
        self.assertEqual(self.bot.quota_state.snapshot(), {})
        self.assertFalse(self.app.consumed())

    def test_unverified_push_cannot_mark_attempt_recovered_or_change_history(self):
        self.observe(3)
        attempt={'cycle':self.ctl.state()['quota_reset_cycle']['id'], 'request_sent':True,
                 'status':'reset', 'recovery_observed':False, 'resets_at':int(time.time())+86400,'used_percent':97}
        self.bot.quota_state.update(quota_reset_attempt=attempt)
        self.bot.on_app_event({'method':'account/rateLimits/updated','params':fixture.quota(100)})
        self.ctl._drain_events()
        self.bot._observe_quota(self.bot.quota_state.fetch())
        self.assertFalse(self.ctl.state()['quota_reset_attempt']['recovery_observed'])
        self.assertEqual(self.ctl.state()['quota_weekly_observation']['used_percent'], 97)
        self.assertFalse(any('提前恢复' in text for text, _ in self.alerts))
        with self.assertRaises(ValueError): self.bot._observe_quota(fixture.quota(100))

    def test_old_verified_snapshot_cannot_overwrite_the_new_account(self):
        old=self.bot.quota_state.fetch()
        self.account('b', 80)
        before=self.bot.quota_state.snapshot()
        with self.assertRaises(ValueError): self.bot._observe_quota(old)
        self.assertEqual(self.bot.quota_state.snapshot(), before)

    def test_legacy_outbox_and_direct_output_reject_old_account_scope(self):
        scope=self.bot.quota_state.scope()
        self.bot._ui_local.quota_scope=scope
        core.TelegramCodexBot.enqueue(self.bot, 'old quota')
        self.bot._ui_local.quota_scope=None
        self.account('b', 80)
        self.bot.outbox.put({'kind':'send','text':'ordinary task','quiet_generation':0})
        self.bot.outbox.put(None)
        with patch.object(self.bot,'send') as send:
            self.bot._outbox_worker()
            self.assertEqual([call.args[0] for call in send.call_args_list], ['ordinary task'])
        self.bot._ui_local.quota_scope=scope
        with patch.object(core,'send_markdown_message') as network:
            with self.assertRaises(ValueError): core.TelegramCodexBot.send(self.bot,'old quota')
            with self.assertRaises(ValueError): core.TelegramCodexBot.edit_message(self.bot, 1,'old quota')
            network.assert_not_called()

    def test_legacy_send_rechecks_scope_after_rendering_before_network(self):
        self.bot._ui_local.quota_scope=self.bot.quota_state.scope()
        def render(text, limit):
            self.bot.quota_state.note_identity_change()
            return text
        with patch.object(core,'markdown_preview',side_effect=render), \
                patch.object(core,'send_markdown_message') as network:
            with self.assertRaises(ValueError): core.TelegramCodexBot.send(self.bot,'quota')
            network.assert_not_called()


class QuotaAccountDeliveryTests(unittest.TestCase):
    setUp=delivery_fixture.ControllerDeliveryTests.setUp
    ready=delivery_fixture.ControllerDeliveryTests.ready
    migrate=delivery_fixture.ControllerDeliveryTests.migrate
    update=delivery_fixture.ControllerDeliveryTests.update
    owned=delivery_fixture.ControllerDeliveryTests.owned

    def test_delayed_confirmation_and_receipt_cannot_cross_account_or_epoch(self):
        scope=self.bot.quota_state.activate('account-a', self.bot.quota_state.connection())
        self.bot._ui_local.quota_scope=scope
        self.bot.quota_state.update(quota_reset_confirmation={'nonce':'nonce','expires_at':time.time()+120})
        self.delivery_error=delivery_fixture.n.TelegramApiError(429,'retry after 5')
        sent=self.bot.send('confirm')
        self.bot.delivery.after_delivery(sent['_pending_outbox'],'state',
            {'key':'quota_reset_confirmation','identity_key':'nonce','identity':'nonce'})
        self.bot._ui_local.quota_scope=None
        self.bot.quota_state.activate('account-b',self.bot.quota_state.connection())
        self.bot.quota_state.update(quota_reset_confirmation={'nonce':'nonce','expires_at':time.time()+120})
        count=len(self.calls);self.ready();self.bot.delivery.tick()
        self.assertEqual(len(self.calls),count)
        self.bot.quota_state.bind_message(scope,'quota_reset_confirmation','nonce','nonce',999)
        self.assertNotIn('message_id', self.bot.quota_state.snapshot()['quota_reset_confirmation'])
        self.bot.quota_state.activate('account-a',self.bot.quota_state.connection())
        self.assertFalse(self.bot.delivery.valid_route({'controller':True,'quota_scope':scope}))

    def test_upgrade_quarantines_only_unowned_background_once_and_preserves_payload(self):
        self.legacy.set('quota_scope_transport_v1',None)
        rows=[]
        routes=[{'controller':True,'background':True},
                {'controller':True,'background':True,'completion':['task','turn']},
                {'controller':True,'background':False},
                {'controller':True,'background':False,'after':['state',{'key':'quota_reset_confirmation'}]},
                {'controller':True,'background':True}]
        for i,route in enumerate(routes):
            rows.append(self.legacy.queue_message('sendMessage',{'text':f'fixture-{i}'},route))
        self.legacy.db.execute("UPDATE outbox SET status='unknown' WHERE id=?",(rows[-1],));self.legacy.db.commit()
        before=self.legacy.db.execute('SELECT payload FROM outbox WHERE id=?',(rows[0],)).fetchone()[0]
        ControllerDelivery(self.bot,self.legacy)
        states=[self.legacy.db.execute('SELECT status FROM outbox WHERE id=?',(identifier,)).fetchone()[0] for identifier in rows]
        self.assertEqual(states,['quarantined','pending','pending','quarantined','unknown'])
        self.assertEqual(self.legacy.get('quota_scope_transport_v1')['quarantined'],2)
        self.assertEqual(self.legacy.db.execute('SELECT payload FROM outbox WHERE id=?',(rows[0],)).fetchone()[0],before)
        new=self.legacy.queue_message('sendMessage',{'text':'new service notice'},{'controller':True,'background':True})
        ControllerDelivery(self.bot,self.legacy)
        self.assertEqual(self.legacy.db.execute('SELECT status FROM outbox WHERE id=?',(new,)).fetchone()[0],'pending')
