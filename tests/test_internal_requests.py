"""Ephemeral Desktop request fingerprints; no network, real prompts or sends."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_telegram_bridge.telegram import internal_requests as r
from codex_telegram_bridge.telegram import notifications as n
from notification_fixtures import verified_completion


def ambient(projectless=False):
    location = 'this Projectless task' if projectless else 'this local project: /fixture'
    recent = ('Recent Projectless Codex tasks with the same cwd:' if projectless
              else 'Recent Codex tasks in this project:')
    ending = ('- write the prompt as something that should launch as a new Projectless Codex task'
              if projectless else '- write the prompt as something that should launch as a new Codex task in this project')
    return (r.AMBIENT_PREFIX + location + '\n\nFixture context.\n\n# Rules\n\nFixture rules.\n\n'
            + recent + '\n[]\n\nAvoid repeating these previously dismissed suggestions:\n[]\n\n'
            '# Response format\n\nEach suggestion must include:\n'
            '- title: fixture\n- description: fixture\n- prompt: fixture\n- appId: fixture\n- pluginId: null.\n' + ending)


POLICY = r.SAFETY_PREFIX + '\n\nFixture policy. Do not store real user data.'
FOOTER = '```\n\n# Output Format\n\nFixture output instructions.'


def safety(count=1):
    candidates = []
    for index in range(count):
        for name, value in [('- suggestion_id: ', f'suggestion-{index + 1}'), ('  title: ', 'Title'),
                            ('  description: ', 'Description'), ('  prompt: ', 'Hello\n<text>'), ('  app_id: ', None)]:
            candidates.append(name + json.dumps(value))
    return POLICY + r.SAFETY_CANDIDATES + '```\n' + '\n'.join(candidates) + '\n' + FOOTER


class InternalRequestTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        for context in (patch.object(n, '_stored_thread_purpose', return_value=('missing', None)),
                        patch.object(n, 'HOOK_DIR', self.root), patch.object(n, 'log_error'),
                        patch.object(r, 'SAFETY_POLICY_SHA256', hashlib.sha256(POLICY.encode()).hexdigest()),
                        patch.object(r, 'SAFETY_FOOTER_SHA256', hashlib.sha256(FOOTER.encode()).hexdigest())):
            mock = context.start(); self.addCleanup(context.stop)
            if context.attribute == '_stored_thread_purpose':
                self.stored = mock

    def payload(self, text, reply='{"exclude":[]}'):
        return {'type': 'agent-turn-complete', 'client': 'Codex Desktop',
                'thread-id': 'ephemeral-fixture', 'turn-id': 'turn-fixture',
                'input-messages': [text], 'last-assistant-message': reply}

    def test_supported_requests_are_classified_independently_of_output(self):
        for request, purpose in [(ambient(), 'ambient_suggestions'), (ambient(True), 'ambient_suggestions'),
                                 (safety(), 'ambient_suggestion_safety'), (safety(3), 'ambient_suggestion_safety')]:
            for reply in ('{"suggestions":[{"title":"Next"}]}', '{"exclude":[]}',
                          '{"exclude":[{"id":"suggestion-1","reason":"fixture"}]}', 'plain result', ''):
                with self.subTest(purpose=purpose, reply=reply):
                    self.assertEqual(n.completion_thread_source(self.payload(request, reply)), purpose)
                    self.assertTrue(n.is_internal_suggestions_completion(self.payload(request, reply)))

    def test_matched_notification_stops_before_credentials_cache_reactions_or_send(self):
        for request in (ambient(), safety()):
            with self.subTest(request=request[:20]), \
                    patch.object(n, 'load_config', side_effect=AssertionError('must not load credentials')), \
                    patch.object(n, 'cache_full_reply') as cache, \
                    patch.object(n, 'resolve_desktop_submissions') as receipts, \
                    patch.object(n, 'complete_payload_reactions') as complete, \
                    patch.object(n, 'send_markdown_message') as send, \
                    patch.object(n, 'forward_computer_use_notification'):
                payload = self.payload(request)
                self.assertEqual(n.send_desktop_notification(payload, json.dumps(payload)), 0)
                for call in (cache, receipts, complete, send): call.assert_not_called()
                self.assertIn('reason=unverified-local-task', n.log_error.call_args.args[0])

    def test_real_user_or_stored_unknown_thread_keeps_even_identical_request(self):
        for state in [('stored', 'user'), ('stored', None), ('unavailable', None)]:
            self.stored.return_value = state
            for request in (ambient(), safety()):
                self.assertFalse(n.is_internal_suggestions_completion(self.payload(request)))

    def test_conflicting_explicit_source_or_thread_id_never_uses_template_fallback(self):
        for extra in ({'threadSource': 'user'}, {'turn_trigger': 'composer'},
                      {'thread_source': []}, {'thread_id': 'another-thread'},
                      {'thread': {'id': 'another-thread'}}, {'threadSource': 'user', 'turnTrigger': 'ambient_suggestions'}):
            for request in (ambient(), safety()):
                self.assertFalse(n.is_internal_suggestions_completion(dict(self.payload(request), **extra)))

    def test_user_copies_quotes_or_discusses_request_without_being_filtered(self):
        for request in (ambient(), safety()):
            for changed in ('Please review this prompt:\n' + request, '```\n' + request + '\n```', request + '\nExplain this.'):
                self.assertIsNone(n.completion_thread_source(self.payload(changed)))
            payload = self.payload('Return JSON', request)
            self.assertIsNone(n.completion_thread_source(payload))

    def test_prior_request_cannot_label_later_turn_and_multiple_inputs_are_not_ephemeral(self):
        for request in (ambient(), safety()):
            for inputs in ([request, 'Normal follow-up'], ['Prior human input', request], []):
                payload = self.payload(request); payload['input-messages'] = inputs
                self.assertIsNone(n.completion_thread_source(payload))

    def test_wrong_client_event_missing_id_and_oversize_are_preserved(self):
        for request in (ambient(), safety()):
            for extra in ({'client': 'other'}, {'type': 'other'}, {'thread-id': ''},
                          {'input-messages': [request + 'x' * r.MAX_REQUEST_LENGTH]}):
                self.assertIsNone(n.completion_thread_source(dict(self.payload(request), **extra)))

    def test_normal_suggestions_and_exclusions_are_delivered_in_both_paths(self):
        replies = ['{"exclude":[]}', '{"exclude":[{"id":"x","reason":"test"}]}',
                   '{"suggestions":[{"title":"test","description":"d","prompt":"p","appId":"github","pluginId":null}]}']
        for body in replies:
            for desktop in (True, False):
                with self.subTest(body=body, desktop=desktop), \
                        patch.object(n, 'load_config', return_value=({'chat_id': '123', 'include_message_preview': True}, '111:fixture')), \
                        patch.object(n, 'notifications_muted', return_value=False), \
                        patch.object(n, 'route_completion', side_effect=verified_completion), \
                        patch.object(n, 'reserve_completion', return_value=True), \
                        patch.object(n, 'subagent_completion_context', return_value=None), \
                        patch.object(n, 'cache_full_reply', return_value=None), \
                        patch.object(n, 'resolve_desktop_submissions'), patch.object(n, 'attach_task_reply'), \
                        patch.object(n, 'complete_payload_reactions') as complete, \
                        patch.object(n, 'forward_computer_use_notification'), \
                        patch.object(n, 'send_markdown_message', return_value={'message_id': 42}) as send:
                    if desktop:
                        payload = self.payload('Return this JSON as requested.', body)
                        n.send_desktop_notification(payload, json.dumps(payload))
                    else:
                        payload = {'hook_event_name': 'Stop', 'thread_id': 'fixture-user', 'last_assistant_message': body}
                        n.send_hook_notification(payload)
                    send.assert_called_once()
                    self.assertIn(body, send.call_args.args[1]['text'])
                    complete.assert_called_once_with(payload)

    def test_ambient_sections_fields_and_scope_must_match(self):
        request = ambient()
        for modified in (request.replace('# Rules', '# Other'), request.replace('pluginId:', 'plugin:'),
                         request.replace('Recent Codex tasks in this project:', 'Recent other tasks:'),
                         request.replace('this local project: /fixture', 'this task'),
                         request.replace('\n# Rules\n', '\n# Rules\n\n# Rules\n'),
                         request.replace('Avoid repeating these previously dismissed suggestions:', 'dismissed:')):
            self.assertIsNone(n.completion_thread_source(self.payload(modified)))

    def test_safety_policy_footer_and_candidate_shape_must_match(self):
        request = safety()
        for modified in (request.replace('Fixture policy.', 'New policy.'),
                         request.replace('Fixture output instructions.', 'New output.'),
                         request.replace('suggestion-1', 'arbitrary-id'),
                         request.replace('  title: ', '  title_value: '),
                         request.replace('  prompt: "Hello\\n<text>"', '  prompt: 42'), safety(0), safety(4)):
            self.assertIsNone(n.completion_thread_source(self.payload(modified)))

    def test_crlf_templates_remain_supported(self):
        for request in (ambient(), safety()):
            self.assertIn(n.completion_thread_source(self.payload('\r\n' + request.replace('\n', '\r\n') + '\r\n')),
                          n.INTERNAL_SUGGESTION_SOURCES)

    def test_unknown_exclusion_is_recorded_without_prompt_content(self):
        secret = 'confidential-user-text'
        payload = self.payload(secret, '{"exclude":[]}')
        self.assertFalse(n.is_internal_suggestions_completion(payload))
        path = self.root / 'telegram_notification_diagnostics.sqlite3'
        self.assertTrue(path.is_file())
        self.assertNotIn(secret.encode(), path.read_bytes())


if __name__ == '__main__':
    unittest.main()
