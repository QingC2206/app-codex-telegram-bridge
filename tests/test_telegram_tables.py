"""Offline regression tests for Telegram table presentation."""
from contextlib import closing
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import codex_telegram_bridge.telegram.notifications as n
SAMPLE = '''按目前实现 + 必要验收的综合情况，我估计：

| Subtask | 当前进度 | 距离完成的主要缺口 |
| --- | --- | --- |
| **S001 空状态统一与优化** | 100% | 已验收完成 |
| **S002 窗口标题栏与底栏统一** | 95% | 少数窗口入口受限，剩余独有交互及显示验收 |
| S003 节点信息与端口数据统一 | 80% | 剩余 8 种真实类型 |

六项等权平均约 **93%**。'''


class TableTests(unittest.TestCase):
    def test_ui_sections_survive_rich_send_edit_and_html_fallback(self):
        body = '| A | B |\n|---|---|\n| 1 | 2 |\n\n- first\n- second\n\n```python\nx = 1\ny = 2\n```'
        text = n.message_sections(['**标题**', '', '\n**字段：** 值', body])
        self.assertEqual(text, '**标题**\n\n**字段：** 值\n\n' + body)
        for edit in (False, True):
            payload = {'chat_id': '123', 'text': text, 'message_id': 42}
            with patch.object(n, 'telegram_call', return_value={}) as call:
                n._markdown_request('111:fake', payload, edit=edit)
            self.assertEqual(call.call_args.args[2]['rich_message']['markdown'], text)
            with patch.object(n, 'telegram_call', side_effect=[
                    n.TelegramApiError(400, 'rich messages are not supported'), {}]) as call:
                n._markdown_request('111:fake', payload, edit=edit)
            html = call.call_args.args[2]['text']
            self.assertIn('<b>标题</b>\n\n<b>字段：</b> 值\n\n', html)
            self.assertIn('x = 1\ny = 2', html)
            self.assertEqual(payload['text'], text)

    def test_redundant_reply_button_hidden_but_route_preserved(self):
        keyboard = {'inline_keyboard': [[{'text': '直接回复', 'callback_data': 'direct|a'}],
                                        [{'text': '详情', 'callback_data': 'task|a'}]]}
        payload = {'chat_id': '123', 'text': '完成', 'reply_markup': keyboard}
        with tempfile.TemporaryDirectory() as folder, patch.object(n, 'NATIVE_REPLY_DIR', Path(folder)):
            with patch.object(n, 'telegram_call', return_value={'message_id': 99}) as call:
                n.send_markdown_message('111:fake', payload)
            self.assertEqual(call.call_args.args[2]['reply_markup']['inline_keyboard'], [keyboard['inline_keyboard'][1]])
            self.assertEqual(n.lookup_task_message('111:fake', '123', 99), 'a')
            self.assertEqual(len(keyboard['inline_keyboard']), 2)
            with patch.object(n, 'telegram_call', side_effect=[n.TelegramApiError(404, 'Not Found'), {'message_id': 99}]) as call:
                n.edit_markdown_message('111:fake', dict(payload, message_id=99))
            self.assertTrue(all('direct|' not in str(item.args[2]) for item in call.call_args_list))
    def test_native_reply_persists_and_isolates_chat_and_bot(self):
        keyboard = {'inline_keyboard': [[{'callback_data': 'direct|thread-a'}]]}
        with tempfile.TemporaryDirectory() as folder, patch.object(n, 'NATIVE_REPLY_DIR', Path(folder)):
            n.remember_task_message('111:secret', {'chat_id': '123', 'reply_markup': keyboard}, {'message_id': 42})
            self.assertEqual(n.lookup_task_message('111:new-secret', '123', 42), 'thread-a')
            self.assertIsNone(n.lookup_task_message('222:secret', '123', 42))
            self.assertIsNone(n.lookup_task_message('111:secret', '456', 42))
            self.assertIsNone(n.lookup_task_message('111:secret', '123', 43))
            self.assertNotIn('secret', next(Path(folder).glob('*.json')).read_text())

    def test_native_reply_rejects_lists_and_quota_buttons(self):
        for actions in (['task|a'], ['task|a', 'task|b'], ['reservequota|abc'], ['direct|a', 'task|b']):
            self.assertIsNone(n.task_from_keyboard({'inline_keyboard': [[{'callback_data': a} for a in actions]]}))
        self.assertEqual(n.task_from_keyboard({'inline_keyboard': [[{'callback_data': 'direct|a'}, {'callback_data': 'task|a'}]]}), 'a')

    def test_native_reply_full_reply_pages(self):
        with patch.object(n, 'load_cached_reply', return_value={'thread_id': 'full-reply-task'}):
            self.assertEqual(n.task_from_keyboard({'inline_keyboard': [[{'callback_data': 'replypage|abc|2'}]]}), 'full-reply-task')

    def test_native_reply_dispatch_and_unknown_reply_does_not_fall_through(self):
        import codex_telegram_bridge.app as b
        bot = b.TelegramCodexBot('111:fake', {'chat_id': '123'})
        sent, routed = [], []
        bot.send = lambda text, *args: sent.append(text)
        bot.submit_direct_reply = lambda task, text: routed.append((task, text))
        bot.awaiting_continue['123'] = 'unrelated-task'
        with patch.object(b, 'lookup_task_message', return_value='chosen-task'):
            bot.handle_text('继续处理', reply_to_message_id=42)
        self.assertEqual(routed, [('chosen-task', '继续处理')])
        with patch.object(b, 'lookup_task_message', return_value=None):
            bot.handle_text('未知引用不能转发', reply_to_message_id=43)
        self.assertEqual(len(routed), 1)
        self.assertEqual(bot.awaiting_continue['123'], 'unrelated-task')
        self.assertIn('没有可确认', sent[-1])

    def test_native_send_records_only_successful_messages(self):
        payload = {'chat_id': '123', 'text': '完成', 'reply_markup': {'inline_keyboard': [[{'callback_data': 'direct|a'}]]}}
        with tempfile.TemporaryDirectory() as folder, patch.object(n, 'NATIVE_REPLY_DIR', Path(folder)):
            with patch.object(n, 'telegram_call', side_effect=TimeoutError):
                with self.assertRaises(TimeoutError):
                    n.send_markdown_message('111:fake', payload)
            self.assertEqual(list(Path(folder).iterdir()), [])
            with patch.object(n, 'telegram_call', return_value={'message_id': 52}):
                n.send_markdown_message('111:fake', payload)
            self.assertEqual(n.lookup_task_message('111:fake', '123', 52), 'a')
    def test_internal_suggestions_filtered_in_both_notification_paths(self):
        suggestion = {'title': 'Add a smoke test', 'description': 'Verify the build',
                      'prompt': 'Add a credential-safe smoke test', 'appId': 'github', 'pluginId': None}
        for items in ([], [suggestion, suggestion]):
            body = json.dumps({'suggestions': items})
            with patch.object(n, 'load_config', side_effect=AssertionError('must not send')), \
                    patch.object(n, 'forward_computer_use_notification'), patch.object(n, 'log_error'):
                self.assertEqual(n.send_hook_notification({'hook_event_name': 'Stop', 'thread_source': 'ambient_suggestions', 'last_assistant_message': body}), 0)
                self.assertEqual(n.send_desktop_notification({'type': 'agent-turn-complete', 'threadSource': 'ambient_suggestions', 'last-assistant-message': body}, '{}'), 0)

    def test_followup_suggestion_envelope_recognized_for_both_payload_styles(self):
        for text in ('先说明必要性和兼容性，再继续修改。', '', 'Line one\nLine two'):
            body = '\n  ' + json.dumps({'suggestion': text}, ensure_ascii=False) + '\n'
            for field in ('last-assistant-message', 'last_assistant_message'):
                with self.subTest(field=field, text=text):
                    self.assertTrue(n.is_internal_suggestions_completion({field: body, 'thread_source': 'composer_predictions'}))

    def test_followup_suggestion_never_sends_or_completes_task_in_either_path(self):
        body = json.dumps({'suggestion': '请先解释兼容性，不要执行下一步。'}, ensure_ascii=False)
        raw = json.dumps({'type': 'agent-turn-complete', 'last-assistant-message': body})
        for event in ('Stop', 'SubagentStop'):
            with self.subTest(event=event), \
                    patch.object(n, 'load_config', side_effect=AssertionError('internal output must not load credentials')), \
                    patch.object(n, 'notifications_muted', side_effect=AssertionError('filter must run first')), \
                    patch.object(n, 'cache_full_reply') as cache, \
                    patch.object(n, 'resolve_desktop_submissions') as resolve, \
                    patch.object(n, 'complete_payload_reactions') as complete, \
                    patch.object(n, 'send_markdown_message') as send, \
                    patch.object(n, 'forward_computer_use_notification') as forward, \
                    patch.object(n, 'log_error'):
                self.assertEqual(n.send_hook_notification({'hook_event_name': event, 'thread_source': 'composer_predictions', 'last_assistant_message': body}), 0)
                self.assertEqual(n.send_desktop_notification({'type': 'agent-turn-complete', 'threadSource': 'composer_predictions', 'last-assistant-message': body}, raw), 0)
                cache.assert_not_called()
                resolve.assert_not_called()
                send.assert_not_called()
                complete.assert_not_called()
                forward.assert_called_once_with(raw)

    def test_suggestion_examples_and_other_json_do_not_match_internal_envelope(self):
        values = ['```json\n{"suggestion":"继续检查"}\n```',
                  '接口示例：{"suggestion":"继续检查"}',
                  '{"suggestion":"继续检查","result":"ok"}',
                  '{"suggestion":null}', '{"suggestion":false}', '{"suggestion":42}',
                  '{"suggestion":[]}', '{"suggestion":{"text":"继续检查"}}',
                  '[{"suggestion":"继续检查"}]', '{"suggestion":', 'suggestion', '{}']
        for value in values:
            with self.subTest(value=value):
                self.assertFalse(n.is_internal_suggestions_completion({'last_assistant_message': value}))

    def test_regular_answer_with_suggestion_content_still_delivered_in_both_paths(self):
        for body in ('**答复：** 下面解释兼容性。',
                     '{"suggestion":"正常的 JSON 回答"}', '{"suggestions":[]}',
                     '```json\n{"suggestion":"继续检查"}\n```',
                     '{"suggestion":"继续检查","result":"ok"}'):
            for desktop in (False, True):
                with self.subTest(body=body, desktop=desktop), \
                        patch.object(n, 'load_config', return_value=({'chat_id': '123', 'include_message_preview': True}, '111:fake')), \
                        patch.object(n, 'notifications_muted', return_value=False), \
                        patch.object(n, 'subagent_completion_context', return_value=None), \
                        patch.object(n, 'cache_full_reply', return_value=None), \
                        patch.object(n, 'resolve_desktop_submissions'), \
                        patch.object(n, 'attach_task_reply'), \
                        patch.object(n, 'payload_turn_id', return_value='turn-a'), \
                        patch.object(n, 'complete_payload_reactions') as complete, \
                        patch.object(n, 'forward_computer_use_notification'), \
                        patch.object(n, 'log_error'), \
                        patch.object(n, 'send_markdown_message', return_value={'message_id': 42}) as send:
                    if desktop:
                        payload = {'type': 'agent-turn-complete', 'thread-id': 'thread-a',
                                   'last-assistant-message': body}
                        self.assertEqual(n.send_desktop_notification(payload, json.dumps(payload)), 0)
                    else:
                        payload = {'hook_event_name': 'Stop', 'thread_id': 'thread-a',
                                   'last_assistant_message': body}
                        self.assertEqual(n.send_hook_notification(payload), 0)
                    send.assert_called_once()
                    self.assertIn(body, send.call_args.args[1]['text'])
                    complete.assert_called_once_with(payload)

    def test_known_internal_source_is_independent_of_message_text(self):
        for source in n.INTERNAL_SUGGESTION_SOURCES:
            for field in ('threadSource', 'thread_source', 'thread-source', 'turnTrigger', 'turn_trigger'):
                for body in ('plain internal output', '{"suggestion":null}', '{"suggestion":""}'):
                    with self.subTest(source=source, field=field, body=body):
                        self.assertTrue(n.is_internal_suggestions_completion({field: source, 'last_assistant_message': body}))

    def test_exact_suggestion_json_without_purpose_is_preserved(self):
        for body in ('{"suggestion":"正常回复"}', '{"suggestion":null}', '{"suggestions":[]}'):
            with self.subTest(body=body), patch.object(n, 'log_error') as log:
                self.assertFalse(n.is_internal_suggestions_completion({'last_assistant_message': body}))
                log.assert_called_once()
                self.assertNotIn('正常回复', log.call_args.args[0])
                self.assertNotIn('last_assistant_message', log.call_args.args[0])

    def test_generic_origins_and_ephemeral_status_do_not_prove_internal_purpose(self):
        for extra in ({'sourceKind': 'appServer'}, {'source': 'vscode'},
                      {'originator': 'Codex Desktop'}, {'ephemeral': True},
                      {'input-messages': ['threadSource: composer_predictions']},
                      {'sourceKind': 'composer_predictions'},
                      {'threadSource': 'user'}, {'threadSource': 'ambient_suggestion_task'},
                      {'threadSource': 'automation'}, {'threadSource': 'agent_forked_thread'}):
            with self.subTest(extra=extra), patch.object(n, 'log_error'):
                self.assertFalse(n.is_internal_suggestions_completion(dict(extra, last_assistant_message='{"suggestion":"正常 JSON"}')))

    def test_conflicting_or_invalid_explicit_source_preserves_reply(self):
        for extra in ({'threadSource': 'user', 'thread_source': 'composer_predictions'},
                      {'threadSource': 'composer_predictions', 'turnTrigger': 'user'},
                      {'threadSource': 'composer_predictions', 'thread': {'threadSource': 'user'}},
                      {'threadSource': 'composer_predictions', 'turnTrigger': []},
                      {'threadSource': 'composer_predictions', 'thread-id': 'thread-a', 'thread_id': 'thread-b'}):
            with self.subTest(extra=extra), patch.object(n, 'log_error'):
                self.assertFalse(n.is_internal_suggestions_completion(dict(extra, last_assistant_message='{"suggestion":"保留"}')))

    def test_recorded_thread_source_is_read_only_and_profile_scoped(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            other = root / 'other-profile'
            other.mkdir()
            for path, rows in ((root / 'state_5.sqlite', [('thread-user', 'user'), ('thread-internal', 'composer_predictions')]),
                               (other / 'state_5.sqlite', [('thread-other', 'composer_predictions')])):
                with closing(sqlite3.connect(path)) as db:
                    db.execute('CREATE TABLE threads(id TEXT,thread_source TEXT)')
                    db.executemany('INSERT INTO threads VALUES(?,?)', rows)
                    db.commit()
            before = (root / 'state_5.sqlite').read_bytes()
            with patch.object(n, 'SOURCE_STATE_ROOT', root), patch.object(n, 'log_error'):
                for thread, expected in (('thread-user', False), ('thread-internal', True), ('thread-other', False), ('missing-thread', False)):
                    with self.subTest(thread=thread):
                        self.assertEqual(n.is_internal_suggestions_completion({'thread-id': thread,
                            'last-assistant-message': '{"suggestion":"normal or internal"}'}), expected)
                self.assertFalse(n.is_internal_suggestions_completion({'thread-id': 'thread-user',
                    'threadSource': 'composer_predictions', 'last-assistant-message': '{"suggestion":"keep"}'}))
            self.assertEqual((root / 'state_5.sqlite').read_bytes(), before)

    def test_missing_or_unsupported_state_schema_preserves_json(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with closing(sqlite3.connect(root / 'state_5.sqlite')) as db:
                db.execute('CREATE TABLE threads(id TEXT,source TEXT)')
                db.execute('INSERT INTO threads VALUES(?,?)', ('thread-a', 'vscode'))
                db.commit()
            (root / 'state_6.sqlite').write_bytes(b'invalid database')
            with patch.object(n, 'SOURCE_STATE_ROOT', root), patch.object(n, 'log_error'):
                self.assertFalse(n.is_internal_suggestions_completion({'thread-id': 'thread-a',
                    'last-assistant-message': '{"suggestion":"must keep"}'}))
                self.assertIsNone(n._stored_thread_source('../outside'))

    def test_newer_record_without_purpose_does_not_inherit_stale_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for version, source in ((5, 'composer_predictions'), (6, None)):
                with closing(sqlite3.connect(root / f'state_{version}.sqlite')) as db:
                    db.execute('CREATE TABLE threads(id TEXT,thread_source TEXT)')
                    db.execute('INSERT INTO threads VALUES(?,?)', ('thread-a', source))
                    db.commit()
            with patch.object(n, 'SOURCE_STATE_ROOT', root), patch.object(n, 'log_error'):
                self.assertFalse(n.is_internal_suggestions_completion({'thread-id': 'thread-a',
                    'last-assistant-message': '{"suggestion":"preserve"}'}))

    def test_nested_thread_source_can_prove_internal_purpose(self):
        with patch.object(n, '_stored_thread_purpose', return_value=('missing', None)):
            self.assertTrue(n.is_internal_suggestions_completion({'thread': {
                'id': 'thread-internal', 'threadSource': 'composer_predictions'},
                'last_assistant_message': 'plain output'}))

    def test_regular_json_and_suggestion_examples_retained(self):
        values = ['任务完成', '{"result": "ok"}', '{"suggestions": ["test"]}',
                  '{"suggestions": [{"title": "test"}]}', '{"suggestions": [], "result": "ok"}',
                  '```json\n{"suggestions": []}\n```', 'Here are suggestions: {"suggestions": []}',
                  '{"suggestions": null}']
        for value in values:
            self.assertFalse(n.is_internal_suggestions_completion({'last_assistant_message': value}), value)

    def test_screenshot_shape_and_formatting(self):
        result = n.markdown_to_telegram_html(SAMPLE)
        self.assertNotIn('|', result)
        self.assertNotIn('---', result)
        self.assertEqual(result.count('<b>当前进度：</b>'), 3)
        self.assertIn('<b>S002 窗口标题栏与底栏统一</b>', result)
        self.assertIn('95%', result)
        self.assertIn('剩余 8 种真实类型', result)
        self.assertIn('平均约 <b>93%</b>', result)

    def test_optional_outer_pipes_and_alignment(self):
        result = n.format_markdown_tables('项目 | 状态\n:--- | ---:\nA | 完成')
        self.assertEqual(result, '**项目：** A\n**状态：** 完成\n')

    def test_code_and_escaped_pipes(self):
        value = '| 示例 | 意义 |\n| --- | --- |\n| `a|b` | A \\| B |'
        result = n.markdown_to_telegram_html(value)
        self.assertIn('<code>a|b</code>', result)
        self.assertIn('A | B', result)
        self.assertEqual(result.count('<b>意义：</b>'), 1)

    def test_multiple_backticks(self):
        self.assertEqual(n._table_cells('| ``a`|b`` | ok |'), ['``a`|b``', 'ok'])

    def test_fenced_and_indented_code_not_transformed(self):
        table = '| A | B |\n| --- | --- |\n| x | y |'
        for fence in ('```', '````', '~~~'):
            code = fence+'\n'+table+'\n'+fence
            self.assertEqual(n.format_markdown_tables(code), code)
        code = '\n'.join('    '+line for line in table.splitlines())
        self.assertEqual(n.format_markdown_tables(code), code)

    def test_no_false_positive_without_valid_separator(self):
        for text in ('A | B\nhello | world', '| A | B |\n| -- | nope |\n| X | Y |',
                     '| A | B |\n| --- | --- |'):
            self.assertEqual(n.format_markdown_tables(text), text)

    def test_malformed_row_preserved(self):
        value = '| A | B |\n| --- | --- |\n| x | y |\n| too | many | columns |'
        self.assertIn('| too | many | columns |', n.format_markdown_tables(value))

    def test_empty_cells_breaks_and_html_safe(self):
        value = '| A | B |\n| --- | --- |\n| | <script>bad</script><br>next |'
        result = n.markdown_to_telegram_html(value)
        self.assertIn('—', result)
        self.assertIn('&lt;script&gt;bad&lt;/script&gt;\nnext', result)
        self.assertNotIn('<script>', result)

    def test_transform_idempotent(self):
        once = n.format_markdown_tables(SAMPLE)
        self.assertEqual(n.format_markdown_tables(once), once)

    def test_long_table_pagination_preserves_all_fields(self):
        source = '| ID | Details |\n| --- | --- |\n' + '\n'.join(f'| item-{i:03d} | result-{i:03d} |' for i in range(100))
        pages = n.split_markdown_pages(source, limit=600)
        self.assertGreater(len(pages), 3)
        self.assertTrue(all(len(page) <= 600 and page.startswith('| ID | Details |\n| --- | --- |') for page in pages))
        combined = '\n'.join(pages)
        for i in range(100):
            self.assertEqual(combined.count(f'item-{i:03d}'), 1)
            self.assertEqual(combined.count(f'result-{i:03d}'), 1)

    def test_display_expansion_creates_cache_but_keeps_original(self):
        source = '| Long header for first field | Long header for second field |\n| --- | --- |\n' + '\n'.join('| x | y |' for _ in range(35))
        self.assertLess(len(source), n.REPLY_PREVIEW_LIMIT)
        self.assertGreater(len(n.format_markdown_tables(source)), n.REPLY_PREVIEW_LIMIT)
        with tempfile.TemporaryDirectory() as folder, patch.object(n, 'REPLY_CACHE_DIR', Path(folder)):
            cache_id = n.cache_full_reply({'last_assistant_message': source})
            self.assertIsNotNone(cache_id)
            record = n.load_cached_reply(cache_id)
            self.assertEqual(record['text'], source)
        preview = n.build_message({'last_assistant_message': source}, True)
        self.assertIn(source, preview)
        self.assertLessEqual(len(preview), n.MAX_TELEGRAM_MESSAGE)

    def test_plain_fallback_uses_readable_cards(self):
        sent = []
        def fake_send(token, method, payload, **kwargs):
            sent.append(payload)
            if len(sent) == 1:
                raise n.TelegramApiError(404, 'Not Found')
            if len(sent) == 2:
                raise n.TelegramApiError(400, "can't parse entities")
            return {'ok': True}
        with patch.object(n, 'telegram_call', side_effect=fake_send):
            n.send_markdown_message('fake', {'text': SAMPLE, 'chat_id': 'test'})
        self.assertEqual(len(sent), 3)
        self.assertNotIn('|', sent[-1]['text'])
        self.assertNotIn('parse_mode', sent[-1])

    def test_native_send_preserves_markdown_and_buttons(self):
        keyboard = {'inline_keyboard': [[{'text': '详情', 'callback_data': 'task|example'}]]}
        payload = {'chat_id': 'test', 'text': SAMPLE, 'reply_markup': keyboard, 'disable_notification': True}
        with patch.object(n, 'telegram_call', return_value={'message_id': 42}) as call:
            self.assertEqual(n.send_markdown_message('fake', payload), {'message_id': 42})
        args = call.call_args.args
        self.assertEqual(args[1], 'sendRichMessage')
        self.assertEqual(args[2]['rich_message'], {'markdown': SAMPLE})
        self.assertEqual(args[2]['reply_markup'], keyboard)
        self.assertTrue(args[2]['disable_notification'])
        self.assertNotIn('text', args[2])
        self.assertEqual(payload['text'], SAMPLE)

    def test_native_edit_and_unchanged(self):
        payload = {'chat_id': 'test', 'message_id': 42, 'text': SAMPLE, 'reply_markup': {'inline_keyboard': []}}
        with patch.object(n, 'telegram_call', return_value=True) as call:
            n.edit_markdown_message('fake', payload)
        self.assertEqual(call.call_args.args[1], 'editMessageText')
        self.assertEqual(call.call_args.args[2]['rich_message']['markdown'], SAMPLE)
        self.assertEqual(call.call_args.args[2]['message_id'], 42)
        with patch.object(n, 'telegram_call', side_effect=n.TelegramApiError(400, 'message is not modified')) as call:
            self.assertIsNone(n.edit_markdown_message('fake', payload))
            self.assertEqual(call.call_count, 1)

    def test_invalid_rich_media_falls_back_with_reply_route(self):
        keyboard = n.build_notification_keyboard('thread-a')
        text = '**任务已完成**\n\n![截图](D:/workspace/preview.png)\n\n' + SAMPLE
        original = {'chat_id': '123', 'text': text, 'reply_markup': keyboard,
                    'reply_parameters': {'message_id': 7, 'allow_sending_without_reply': True},
                    'disable_notification': True, 'protect_content': True}
        for edit in (False, True):
            for description in ('Bad Request: RICH_MESSAGE_PHOTO_URL_INVALID',
                                'RICH_MESSAGE_VIDEO_URL_INVALID', 'RICH_MESSAGE_INVALID'):
                with self.subTest(edit=edit, description=description), tempfile.TemporaryDirectory() as folder, \
                        patch.object(n, 'NATIVE_REPLY_DIR', Path(folder)), patch.object(n, 'log_error'), \
                        patch.object(n, 'telegram_call', side_effect=[
                            n.TelegramApiError(400, description), {'message_id': 42}]) as call:
                    payload = dict(original, message_id=42) if edit else dict(original)
                    fn = n.edit_markdown_message if edit else n.send_markdown_message
                    self.assertEqual(fn('111:fake', payload), {'message_id': 42})
                    self.assertEqual(call.call_count, 2)
                    self.assertEqual([c.args[1] for c in call.call_args_list],
                                     ['editMessageText', 'editMessageText'] if edit else ['sendRichMessage', 'sendMessage'])
                    fallback = call.call_args.args[2]
                    self.assertEqual(fallback['parse_mode'], 'HTML')
                    self.assertNotIn('rich_message', fallback)
                    self.assertIn('D:/workspace/preview.png', fallback['text'])
                    self.assertIn('<b>任务已完成</b>', fallback['text'])
                    self.assertEqual(fallback['reply_parameters'], original['reply_parameters'])
                    self.assertTrue(fallback['disable_notification'])
                    self.assertTrue(fallback['protect_content'])
                    self.assertEqual(fallback['reply_markup']['inline_keyboard'][0], keyboard['inline_keyboard'][0][:1])
                    self.assertEqual(fallback['reply_markup']['inline_keyboard'][-1][0]['copy_text']['text'], 'D:/workspace/preview.png')
                    self.assertEqual(n.lookup_task_message('111:fake', '123', 42), 'thread-a')
                    self.assertEqual(payload['text'], text)
                    self.assertEqual(payload['reply_markup'], keyboard)
        self.assertFalse(n._format_rejected(n.TelegramApiError(400, 'RICH_MESSAGE_PHOTO_URL_INVALID')))

    def test_invalid_rich_media_and_html_rejection_reach_plain_text(self):
        payload = {'chat_id': '123', 'text': '**完成**\n\n![截图](D:/preview.png)',
                   'reply_parameters': {'message_id': 7}, 'reply_markup': n.build_notification_keyboard('thread-a')}
        with tempfile.TemporaryDirectory() as folder, patch.object(n, 'NATIVE_REPLY_DIR', Path(folder)), \
                patch.object(n, 'log_error'), patch.object(n, 'telegram_call', side_effect=[
                    n.TelegramApiError(400, 'RICH_MESSAGE_PHOTO_URL_INVALID'),
                    n.TelegramApiError(400, "can't parse entities"), {'message_id': 42}]) as call:
            n.send_markdown_message('111:fake', payload)
            self.assertEqual(call.call_count, 3)
            plain = call.call_args.args[2]
            self.assertNotIn('rich_message', plain)
            self.assertNotIn('parse_mode', plain)
            self.assertIn('**完成**', plain['text'])
            self.assertIn('D:/preview.png', plain['text'])
            self.assertNotIn('![', plain['text'])
            self.assertEqual(plain['reply_parameters'], payload['reply_parameters'])
            self.assertEqual(n.lookup_task_message('111:fake', '123', 42), 'thread-a')

    def test_fallback_transport_failure_is_not_retried_or_bound(self):
        payload = {'chat_id': '123', 'text': '![截图](D:/preview.png)',
                   'reply_markup': n.build_notification_keyboard('thread-a')}
        with tempfile.TemporaryDirectory() as folder, patch.object(n, 'NATIVE_REPLY_DIR', Path(folder)), \
                patch.object(n, 'log_error'), patch.object(n, 'telegram_call', side_effect=[
                    n.TelegramApiError(400, 'RICH_MESSAGE_PHOTO_URL_INVALID'), TimeoutError('timeout')]) as call:
            with self.assertRaises(TimeoutError):
                n.send_markdown_message('111:fake', payload)
            self.assertEqual(call.call_count, 2)
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_both_completion_paths_mark_ok_only_after_fallback_delivery(self):
        body = '**修复完成**\n\n![截图](D:/workspace/preview.png)'
        for desktop in (False, True):
            for success in (False, True):
                payload = {'type': 'agent-turn-complete', 'hook_event_name': 'Stop',
                           'thread-id': 'thread-a', 'thread_id': 'thread-a', 'turn_id': 'turn-a',
                           'last-assistant-message': body, 'last_assistant_message': body}
                events = []
                def api(token, method, request, **kwargs):
                    if 'rich_message' in request:
                        events.append('rejected')
                        raise n.TelegramApiError(400, 'Bad Request: RICH_MESSAGE_PHOTO_URL_INVALID')
                    events.append('delivery' if success else 'uncertain')
                    if not success:
                        raise TimeoutError('timeout')
                    return {'message_id': 42}
                with self.subTest(desktop=desktop, success=success), tempfile.TemporaryDirectory() as folder, \
                        patch.object(n, 'NATIVE_REPLY_DIR', Path(folder)), patch.object(n, 'log_error'), \
                        patch.object(n, 'load_config', return_value=({'chat_id': '123', 'include_message_preview': True}, '111:fake')), \
                        patch.object(n, 'notifications_muted', return_value=False), \
                        patch.object(n, 'subagent_completion_context', return_value=None), \
                        patch.object(n, 'forward_computer_use_notification'), \
                        patch.object(n, 'conversation_title', return_value='任务'), \
                        patch.object(n, 'cache_full_reply', return_value=None), \
                        patch.object(n, 'resolve_desktop_submissions'), patch.object(n, 'attach_task_reply'), \
                        patch.object(n, 'telegram_call', side_effect=api) as call, \
                        patch.object(n, 'complete_payload_reactions', side_effect=lambda _: events.append('ok')) as done:
                    if desktop:
                        n.send_desktop_notification(payload, '{}')
                    else:
                        n.send_hook_notification(payload)
                    self.assertEqual(call.call_count, 2)
                    if success:
                        self.assertEqual(events, ['rejected', 'delivery', 'ok'])
                        done.assert_called_once_with(payload)
                        self.assertEqual(n.lookup_task_message('111:fake', '123', 42), 'thread-a')
                    else:
                        self.assertEqual(events, ['rejected', 'uncertain'])
                        done.assert_not_called()
                        self.assertEqual(list(Path(folder).iterdir()), [])

    def test_no_downgrade_or_resend_on_ambiguous_or_unrelated_errors(self):
        for error in [TimeoutError('timeout'), RuntimeError('proxy 404 Not Found'),
                      n.TelegramApiError(429, 'Too Many Requests'), n.TelegramApiError(401, 'Unauthorized'),
                      n.TelegramApiError(403, 'Forbidden'), n.TelegramApiError(500, 'Internal Server Error'),
                      n.TelegramApiError(400, 'chat not found'), n.TelegramApiError(400, 'CHAT_ID_INVALID'),
                      n.TelegramApiError(500, 'RICH_MESSAGE_PHOTO_URL_INVALID'),
                      n.TelegramApiError(429, 'RICH_MESSAGE_PHOTO_URL_INVALID'),
                      n.TelegramApiError(403, 'RICH_MESSAGE_PHOTO_URL_INVALID')]:
            for fn in (n.send_markdown_message, n.edit_markdown_message):
                with self.subTest(error=error, fn=fn.__name__), patch.object(n, 'telegram_call', side_effect=error) as call:
                    with self.assertRaises(type(error)):
                        fn('fake', {'chat_id': 'test', 'text': SAMPLE})
                    self.assertEqual(call.call_count, 1)

    def test_unsupported_edit_fallback_preserves_id_and_keyboard(self):
        payload = {'chat_id': 'test', 'message_id': 42, 'text': SAMPLE, 'reply_markup': {'inline_keyboard': []}}
        with patch.object(n, 'telegram_call', side_effect=[n.TelegramApiError(400, 'rich_message is not supported'), True]) as call:
            n.edit_markdown_message('fake', payload)
        self.assertEqual(call.call_count, 2)
        sent = call.call_args.args[2]
        self.assertEqual(sent['message_id'], 42)
        self.assertEqual(sent['reply_markup'], payload['reply_markup'])
        self.assertNotIn('rich_message', sent)
        self.assertNotIn('|', sent['text'])

    def test_preview_does_not_cut_a_table_row(self):
        table = '| ID | Details |\n| --- | --- |\n' + '\n'.join(f'| item-{i:03d} | result-{i:03d} |' for i in range(100))
        preview = n.markdown_preview(table, 600)
        self.assertLessEqual(len(preview), 600)
        self.assertIn('| ID | Details |\n| --- | --- |', preview)
        self.assertTrue(all(line.endswith('|') for line in preview.splitlines() if line.startswith('|')))

    def test_fenced_table_not_reinterpreted(self):
        source = '```markdown\n' + SAMPLE + '\n```'
        self.assertEqual(n.split_markdown_pages(source), [source])

    def test_enormous_row_preserves_content(self):
        source = '| ID | Details |\n| --- | --- |\n| long | ' + 'x' * 1600 + ' |'
        pages = n.split_markdown_pages(source, 600)
        self.assertTrue(all(len(page) <= 600 for page in pages))
        self.assertEqual(''.join(pages).count('x'), 1600)


class PredictionRequestTests(unittest.TestCase):
    """Regression fixture based on a captured Desktop generation input."""

    def setUp(self):
        self.schema = {'$schema': 'https://json-schema.org/draft/2020-12/schema',
            'type': 'object', 'properties': {'suggestion': {'anyOf': [{'type': 'string'}, {'type': 'null'}]}},
            'required': ['suggestion'], 'additionalProperties': False}
        self.request = ('This is an app-generated request for a suggested next user message. '
            'The user did not write this message.\n\n'
            "Your suggestion will appear in the user's empty message composer. "
            'They can accept it, edit it, or ignore it; it will not be sent automatically.\n\n'
            'Predict the single next user message.\n\n'
            'Return only JSON matching this schema: ' + json.dumps(self.schema) + '.')
        self.payload = {'type': 'agent-turn-complete', 'client': 'Codex Desktop',
            'thread-id': 'prediction-thread', 'input-messages': ['earlier human input', self.request],
            'last-assistant-message': '{"suggestion":"a suggested next question"}'}
        state = patch.object(n, '_stored_thread_purpose', return_value=('missing', None))
        self.state = state.start()
        self.addCleanup(state.stop)
        log = patch.object(n, 'log_error')
        self.log = log.start()
        self.addCleanup(log.stop)

    def test_generation_purpose_does_not_depend_on_reply_json(self):
        for body in ('{"suggestion":"question"}', '{"suggestion":null}', 'plain internal result', ''):
            with self.subTest(body=body):
                payload = dict(self.payload, **{'last-assistant-message': body})
                self.assertTrue(n.is_internal_suggestions_completion(payload))
                self.assertEqual(n.completion_thread_source(payload), 'composer_predictions')

    def test_generation_request_filtered_before_credentials_send_and_completion(self):
        with patch.object(n, 'load_config', side_effect=AssertionError('must not load credentials')), \
                patch.object(n, 'send_markdown_message') as send, \
                patch.object(n, 'cache_full_reply') as cache, \
                patch.object(n, 'complete_payload_reactions') as complete, \
                patch.object(n, 'forward_computer_use_notification') as forward:
            raw = json.dumps(self.payload)
            self.assertEqual(n.send_desktop_notification(self.payload, raw), 0)
            send.assert_not_called()
            cache.assert_not_called()
            complete.assert_not_called()
            forward.assert_called_once_with(raw)
            self.assertIn('evidence=app-generated-prediction-request', self.log.call_args.args[0])

    def test_history_generation_marker_never_labels_later_user_input(self):
        payload = dict(self.payload, **{'input-messages': [self.request, '请返回 suggestion 字段的 JSON。']})
        self.assertFalse(n.is_internal_suggestions_completion(payload))

    def test_existing_user_or_unknown_stored_thread_overrides_copied_request(self):
        for source in ('user', None):
            with self.subTest(source=source):
                self.state.return_value = ('stored', source)
                self.assertFalse(n.is_internal_suggestions_completion(self.payload))

    def test_unreadable_profile_or_explicit_conflict_does_not_use_input_fallback(self):
        self.state.return_value = ('unavailable', None)
        self.assertFalse(n.is_internal_suggestions_completion(self.payload))
        self.state.return_value = ('missing', None)
        for extra in ({'threadSource': 'user'}, {'threadSource': 'other'},
                      {'threadSource': 'user', 'thread_source': 'composer_predictions'},
                      {'thread_id': 'another-thread'}, {'threadSource': []}):
            with self.subTest(extra=extra):
                self.assertFalse(n.is_internal_suggestions_completion(dict(self.payload, **extra)))

    def test_quoted_partial_or_body_only_marker_is_preserved(self):
        for request in ('```text\n' + self.request + '\n```', '示例：' + self.request,
                        'composer_predictions', self.request.split('\n\n', 1)[0],
                        self.request.replace('The user did not write this message.', 'The user wrote this message.')):
            with self.subTest(request=request[:70]):
                self.assertFalse(n.is_internal_suggestions_completion(dict(self.payload, **{'input-messages': [request]})))
        payload = dict(self.payload, **{'input-messages': ['输出 JSON 即可'], 'last-assistant-message': self.request})
        self.assertFalse(n.is_internal_suggestions_completion(payload))

    def test_missing_different_or_extended_schema_does_not_prove_generation(self):
        for schema in ({'type': 'object', 'properties': {'suggestion': {'type': 'string'}}},
                       dict(self.schema, additionalProperties=True),
                       dict(self.schema, required=[]),
                       dict(self.schema, properties={'result': {'type': 'string'}})):
            request = self.request.partition('Return only JSON matching this schema: ')[0]
            request += 'Return only JSON matching this schema: ' + json.dumps(schema) + '.'
            with self.subTest(schema=schema):
                self.assertFalse(n.is_internal_suggestions_completion(dict(self.payload, **{'input-messages': [request]})))
        self.assertFalse(n.is_internal_suggestions_completion(dict(self.payload, **{'input-messages': [self.request + '\nAn explanation follows.']})))

    def test_desktop_client_and_input_array_are_required(self):
        for extra in ({'client': 'other'}, {'client': {'name': 'Codex Desktop'}},
                      {'type': 'other'}, {'input-messages': []}, {'input-messages': self.request},
                      {'input-messages': [{'text': self.request}]}, {'input-messages': [None]}):
            with self.subTest(extra=extra):
                self.assertFalse(n.is_internal_suggestions_completion(dict(self.payload, **extra)))

    def test_whitespace_and_crlf_do_not_hide_observed_request(self):
        payload = dict(self.payload, **{'input-messages': ['\n ' + self.request.replace('\n', '\r\n') + '\r\n ']})
        self.assertTrue(n.is_internal_suggestions_completion(payload))


if __name__ == '__main__':
    unittest.main(verbosity=2)
