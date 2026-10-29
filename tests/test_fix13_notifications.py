"""R06/R07/R08 regressions: isolated profiles and fake Telegram/keychain only."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_telegram_bridge.telegram import notifications as n
from codex_telegram_bridge.telegram import completion_routing as routing

THREAD = '11111111-1111-4111-8111-111111111111'
CHILD = '22222222-2222-4222-8222-222222222222'
TURN = '33333333-3333-4333-8333-333333333333'


class NotificationFixTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profile = self.root/'profile'; self.profile.mkdir()
        self.data = self.root/'data'; self.data.mkdir()
        self.config = self.data/'credentials.json'
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        for field, value in {'HOOK_DIR':self.data, 'CONFIG_PATH':self.config,
                             'REPLY_CACHE_DIR':self.data/'replies',
                             'SOURCE_STATE_ROOT':self.profile, 'SESSION_ROOT':self.profile/'sessions'}.items():
            self.stack.enter_context(patch.object(n, field, value))
        self.stack.enter_context(patch.object(n, 'log_error'))

    def setup_bot(self, chat='-100', answers=('', 'n', 'n', 'n'), bot_id=111, previous=None):
        if previous is not None:
            self.config.write_text(json.dumps(previous))
        with patch.object(n.getpass, 'getpass', return_value=str(bot_id)+':fixture-token'), \
             patch.object(n, 'telegram_call', return_value={'id':bot_id,'username':'fixture'}), \
             patch.object(n, 'choose_chat_id', return_value=chat), \
             patch.object(n, 'store_token', return_value={'token_keyring':'test-reference'}) as store, \
             patch.object(n, 'load_token', return_value='111:old-fixture-token') as load, \
             patch.object(n, 'send_message') as send, \
             patch('builtins.input', side_effect=answers), patch('builtins.print'):
            result = n.setup()
        saved = json.loads(self.config.read_text()) if self.config.exists() else None
        return result, saved, store, load, send

    def test_group_reconfigure_preserves_existing_allowlist_for_same_bot_and_chat(self):
        result, saved, _, _, _ = self.setup_bot(previous={'chat_id':'-100','bot_id':111,'allowed_user_ids':[42,43]})
        self.assertEqual(result, 0)
        self.assertEqual(saved['allowed_user_ids'], [42,43])

    def test_legacy_group_reconfigure_checks_protected_old_token_identity(self):
        result, saved, _, load, _ = self.setup_bot(previous={'chat_id':'-100','allowed_user_ids':['42']})
        self.assertEqual(result, 0); load.assert_called_once()
        self.assertEqual(saved['allowed_user_ids'], [42])
        self.assertEqual(saved['bot_id'], 111)

    def test_new_group_requires_explicit_valid_user_ids(self):
        result, saved, _, _, _ = self.setup_bot(answers=('42, 43 42', 'n', 'n', 'n'))
        self.assertEqual(result, 0)
        self.assertEqual(saved['allowed_user_ids'], [42,43])

    def test_reconfigure_can_explicitly_replace_group_allowlist(self):
        result, saved, _, _, _ = self.setup_bot(answers=('44', 'n', 'n', 'n'),
            previous={'chat_id':'-100','bot_id':111,'allowed_user_ids':[42]})
        self.assertEqual(result, 0); self.assertEqual(saved['allowed_user_ids'], [44])

    def test_changed_bot_or_chat_never_inherits_existing_allowlist(self):
        previous={'chat_id':'-100','bot_id':111,'allowed_user_ids':[42]}
        for chat, bot in [('-101',111),('-100',222)]:
            with self.subTest(chat=chat,bot=bot):
                result, saved, store, _, send = self.setup_bot(chat=chat, bot_id=bot, previous=previous)
                self.assertEqual(result, 1)
                self.assertEqual(saved, previous)
                store.assert_not_called(); send.assert_not_called()

    def test_empty_invalid_group_allowlist_leaves_old_configuration_unchanged(self):
        previous={'chat_id':'-100','bot_id':111,'allowed_user_ids':[42]}
        for answer in ('-1', '0', '1.5', 'true', 'abc'):
            with self.subTest(answer=answer):
                result, saved, store, _, send = self.setup_bot(answers=(answer,), previous=previous)
                self.assertEqual(result, 1); self.assertEqual(saved, previous)
                store.assert_not_called(); send.assert_not_called()

    def test_private_chat_does_not_prompt_for_or_inherit_group_allowlist(self):
        result, saved, _, load, _ = self.setup_bot(chat='42', answers=('n','n','n'),
            previous={'chat_id':'-100','bot_id':111,'allowed_user_ids':[42,43]})
        self.assertEqual(result, 0); self.assertNotIn('allowed_user_ids', saved)
        load.assert_not_called()

    def task(self, thread=THREAD, *, source='user', origin='Codex Desktop'):
        sessions=self.profile/'sessions'; sessions.mkdir(exist_ok=True)
        path=sessions/f'rollout-fixture-{thread}.jsonl'
        path.write_text(json.dumps({'type':'session_meta','payload':{
            'id':thread,'thread_source':source,'originator':origin,'source':'vscode'}})+'\n')
        return path

    def question(self, **extra):
        return {'hook_event_name':'PreToolUse','tool_name':'request_user_input',
                'session_id':THREAD, 'turn_id':TURN,
                'tool_input':{'questions':[{'id':'q','header':'Choose','question':'Choose A or B',
                                          'options':[{'label':'A'},{'label':'B'}]}]}, **extra}

    def deliver_question(self, payload, *, subagents=False):
        with patch.object(n, 'notifications_muted', return_value=False), \
             patch.object(n, 'load_config', return_value=({'chat_id':'42','include_subagent_notifications':subagents}, '111:fixture')), \
             patch.object(n, 'send_markdown_message') as send, \
             patch.object(n, 'reserve_completion') as reserve, \
             patch.object(n, 'route_completion') as route:
            n.send_hook_notification(payload)
        reserve.assert_not_called(); route.assert_not_called()
        return send

    def test_verified_question_is_sent_without_consuming_completion(self):
        self.task()
        send=self.deliver_question(self.question())
        send.assert_called_once()
        self.assertIn('Choose A or B', send.call_args.args[1]['text'])

    def test_internal_unknown_and_foreign_questions_are_not_sent(self):
        for source, origin in [('thread_title','Codex Desktop'),('not_a_known_purpose','Codex Desktop'),('user','VS Code')]:
            with self.subTest(source=source,origin=origin):
                self.task(source=source,origin=origin)
                self.deliver_question(self.question()).assert_not_called()

    def test_missing_and_wrong_profile_question_evidence_are_not_sent(self):
        self.deliver_question(self.question()).assert_not_called()
        self.task()
        foreign=self.root/'foreign.jsonl'; foreign.write_text('{}')
        self.deliver_question(self.question(transcript_path=str(foreign))).assert_not_called()

    def test_subagent_question_uses_child_provenance_and_opt_in(self):
        self.task(source='user'); self.task(CHILD,source='subagent')
        payload=self.question(agent_id=CHILD)
        self.deliver_question(payload).assert_not_called()
        send=self.deliver_question(payload,subagents=True); send.assert_called_once()
        self.assertIn('direct|'+CHILD,json.dumps(send.call_args.args[1]['reply_markup']))
        self.task(CHILD,source='thread_title')
        self.deliver_question(payload,subagents=True).assert_not_called()

    def test_utf16_truncated_preview_always_has_full_reply_buttons(self):
        source='😀'*1000
        self.assertIn('…', n.markdown_preview(source,n.REPLY_PREVIEW_LIMIT))
        cache=n.cache_full_reply({'last_assistant_message':source},THREAD)
        self.assertIsNotNone(cache)
        self.assertEqual(n.load_cached_reply(cache)['text'],source)
        markup=json.dumps(n.build_notification_keyboard(THREAD,cache))
        self.assertIn('reply|'+cache,markup); self.assertIn('replyfile|'+cache,markup)

    def test_cache_matches_utf16_ascii_code_and_table_preview_boundaries(self):
        for source, cached in [('x'*1600,False),('x'*1601,True),('😀'*800,False),('😀'*801,True),
                               ('```\n'+'😀'*800+'\n```',True),
                               ('| Label | Value |\n|---|---|\n'+'| 😀 | hello |\n'*100,True)]:
            with self.subTest(length=len(source),cached=cached):
                cache=n.cache_full_reply({'last_assistant_message':source},THREAD)
                self.assertEqual(cache is not None,cached)
                if cache:self.assertEqual(n.load_cached_reply(cache)['text'],source)


if __name__=='__main__':
    unittest.main()
