"""Hook and recovery contracts for the production shared controller."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_telegram_bridge.management import desktop_hook
from codex_telegram_bridge.features.elicitation import form_questions
import test_shared_controller as fixtures


class HookTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        p = patch.dict(os.environ, CODEX_HOME=str(self.root), CODEX_TELEGRAM_DATA_DIR=str(self.root/'data'))
        p.start(); self.addCleanup(p.stop)

    def test_install_is_idempotent_preserves_notify_and_other_handlers(self):
        config = self.root/'config.toml'; config.write_text('notify=["existing-program"]\n'); original = config.read_bytes()
        foreign = {'type': 'mcp_tool', 'server': 'test', 'tool': 'test'}
        path = self.root/'hooks.json'; path.write_text(json.dumps({'hooks': {'Stop': [{'hooks': [foreign]}]}}))
        desktop_hook.register(); desktop_hook.register()
        hooks = json.loads(path.read_text())['hooks']
        self.assertEqual(len(hooks['Stop']), 2)
        self.assertEqual(len(hooks['SubagentStop']), 1)
        self.assertEqual(hooks['PreToolUse'][0]['matcher'], '^request_user_input(?:_async)?$')
        self.assertEqual(config.read_bytes(), original)
        desktop_hook.register(remove=True)
        self.assertEqual(json.loads(path.read_text())['hooks'], {'Stop': [{'hooks': [foreign]}]})
        self.assertEqual(config.read_bytes(), original)

    def test_hook_preserves_full_provenance_for_shared_classifier(self):
        folder = self.root/'sessions/2026/10/28'; folder.mkdir(parents=True)
        path = folder/'rollout-example-desktop-thread.jsonl'
        path.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'desktop-thread', 'originator': 'Codex Desktop'}})+'\n')
        payload = {'hook_event_name': 'Stop', 'session_id': 'desktop-thread', 'turn_id': 'turn-one',
            'transcript_path': str(path), 'last_assistant_message': 'ok', 'cwd': str(self.root),
            'thread_source': 'user', 'turn_trigger': 'remote_ios'}
        envelope = desktop_hook.envelope(payload)
        self.assertEqual(envelope['payload'], payload)
        self.assertEqual(envelope['turn_id'], 'turn-one')
        payload['session_id'] = 'different-thread'
        self.assertIsNone(desktop_hook.envelope(payload))

    def test_no_transcript_path_escape_or_invalid_turn(self):
        with tempfile.TemporaryDirectory() as outside:
            path = Path(outside)/'transcript'; path.write_text('{}')
            self.assertIsNone(desktop_hook.envelope({'hook_event_name': 'Stop', 'session_id': 'desktop-thread', 'transcript_path': str(path)}))
        self.assertIsNone(desktop_hook.envelope({'hook_event_name': 'Stop', 'session_id': 'desktop-thread', 'turn_id': '../bad'}))

    def test_external_schema_and_unimplemented_root_constraints_are_rejected(self):
        for extra in ({'$ref': 'https://example.com/schema'}, {'allOf': []}, {'additionalProperties': {'type': 'string'}}):
            schema = {'type': 'object', 'properties': {'value': {'type': 'string'}}, **extra}
            self.assertIsNone(form_questions({'mode': 'form', 'requestedSchema': schema}))


class SharedHookDeliveryTests(unittest.TestCase):
    setUp = fixtures.SharedControllerTests.setUp
    migrate = fixtures.SharedControllerTests.migrate

    def test_raw_hook_reaches_shared_classifier_without_rewriting_sources(self):
        payload = {'hook_event_name': 'Stop', 'session_id': 'desktop-thread', 'turn_id': 'turn-one',
                   'thread_source': 'user', 'turn_trigger': 'goal', 'last_assistant_message': 'answer'}
        self.legacy.receive_hook('event', {'payload': payload})
        with patch('codex_telegram_bridge.telegram.notifications.send_hook_notification') as send:
            self.bot.process_hooks(); self.bot.process_hooks()
            send.assert_called_once_with(payload)
        self.assertFalse(self.legacy.pending_hooks())

    def test_quiet_generation_and_ingest_suppression_survive_turning_quiet_off(self):
        self.bot.state.update(quiet_mode=False, quiet_generation=2)
        for key, event in [('old', {'quiet_generation': 1}), ('quiet', {'quiet_generation': 2, 'suppressed': True})]:
            self.legacy.receive_hook(key, {'payload': {'hook_event_name': 'Stop'}, **event})
        with patch('codex_telegram_bridge.telegram.notifications.send_hook_notification') as send:
            self.bot.process_hooks(); send.assert_not_called()

    def test_preupgrade_hook_envelope_is_restored_without_task_execution(self):
        self.legacy.receive_hook('old', {'thread_id': 'desktop-thread', 'turn_id': 'turn-one', 'text': 'answer', 'cwd': '/repo'})
        with patch('codex_telegram_bridge.telegram.notifications.send_hook_notification') as send:
            self.bot.process_hooks()
            self.assertEqual(send.call_args.args[0]['last_assistant_message'], 'answer')
            self.assertEqual(send.call_args.args[0]['session_id'], 'desktop-thread')
        self.rpc.request.assert_not_called()
