"""Compatibility UI and optional runtime settings use the shared controller."""
import json
from unittest import TestCase
from unittest.mock import patch

import test_shared_controller as fixtures
from codex_telegram_bridge.backends.app_server.compatibility import translate_callback, handle_callback
from codex_telegram_bridge.i18n import using_language
from codex_telegram_bridge.telegram import notifications as n


class RuntimeUITests(TestCase):
    setUp = fixtures.SharedControllerTests.setUp
    migrate = fixtures.SharedControllerTests.migrate
    update = fixtures.SharedControllerTests.update

    def test_models_use_stable_values_and_stale_button_never_selects_new_index(self):
        self.rpc.request.return_value = {'data': [{'model': 'model-a'}, {'model': 'model-b'}]}
        self.bot.runtime_options.show()
        old = self.sent[-1][1][0]['inline_keyboard'][0][0]['callback_data'].partition('|')[2]
        self.rpc.request.return_value = {'data': [{'model': 'model-b'}, {'model': 'model-a'}]}
        self.bot.runtime_options.show()
        self.bot.runtime_options.callback(old)
        self.assertEqual(self.bot.settings['model'], 'model-a')
        self.assertEqual(self.bot.state.snapshot()['runtime_model'], 'model-a')

    def test_removed_model_is_rejected_instead_of_selecting_another(self):
        self.rpc.request.return_value = {'data': [{'model': 'model-a'}]}
        self.bot.runtime_options.show()
        old = self.sent[-1][1][0]['inline_keyboard'][0][0]['callback_data'].partition('|')[2]
        self.rpc.request.return_value = {'data': [{'model': 'model-b'}]}
        with self.assertRaises(ValueError): self.bot.runtime_options.callback(old)
        self.assertNotEqual(self.bot.settings.get('model'), 'model-b')

    def test_effort_is_validated_against_current_model_and_unchanged_on_error(self):
        self.bot.settings['model'] = 'model-a'
        self.rpc.request.return_value = {'data': [{'model': 'model-a', 'supportedReasoningEfforts': [{'reasoningEffort': 'high'}]}]}
        with self.assertRaises(ValueError): self.bot.runtime_options.configure('effort', 'unsupported')
        self.bot.runtime_options.configure('effort', 'high')
        self.assertEqual(self.bot.state.snapshot()['runtime_effort'], 'high')

    def test_opening_settings_never_changes_selection_or_defaults(self):
        before = self.bot.state.snapshot()
        self.update('/settings')
        self.assertEqual(self.bot.state.snapshot(), before)
        self.rpc.request.assert_not_called()
        labels = [b['text'] for row in self.sent[-1][1][0]['inline_keyboard'] for b in row]
        self.assertIn('🌙 静默模式', labels); self.assertIn('选择模型', labels)

    def test_settings_and_model_labels_follow_language_without_changing_task_text(self):
        self.rpc.request.return_value = {'data': [{'model': 'model-a', 'displayName': '原始模型名'}]}
        with using_language('en'):
            self.bot.command_settings()
            self.assertIn('Settings', self.sent[-1][0])
            self.bot.runtime_options.show()
            self.assertIn('Choose model', self.sent[-1][0])
            self.assertEqual(self.sent[-1][1][0]['inline_keyboard'][0][0]['text'], '原始模型名')

    def test_legacy_page_and_download_resolve_same_original_markdown(self):
        row = self.legacy.new_conversation(str(self.root)); self.legacy.bind(row['id'], 'original-thread')
        body = '| A | B |\n|---|---|\n| 1 | 2 |\n\n```py\na=1\n```'
        key = self.legacy.save_view(row['id'], body, 'turn-one')
        self.migrate()
        page = translate_callback(self.bot, {'data': 'ux|page|'+key+':0'})['data']
        download = translate_callback(self.bot, {'data': 'ux|download|'+key})['data']
        cache = page.split('|')[1]
        self.assertEqual(download, 'replyfile|'+cache)
        self.assertEqual(n.load_cached_reply(cache)['text'], body)

    def test_failed_draft_download_never_submits_and_retains_text(self):
        row = self.legacy.new_conversation(str(self.root)); self.legacy.bind(row['id'], 'original-thread')
        self.legacy.enqueue(20, row['id'], 'original draft', kind='desktop')
        with patch.object(n, 'send_document') as send:
            handle_callback(self.bot, 'legacydraft', '20')
            self.assertEqual(send.call_args.kwargs['content'], b'original draft')
        self.rpc.request.assert_not_called(); self.bot.desktop.send_follow_up.assert_not_called()

    def test_unknown_draft_is_not_retryable(self):
        row = self.legacy.desktop_conversation('original-thread', str(self.root), 'Original')
        self.legacy.enqueue(20, row['id'], 'unknown input', kind='desktop')
        self.legacy.update_job(20, status='uncertain')
        with self.assertRaises(ValueError): handle_callback(self.bot, 'legacyretry', '20')
        self.bot.desktop.send_follow_up.assert_not_called()

    def test_failed_attachment_retry_retains_original_attachment_path(self):
        row = self.legacy.desktop_conversation('original-thread', str(self.root), 'Original')
        file = self.root/'picture.png'; file.write_bytes(b'fixture')
        self.legacy.enqueue(20, row['id'], 'look', [{'type': 'text', 'text': 'look'}, {'type': 'localImage', 'path': str(file)}], kind='desktop')
        self.legacy.update_job(20, status='failed')
        from unittest.mock import Mock
        self.bot.submit_direct_reply = Mock()
        handle_callback(self.bot, 'legacyretry', '20')
        self.assertEqual(self.bot.submit_direct_reply.call_args.args[0], 'original-thread')
        self.assertIn(str(file), self.bot.submit_direct_reply.call_args.args[1])
        with self.assertRaises(ValueError): handle_callback(self.bot, 'legacyretry', '20')
        self.assertEqual(self.bot.submit_direct_reply.call_count, 1)

    def test_file_download_rejects_escape_and_symlink_and_keeps_workspace_file(self):
        from pathlib import Path
        import tempfile
        self.bot.resolve_thread = lambda ref: 'original-thread'
        self.rpc.request.return_value = {'thread': {'id': 'original-thread', 'cwd': str(self.root)}}
        good = self.root/'result.txt'; good.write_text('safe result')
        with tempfile.TemporaryDirectory() as external:
            outside = Path(external)/'private.txt'; outside.write_text('must not send')
            targets = [str(outside), '../private.txt']
            import os
            if os.name != 'nt':
                (self.root/'link.txt').symlink_to(outside)
                targets.append('link.txt')
            with patch('codex_telegram_bridge.features.runtime_options.send_document') as send:
                for target in targets:
                    with self.assertRaises(ValueError): self.bot.runtime_options.command('/file', 'original '+target, None)
                send.assert_not_called()
                self.bot.runtime_options.command('/file', 'original result.txt', None)
                self.assertEqual(send.call_args.kwargs['content'], b'safe result')
