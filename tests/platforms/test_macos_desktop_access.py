"""macOS access policy contracts use injected adapters, never a fake host OS."""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import test_desktop_bridge as fixtures
import codex_telegram_bridge.codex.desktop as d
import codex_telegram_bridge.platforms.macos.desktop_access as mac


class MacOSReplyTests(unittest.TestCase):
    mutations = fixtures.BridgeTests.mutations

    def setUp(self):
        fixtures.BridgeTests.setUp(self)
        self.control.access = mac.MacOSDesktopAccess()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.application = Path(temporary.name).resolve()/'ChatGPT.app'

    def test_mac_normal_reply_uses_verified_owner_without_signed_app_tools(self):
        with patch.object(mac, 'validate_local_desktop_thread') as validate, \
                patch.object(d.DesktopAppTools, 'read_thread', side_effect=AssertionError('signed API must not be used')):
            self.owner.steer_inactive = True
            result = self.control.send_follow_up('original-task', 'continue', '/repo')
        validate.assert_called_once_with(self.discover.return_value, 'original-task')
        self.assertEqual(result['turn_id'], 'new-turn')
        self.assertEqual([r['method'] for r in self.mutations()],
                         ['thread-follower-steer-turn', 'thread-follower-start-turn'])
        self.assertEqual(self.mutations()[-1]['params']['turnStart']['request']['threadId'], 'original-task')
        self.assertEqual(self.transport.requests, [])

    def test_mac_unverified_local_target_never_receives_input(self):
        with patch.object(mac, 'validate_local_desktop_thread', side_effect=d.DesktopBridgeSafetyError('wrong profile')):
            with self.assertRaises(d.DesktopBridgeSafetyError):
                self.control.send_follow_up('original-task', 'continue', '/repo')
        self.assertFalse(self.mutations())

    def test_mac_turn_specific_reply_retains_live_turn_check(self):
        from codex_telegram_bridge.codex.follower import FollowerReader
        state = {'thread': {'id': 'task', 'status': {'type': 'active'}},
                 'turns': [{'id': 'old-turn', 'status': 'inProgress'}]}
        with patch.object(FollowerReader, 'read_thread', return_value=state) as read:
            self.owner.result_turn_id = 'old-turn'
            self.control.send_follow_up('task', 'answer', '', expected_turn_id='old-turn')
            self.control.send_follow_up('task', 'steer', '', start_if_inactive=False)
        self.assertEqual(read.call_count, 2)
        self.assertEqual(self.transport.requests, [])

    def test_mac_restart_during_local_validation_prevents_input(self):
        def restart(*args):
            self.discover.return_value = d.DesktopEndpoint(99, 'other-pipe')
        with patch.object(mac, 'validate_local_desktop_thread', side_effect=restart):
            with self.assertRaises(d.DesktopBridgeSafetyError):
                self.control.send_follow_up('original-task', 'continue', '/repo')
        self.assertFalse(self.mutations())

    def test_mac_missing_owner_opens_only_original_thread_then_submits_once(self):
        self.owner.owner_present = False
        root = {'ProcessId':44, 'ExecutablePath':str(self.application/'Contents/MacOS/ChatGPT')}
        def opened(*args, **kwargs):
            self.owner.owner_present = True
        with patch.object(mac, 'validate_local_desktop_thread'), \
                patch.object(mac, 'desktop_roots', return_value=[root]), \
                patch.object(mac.subprocess, 'run', side_effect=opened) as opener:
            result = self.control.send_follow_up('original-task', 'continue', '/repo', recover_owner=True)
        self.assertTrue(result['owner_recovered'])
        self.assertEqual(len(self.mutations()),1)
        self.assertEqual(opener.call_args.args[0], ['/usr/bin/open','-a',str(self.application),'codex://threads/original-task'])
        self.assertEqual(self.transport.requests,[])

    def test_mac_lost_ack_never_opens_or_replays(self):
        self.owner.fail_read_call = True
        with patch.object(mac, 'validate_local_desktop_thread'), \
                patch.object(mac.subprocess, 'run') as opener:
            with self.assertRaises(d.DesktopSubmissionUnknown):
                self.control.send_follow_up('original-task','continue','/repo',recover_owner=True)
        opener.assert_not_called()
        self.assertEqual(len(self.mutations()),1)

    def test_mac_unanswered_owner_discovery_opens_original_before_one_input(self):
        root = {'ProcessId':44, 'ExecutablePath':str(self.application/'Contents/MacOS/ChatGPT')}
        receive = self.owner._next_message
        opened = False
        def next_message(*args):
            if self.owner.requests[-1]['method'] == 'thread-owner-discovery' and not opened:
                raise TimeoutError('unloaded renderer did not answer')
            return receive(*args)
        def open_original(*args, **kwargs):
            nonlocal opened
            self.assertEqual(self.mutations(), [])
            opened = True
        with patch.object(mac, 'validate_local_desktop_thread'), \
                patch.object(mac, 'desktop_roots', return_value=[root]), \
                patch.object(mac.subprocess, 'run', side_effect=open_original) as opener, \
                patch.object(self.owner, '_next_message', side_effect=next_message):
            result = self.control.send_follow_up('original-task', 'continue', '/repo', recover_owner=True)
        self.assertTrue(result['owner_recovered'])
        self.assertEqual(len(self.mutations()), 1)
        opener.assert_called_once()
        self.assertEqual(opener.call_args.args[0][-1], 'codex://threads/original-task')
        self.assertEqual(self.transport.requests, [])

    def test_mac_discovery_failure_without_recovery_permission_never_opens(self):
        receive = self.owner._next_message
        def next_message(*args):
            if self.owner.requests[-1]['method'] == 'thread-owner-discovery':
                raise TimeoutError('unanswered discovery')
            return receive(*args)
        with patch.object(self.owner, '_next_message', side_effect=next_message), \
                patch.object(mac.subprocess, 'run') as opener:
            with self.assertRaises(d.DesktopOwnerDiscoveryUnavailable):
                self.control.send_follow_up('original-task', 'continue', '/repo')
        opener.assert_not_called()
        self.assertEqual(self.mutations(), [])

    def test_mac_owner_loaded_after_first_probe_is_found_without_reopening(self):
        self.owner.owner_present = False
        root = {'ProcessId':44, 'ExecutablePath':str(self.application/'Contents/MacOS/ChatGPT')}
        receive = self.owner._next_message
        opened, probes = False, 0
        def open_original(*args, **kwargs):
            nonlocal opened
            opened = True
        def next_message(*args):
            nonlocal probes
            if opened and self.owner.requests[-1]['method'] == 'thread-owner-discovery':
                probes += 1
                if probes == 1:
                    self.assertLessEqual(self.owner.requests[-1]['timeoutMs'], 1000)
                    self.owner.owner_present = True
                    raise TimeoutError('owner registered after this probe began')
            return receive(*args)
        def transport(**kwargs):
            self.owner.timeout = kwargs['timeout']
            return self.owner
        self.control.transport_factory = transport
        with patch.object(mac, 'validate_local_desktop_thread'), \
                patch.object(mac, 'desktop_roots', return_value=[root]), \
                patch.object(mac.subprocess, 'run', side_effect=open_original) as opener, \
                patch.object(self.owner, '_next_message', side_effect=next_message), \
                patch.object(d.time, 'sleep'):
            result = self.control.send_follow_up('original-task', 'continue', '/repo', recover_owner=True)
        self.assertTrue(result['owner_recovered'])
        self.assertEqual(len(self.mutations()), 1)
        self.assertEqual(probes, 3)  # two read probes, then the one input's guard
        opener.assert_called_once()

    def test_mac_initialize_failure_does_not_open_or_submit(self):
        with patch.object(self.owner, '_next_message', side_effect=TimeoutError('initialize failed')), \
                patch.object(mac.subprocess, 'run') as opener:
            with self.assertRaises(d.DesktopIpcUnavailable) as caught:
                self.control.send_follow_up('original-task', 'continue', '/repo', recover_owner=True)
        self.assertNotIsInstance(caught.exception, d.DesktopOwnerDiscoveryUnavailable)
        opener.assert_not_called()
        self.assertEqual(self.mutations(), [])

    def test_mac_discovery_timeout_still_requires_original_profile_before_open(self):
        receive = self.owner._next_message
        def next_message(*args):
            if self.owner.requests[-1]['method'] == 'thread-owner-discovery':
                raise TimeoutError('unanswered discovery')
            return receive(*args)
        with patch.object(self.owner, '_next_message', side_effect=next_message), \
                patch.object(mac, 'validate_local_desktop_thread', side_effect=d.DesktopBridgeSafetyError('wrong profile')), \
                patch.object(mac.subprocess, 'run') as opener:
            with self.assertRaises(d.DesktopBridgeSafetyError):
                self.control.send_follow_up('original-task', 'continue', '/repo', recover_owner=True)
        opener.assert_not_called()
        self.assertEqual(self.mutations(), [])


class LocalDesktopTargetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.profile = Path(temporary.name)
        self.transcript = self.profile / 'sessions' / 'rollout-original-task.jsonl'
        self.transcript.parent.mkdir()
        self.header = {'id': 'original-task', 'originator': 'Codex Desktop', 'source': 'vscode'}
        self.write_header()
        self.db = sqlite3.connect(self.profile / 'state_5.sqlite')
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE threads (id TEXT, rollout_path TEXT, archived INTEGER)')
        self.db.execute('INSERT INTO threads VALUES (?, ?, 0)', ('original-task', str(self.transcript)))
        self.db.commit()
        self.endpoint = d.DesktopEndpoint(44, None, self.profile)

    def write_header(self):
        self.transcript.write_text(json.dumps({'type': 'session_meta', 'payload': self.header})+'\n')

    def test_genuine_desktop_origin_in_current_profile_is_accepted(self):
        mac.validate_local_desktop_thread(self.endpoint, 'original-task')

    def test_missing_archived_other_profile_and_non_desktop_targets_are_rejected(self):
        for change in ({'id': 'other-task'}, {'originator': 'codex_cli_rs'}, {'source': {'subagent': {}}}):
            with self.subTest(change=change):
                old = self.header.copy(); self.header.update(change); self.write_header()
                with self.assertRaises(d.DesktopBridgeSafetyError):
                    mac.validate_local_desktop_thread(self.endpoint, 'original-task')
                self.header = old; self.write_header()
        for target in ('missing-task', '../outside', ''):
            with self.assertRaises(d.DesktopBridgeSafetyError):
                mac.validate_local_desktop_thread(self.endpoint, target)
        self.db.execute('UPDATE threads SET archived=1'); self.db.commit()
        with self.assertRaises(d.DesktopBridgeSafetyError):
            mac.validate_local_desktop_thread(self.endpoint, 'original-task')
        with self.assertRaises(d.DesktopBridgeSafetyError):
            mac.validate_local_desktop_thread(d.DesktopEndpoint(44, None, self.profile/'other'), 'original-task')

    @unittest.skipIf(os.name == 'nt', 'macOS symlink guard; Windows symlink privileges differ')
    def test_transcript_symlink_outside_profile_is_rejected(self):
        outside = self.profile / 'outside.jsonl'
        self.transcript.rename(outside); self.transcript.symlink_to(outside)
        with self.assertRaises(d.DesktopBridgeSafetyError):
            mac.validate_local_desktop_thread(self.endpoint, 'original-task')
