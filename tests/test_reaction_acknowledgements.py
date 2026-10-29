"""A desired reaction is not evidence that Telegram applied it."""
from pathlib import Path
from contextlib import closing
import tempfile
import unittest
from unittest.mock import Mock, patch

from codex_telegram_bridge.core import bot as b
from codex_telegram_bridge.telegram import notifications as n


class ReactionAcknowledgementTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        for owner, field, value in ((n, 'REACTION_DB', root / 'reactions.db'),
                                    (n, 'LOG_PATH', root / 'notify.log')):
            context = patch.object(owner, field, value)
            context.start()
            self.addCleanup(context.stop)

    def test_definite_rejection_stays_false_and_submission_keeps_text_and_target(self):
        def api(token, method, payload, **kwargs):
            if payload['reaction']:
                raise n.TelegramApiError(400, 'Bad Request: REACTION_INVALID')
            return True
        bot = b.TelegramCodexBot('111:test', {'chat_id': '123'})
        bot._ui_local.source_message_id = 42
        bot.send = Mock()
        buttons = {'inline_keyboard': [[{'text': '查看详情', 'callback_data': 'task|task'}]]}
        with patch.object(n, 'telegram_call', side_effect=api) as call:
            self.assertFalse(n.set_task_reaction(bot.token, bot.chat_id, 42, '🤔', 'task', 'turn'))
            self.assertFalse(n.set_task_reaction(bot.token, bot.chat_id, 42, '🤔', 'task', 'turn'))
            self.assertEqual(call.call_count, 1)  # Permanent rejection is not retried.
            bot._submission_feedback('task', 'turn', '已提交到原任务', buttons)
        bot.send.assert_called_once_with('已提交到原任务', buttons)
        self.assertEqual(n.task_reply_source(bot.token, bot.chat_id, 'task', 'turn'), 42)

    def test_only_confirmed_success_is_deduplicated_and_old_state_is_revalidated(self):
        with closing(n._reaction_db()) as db, db:
            # Prior installations persisted desired state without an API receipt.
            db.execute('INSERT INTO reactions VALUES (?,?,?,?,?,?,?)',
                       ('111', '123', 42, 'task', 'turn', '🤔', 1))
        with patch.object(n, 'telegram_call', return_value=True) as call:
            self.assertTrue(n.set_task_reaction('111:test', '123', 42, '🤔', 'task', 'turn'))
            self.assertEqual(call.call_count, 1)
            self.assertTrue(n.set_task_reaction('111:test', '123', 42, '🤔', 'task', 'turn'))
            self.assertEqual(call.call_count, 1)

    def test_rejection_does_not_block_new_state_or_transient_retry(self):
        with patch.object(n, 'telegram_call', side_effect=n.TelegramApiError(400, 'REACTION_INVALID')):
            self.assertFalse(n.set_task_reaction('111:test', '123', 42, '👀', 'task'))
        with patch.object(n, 'telegram_call', side_effect=TimeoutError('connection interrupted')):
            self.assertFalse(n.set_task_reaction('111:test', '123', 42, '🤔', 'task', 'turn'))
        with patch.object(n, 'telegram_call', return_value=True) as call:
            n.retry_task_reactions('111:test')
            self.assertTrue(n.set_task_reaction('111:test', '123', 42, '🤔', 'task', 'turn'))
            self.assertEqual(call.call_count, 1)
            self.assertTrue(n.set_task_reaction('111:test', '123', 42, '🎉', 'task', 'turn'))
        self.assertEqual(n.task_reactions('111:test'), [])
        with closing(n._reaction_db()) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM reaction_results').fetchone()[0], 0)
