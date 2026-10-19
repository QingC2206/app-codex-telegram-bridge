"""No payload text or credentials may enter structural notification samples."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from codex_telegram_bridge.telegram import notification_diagnostics as d
from codex_telegram_bridge.telegram import notifications as n


class NotificationDiagnosticTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.path = self.root / 'telegram_notification_diagnostics.sqlite3'
        self.payload = {'type': 'agent-turn-complete', 'client': 'Codex Desktop',
                        'thread-id': 'fixture-thread', 'last-assistant-message': '{"suggestions":[]}'}
        for context in (patch.object(n, 'HOOK_DIR', self.root),
                        patch.object(n, '_stored_thread_purpose', return_value=('missing', None)),
                        patch.object(n, 'log_error')):
            context.start()
            self.addCleanup(context.stop)

    def rows(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute('SELECT id,occurrences,snapshot FROM samples ORDER BY rowid').fetchall()

    def record(self, payload=None):
        return d.record(payload or self.payload, self.path, stored_status='missing',
                        stored_source=None, composer_match=False)

    def test_sensitive_values_and_unknown_field_names_are_never_persisted(self):
        secret = 'private-token-123456:ABCDE_0123456789_PRIVATE'
        user_text = 'Patient Alice confidential message'
        payload = dict(self.payload, cwd='C:/Users/private-name/secret-project',
                       metadata={'Authorization': secret, secret: user_text, 'threadSource': secret},
                       **{'input-messages': [secret, user_text], 'last-assistant-message': json.dumps({'suggestion': user_text})})
        self.record(payload)
        serialized = self.path.read_bytes()
        for value in (secret, user_text, 'private-name', 'secret-project', 'fixture-thread'):
            self.assertNotIn(value.encode(), serialized)
        sample = json.loads(self.rows()[0][2])
        fields = sample['payload_shape']['fields']
        self.assertEqual(fields['client']['value'], 'Codex Desktop')
        self.assertEqual(fields['metadata']['fields']['threadSource'], {'type': 'string', 'length': len(secret)})
        self.assertIn('<redacted-key-0>', fields['metadata']['fields'])
        self.assertEqual(sample['latest_input_shape']['length'], len(user_text))

    def test_structured_metadata_types_and_conflicts_are_visible(self):
        payload = dict(self.payload, threadSource='ambient_suggestions', turnTrigger='user',
                       thread={'ephemeral': True, 'thread_source': ['bad']})
        sample = d.summarize(payload, stored_status='stored', stored_source='user', composer_match=False)
        fields = sample['payload_shape']['fields']
        self.assertEqual(fields['threadSource']['value'], 'ambient_suggestions')
        self.assertEqual(fields['turnTrigger']['value'], 'user')
        self.assertEqual(fields['thread']['fields']['thread_source']['type'], 'array')
        self.assertTrue(fields['thread']['fields']['ephemeral']['value'])

    def test_latest_request_flags_never_inspect_older_or_reply_text(self):
        request = ('# Overview\n\nGenerate 0 to 3 hyperpersonalized suggestions for what this user can do with Codex in this local project: /private\n'
                   '\n# Rules\n\nRecent Codex tasks in this project:\n[]\n'
                   '\nAvoid repeating these previously dismissed suggestions:\n[]\n\n# Response format\n')
        for inputs, expected in ((['earlier', request], True), ([request, 'human input'], False)):
            sample = d.summarize(dict(self.payload, **{'input-messages': inputs}),
                                 stored_status='missing', stored_source=None, composer_match=False)
            self.assertEqual(sample['request_features']['ambient_overview_prefix'], expected)
            self.assertNotIn('/private', json.dumps(sample))

    def test_unknown_suggestion_is_recorded_but_not_filtered(self):
        self.assertFalse(n.is_internal_suggestions_completion(self.payload))
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(json.loads(self.rows()[0][2])['decision'], 'preserved_unknown_source')

    def test_ordinary_answers_and_attributed_suggestions_do_not_create_samples(self):
        for payload in (dict(self.payload, **{'last-assistant-message': 'normal answer'}),
                        dict(self.payload, threadSource='user'), dict(self.payload, threadSource='ambient_suggestions')):
            n.is_internal_suggestions_completion(payload)
        self.assertFalse(self.path.exists())

    def test_notification_is_still_delivered_with_original_json(self):
        with patch.object(n, 'forward_computer_use_notification'), \
             patch.object(n, 'notifications_muted', return_value=False), \
             patch.object(n, 'load_config', return_value=({'chat_id':'123', 'include_message_preview':True}, '111:fixture')), \
             patch.object(n, 'subagent_completion_context', return_value=None), \
             patch.object(n, 'cache_full_reply', return_value=None), \
             patch.object(n, 'resolve_desktop_submissions'), \
             patch.object(n, 'attach_task_reply'), \
             patch.object(n, 'complete_payload_reactions'), \
             patch.object(n, 'send_markdown_message', return_value={'message_id':42}) as send:
            n.send_desktop_notification(self.payload, '{}')
            send.assert_called_once()
            self.assertIn('{"suggestions":[]}', send.call_args.args[1]['text'])

    def test_write_failure_does_not_change_classification_or_leak_exception(self):
        with patch.object(d, 'record', side_effect=OSError('secret-token-in-error')):
            self.assertFalse(n.is_internal_suggestions_completion(self.payload))
        self.assertNotIn('secret-token-in-error', str(n.log_error.call_args_list))

    def test_deduplication_retention_and_concurrent_writers(self):
        self.record()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: self.record(), range(8)))
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0][1], 9)
        with patch.object(d, 'MAX_SAMPLES', 2):
            self.record(dict(self.payload, **{'thread-id':'fixture-two'}))
            self.record(dict(self.payload, **{'thread-id':'fixture-three'}))
        self.assertEqual(len(self.rows()), 2)
        with patch.object(d.time, 'time', return_value=d.time.time()+8*86400):
            self.record(dict(self.payload, **{'thread-id':'fixture-four'}))
        self.assertEqual(len(self.rows()), 1)

    def test_oversized_and_deep_inputs_are_bounded(self):
        nested = 'sensitive-body'
        for _ in range(100):
            nested = {'metadata':nested}
        payload = dict(self.payload, metadata=nested, **{'input-messages':['x'*(2*1024*1024+1)],
                                                       'content':[{f'secret-key-{i}':'secret-value'} for i in range(1000)]})
        self.record(payload)
        sample = json.loads(self.rows()[0][2])
        self.assertLess(len(self.rows()[0][2].encode()), d.MAX_RECORD_BYTES)
        self.assertFalse(sample['request_features']['latest_input_inspected'])
        self.assertNotIn('secret', self.rows()[0][2])


if __name__ == '__main__':
    unittest.main()
