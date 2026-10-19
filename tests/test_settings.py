"""Settings navigation, safe menu ordering, and legacy compatibility."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from codex_telegram_bridge import app as b, i18n
from codex_telegram_bridge.telegram import notifications as n


class SettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / 'credentials.json'
        self.original = {'token_dpapi': 'opaque-ciphertext', 'chat_id': '123',
                         'silent': True, 'include_message_preview': True, 'custom': {'keep': 1}}
        self.config.write_text(json.dumps(self.original), encoding='utf-8')
        for target, name, value in [(i18n, 'data_dir', lambda: self.root),
                                    (n, 'CONFIG_PATH', self.config)]:
            mocked = patch.object(target, name, value)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.bot = b.TelegramCodexBot('111:fake', {'chat_id': '123'})
        self.bot.state = b.StateStore(self.root / 'state.json')
        self.bot.send = Mock(return_value={'message_id': 17})
        self.bot._answer_callback = Mock()
        self.bot._set_commands = Mock(return_value=True)
        self.bot.plugins.menu = Mock(return_value=[{'command': 'screenshot', 'description': 'Capture screens'}])
        self.bot.app = Mock()
        self.bot.desktop = Mock()

    def callback(self, value, user=123):
        callback = {'id': 'cb', 'data': 'settings|' + value, 'from': {'id': user, 'is_bot': False},
                    'message': {'message_id': 17, 'chat': {'id': 123}}}
        with patch.object(n, 'navigation_view', return_value=None):
            self.bot.handle_callback(callback)

    def test_menu_query_first_settings_consolidated_and_stop_last(self):
        commands = [item['command'] for item in self.bot._command_list()]
        self.assertEqual(commands[:3], ['quota', 'tasks', 'running'])
        self.assertEqual(commands[-5:], ['side', 'send', 'steer', 'new', 'stop'])
        self.assertIn('settings', commands)
        self.assertEqual(commands[commands.index('plugins') + 1], 'screenshot')
        self.assertFalse(set(commands) & {'mute', 'unmute', 'language', 'cwd', 'subagents'})
        self.assertEqual(len(commands), len(set(commands)))
        self.bot.plugins.menu.return_value = [dict(command=f'plugin{i}', description='Plugin') for i in range(110)]
        commands = self.bot._command_list()
        self.assertEqual(len(commands), 100)
        self.assertEqual(commands[-1]['command'], 'stop')

    def test_browsing_and_help_preserve_preferences_and_pending_inputs(self):
        self.bot.awaiting_continue['123'] = 'thread-a'
        self.bot.awaiting_steer['123'] = 'thread-b'
        self.bot.awaiting_direct_reply[7] = 'thread-c'
        self.bot.pending_direct_replies['pending'] = {'source_message_id': 8}
        before = self.bot.state.snapshot()
        for page in ('', 'notifications', 'language', 'cwd', 'home'):
            self.callback(page)
        self.bot.command_help()
        self.assertEqual(self.bot.state.snapshot(), before)
        self.assertEqual(json.loads(self.config.read_text()), self.original)
        self.assertFalse((self.root / 'telegram_language.json').exists())
        self.assertEqual(self.bot.awaiting_continue, {'123': 'thread-a'})
        self.assertEqual(self.bot.awaiting_steer, {'123': 'thread-b'})
        self.assertEqual(self.bot.awaiting_direct_reply, {7: 'thread-c'})
        self.assertIn('pending', self.bot.pending_direct_replies)
        self.bot.app.request.assert_not_called()
        self.bot.desktop.send_user_message.assert_not_called()
        self.bot._set_commands.assert_not_called()

    def test_notification_actions_are_explicit_and_preserve_credentials(self):
        for _ in range(2):
            self.callback('completion:off')
            self.callback('subagents:on')
        self.assertTrue(self.bot.state.snapshot()['muted'])
        self.assertEqual(json.loads(self.config.read_text()), {**self.original, 'include_subagent_notifications': True})
        self.callback('completion:on')
        self.callback('subagents:off')
        self.assertFalse(self.bot.state.snapshot()['muted'])
        self.assertEqual(json.loads(self.config.read_text()), {**self.original, 'include_subagent_notifications': False})

    def test_invalid_and_unauthorized_settings_cannot_write(self):
        before = self.bot.state.snapshot()
        for value in ('completion:off', 'subagents:on', 'language:en', 'cwd ' + str(self.root)):
            self.callback(value, user=999)
        for value in ('completion:toggle', 'subagents:yes', 'language:fr', 'language:en:extra'):
            self.callback(value)
        self.assertEqual(self.bot.state.snapshot(), before)
        self.assertEqual(json.loads(self.config.read_text()), self.original)
        self.assertFalse((self.root / 'telegram_language.json').exists())

    def test_language_switch_refreshes_menu_and_keeps_back_navigation(self):
        self.callback('language:en')
        self.assertEqual(i18n.get_language(), 'en')
        self.bot._set_commands.assert_called_once()
        text, markup = self.bot.send.call_args.args
        self.assertIn('Interface language: English', text)
        buttons = [button for row in markup['inline_keyboard'] for button in row]
        self.assertIn({'text': '⬅️ Back to settings', 'callback_data': 'settings|home'}, buttons)
        self.bot._set_commands.return_value = False
        self.callback('language:en')
        self.assertIn('menu could not refresh', self.bot.send.call_args.args[0])
        self.callback('home')
        self.assertIn('Settings', self.bot.send.call_args.args[0])
        self.callback('language:zh-CN')
        self.assertEqual(i18n.get_language(), 'zh-CN')

    def test_settings_cwd_and_legacy_commands_remain_available(self):
        folder = self.root / 'folder with spaces'
        folder.mkdir()
        self.bot.handle_text('/settings cwd ' + str(folder))
        self.assertEqual(self.bot.state.snapshot()['default_cwd'], str(folder.resolve()))
        self.bot.handle_text('/mute')
        self.assertTrue(self.bot.state.snapshot()['muted'])
        self.bot.handle_text('/unmute')
        self.assertFalse(self.bot.state.snapshot()['muted'])
        self.bot.handle_text('/subagents on')
        self.assertTrue(n.subagent_notifications_enabled())
        self.bot.handle_text('/language en')
        self.assertEqual(i18n.get_language(), 'en')
        self.bot.handle_text('/cwd ' + str(self.root))
        self.assertEqual(self.bot.state.snapshot()['default_cwd'], str(self.root.resolve()))


if __name__ == '__main__':
    unittest.main()
