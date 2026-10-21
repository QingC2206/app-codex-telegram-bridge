"""Offline checks for local photo uploads, fallback buttons and reply routing."""
import base64
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch, Mock
from urllib.error import HTTPError

import codex_telegram_bridge.telegram.local_images as images
import codex_telegram_bridge.telegram.notifications as n
from notification_fixtures import verified_completion
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jk1sAAAAASUVORK5CYII=')


class LocalImageTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.photo = self.root / 'preview (版本 2).png'
        self.photo.write_bytes(PNG)
        self.path = self.photo.as_posix()
        for owner, name, value in ((n, 'NATIVE_REPLY_DIR', self.root / 'routes'),
                                   (images, 'PATH_CACHE_DIR', self.root / 'paths')):
            patcher = patch.object(owner, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        log = patch.object(n, 'log_error')
        log.start()
        self.addCleanup(log.stop)
        self.payload = {'chat_id': '123', 'text': '**完成**\n\n![截图](' + self.path + ')',
                        'reply_markup': n.build_notification_keyboard('thread-a'),
                        'reply_parameters': {'message_id': 7, 'allow_sending_without_reply': True},
                        'disable_notification': True, 'protect_content': True}

    def test_windows_paths_file_uri_angles_and_relative_base(self):
        text = ('![one](D:/project/shot.png)\n![two](</D:/my folder/shot (2).png>)\n'
                r'![three](C:\work\shot.jpg)' + '\n![four](file:///D:/my%20folder/shot.png)\n')
        self.assertEqual([x.path for x in images.find_local_images(text)],
                         ['D:/project/shot.png', 'D:/my folder/shot (2).png', 'C:/work/shot.jpg', 'D:/my folder/shot.png'])
        relative = images.find_local_images('![relative](preview.png)', str(self.root))
        self.assertEqual(relative[0].path, (self.root / 'preview.png').as_posix())
        self.assertEqual(images.local_path("D:/folder 'review'/shot.png"), "D:/folder 'review'/shot.png")
        self.assertEqual(images.local_path("D:/shot.png 'Photo caption'"), 'D:/shot.png')

    def test_code_examples_escaped_embeds_and_remote_urls_are_untouched(self):
        text = ('```markdown\n![one](D:/secret.png)\n```\n'
                '~~~\n![two](D:/secret.png)\n~~~\n'
                '    ![three](D:/secret.png)\n'
                '`![four](D:/secret.png)` and ``![five](D:/secret.png)``\n'
                r'\![six](D:/secret.png)' + '\n![remote](https://example.test/photo.png)\n'
                '![emoji](tg://emoji?id=123)\n')
        self.assertEqual(images.find_local_images(text), [])
        with patch.object(n, 'telegram_multipart_call') as upload:
            prepared = n.prepare_local_image_reply('111:secret', dict(self.payload, text=text), upload=True)
            self.assertEqual(prepared['text'], text)
            upload.assert_not_called()

    def test_upload_precedes_text_and_preserves_both_reply_routes(self):
        events = []
        def photo(*args, **kwargs):
            events.append('photo')
            return {'message_id': 20}
        def text(*args, **kwargs):
            events.append('text')
            return {'message_id': 21}
        with patch.object(n, 'telegram_multipart_call', side_effect=photo) as upload, \
                patch.object(n, 'telegram_call', side_effect=text) as send:
            result = n.send_markdown_message('111:secret', self.payload)
        self.assertEqual(result['message_id'], 21)
        self.assertEqual(events, ['photo', 'text'])
        self.assertEqual(upload.call_args.args[1], 'sendPhoto')
        fields = upload.call_args.args[2]
        self.assertEqual(fields['reply_parameters'], self.payload['reply_parameters'])
        self.assertTrue(fields['disable_notification'])
        self.assertTrue(fields['protect_content'])
        self.assertEqual(upload.call_args.kwargs['content'], PNG)
        self.assertEqual(upload.call_args.kwargs['content_type'], 'image/png')
        self.assertEqual(upload.call_args.kwargs['file_field'], 'photo')
        self.assertNotIn('direct|', str(fields['reply_markup']))
        sent = send.call_args.args[2]
        self.assertIn('图片已发送', sent['rich_message']['markdown'])
        self.assertNotIn('![', sent['rich_message']['markdown'])
        self.assertNotIn('copy_text', str(sent['reply_markup']))
        for message in (20, 21):
            self.assertEqual(n.lookup_task_message('111:secret', '123', message), 'thread-a')
        self.assertIn('![截图]', self.payload['text'])
        self.assertIn('direct|', str(self.payload['reply_markup']))

    def test_upload_rejection_keeps_text_and_copy_path_button(self):
        with patch.object(n, 'telegram_multipart_call', side_effect=n.TelegramApiError(400, 'PHOTO_INVALID')) as upload, \
                patch.object(n, 'telegram_call', return_value={'message_id': 21}) as send:
            n.send_markdown_message('111:secret', self.payload)
        self.assertEqual(upload.call_count, 1)
        self.assertEqual(send.call_count, 1)
        payload = send.call_args.args[2]
        self.assertIn('上传失败', payload['rich_message']['markdown'])
        self.assertIn(self.path, payload['rich_message']['markdown'])
        self.assertEqual(payload['reply_markup']['inline_keyboard'][-1][0]['copy_text']['text'], self.path)
        self.assertEqual(n.lookup_task_message('111:secret', '123', 21), 'thread-a')

    def test_upload_timeout_is_not_retried_and_text_still_sends(self):
        with patch.object(n, 'telegram_multipart_call', side_effect=TimeoutError('timeout')) as upload, \
                patch.object(n, 'telegram_call', return_value={'message_id': 21}) as send:
            n.send_markdown_message('111:secret', self.payload)
        self.assertEqual(upload.call_count, 1)
        self.assertEqual(send.call_count, 1)
        payload = send.call_args.args[2]
        self.assertIn('发送未确认', payload['rich_message']['markdown'])
        self.assertEqual(payload['reply_markup']['inline_keyboard'][-1][0]['copy_text']['text'], self.path)

    def test_missing_invalid_and_oversized_images_do_not_block_text(self):
        invalid = self.root / 'credentials.png'
        invalid.write_text('private token content', encoding='utf-8')
        oversized = self.root / 'large.png'
        with oversized.open('wb') as f:
            f.truncate(images.MAX_PHOTO_BYTES + 1)
        for path in (self.root / 'missing.png', invalid, oversized):
            with self.subTest(path=path.name), patch.object(n, 'telegram_multipart_call') as upload, \
                    patch.object(n, 'telegram_call', return_value={'message_id': 21}) as send:
                n.send_markdown_message('111:secret', dict(self.payload, text='![preview](' + path.as_posix() + ')'))
                upload.assert_not_called()
                self.assertEqual(send.call_count, 1)
                payload = send.call_args.args[2]
                self.assertEqual(payload['reply_markup']['inline_keyboard'][-1][0]['copy_text']['text'], path.as_posix())
                self.assertNotIn('private token content', payload['rich_message']['markdown'])

    def test_network_device_and_relative_paths_are_not_read(self):
        for path in ('//server/share/image.png', '//./pipe/image.png', 'relative.png', 'D:/image.png:secret'):
            with self.subTest(path=path), patch.object(Path, 'open') as opened:
                with self.assertRaises(ValueError):
                    images.read_photo(path)
                opened.assert_not_called()

    def test_duplicate_image_references_upload_once(self):
        payload = dict(self.payload, text=self.payload['text'] + '\n\n![同一截图](' + self.path + ')')
        with patch.object(n, 'telegram_multipart_call', return_value={'message_id': 20}) as upload, \
                patch.object(n, 'telegram_call', return_value={'message_id': 21}) as send:
            n.send_markdown_message('111:secret', payload)
        self.assertEqual(upload.call_count, 1)
        self.assertEqual(send.call_args.args[2]['rich_message']['markdown'].count('图片已发送'), 2)

    def test_multiple_photos_partial_failure_keeps_one_fallback_button(self):
        other = self.root / 'other.png'
        other.write_bytes(PNG)
        payload = dict(self.payload, text=self.payload['text'] + '\n\n![另一张](' + other.as_posix() + ')')
        with patch.object(n, 'telegram_multipart_call', side_effect=[{'message_id': 20}, n.TelegramApiError(400, 'PHOTO_INVALID')]) as upload, \
                patch.object(n, 'telegram_call', return_value={'message_id': 21}) as send:
            n.send_markdown_message('111:secret', payload)
        self.assertEqual(upload.call_count, 2)
        sent = send.call_args.args[2]
        self.assertIn('图片已发送', sent['rich_message']['markdown'])
        self.assertIn('上传失败', sent['rich_message']['markdown'])
        buttons = [b for row in sent['reply_markup']['inline_keyboard'] for b in row if 'copy_text' in b]
        self.assertEqual([b['copy_text']['text'] for b in buttons], [other.as_posix()])

    def test_edit_or_page_refresh_does_not_upload(self):
        with patch.object(n, 'telegram_multipart_call') as upload, patch.object(n, 'telegram_call', return_value={'message_id': 21}) as send:
            n.edit_markdown_message('111:secret', dict(self.payload, message_id=21))
        upload.assert_not_called()
        self.assertEqual(send.call_args.args[1], 'editMessageText')
        self.assertEqual(send.call_args.args[2]['reply_markup']['inline_keyboard'][-1][0]['copy_text']['text'], self.path)

    def test_reply_photo_count_and_byte_budgets_use_paths_for_remainder(self):
        other = self.root / 'other.png'
        other.write_bytes(PNG)
        payload = dict(self.payload, text=self.payload['text'] + '\n\n![另一张](' + other.as_posix() + ')')
        for setting, limit in (('MAX_REPLY_PHOTOS', 1), ('MAX_REPLY_PHOTO_BYTES', len(PNG))):
            with self.subTest(setting=setting), patch.object(images, setting, limit), \
                    patch.object(n, 'telegram_multipart_call', return_value={'message_id': 20}) as upload, \
                    patch.object(n, 'telegram_call', return_value={'message_id': 21}) as send:
                n.send_markdown_message('111:secret', payload)
                self.assertEqual(upload.call_count, 1)
                self.assertEqual(send.call_args.args[2]['reply_markup']['inline_keyboard'][-1][0]['copy_text']['text'], other.as_posix())

    def test_relative_notice_image_uses_cwd_and_does_not_send_private_fields(self):
        payload = dict(self.payload, text='![截图](<preview (版本 2).png>)', _local_image_base=str(self.root))
        with patch.object(n, 'telegram_multipart_call', return_value={'message_id': 20}) as upload, \
                patch.object(n, 'telegram_call', return_value={'message_id': 21}) as send:
            n.send_markdown_message('111:secret', payload)
        self.assertEqual(upload.call_args.kwargs['content'], PNG)
        self.assertNotIn('_local_image_base', upload.call_args.args[2])
        self.assertNotIn('_local_image_base', send.call_args.args[2])

    def test_photo_after_notification_preview_is_still_uploaded(self):
        preview = '**任务完成**\n\n这是文字预览。'
        payload = dict(self.payload, text=preview, _local_image_text='完整回复正文\n\n' + self.payload['text'])
        with patch.object(n, 'telegram_multipart_call', return_value={'message_id': 20}) as upload, \
                patch.object(n, 'telegram_call', return_value={'message_id': 21}) as send:
            n.send_markdown_message('111:secret', payload)
        self.assertEqual(upload.call_count, 1)
        self.assertEqual(send.call_args.args[2]['rich_message']['markdown'], preview)
        self.assertNotIn('_local_image_text', send.call_args.args[2])
        self.assertNotIn('_local_image_text', upload.call_args.args[2])

    def test_failed_photo_after_preview_keeps_path_button(self):
        payload = dict(self.payload, text='文字预览', _local_image_text=self.payload['text'])
        with patch.object(n, 'telegram_multipart_call', side_effect=n.TelegramApiError(400, 'PHOTO_INVALID')), \
                patch.object(n, 'telegram_call', return_value={'message_id': 21}) as send:
            n.send_markdown_message('111:secret', payload)
        self.assertEqual(send.call_args.args[2]['reply_markup']['inline_keyboard'][-1][0]['copy_text']['text'], self.path)

    def test_long_path_button_is_isolated_by_bot_chat_and_expiry(self):
        path = 'D:/' + 'folder/' * 48 + 'image.png'
        button = images.path_button('111:secret', '123', path)
        key = button['callback_data'].split('|')[1]
        self.assertLessEqual(len(button['callback_data'].encode()), 64)
        self.assertEqual(images.load_path('111:new-secret', '123', key), path)
        self.assertNotIn('secret', (images.PATH_CACHE_DIR / (key + '.json')).read_text())
        for token, chat, candidate in (('222:secret', '123', key), ('111:secret', '456', key), ('111:secret', '123', '../secret')):
            with self.subTest(chat=chat, candidate=candidate), self.assertRaises(ValueError):
                images.load_path(token, chat, candidate)
        with patch.object(images.time, 'time', return_value=time.time() + images.PATH_RETENTION + 10):
            with self.assertRaises(ValueError):
                images.load_path('111:secret', '123', key)

    def test_short_path_button_and_unicode_limit(self):
        self.assertEqual(images.path_button('111:secret', '123', self.path)['copy_text']['text'], self.path)
        astral = 'D:/' + '😀' * 130 + '.png'
        self.assertIn('callback_data', images.path_button('111:secret', '123', astral))

    def test_long_path_callback_displays_path_without_upload_or_task_command(self):
        import codex_telegram_bridge.app as b
        path = 'D:/' + 'folder/' * 48 + 'image.png'
        button = images.path_button('111:secret', '123', path)
        bot = b.TelegramCodexBot('111:secret', {'chat_id': '123'})
        bot.send = Mock()
        bot._answer_callback = Mock()
        bot.start_turn = Mock()
        callback = {'id': 'callback', 'from': {'id': 123, 'is_bot': False}, 'data': button['callback_data'],
                    'message': {'message_id': 21, 'chat': {'id': 123, 'type': 'private'}}}
        with patch.object(n, 'telegram_multipart_call') as upload:
            bot.handle_callback(callback)
        upload.assert_not_called()
        bot.start_turn.assert_not_called()
        self.assertIn(path, bot.send.call_args.args[0])

    def test_completion_ok_waits_for_full_text_after_photo(self):
        events = []
        body = self.payload['text']
        payload = {'hook_event_name': 'Stop', 'thread_id': 'thread-a', 'turn_id': 'turn-a', 'last_assistant_message': body}
        def photo(*args, **kwargs):
            events.append('photo')
            return {'message_id': 20}
        for success in (False, True):
            events.clear()
            def text(*args, **kwargs):
                events.append('text')
                if not success:
                    raise TimeoutError('timeout')
                return {'message_id': 21}
            with self.subTest(success=success), patch.object(n, 'load_config', return_value=({'chat_id': '123', 'include_message_preview': True}, '111:secret')), \
                    patch.object(n, 'notifications_muted', return_value=False), patch.object(n, 'route_completion', side_effect=verified_completion), patch.object(n, 'reserve_completion', return_value=True), patch.object(n, 'subagent_completion_context', return_value=None), \
                    patch.object(n, 'cache_full_reply', return_value=None), patch.object(n, 'conversation_title', return_value='任务'), \
                    patch.object(n, 'resolve_desktop_submissions'), patch.object(n, 'attach_task_reply'), \
                    patch.object(n, 'telegram_multipart_call', side_effect=photo), patch.object(n, 'telegram_call', side_effect=text), \
                    patch.object(n, 'complete_payload_reactions', side_effect=lambda _: events.append('ok')) as done:
                n.send_hook_notification(payload)
                self.assertEqual(events, ['photo', 'text', 'ok'] if success else ['photo', 'text'])
                self.assertEqual(done.call_count, int(success))

    def test_multipart_explicit_rejections_are_typed_and_redacted(self):
        error = HTTPError('https://api.telegram.org/bot111:secret/sendPhoto', 400, 'Bad Request', {},
                          io.BytesIO(json.dumps({'ok': False, 'description': 'PHOTO_INVALID'}).encode()))
        with patch.object(n, 'urlopen', side_effect=error):
            with self.assertRaises(n.TelegramApiError) as rejected:
                n.telegram_multipart_call('111:secret', 'sendPhoto', {'chat_id': '123'}, file_field='photo', filename='test.png', content=PNG)
        self.assertEqual(rejected.exception.code, 400)
        self.assertNotIn('secret', str(rejected.exception))


if __name__ == '__main__':
    unittest.main(verbosity=2)
