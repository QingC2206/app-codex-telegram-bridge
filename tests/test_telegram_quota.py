"""Offline tests; all account/Telegram calls are fakes, never spend real credits."""
import copy
from datetime import datetime
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import codex_telegram_bridge.app as module
def quota(remaining=10, reset_at=None, count=2, five_hour=0):
    return {"rateLimitsByLimitId": {"codex": {
        "primary": {"usedPercent": 100-five_hour, "windowDurationMins": 300,
                    "resetsAt": int(time.time())+18000},
        "secondary": {"usedPercent": 100-remaining, "windowDurationMins": 10080,
                      "resetsAt": reset_at or int(time.time())+86400}}},
        "rateLimitResetCredits": {"availableCount": count, "credits": [
            {"id": "card-1", "status": "available", "expiresAt": int(time.time())+99999},
            {"id": "card-2", "status": "available", "expiresAt": int(time.time())+199999}
        ] if count else []}}


class FakeApp:
    def __init__(self):
        self.latest = quota()
        self.calls = []
        self.outcome = "reset"
        self.email = "test@example.invalid"
        self.hook = None
        self.fail = None

    def request(self, method, params=None, **kwargs):
        self.calls.append((method, copy.deepcopy(params)))
        if self.hook:
            self.hook(method)
        if method == self.fail:
            raise TimeoutError("simulated timeout")
        if method == "account/read":
            return {"account": {"type": "chatgpt", "email": self.email}}
        if method == "account/rateLimits/read":
            return copy.deepcopy(self.latest)
        if method.endswith("/consume"):
            return {"outcome": self.outcome}
        raise AssertionError(method)

    def consumed(self):
        return [p for m, p in self.calls if m.endswith("/consume")]


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = patch('time.time', return_value=1800000000).start()
        self.addCleanup(patch.stopall)
        patch.object(module, 'log').start()
        self.bot = module.TelegramCodexBot('fake-token', {'chat_id': '123'})
        self.bot.state = module.StateStore(Path(self.tmp.name)/'state.json')
        self.app = self.bot.app = FakeApp()
        self.sent, self.alerts = [], []
        self.bot.send = self.send
        self.bot.enqueue = lambda text, keyboard=None: self.alerts.append((text, keyboard))
        self.bot._schedule_quota_monitor_poll = lambda *args: None
        self.bot._answer_callback = lambda *args: None
        self.bot.quota_state.fetch()
        self.bot.quota_state.update(quota_reset_credit_baseline_alerted=True)
        self.app.calls.clear()
        self.ctl = self.bot.quota_resets

    def send(self, text, keyboard=None):
        self.sent.append((text, keyboard))
        return {'message_id': len(self.sent)}

    def observe(self, remaining, reset_at=None):
        self.app.latest = quota(remaining, reset_at=reset_at)
        self.bot._observe_quota(self.bot.quota_state.fetch())

    def button(self, label, actor=123):
        for _, keyboard in reversed(self.alerts + self.sent):
            for row in (keyboard or {}).get('inline_keyboard', []):
                for button in row:
                    if button['text'] == label:
                        return {'data': button['callback_data'], 'from': {'id': actor},
                                'message': {'chat': {'id': 123}, 'message_id': len(self.sent)}}
        self.fail('Missing button '+label)

    def click(self, label):
        self.ctl.handle(self.button(label))

    def reserve(self, remaining=10):
        self.observe(remaining)
        self.click('预约重置')
        self.click('6 小时')

    def test_quota_card_and_credit_notice_have_readable_paragraphs(self):
        from unittest.mock import Mock
        self.bot._refresh_quota_app_server_if_outdated = Mock()
        self.bot.replace_progress_message = Mock()
        self.app.latest['rateLimitsByLimitId']['codex']['primary']['resetsAt'] = int(time.time()) + 5*3600 + 24*60
        self.app.latest['rateLimitsByLimitId']['codex']['secondary']['resetsAt'] = int(time.time()) + 2*86400 + 5*3600
        with patch.object(module, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime.fromtimestamp(time.time()).astimezone()
            self.bot.command_quota()
        text = self.bot.replace_progress_message.call_args.args[1]
        self.assertNotRegex(text, r'(?<!\n)\n(?!\n)')
        self.assertNotIn('\n\n\n', text)
        self.assertIn('**📊 Codex 剩余额度**\n\n**5 小时窗口：**', text)
        self.assertIn('**距离重置：** 还有 5 小时 24 分钟', text)
        self.assertIn('**距离重置：** 还有 2 天 5 小时', text)
        self.assertNotIn('（还有', text)
        notice = self.bot._format_reset_credit_alert(
            module.reset_credit_snapshot(self.app.latest), 2, set(), baseline=True)
        self.assertNotRegex(notice, r'(?<!\n)\n(?!\n)')
        self.assertEqual(self.app.consumed(), [])

    def test_quota_countdown_handles_short_elapsed_and_missing_reset_times(self):
        from unittest.mock import Mock
        self.bot._refresh_quota_app_server_if_outdated = Mock()
        self.bot.replace_progress_message = Mock()
        for delta, expected in [(1800, '还有 30 分钟'), (30, '还有 不到 1 分钟'),
                                (0, '已到重置时间'), (-30, '已到重置时间'), (None, '未知')]:
            with self.subTest(delta=delta):
                self.app.latest['rateLimitsByLimitId']['codex']['primary']['resetsAt'] = None if delta is None else int(time.time()) + delta
                with patch.object(module, 'datetime', wraps=datetime) as clock:
                    clock.now.return_value = datetime.fromtimestamp(time.time()).astimezone()
                    self.bot.command_quota()
                text = self.bot.replace_progress_message.call_args.args[1]
                reset_line = next(line for line in text.splitlines() if line.startswith('**距离重置：**'))
                self.assertIn(expected, reset_line)
        self.assertEqual(self.app.consumed(), [])

    def test_reservation_picker_and_card_keep_paragraphs_and_current_duration_hint(self):
        self.observe(10)
        text = self.alerts[-1][0]
        self.assertIn('6／12／24', text)
        self.assertNotIn('两小时', text)
        self.assertNotRegex(text, r'(?<!\n)\n(?!\n)')
        self.click('预约重置')
        self.assertNotRegex(self.sent[-1][0], r'(?<!\n)\n(?!\n)')
        self.click('6 小时')
        text, _ = self.ctl.render_reservation()
        self.assertNotRegex(text, r'(?<!\n)\n(?!\n)')
        self.assertIn('**\n\n到期：', text)
        self.assertEqual(self.app.consumed(), [])

    def test_immediate_reset_confirmation_has_distinct_heading_and_no_consumption(self):
        self.observe(5)
        self.click('立即重置')
        self.assertTrue(self.sent[-1][0].startswith('**确认立即重置**\n\n'))
        self.assertEqual(self.app.consumed(), [])

    def test_threshold_buttons_weekly_only(self):
        for remaining, labels in [(10, ['预约重置']), (5, ['预约重置','立即重置']),
                                  (3, ['预约重置','立即重置']), (1, ['预约重置','立即重置'])]:
            self.observe(remaining)
            self.assertEqual([b['text'] for b in self.alerts[-1][1]['inline_keyboard'][0]], labels)
        self.assertEqual(len(self.alerts), 4)
        self.observe(0)
        self.assertEqual(len(self.alerts), 4)

    def check_duration(self, hours):
        self.observe(10, reset_at=int(time.time()) + 200000)
        self.click('预约重置')
        self.assertFalse(self.ctl.state().get('quota_reset_reservation'))
        self.assertEqual(self.app.consumed(), [])
        self.click(f'{hours} 小时')
        reservation = self.ctl.state()['quota_reset_reservation']
        self.assertEqual(reservation['hours'], hours)
        self.assertEqual(reservation['expires_at'], time.time() + hours * 3600)
        self.clock.return_value += hours * 3600
        self.app.latest = quota(1, reset_at=1800200000)
        self.ctl.maybe_execute()
        self.assertEqual(self.app.consumed(), [])
        self.assertEqual(self.ctl.state()['quota_reset_reservation']['status'], 'cancelled')

    def test_six_hour_duration(self):
        self.check_duration(6)

    def test_real_callback_scope_updates_reservation_card_without_rearming(self):
        self.observe(5)
        edits = []
        self.bot.edit_message = lambda mid, text, keyboard=None, **kw: edits.append((mid, text, keyboard)) or {'message_id': mid}
        self.bot.send = module.TelegramCodexBot.send.__get__(self.bot)
        callback = self.button('预约重置')
        callback['message']['message_id'] = 70
        self.bot.handle_callback(callback)
        self.assertIn('选择有效时长', edits[-1][1])
        def edited_button(label):
            for row in edits[-1][2]['inline_keyboard']:
                for item in row:
                    if item['text'] == label:
                        return {'data': item['callback_data'], 'from': {'id': 123},
                                'message': {'chat': {'id': 123}, 'message_id': 70}}
            self.fail(label)
        choose = edited_button('12 小时')
        self.bot.handle_callback(choose)
        self.assertEqual(self.ctl.state()['quota_reset_reservation']['message_id'], 70)
        self.assertIn('已生效', edits[-1][1])
        before = self.ctl.state()['quota_reset_reservation'].copy()
        self.bot.handle_callback(edited_button('刷新预约'))
        self.assertEqual(self.ctl.state()['quota_reset_reservation'], before)
        self.bot.handle_callback(edited_button('取消预约'))
        self.assertIn('已结束', edits[-1][1])
        self.assertNotIn('cancelreserve', str(edits[-1][2]))
        self.assertEqual(self.app.consumed(), [])

    def test_real_callback_immediate_confirmation_same_card_is_single_use(self):
        self.observe(5)
        edits = []
        self.bot.edit_message = lambda mid, text, keyboard=None, **kw: edits.append((mid, text, keyboard)) or {'message_id': mid}
        self.bot.send = module.TelegramCodexBot.send.__get__(self.bot)
        callback = self.button('立即重置')
        callback['message']['message_id'] = 80
        self.bot.handle_callback(callback)
        confirm = {'data': edits[-1][2]['inline_keyboard'][0][0]['callback_data'], 'from': {'id': 123},
                   'message': {'chat': {'id': 123}, 'message_id': 80}}
        self.bot.handle_callback(confirm)
        self.assertIn('已使用 1 张', edits[-1][1])
        self.assertNotIn('confirmquota', str(edits[-1][2]))
        self.bot.handle_callback(confirm)
        self.assertEqual(len(self.app.consumed()), 1)

    def test_cancel_duration_selection_allows_return_without_arming(self):
        self.observe(10)
        self.click('预约重置')
        old = self.button('6 小时')
        cancel = self.button('取消')
        spoof = copy.deepcopy(cancel)
        spoof['from']['id'] = 99
        with self.assertRaises(ValueError):
            self.ctl.handle(spoof)
        self.ctl.handle(cancel)
        self.assertFalse(self.ctl.state().get('quota_reset_reservation'))
        self.assertFalse(self.ctl.state().get('quota_reset_selection'))
        with self.assertRaises(ValueError):
            self.ctl.handle(old)
        self.click('预约重置')
        with self.assertRaises(ValueError):
            self.ctl.handle(old)
        self.click('12 小时')
        self.assertEqual(self.ctl.state()['quota_reset_reservation']['hours'], 12)
        self.assertEqual(self.app.consumed(), [])

    def test_twelve_hour_duration(self):
        self.check_duration(12)

    def test_twenty_four_hour_duration(self):
        self.check_duration(24)

    def test_duration_requires_same_actor_message_and_valid_choice(self):
        self.observe(10)
        self.click('预约重置')
        valid = self.button('12 小时')
        for kind in ('actor', 'message', 'duration'):
            spoof = copy.deepcopy(valid)
            if kind == 'actor':
                spoof['from']['id'] = 99
            elif kind == 'message':
                spoof['message']['message_id'] += 1
            else:
                spoof['data'] = spoof['data'].rsplit('|', 1)[0] + '|48'
            with self.assertRaises(ValueError):
                self.ctl.handle(spoof)
        self.ctl.handle(valid)
        before = self.ctl.state()['quota_reset_reservation']
        with self.assertRaises(ValueError):
            self.ctl.handle(valid)
        self.assertEqual(self.ctl.state()['quota_reset_reservation'], before)
        self.assertEqual(self.app.consumed(), [])

    def test_duration_selection_expires_and_reset_invalidates(self):
        self.observe(10)
        self.click('预约重置')
        callback = self.button('24 小时')
        self.clock.return_value += 120
        with self.assertRaises(ValueError):
            self.ctl.handle(callback)
        self.click('预约重置')
        callback = self.button('24 小时')
        self.observe(100)
        with self.assertRaises(ValueError):
            self.ctl.handle(callback)
        self.assertFalse(self.ctl.state().get('quota_reset_reservation'))

    def test_quota_query_allows_advance_reservation(self):
        self.app.latest = quota(80)
        self.bot._refresh_quota_app_server_if_outdated = lambda: None
        self.bot.replace_progress_message = lambda progress, text, keyboard=None: self.send(text, keyboard)
        self.bot.command_quota()
        self.click('预约重置')
        self.click('12 小时')
        self.assertEqual(self.ctl.state()['quota_reset_reservation']['status'], 'armed')
        self.ctl.maybe_execute()
        self.assertEqual(self.app.consumed(), [])

    def test_five_hour_has_no_alert(self):
        for remaining in [10,5,3,1,0,100]:
            self.app.latest = quota(80, five_hour=remaining)
            self.bot._observe_quota(self.bot.quota_state.fetch())
        self.assertEqual(self.alerts, [])

    def test_command_cannot_reset(self):
        self.bot.handle_text('/reset', requester_id='42')
        self.assertEqual(self.app.calls, [])
        self.assertIn('无法识别', self.sent[-1][0])

    def test_reserve_exact_three_not_triggered_then_single_use(self):
        self.reserve()
        self.observe(3)
        self.ctl.maybe_execute()
        self.assertEqual(self.app.consumed(), [])
        self.observe(2.9)
        self.ctl.maybe_execute()
        self.ctl.maybe_execute()
        self.assertEqual(len(self.app.consumed()), 1)
        self.assertEqual(self.app.consumed()[0]['creditId'], 'card-1')
        self.assertEqual(self.ctl.state()['quota_reset_reservation']['status'], 'triggered')

    def test_duplicate_reservation_does_not_extend_expiry(self):
        self.reserve()
        before = self.ctl.state()['quota_reset_reservation']
        self.clock.return_value += 3600
        self.click('预约重置')
        self.assertEqual(self.ctl.state()['quota_reset_reservation'], before)

    def test_expiration_and_old_button_do_not_rearm(self):
        self.reserve()
        old = self.button('预约重置')
        self.clock.return_value += 21600
        self.app.latest = quota(1, reset_at=1800086400)
        self.ctl.maybe_execute()
        self.assertEqual(self.app.consumed(), [])
        self.ctl.handle(old)
        self.assertNotEqual(self.ctl.state()['quota_reset_reservation']['status'], 'armed')

    def test_cancel_reservation(self):
        self.reserve()
        self.click('取消预约')
        self.observe(1)
        self.ctl.maybe_execute()
        self.assertEqual(self.app.consumed(), [])

    def test_expiry_is_capped_at_natural_reset(self):
        self.observe(5, reset_at=int(time.time())+600)
        self.click('预约重置')
        self.click('6 小时')
        self.assertEqual(self.ctl.state()['quota_reset_reservation']['expires_at'], time.time()+600)

    def test_natural_new_cycle_old_button_never_works(self):
        self.reserve()
        old = self.button('预约重置')
        self.observe(1, reset_at=int(time.time())+604800)
        self.ctl.maybe_execute()
        with self.assertRaises(ValueError):
            self.ctl.handle(old)
        self.assertEqual(self.app.consumed(), [])

    def test_activity_reset_same_deadline_then_depletion_does_not_trigger(self):
        self.reserve(5)
        self.observe(100)
        self.observe(1)
        self.ctl.maybe_execute()
        self.assertEqual(self.app.consumed(), [])

    def test_partial_recovery_invalidates_reservation(self):
        self.reserve(5)
        self.observe(6)
        self.observe(1)
        self.ctl.maybe_execute()
        self.assertEqual(self.app.consumed(), [])

    def test_missing_data_cancels_and_never_rearms(self):
        self.reserve()
        self.app.latest = {}
        self.bot._observe_quota(self.bot.quota_state.fetch())
        self.observe(1)
        self.ctl.maybe_execute()
        self.assertEqual(self.app.consumed(), [])

    def test_restart_cancels_reservation(self):
        self.reserve()
        self.bot.state = module.StateStore(self.bot.state.path)
        self.ctl.startup()
        self.observe(1)
        self.ctl.maybe_execute()
        self.assertEqual(self.app.consumed(), [])

    def test_immediate_requires_confirmation_and_rechecks_five(self):
        self.observe(5)
        self.click('立即重置')
        self.assertEqual(self.app.consumed(), [])
        confirmation = self.button('确认使用 1 张重置卡')
        self.app.latest = quota(6)
        with self.assertRaises(ValueError):
            self.ctl.handle(confirmation)
        self.assertEqual(self.app.consumed(), [])

    def test_immediate_success_duplicate_click_and_old_alert_locked(self):
        self.observe(5)
        old = self.button('立即重置')
        self.click('立即重置')
        confirm = self.button('确认使用 1 张重置卡')
        self.ctl.handle(confirm)
        for callback in [confirm, old]:
            with self.assertRaises(ValueError):
                self.ctl.handle(callback)
        self.assertEqual(len(self.app.consumed()), 1)

    def test_immediate_cancels_active_reservation(self):
        self.reserve(5)
        self.click('立即重置')
        confirm = self.button('确认使用 1 张重置卡')
        self.observe(1)
        self.ctl.maybe_execute()
        self.assertEqual(self.app.consumed(), [])
        self.ctl.handle(confirm)
        self.assertEqual(len(self.app.consumed()), 1)

    def test_duplicate_immediate_does_not_replace_confirmation(self):
        self.observe(5)
        self.click('立即重置')
        before = self.ctl.state()['quota_reset_confirmation']
        self.clock.return_value += 30
        self.app.latest = quota(5, reset_at=1800086400)
        self.click('立即重置')
        self.assertEqual(self.ctl.state()['quota_reset_confirmation'], before)

    def test_expired_confirmation_actor_chat_message_spoof(self):
        self.observe(5)
        self.click('立即重置')
        original = self.button('确认使用 1 张重置卡')
        for field in ['actor','chat','message']:
            c = copy.deepcopy(original)
            if field == 'actor': c['from']['id'] = 43
            if field == 'chat': c['message']['chat']['id'] = 999
            if field == 'message': c['message']['message_id'] = 999
            with self.assertRaises(ValueError): self.ctl.handle(c)
        self.clock.return_value += 120
        with self.assertRaises(ValueError): self.ctl.handle(original)
        self.assertEqual(self.app.consumed(), [])

    def test_account_changed_and_card_gone_fail_closed(self):
        self.reserve(3)
        account = self.bot.quota_state.scope()['account']
        self.app.email = 'another@example.invalid'
        self.observe(1)
        self.ctl.maybe_execute()
        self.assertEqual(self.app.consumed(), [])
        self.assertEqual(self.bot.state.snapshot()['quota_accounts']['accounts'][account]['quota_reset_reservation']['status'], 'cancelled')

    def test_pinned_card_not_replaced(self):
        self.reserve(3)
        self.observe(1)
        self.app.latest['rateLimitResetCredits']['credits'].pop(0)
        self.ctl.maybe_execute()
        self.assertEqual(self.app.consumed(), [])

    def test_timeout_never_retries_even_restart(self):
        self.reserve(3)
        self.observe(1)
        self.app.fail = 'account/rateLimitResetCredit/consume'
        self.ctl.maybe_execute()
        self.ctl.maybe_execute()
        self.bot.state = module.StateStore(self.bot.state.path)
        self.ctl.startup()
        self.observe(100)
        self.observe(5)
        with self.assertRaises(ValueError): self.click('立即重置')
        self.assertEqual(len(self.app.consumed()), 1)

    def test_durable_before_consume_and_concurrent_ticks(self):
        self.reserve(3)
        self.observe(1)
        account = self.bot.quota_state.scope()['account']
        observed = []
        def hook(method):
            if method.endswith('/consume'):
                state = module.StateStore(self.bot.state.path).snapshot()
                observed.append(state['quota_accounts']['accounts'][account])
        self.app.hook = hook
        workers = [threading.Thread(target=self.ctl.maybe_execute) for _ in range(8)]
        for worker in workers: worker.start()
        for worker in workers: worker.join(2)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(len(self.app.consumed()), 1)
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]['quota_reset_reservation']['status'], 'triggered')
        self.assertEqual(observed[0]['quota_reset_attempt']['status'], 'consuming')

    def test_recovery_event_during_final_read_cancels(self):
        self.reserve(3)
        self.observe(1)
        def hook(method):
            if method == 'account/rateLimits/read':
                self.bot.on_app_event({'method':'account/rateLimits/updated','params':quota(100)})
        self.app.hook = hook
        self.ctl.maybe_execute()
        self.assertEqual(self.app.consumed(), [])

    def test_rpc_event_never_waits_for_reset_lock(self):
        with self.ctl.lock:
            worker = threading.Thread(target=self.bot.on_app_event,
                                      args=({'method':'account/rateLimits/updated','params':quota(1)},))
            worker.start()
            worker.join(1)
            self.assertFalse(worker.is_alive())

    def test_no_credit_and_unknown_outcome_stop(self):
        self.reserve(3)
        self.observe(1)
        self.app.outcome = 'newUnknownValue'
        self.ctl.maybe_execute()
        self.assertEqual(self.ctl.state()['quota_reset_attempt']['status'], 'uncertain')
        self.ctl.maybe_execute()
        self.assertEqual(len(self.app.consumed()), 1)

    def test_five_hour_cannot_authorize_reset(self):
        with self.assertRaises(ValueError): module.reset_credit_gate(quota(80, five_hour=0))
        for value in [None, float('nan'), float('inf'), -1,101,True]:
            invalid = quota(1)
            invalid['rateLimitsByLimitId']['codex']['secondary']['usedPercent'] = value
            with self.assertRaises(ValueError): module.reset_credit_gate(invalid)

    def test_expiry_during_last_read_blocks_consume(self):
        self.reserve(3)
        self.observe(1)
        def hook(method):
            if method == 'account/read':
                self.clock.return_value += 21600
        self.app.hook = hook
        self.ctl.maybe_execute()
        self.assertEqual(self.app.consumed(), [])

    def test_unavailable_card_result_never_uses_second_card(self):
        self.reserve(3)
        self.observe(1)
        self.app.outcome = 'noCredit'
        self.ctl.maybe_execute()
        self.ctl.maybe_execute()
        self.assertEqual(len(self.app.consumed()), 1)
        self.assertEqual(self.app.consumed()[0]['creditId'], 'card-1')

    def test_read_failure_cancels_reservation_without_consume(self):
        self.reserve(3)
        account = self.bot.quota_state.scope()['account']
        self.app.fail = 'account/rateLimits/read'
        self.ctl.maybe_execute()
        self.assertEqual(self.app.consumed(), [])
        self.assertEqual(self.bot.state.snapshot()['quota_accounts']['accounts'][account]['quota_reset_reservation']['status'], 'cancelled')

    def test_event_worker_does_not_hide_recovery_from_executor(self):
        self.reserve(3)
        with self.ctl.lock:
            self.bot.on_app_event({'method':'account/rateLimits/updated','params':quota(100)})
            worker = threading.Thread(target=self.bot._quota_event_worker)
            worker.start()
            self.assertEqual(self.bot.quota_events.qsize(), 1)
            self.ctl._drain_events()
            self.assertEqual(self.ctl.state()['quota_reset_reservation']['status'], 'cancelled')
            self.bot.stop_event.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())

    def test_queue_overflow_cancels_reservation(self):
        self.reserve(3)
        for remaining in [100,1]:
            self.bot.on_app_event({'method':'account/rateLimits/updated','params':quota(remaining)})
        self.ctl.maybe_execute()
        self.assertEqual(self.app.consumed(), [])
        self.assertEqual(self.ctl.state()['quota_reset_reservation']['status'], 'cancelled')

    def test_missing_snapshot_cannot_unlock_second_credit(self):
        self.observe(5)
        self.click('立即重置')
        self.click('确认使用 1 张重置卡')
        self.app.latest = {}
        self.bot._observe_quota(self.bot.quota_state.fetch())
        self.observe(5)
        with self.assertRaises(ValueError): self.click('立即重置')
        self.assertEqual(len(self.app.consumed()), 1)

    def test_completed_reset_allows_new_explicit_authorization_after_recovery(self):
        self.reserve(3)
        self.observe(1)
        self.ctl.maybe_execute()
        self.observe(100)
        self.observe(1)
        self.ctl.maybe_execute()
        self.assertEqual(len(self.app.consumed()), 1)
        self.click('立即重置')
        self.click('确认使用 1 张重置卡')
        self.assertEqual(len(self.app.consumed()), 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
