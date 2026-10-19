"""Offline screenshot regressions: no desktop modifications or Telegram uploads."""
import ctypes
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import zlib
import screen_capture as s
import telegram_bot as b


class ScreenTests(unittest.TestCase):
    def setUp(self):
        self.bot = b.TelegramCodexBot('111:secret', {'chat_id': '123'})
        self.bot.send = Mock(return_value={'message_id': 9})
        self.bot.edit_message = Mock()
        self.bot.app = Mock()
        reaction = patch.object(b.telegram_notify_module, 'set_task_reaction', return_value=True)
        self.react = reaction.start()
        self.addCleanup(reaction.stop)

    def test_negative_monitor_coordinates_and_vertical_layout(self):
        displays = [{'rect': [-1920, -1080, 0, 0]}, {'rect': [0, 0, 2560, 1440]}]
        self.assertEqual(s.desktop_rect(displays), [-1920, -1080, 2560, 1440])

    def test_png_preserves_colors_dimensions_and_row_order(self):
        png = s.encode_png(1, 2, bytes([0, 0, 255, 255, 255, 0, 0, 255]))
        self.assertEqual(struct.unpack('!II', png[16:24]), (1, 2))
        at, chunks = 8, []
        while at < len(png):
            size = struct.unpack('!I', png[at:at+4])[0]
            if png[at+4:at+8] == b'IDAT': chunks.append(png[at+8:at+8+size])
            at += size+12
        self.assertEqual(zlib.decompress(b''.join(chunks)), b'\0\xff\0\0\0\0\0\xff')

    def test_invalid_or_huge_image_rejected(self):
        for dimensions in [(0, 1), (100000, 100000), (2, 2)]:
            with self.assertRaises(ValueError): s.encode_png(*dimensions, b'')

    def test_store_build_recognized_without_matching_normal_chatgpt(self):
        self.assertTrue(s.is_codex_executable('D:/Apps/OpenAI.Codex_1.0/app/ChatGPT.exe'))
        self.assertTrue(s.is_codex_executable('D:/Apps/Codex.exe'))
        self.assertFalse(s.is_codex_executable('D:/Apps/ChatGPT.exe'))
        self.assertFalse(s.is_codex_executable('D:/Apps/Codex-helper.exe'))

    def test_minimized_window_restored_after_child_timeout(self):
        u = Mock()
        u.SetThreadDpiAwarenessContext.return_value = 0
        u.IsIconic.return_value = True
        windows = [{'hwnd': 1, 'pid': 2}]
        with patch.object(s, 'api', return_value=(u, Mock(), Mock())), patch.object(s, 'codex_windows', return_value=windows), patch.object(s.time, 'sleep'), patch.object(s.subprocess, 'run', side_effect=subprocess.TimeoutExpired('capture', 18)):
            with self.assertRaisesRegex(RuntimeError, '超时'):
                s.capture({'kind': 'codex', 'hwnd': 1, 'pid': 2})
        u.ShowWindow.assert_called_once_with(1, 4)
        u.SetWindowPlacement.assert_called_once()

    def test_reused_hwnd_rejected_by_pid(self):
        u = Mock()
        u.SetThreadDpiAwarenessContext.return_value = 0
        with patch.object(s, 'api', return_value=(u, Mock(), Mock())), patch.object(s, 'codex_windows', return_value=[{'hwnd': 1, 'pid': 99}]), patch.object(s.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, '窗口已关闭'):
                s.capture({'kind': 'codex', 'hwnd': 1, 'pid': 2})
            run.assert_not_called()

    def plugin_capture(self):
        import importlib.util
        from plugin_api import Context
        spec = importlib.util.spec_from_file_location('screenshot_plugin', Path(__file__).parent/'bundled_plugins/screenshot/main.py')
        plugin = importlib.util.module_from_spec(spec); spec.loader.exec_module(plugin)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            value = plugin.handle({'kind':'command'}, Context(output,output,output,{}))
            for action in value['actions']:
                if action['type']=='photos':
                    for photo in action['items']:photo['content']=Path(photo['path']).read_bytes()
            return value

    def test_old_screenshot_buttons_delegate_to_plugin_without_model(self):
        self.bot.plugins.command = Mock(return_value=True)
        self.bot.command_screenshot('desktop')
        self.bot.plugins.command.assert_called_once_with('screenshot','desktop')
        self.bot.app.request.assert_not_called()

    def test_plugin_captures_monitors_first_but_returns_codex_first(self):
        windows = [{'hwnd':1,'pid':2}]
        displays = [{'id':1}, {'id':2}]
        with patch.object(s,'codex_windows',return_value=windows), patch.object(s,'monitors',return_value=displays), patch.object(s,'capture',return_value=(b'png',{'width':1920,'height':1080})) as capture:
            value=self.plugin_capture()
        self.assertEqual([x.args[0]['kind'] for x in capture.call_args_list],['monitor','monitor','codex'])
        pictures=value['actions'][0]['items']
        self.assertEqual([p['content'] for p in pictures],[b'png']*3)
        self.assertIn('Codex',pictures[0]['caption'])
        self.assertIn('显示器 1',pictures[1]['caption'])
        self.assertIn('显示器 2',pictures[2]['caption'])
        self.assertTrue(value['ok'])

    def test_partial_capture_returns_successful_screens_and_warning(self):
        with patch.object(s,'codex_windows',return_value=[{'hwnd':1}]), patch.object(s,'monitors',return_value=[{'id':1}]), patch.object(s,'capture',side_effect=[(b'png',{'width':20,'height':20}),RuntimeError('closed')]):
            value=self.plugin_capture()
        self.assertFalse(value['ok'])
        self.assertEqual(len(value['actions'][0]['items']),1)
        self.assertIn('未截取',value['actions'][1]['text'])

    def test_photo_group_uses_unmodified_input_and_reply_target(self):
        n = b.telegram_notify_module
        pictures = [{'content': b'png', 'width': 1920, 'height': 1080, 'caption': str(i)} for i in range(3)]
        with patch.object(n, 'telegram_multipart_call') as upload:
            n.send_screenshot_album('111:secret', '123', pictures, reply_to=42)
            upload.assert_called_once()
            self.assertEqual(upload.call_args.args[1], 'sendMediaGroup')
            fields = upload.call_args.args[2]
            self.assertEqual([x['media'] for x in fields['media']], ['attach://shot0','attach://shot1','attach://shot2'])
            self.assertTrue(all(x['type'] == 'photo' for x in fields['media']))
            self.assertEqual(fields['reply_parameters']['message_id'], 42)
            self.assertEqual(len(upload.call_args.kwargs['files']), 3)
            self.assertTrue(all('disable_content_type_detection' not in x for x in fields['media']))
            self.assertEqual([x['content'] for x in upload.call_args.kwargs['files']], [x['content'] for x in pictures])

    def test_one_photo_and_album_limit(self):
        n = b.telegram_notify_module
        picture = {'content': b'png', 'width': 20, 'height': 20, 'caption': 'screen'}
        with patch.object(n, 'telegram_multipart_call') as upload:
            n.send_screenshot_album('111:secret', '123', [picture]*11)
            self.assertEqual([x.args[1] for x in upload.call_args_list], ['sendMediaGroup', 'sendPhoto'])

    def test_oversized_photo_rejected_before_sending_without_file_fallback(self):
        n = b.telegram_notify_module
        picture = {'content': b'png', 'width': 10000, 'height': 1000, 'caption': 'screen'}
        with patch.object(n, 'telegram_multipart_call') as upload:
            with self.assertRaises(ValueError):
                n.send_screenshot_album('111:secret', '123', [picture]*2)
            upload.assert_not_called()

    def test_other_chat_cannot_request_screenshot(self):
        self.bot.command_screenshot = Mock()
        self.bot.handle_update({'message': {'chat': {'id': 999}, 'message_id': 3, 'text': '/screenshot desktop'}})
        self.bot.handle_callback({'message': {'chat': {'id': 999}}, 'data': 'screenshot|desktop'})
        self.bot.command_screenshot.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
