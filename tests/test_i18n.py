"""Language persistence and UI routing tests; no real Telegram/Desktop calls."""
import ast
import json
import os
from pathlib import Path
import string
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from codex_telegram_bridge import i18n
from codex_telegram_bridge import app as b
from codex_telegram_bridge.telegram import notifications as n
from codex_telegram_bridge.plugins.api import Context


class LanguageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        directory = patch.object(i18n, 'data_dir', return_value=self.root)
        directory.start(); self.addCleanup(directory.stop)
        i18n._read_preference.cache_clear()

    def bot(self):
        bot = b.TelegramCodexBot('111:fake', {'chat_id': '123'})
        bot.send = Mock(return_value={'message_id': 8})
        bot._reaction = Mock(return_value=True)
        bot._operation_reaction = Mock(return_value=True)
        bot._answer_callback = Mock()
        bot._set_commands = Mock(return_value=True)
        bot.plugins.menu = Mock(return_value=[])
        bot.plugins.help = Mock(return_value='')
        return bot

    def test_default_invalid_and_persisted_preferences(self):
        self.assertEqual(i18n.get_language(), 'zh-CN')
        for value in ['{', 'null', '[]', '{"language":"fr"}']:
            (self.root/'telegram_language.json').write_text(value)
            self.assertEqual(i18n.get_language(), 'zh-CN')
        (self.root/'unrelated.json').write_text('preserve')
        i18n.set_language('en')
        self.assertEqual(i18n.get_language(), 'en')
        # Simulate an independently running notification process.
        env = {**os.environ, 'CODEX_TELEGRAM_DATA_DIR': str(self.root)}
        value = subprocess.check_output([sys.executable, '-c',
            'from codex_telegram_bridge.i18n import get_language; print(get_language())'], env=env, text=True)
        self.assertEqual(value.strip(), 'en')
        i18n.set_language('zh')
        self.assertEqual(i18n.get_language(), 'zh-CN')
        self.assertEqual((self.root/'unrelated.json').read_text(), 'preserve')
        with self.assertRaises(ValueError): i18n.set_language('../en')
        self.assertEqual(i18n.get_language(), 'zh-CN')

    def test_profile_isolation_and_scoped_language(self):
        i18n.set_language('en')
        with patch.object(i18n, 'data_dir', return_value=self.root/'other'):
            self.assertEqual(i18n.get_language(), 'zh-CN')
        self.assertEqual(i18n.get_language(), 'en')
        with i18n.using_language('zh'):
            self.assertEqual(i18n.tr('刷新'), '刷新')
        self.assertEqual(i18n.tr('刷新'), 'Refresh')

    def test_cli_language_is_explicit_and_persistent(self):
        from codex_telegram_bridge.cli import main
        with patch('codex_telegram_bridge.paths.ensure_data_dir'), patch('builtins.print') as output:
            self.assertEqual(main(['language']), 0)
            output.assert_called_with('zh-CN')
            self.assertFalse((self.root/'telegram_language.json').exists())
            self.assertEqual(main(['language', 'en']), 0)
            self.assertEqual(i18n.get_language(), 'en')

    def test_catalog_coverage_and_placeholders(self):
        catalog = i18n.english_catalog()
        formatter = string.Formatter()
        fields = lambda text: sorted(name for _, name, _, _ in formatter.parse(text) if name is not None)
        for zh, en in catalog.items():
            self.assertTrue(en)
            self.assertEqual(fields(zh), fields(en), zh)
            self.assertFalse(any('\u4e00' <= c <= '\u9fff' for c in en), zh)
        for path in Path(b.__file__).parent.rglob('*.py'):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for call in ast.walk(tree):
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == '_t':
                    self.assertIsInstance(call.args[0], ast.Constant, str(path))
                    self.assertIn(call.args[0].value, catalog, str(path))
                    self.assertEqual(sorted(k.arg for k in call.keywords), fields(call.args[0].value))

    def test_template_values_and_model_json_are_never_translated(self):
        body = '{"suggestion":"取消", "message":"刷新 {p0} <tag> 中文"}'
        with i18n.using_language('en'), patch.object(n, 'conversation_title', return_value='中文任务'):
            self.assertEqual(i18n.tr('**任务：** {p0}', p0=body), '**Task:** '+body)
            result = n.build_message({'thread_id':'task-123', 'cwd':'/workspace/中文目录',
                                      'last_assistant_message':body}, True)
            self.assertIn('**Last reply:**', result)
            self.assertIn('中文任务', result)
            self.assertIn('中文目录', result)
            self.assertTrue(result.endswith(body))
            self.assertFalse(n.is_internal_suggestions_completion({'last-assistant-message':body}))

    def test_command_switch_and_pending_input_keep_original_payload(self):
        bot = self.bot()
        bot.awaiting_continue[bot.chat_id] = 'task-original'
        bot.start_turn = Mock()
        bot.handle_text('/language en')
        self.assertEqual(i18n.get_language(), 'en')
        self.assertIn('English', bot.send.call_args.args[0])
        bot.command_help()
        self.assertIn('Command help', bot.send.call_args.args[0])
        bot.awaiting_continue[bot.chat_id] = 'task-original'
        body = '刷新 {p0} /language zh；保留原文'
        bot.handle_text(body)
        bot.start_turn.assert_called_once_with('task-original', body)
        bot.async_inputs.reply_to = Mock(return_value=False)
        bot.sides.lookup = Mock(return_value=None)
        bot.submit_direct_reply = Mock()
        with patch.object(b, 'lookup_task_message', return_value='task-original'):
            bot.handle_text(body, reply_to_message_id=19)
        bot.submit_direct_reply.assert_called_once_with('task-original', body)
        bot.command_language('zh')
        self.assertEqual(i18n.get_language(), 'zh-CN')
        bot.command_language('invalid')
        self.assertIn('用法', bot.send.call_args.args[0])

    def test_language_callback_and_unauthorized_sender(self):
        bot = self.bot()
        callback = {'id':'cb', 'data':'language|en', 'from':{'id':999,'is_bot':False},
                    'message':{'chat':{'id':123},'message_id':9}}
        bot.handle_callback(callback)
        self.assertEqual(i18n.get_language(), 'zh-CN')
        callback['from']['id'] = 123
        with patch.object(n, 'navigation_view', return_value=None), patch.object(bot, 'edit_message'):
            bot.handle_callback(callback)
        self.assertEqual(i18n.get_language(), 'en')

    def test_english_telegram_client_does_not_override_chinese_default(self):
        bot = self.bot()
        bot.handle_update({'message': {'message_id':7, 'text':'/language',
            'chat':{'id':123,'type':'private'}, 'from':{'id':123,'is_bot':False,'language_code':'en'}}})
        self.assertEqual(i18n.get_language(), 'zh-CN')
        self.assertIn('当前语言', bot.send.call_args.args[0])

    def test_menus_default_chinese_with_selected_chat_override(self):
        bot = self.bot()
        i18n.set_language('en')
        with patch.object(b, 'telegram_call', return_value=True) as api:
            self.assertTrue(b.TelegramCodexBot._set_commands(bot))
        default, selected = [call.args[2] for call in api.call_args_list]
        self.assertNotIn('scope', default)
        self.assertEqual(selected['scope'], {'type':'chat','chat_id':'123'})
        self.assertTrue(any('查看' in item['description'] for item in default['commands']))
        self.assertFalse(any('\u4e00' <= c <= '\u9fff' for item in selected['commands'] for c in item['description']))
        self.assertTrue(all(len(item['description']) <= 100 for item in selected['commands']))
        self.assertIn('settings', [item['command'] for item in selected['commands']])

    def test_callback_payloads_stay_identical_after_switch(self):
        keyboards = []
        for language in ('zh', 'en'):
            with i18n.using_language(language):
                keyboards.append(n.build_notification_keyboard('thread-123', 'a'*32))
        callbacks = lambda k: [b['callback_data'] for row in k['inline_keyboard'] for b in row]
        self.assertEqual(callbacks(keyboards[0]), callbacks(keyboards[1]))
        self.assertNotEqual(keyboards[0], keyboards[1])

    def test_unknown_quota_and_plugin_context(self):
        with i18n.using_language('en'):
            self.assertEqual(b.format_quota_reset_countdown(None), 'Unknown')
        ctx = Context(self.root, self.root, self.root, {})
        self.assertEqual(ctx.tr('中文', 'English'), '中文')
        ctx = Context(self.root, self.root, self.root, {}, 'en')
        self.assertEqual(ctx.tr('中文', 'English'), 'English')


if __name__ == '__main__':
    unittest.main()
