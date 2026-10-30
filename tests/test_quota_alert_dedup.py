"""Low-quota notifications use account history, not reset-button validity."""
import time
import unittest

import test_telegram_quota as fixture
from codex_telegram_bridge.core import bot as core


class QuotaAlertDedupTests(unittest.TestCase):
    setUp = fixture.QuotaTests.setUp
    send = fixture.QuotaTests.send
    observe = fixture.QuotaTests.observe
    button = fixture.QuotaTests.button

    def low_alerts(self):
        return [text for text, _ in self.alerts if '额度低于' in text]

    def test_exhausted_quota_and_second_level_deadline_jitter_notify_once(self):
        deadline = int(time.time()) + 86400
        self.observe(0, deadline)
        old_button = self.button('立即重置')
        first_cycle = self.ctl.state()['quota_reset_cycle']['id']
        for offset in (1, -1, 0, 1, 0):
            self.clock.return_value += 300
            self.observe(0, deadline + offset)
        self.assertNotEqual(self.ctl.state()['quota_reset_cycle']['id'], first_cycle)
        self.assertEqual(len(self.low_alerts()), 1)
        with self.assertRaises(ValueError):
            self.ctl.handle(old_button)
        self.assertEqual(self.app.consumed(), [])

    def test_low_percentage_fluctuation_does_not_repeat_one_percent_alert(self):
        deadline = int(time.time()) + 86400
        for remaining in (1, 0, 0.5, 0, 1, 0):
            self.clock.return_value += 300
            self.observe(remaining, deadline)
        self.assertEqual(len(self.low_alerts()), 1)

    def test_missing_snapshot_does_not_erase_low_alert_history(self):
        deadline = int(time.time()) + 86400
        self.observe(0, deadline)
        self.app.latest = {}
        self.bot._observe_quota(self.bot.quota_state.fetch())
        self.assertFalse(self.ctl.state()['quota_reset_cycle'])
        self.observe(0, deadline)
        self.assertEqual(len(self.low_alerts()), 1)

    def test_restart_and_reconnect_retain_sent_thresholds(self):
        deadline = int(time.time()) + 86400
        self.observe(0, deadline)
        self.bot.state = core.StateStore(self.bot.state.path)
        self.ctl.startup()
        self.observe(0, deadline + 1)
        self.app.connection_generation = 1
        self.observe(0, deadline)
        self.assertEqual(len(self.low_alerts()), 1)

    def test_later_deadline_without_recovery_is_not_a_new_alert_cycle(self):
        deadline = int(time.time()) + 86400
        self.observe(0, deadline)
        self.observe(0, deadline + 3600)
        self.assertEqual(len(self.low_alerts()), 1)

    def test_progressively_lower_thresholds_still_notify_once_each(self):
        deadline = int(time.time()) + 86400
        for i, remaining in enumerate((10, 10, 5, 5, 3, 3, 1, 1, 0)):
            self.observe(remaining, deadline + i % 2)
        self.assertEqual(len(self.low_alerts()), 4)
        for text, threshold in zip(self.low_alerts(), (10, 5, 3, 1)):
            self.assertIn(f'等于 {threshold}%', text)

    def test_confirmed_natural_reset_rearms_low_alerts_once(self):
        deadline = int(time.time()) + 3600
        self.observe(0, deadline)
        self.clock.return_value = deadline + 1
        self.observe(0, deadline + 604800)
        self.observe(0, deadline + 604801)
        self.assertEqual(len(self.low_alerts()), 2)

    def test_confirmed_early_recovery_rearms_low_alerts_once(self):
        deadline = int(time.time()) + 86400
        self.observe(0, deadline)
        self.observe(100, deadline)
        self.observe(1, deadline)
        self.observe(0, deadline + 1)
        self.assertEqual(len(self.low_alerts()), 2)

    def test_account_switch_back_preserves_each_accounts_alert_history(self):
        deadline = int(time.time()) + 86400
        for account, offset in (('a', 0), ('b', 0), ('a', 1), ('b', -1)):
            self.app.email = account + '@example.invalid'
            self.observe(0, deadline + offset)
        self.assertEqual(len(self.low_alerts()), 2)


if __name__ == '__main__':
    unittest.main()
