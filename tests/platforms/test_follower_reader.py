"""Recorded follower protocol shape, with adversarial routing and freshness."""
import copy
from pathlib import Path
import unittest
from unittest.mock import patch

from codex_telegram_bridge.codex.desktop import DesktopEndpoint, DesktopIpcUnavailable, DesktopBridgeSafetyError
from codex_telegram_bridge.codex.follower import FollowerReader, normalize_snapshot


def snapshot(active=False):
    return {'id': 'thread-one', 'hostId': 'local', 'threadRuntimeStatus': {'type': 'active' if active else 'idle'},
        'turns': [], 'cwd': '/repo', 'title': 'original',
        'turnHistory': {'kind': 'canonical', 'history': {'entitiesByKey': {
            'later': {'turnId': 'turn-new', 'status': 'inProgress' if active else 'completed', 'items': []},
            'earlier': {'turnId': 'turn-old', 'status': 'completed', 'items': []}},
            'islands': [{'entries': [{'value': 'earlier'}, {'value': 'later'}],
                         'newerBoundary': {'status': 'exhausted'}}]}}}


class FakeTransport:
    _INITIAL_CLIENT_ID = 'initial'
    def __init__(self, timeout):
        self.timeout = timeout
        self.requests = []; self.writes = []; self.closed = False
        self.messages = [{'type': 'broadcast', 'method': 'thread-stream-state-changed',
            'sourceClientId': 'owner', 'version': 11,
            'params': {'hostId': 'local', 'conversationId': 'thread-one',
                'change': {'type': 'snapshot', 'revision': 1, 'conversationState': snapshot(True)}}}]
    def _open(self): return 1
    def _close(self, handle): self.closed = True
    def _request(self, handle, client, method, params):
        self.requests.append((method, params))
        return {'result': {'clientId': 'reader'}} if method == 'initialize' else {'handledByClientId': 'owner'}
    def _write(self, handle, message): self.writes.append(message)
    def _next_message(self, handle, deadline):
        if not self.messages: raise TimeoutError('no snapshot')
        return self.messages.pop(0)


class SnapshotTests(unittest.TestCase):
    def test_canonical_order_uses_islands_not_dictionary_or_empty_legacy_array(self):
        result = normalize_snapshot(snapshot(True), 'thread-one')
        self.assertEqual([t['id'] for t in result['turns']], ['turn-new', 'turn-old'])
        self.assertEqual([t['id'] for t in result['thread']['turns']], ['turn-old', 'turn-new'])
        self.assertEqual(result['thread']['status'], {'type': 'active'})

    def test_wrong_task_or_host_is_rejected(self):
        for key, value in [('id', 'other'), ('hostId', 'remote')]:
            state = snapshot(); state[key] = value
            with self.subTest(key=key), self.assertRaises(DesktopBridgeSafetyError):
                normalize_snapshot(state, 'thread-one')

    def test_unknown_runtime_state_is_not_idle(self):
        for kind in ('notLoaded', 'loading', 'unknown', None):
            state = snapshot(); state['threadRuntimeStatus'] = {'type': kind}
            with self.subTest(kind=kind), self.assertRaises(DesktopIpcUnavailable):
                normalize_snapshot(state, 'thread-one')

    def test_old_page_missing_entity_and_optimistic_turn_are_not_current_turn(self):
        for case in ('boundary', 'entity', 'id', 'status'):
            state = snapshot(True); h = state['turnHistory']['history']
            if case == 'boundary': h['islands'][-1]['newerBoundary']['status'] = 'unloaded'
            if case == 'entity': h['entitiesByKey'].pop('later')
            if case == 'id': h['entitiesByKey']['later'].pop('turnId')
            if case == 'status': h['entitiesByKey']['later']['status'] = 'completed'
            with self.subTest(case=case), self.assertRaises(DesktopIpcUnavailable):
                normalize_snapshot(state, 'thread-one')


class FollowerTransportTests(unittest.TestCase):
    def setUp(self):
        self.transport = FakeTransport(1)
        self.endpoint = DesktopEndpoint(11, None, Path('/profile'))
        self.reader = FollowerReader(lambda **_: self.transport, 1, lambda: self.endpoint)
        for target in ('codex_telegram_bridge.codex.follower.validate_thread_profile',
                       'codex_telegram_bridge.platforms.macos.desktop_access.validate_local_desktop_thread'):
            p = patch(target); p.start(); self.addCleanup(p.stop)

    def test_probe_only_initializes_verified_broker_without_task_discovery(self):
        self.reader.probe()
        self.assertEqual([m for m,p in self.transport.requests], ['initialize'])
        self.assertFalse(self.transport.writes)
        self.assertTrue(self.transport.closed)

    def test_fresh_read_only_subscription_is_pinned_and_always_unsubscribed(self):
        value = self.reader.read_thread('thread-one')
        self.assertEqual(value['turns'][0]['id'], 'turn-new')
        self.assertEqual(self.transport.expected_server_pid, 11)
        self.assertEqual([r[0] for r in self.transport.requests], ['initialize', 'thread-owner-discovery'])
        self.assertEqual([r['params']['following'] for r in self.transport.writes], [True, False])
        self.assertTrue(self.transport.closed)

    def test_wrong_owner_host_task_and_delta_are_ignored_before_fresh_snapshot(self):
        expected = self.transport.messages[0]
        messages = []
        for field in ('owner', 'host', 'thread', 'delta'):
            bad = copy.deepcopy(expected)
            if field == 'owner': bad['sourceClientId'] = 'untrusted'
            if field == 'host': bad['params']['hostId'] = 'remote'
            if field == 'thread': bad['params']['conversationId'] = 'another-thread'
            if field == 'delta': bad['params']['change']['type'] = 'delta'
            messages.append(bad)
        self.transport.messages = [*messages, expected]
        self.assertEqual(self.reader.read_thread('thread-one')['thread']['id'], 'thread-one')

    def test_unsupported_version_and_invalid_revision_fail_closed_and_close(self):
        for field, value in [('version', 12), ('revision', None)]:
            self.setUp()
            message = self.transport.messages[0]
            if field == 'version': message['version'] = value
            else: message['params']['change']['revision'] = value
            with self.subTest(field=field), self.assertRaises(DesktopIpcUnavailable):
                self.reader.read_thread('thread-one')
            self.assertTrue(self.transport.closed)
            self.assertFalse(self.transport.writes[-1]['params']['following'])

    def test_timeout_never_navigates_resumes_or_submits(self):
        self.transport.messages = []
        with self.assertRaises(TimeoutError): self.reader.read_thread('thread-one')
        self.assertEqual(len(self.transport.requests), 2)
        self.assertTrue(self.transport.closed)

    def test_restart_invalidates_snapshot(self):
        endpoints = iter([self.endpoint, DesktopEndpoint(22, None, Path('/profile'))])
        self.reader.discover = lambda: next(endpoints)
        with self.assertRaises(DesktopBridgeSafetyError): self.reader.read_thread('thread-one')

    def test_discovery_request_does_not_claim_task_ownership(self):
        self.transport.messages.insert(0, {'type': 'client-discovery-request', 'requestId': 'probe'})
        self.reader.read_thread('thread-one')
        self.assertEqual(self.transport.writes[1]['response'], {'canHandle': False})
