"""Provenance routing across transports, profiles, delayed records and JSON replies."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from codex_telegram_bridge.telegram import completion_routing as r
from codex_telegram_bridge.telegram import pending_completions as q
from codex_telegram_bridge.telegram import notifications as n

THREAD = '01a15c92-d617-74c2-a352-bad8edc4e789'
INTERNAL = '01a15c92-fe82-7152-af22-ee4a58dd0a59'
TURN = '01a15c93-0047-7602-abbe-542530348649'


class CompletionRoutingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.profile = self.root / 'profile'; self.profile.mkdir()
        self.data = self.root / 'data'; self.data.mkdir()
        self.queue = self.data / 'telegram_completions.sqlite3'
        with closing(sqlite3.connect(self.profile/'state_5.sqlite')) as db, db:
            db.execute('CREATE TABLE threads (id TEXT PRIMARY KEY,thread_source TEXT,source TEXT,originator TEXT,cwd TEXT,rollout_path TEXT)')
        self.payload = {'type':'agent-turn-complete', 'client':'Codex Desktop', 'thread-id':THREAD,
                        'turn-id':TURN, 'last-assistant-message':'Normal reply'}
        for context in (patch.object(n, 'SOURCE_STATE_ROOT', self.profile), patch.object(n, 'SESSION_ROOT', self.profile/'sessions'),
                        patch.object(n, 'HOOK_DIR', self.data), patch.object(n, 'log_error')):
            context.start(); self.addCleanup(context.stop)

    def task(self, thread=THREAD, *, source='user', origin='Codex Desktop', trigger=None, text=None, turn=TURN, stored=True):
        path = self.profile/'sessions'/f'rollout-fixture-{thread}.jsonl'
        path.parent.mkdir(exist_ok=True)
        meta = {'id':thread, 'thread_source':source, 'originator':origin, 'source':'vscode', 'cwd':'/fixture'}
        records = [{'type':'session_meta','payload':meta},
                   {'type':'event_msg','payload':{'type':'task_started','turn_id':turn,
                    'turn_attribution':{'turn_id':turn,'turn_trigger':trigger}}}]
        if text is not None:
            records.append({'type':'event_msg','payload':{'type':'task_complete','turn_id':turn,'last_agent_message':text}})
        path.write_text(''.join(json.dumps(x)+'\n' for x in records), encoding='utf-8')
        if stored:
            with closing(sqlite3.connect(self.profile/'state_5.sqlite')) as db, db:
                db.execute('INSERT OR REPLACE INTO threads VALUES (?,?,?,?,?,?)', (thread,source,'vscode',origin,'/fixture',str(path)))
        return path

    def classify(self, payload=None):
        return r.classify(self.payload if payload is None else payload, self.profile)

    def test_task_source_and_turn_trigger_are_independent(self):
        for trigger in ('composer','remote_ios','remote_android','queue','goal','app_tool_send_message',None):
            with self.subTest(trigger=trigger):
                self.task(trigger=trigger)
                d = self.classify(dict(self.payload, threadSource='user', turnTrigger=trigger))
                self.assertEqual((d.kind,d.thread_source,d.turn_trigger),('conversation','user',trigger))

    def test_visible_task_sources_follow_desktop_contract(self):
        for source in r.CONVERSATION_SOURCES | {None}:
            with self.subTest(source=source):
                self.task(source=source)
                self.assertEqual(self.classify().kind,'conversation')

    def test_mobile_client_label_does_not_override_verified_host_and_turn(self):
        # Display labels are fixtures, not an assumed list of mobile products.
        for client in ('Fixture iOS controller', 'Fixture Android controller', 'Renamed mobile app'):
            for trigger in ('remote_ios','remote_android','queue','edit_user_message','capacity_retry_manual','goal'):
                with self.subTest(client=client,trigger=trigger):
                    self.task(trigger=trigger)
                    decision=self.classify(dict(self.payload,client=client))
                    self.assertEqual(decision.kind,'conversation')
                    self.assertTrue(decision.requires_turn_evidence)

    def test_mobile_callback_requires_local_evidence_for_exact_turn(self):
        self.task(trigger='remote_ios')
        for fields in ({'turn-id':'different-turn'}, {'turn-id':None}):
            payload=dict(self.payload,client='Fixture mobile',turnTrigger='remote_ios',**fields)
            self.assertEqual(self.classify(payload).reason,'unverified-local-turn')
        self.task(trigger=None)
        payload=dict(self.payload,client='Fixture mobile',turnTrigger='remote_android')
        self.assertEqual(self.classify(payload).reason,'unverified-local-turn')
        self.task(trigger='remote_ios')
        self.assertEqual(self.classify(payload).reason,'conflicting-attribution')

    def test_mobile_client_never_authorizes_cli_wrong_profile_or_internal_work(self):
        payload=dict(self.payload,client='Fixture mobile')
        for origin in ('codex_vscode','codex_cli_rs',None):
            self.task(origin=origin,trigger='remote_ios')
            self.assertFalse(self.classify(payload).deliver)
        self.task(trigger='remote_android')
        other=self.root/'other-profile';other.mkdir()
        self.assertFalse(r.classify(payload,other).deliver)
        self.assertFalse(self.classify(dict(payload,**{'thread-id':INTERNAL})).deliver)
        for source in r.INTERNAL_SOURCES:
            self.task(source=source,trigger='remote_ios')
            self.assertEqual(self.classify(payload).kind,'internal')
        self.task(trigger='thread_title')
        self.assertEqual(self.classify(payload).kind,'internal')

    def test_mobile_subagent_still_follows_notification_preference(self):
        payload=dict(self.payload,client='Fixture mobile')
        self.task(source='subagent',trigger='remote_android')
        self.assertEqual(self.classify(payload).kind,'subagent')
        with ExitStack() as stack:
            send=self.delivery_patches(stack)
            n.send_desktop_notification(payload,'original')
            send.assert_not_called()
            self.assertFalse(q.reserve(payload,self.profile,self.queue))

    def test_malformed_client_label_is_not_deferred_as_mobile(self):
        self.task(trigger='remote_ios')
        for value in ({}, [], 1, 'x'*257):
            payload=dict(self.payload,client=value)
            decision=self.classify(payload)
            self.assertEqual(decision.reason,'invalid-client-label')
            self.assertFalse(q.defer(payload,decision,self.profile,self.queue))

    def test_normal_json_is_not_a_classifier_input(self):
        self.task()
        for text in ('{"title":"x","description":"y"}','{"suggestions":[1]}','{"exclude":[]}',
                     '{"suggestion":"next"}', 'An ordinary answer', '# Overview\nGenerate suggestions'):
            self.assertTrue(self.classify(dict(self.payload, **{'last-assistant-message':text})).deliver)

    def test_internal_purposes_suppressed_without_reading_output(self):
        for source in r.INTERNAL_SOURCES:
            with self.subTest(source=source):
                self.assertEqual(self.classify(dict(self.payload, threadSource=source)).kind,'internal')
                self.task(source='user', trigger=source)
                self.assertEqual(self.classify().kind,'internal')
                (self.profile/'sessions'/f'rollout-fixture-{THREAD}.jsonl').unlink()
                with closing(sqlite3.connect(self.profile/'state_5.sqlite')) as db, db:
                    db.execute('DELETE FROM threads')

    def test_unknown_source_never_promoted_even_for_plain_text(self):
        for body in ('Hello', '{"title":"x","description":"y"}', '{"suggestions":[]}'):
            self.assertEqual(self.classify(dict(self.payload, **{'last-assistant-message':body})).kind,'unknown')
        self.task(source='new_background_feature')
        self.assertEqual(self.classify().reason,'unknown-thread-purpose')
        self.task(trigger='new_trigger')
        self.assertEqual(self.classify().reason,'unknown-turn-trigger')

    def test_shared_short_prefix_never_uses_parent_task_proof(self):
        self.task()
        self.assertEqual(self.classify(dict(self.payload, **{'thread-id':INTERNAL})).kind,'unknown')
        self.assertEqual(self.classify(dict(self.payload, **{'thread-id':THREAD[:8]})).kind,'unknown')

    def test_conflicting_fields_and_ids_are_held(self):
        self.task(trigger='composer')
        for fields in ({'threadSource':'thread_title'}, {'thread_source':['user']}, {'thread_id':INTERNAL},
                       {'turnTrigger':'remote_ios'}, {'turn':{'id':'different-turn'}},
                       {'turn_attribution':{'turn_id':'different-turn','turn_trigger':'composer'}},
                       {'threadSource':'user','thread-source':'ambient_suggestions'}):
            with self.subTest(fields=fields):
                self.assertEqual(self.classify(dict(self.payload, **fields)).kind,'unknown')

    def test_rollout_fallback_handles_missing_or_unavailable_state_database(self):
        self.task(stored=False)
        self.assertTrue(self.classify().deliver)
        (self.profile/'state_5.sqlite').write_bytes(b'broken database')
        self.assertTrue(self.classify().deliver)

    def test_rewritten_rollout_from_database_wins_over_original_file(self):
        original=self.task(trigger='composer')
        rewritten=original.with_name(original.stem+'_rewrite-generation.jsonl')
        contents=original.read_text(encoding='utf-8').replace('"composer"','"remote_ios"')
        rewritten.write_text(contents,encoding='utf-8')
        with closing(sqlite3.connect(self.profile/'state_5.sqlite')) as db, db:
            db.execute('UPDATE threads SET rollout_path=? WHERE id=?',(str(rewritten),THREAD))
        decision=self.classify(dict(self.payload,client='Fixture mobile'))
        self.assertTrue(decision.deliver)
        self.assertEqual(decision.turn_trigger,'remote_ios')
        self.assertTrue(Path(decision.record['rollout_path']).samefile(rewritten))
        with closing(sqlite3.connect(self.profile/'state_5.sqlite')) as db, db:
            db.execute('DELETE FROM threads')
        self.assertEqual(self.classify().reason,'conflicting-local-records')

    def test_rewritten_rollout_requires_matching_header_identity(self):
        original=self.task(trigger='remote_android')
        rewritten=original.with_name(original.stem+'_rewrite-generation.jsonl')
        rewritten.write_text(original.read_text(encoding='utf-8').replace(THREAD,INTERNAL),encoding='utf-8')
        with closing(sqlite3.connect(self.profile/'state_5.sqlite')) as db, db:
            db.execute('UPDATE threads SET rollout_path=? WHERE id=?',(str(rewritten),THREAD))
        self.assertEqual(self.classify().reason,'conflicting-local-records')

    @unittest.skipUnless(os.name=='nt','Windows extended-length path syntax')
    def test_extended_windows_rollout_path_stays_in_same_profile(self):
        path=self.task(trigger='remote_ios')
        rewritten=path.with_name(path.stem+'_rewrite-generation.jsonl')
        path.rename(rewritten)
        extended='\\\\?\\'+str(rewritten.resolve())
        with closing(sqlite3.connect(self.profile/'state_5.sqlite')) as db, db:
            db.execute('UPDATE threads SET rollout_path=? WHERE id=?',(extended,THREAD))
        decision=self.classify(dict(self.payload,client='Fixture mobile'))
        self.assertTrue(decision.deliver)
        self.assertTrue(Path(decision.record['rollout_path']).samefile(rewritten))
        outside=self.root/'outside';outside.mkdir()
        other=outside/rewritten.name
        other.write_bytes(rewritten.read_bytes())
        self.assertIsNone(r._safe_rollout(self.profile,'\\\\?\\'+str(other.resolve()),THREAD))

    def test_long_mobile_turn_without_history_projection_recovers_attribution(self):
        path=self.task(trigger='remote_android')
        with path.open('a',encoding='utf-8') as stream:
            stream.write(json.dumps({'type':'response_item','payload':{'output':'x'*5000}})+'\n')
            stream.write(json.dumps({'type':'event_msg','payload':{'type':'task_complete','turn_id':TURN,
                                                                 'last_agent_message':'Normal reply'}})+'\n')
        payload=dict(self.payload,client='Fixture mobile')
        with patch.object(r,'READ_LIMIT',1024), patch.object(r,'ATTRIBUTION_READ_LIMIT',8192):
            decision=self.classify(payload)
            self.assertTrue(decision.deliver)
            self.assertEqual(decision.turn_trigger,'remote_android')
        with patch.object(r,'READ_LIMIT',1024), patch.object(r,'ATTRIBUTION_READ_LIMIT',2048):
            self.assertEqual(self.classify(payload).reason,'unverified-local-turn')

    def test_stale_schema_and_wrong_profile_cannot_authorize(self):
        self.task()
        other = self.root/'other';other.mkdir()
        self.assertFalse(r.classify(self.payload, other).deliver)
        # With a newer readable DB, the old database is not an authority. A
        # valid local rollout may still bridge the migration independently.
        (self.profile/'sessions'/f'rollout-fixture-{THREAD}.jsonl').unlink()
        with closing(sqlite3.connect(self.profile/'state_6.sqlite')) as db, db:
            db.execute('CREATE TABLE threads (id TEXT, thread_source TEXT, originator TEXT)')
        self.assertFalse(self.classify().deliver)

    def test_foreign_origin_and_ephemeral_flags_do_not_authorize(self):
        for origin in ('codex_vscode','codex_cli_rs',None):
            self.task(origin=origin)
            self.assertFalse(self.classify().deliver)
        self.task()
        for fields in ({'client':'VS Code'}, {'ephemeral':True}, {'ephemeral':'invalid'}, {'thread':{'ephemeral':True}}):
            self.assertFalse(self.classify(dict(self.payload, **fields)).deliver)

    def test_subagent_context_uses_child_id_and_preference(self):
        self.task(source='subagent')
        payload = {'hook_event_name':'SubagentStop','session_id':INTERNAL,'agent_id':THREAD,'turn_id':TURN}
        self.assertEqual(r.identity(payload),(THREAD,TURN,True))
        self.assertEqual(self.classify(payload).kind,'subagent')

    def test_skipped_subagent_question_does_not_reserve_its_completion(self):
        payload={'hook_event_name':'PreToolUse','tool_name':'request_user_input',
                 'session_id':INTERNAL,'agent_id':THREAD,'turn_id':TURN}
        with patch.object(n,'notifications_muted',return_value=False), \
             patch.object(n,'load_config',return_value=({'chat_id':'1'},'111:fixture')), \
             patch.object(n,'reserve_completion') as reserve:
            n.send_hook_notification(payload)
            reserve.assert_not_called()

    def test_filtered_subagent_completion_is_not_replayed_after_preference_change(self):
        q.defer(self.payload,self.classify(),self.profile,self.queue)
        self.task(source='subagent',text=self.payload['last-assistant-message'])
        with ExitStack() as stack:
            send=self.delivery_patches(stack)
            with patch.object(q.time,'time',return_value=q.time.time()+3):
                n.retry_pending_completions()
            send.assert_not_called()
            with patch.object(n,'load_config',return_value=({'chat_id':'1','include_subagent_notifications':True},'111:fixture')), \
                 patch.object(q.time,'time',return_value=q.time.time()+900):
                n.retry_pending_completions()
            send.assert_not_called()

    def test_only_matching_turn_attribution_is_used(self):
        self.task(trigger='composer')
        payload = dict(self.payload, **{'turn-id':'different-turn'})
        self.assertIsNone(self.classify(payload).turn_trigger)

    def test_delayed_metadata_recovers_exact_reply_without_storing_text(self):
        text='private-answer-and-token-123456'
        payload=dict(self.payload, **{'last-assistant-message':text,'input-messages':['private prompt']})
        self.assertTrue(q.defer(payload,self.classify(payload),self.profile,self.queue))
        self.assertNotIn(text.encode(),self.queue.read_bytes())
        self.assertNotIn(b'private prompt',self.queue.read_bytes())
        self.task(text=text,trigger='remote_ios')
        with patch.object(q.time,'time',return_value=q.time.time()+3):
            ready=q.ready(self.profile,self.queue)
        self.assertEqual(len(ready),1)
        self.assertEqual(ready[0]['last-assistant-message'],text)
        self.assertTrue(q.reserve(ready[0],self.profile,self.queue))
        self.assertFalse(q.reserve(payload,self.profile,self.queue))
        with patch.object(q.time,'time',return_value=q.time.time()+900):
            self.assertEqual(q.ready(self.profile,self.queue),[])

    def test_mobile_replay_retains_turn_requirement_across_missing_metadata(self):
        client='private-controller-label'
        body='private mobile reply'
        payload=dict(self.payload,client=client,**{'last-assistant-message':body})
        self.assertTrue(q.defer(payload,self.classify(payload),self.profile,self.queue))
        self.assertNotIn(client.encode(),self.queue.read_bytes())
        self.assertNotIn(body.encode(),self.queue.read_bytes())
        self.task(text=body,trigger=None)
        with patch.object(q.time,'time',return_value=q.time.time()+3):
            self.assertEqual(q.ready(self.profile,self.queue),[])
        self.task(text=body,trigger='remote_android',turn='different-turn')
        with patch.object(q.time,'time',return_value=q.time.time()+100):
            self.assertEqual(q.ready(self.profile,self.queue),[])
        self.task(text=body,trigger='queue')
        with patch.object(q.time,'time',return_value=q.time.time()+200):
            ready=q.ready(self.profile,self.queue)
        self.assertEqual(len(ready),1)
        self.assertTrue(ready[0]['_requires_turn_evidence'])
        self.assertEqual(ready[0]['last-assistant-message'],body)
        self.assertTrue(q.reserve(ready[0],self.profile,self.queue))
        self.assertFalse(q.reserve(payload,self.profile,self.queue))

    def test_mobile_missing_turn_evidence_enters_queue_and_respects_quiet_generation(self):
        self.task(text=self.payload['last-assistant-message'])
        payload=dict(self.payload,client='Fixture mobile')
        decision=self.classify(payload)
        self.assertEqual(decision.reason,'unverified-local-turn')
        self.assertTrue(q.defer(payload,decision,self.profile,self.queue,generation=2))
        self.task(trigger='remote_ios',text=self.payload['last-assistant-message'])
        with patch.object(q.time,'time',return_value=q.time.time()+3):
            self.assertEqual(q.ready(self.profile,self.queue,generation=3),[])

    def test_mobile_notify_preserves_normal_json_and_dedupes_with_stop(self):
        body='{"title":"requested title","suggestions":["normal answer"]}'
        payload=dict(self.payload,client='Fixture mobile',**{'last-assistant-message':body})
        self.task(trigger='remote_ios',text=body)
        with ExitStack() as stack:
            send=self.delivery_patches(stack)
            n.send_desktop_notification(payload,'original')
            send.assert_called_once()
            self.assertIn(body,send.call_args.args[1]['text'])
            n.send_desktop_notification(payload,'duplicate')
            n.send_hook_notification({'hook_event_name':'Stop','session_id':THREAD,'turn_id':TURN,'last_assistant_message':body})
            send.assert_called_once()

    def test_recovery_requires_exact_turn_and_body_and_respects_mute(self):
        q.defer(self.payload,self.classify(),self.profile,self.queue)
        self.task(text='Another reply')
        with patch.object(q.time,'time',return_value=q.time.time()+3):
            self.assertEqual(q.ready(self.profile,self.queue),[])
        self.task(text=self.payload['last-assistant-message'])
        q.ready(self.profile,self.queue,muted=True)
        with patch.object(q.time,'time',return_value=q.time.time()+900):
            self.assertEqual(q.ready(self.profile,self.queue),[])

    def test_delivery_reservation_is_atomic_across_workers(self):
        with ThreadPoolExecutor(max_workers=4) as executor:
            results=list(executor.map(lambda _:q.reserve(self.payload,self.profile,self.queue),range(8)))
        self.assertEqual(results.count(True),1)

    def test_quiet_mode_toggled_while_worker_stopped_does_not_replay(self):
        q.defer(self.payload,self.classify(),self.profile,self.queue,generation=2)
        self.task(text=self.payload['last-assistant-message'])
        with patch.object(q.time,'time',return_value=q.time.time()+3):
            self.assertEqual(q.ready(self.profile,self.queue,generation=3),[])

    def test_deferred_replay_uses_existing_turn_and_calls_computer_use_only_once(self):
        with ExitStack() as stack:
            send=self.delivery_patches(stack)
            n.send_desktop_notification(self.payload,'original')
            send.assert_not_called()
            n.forward_computer_use_notification.assert_called_once_with('original')
            self.task(text=self.payload['last-assistant-message'])
            with patch.object(q.time,'time',return_value=q.time.time()+3):
                n.retry_pending_completions()
            send.assert_called_once()
            n.forward_computer_use_notification.assert_called_once_with('original')

    def test_paginated_history_recovers_final_item_with_matching_turn(self):
        self.task()
        with closing(sqlite3.connect(self.profile/'thread_history_1.sqlite')) as db, db:
            db.execute('CREATE TABLE thread_turns (thread_id TEXT,turn_id TEXT,status TEXT,final_agent_item_id TEXT,rollout_byte_offset INTEGER)')
            db.execute('CREATE TABLE thread_items (thread_id TEXT,turn_id TEXT,item_id TEXT,item_json TEXT)')
            db.execute('INSERT INTO thread_turns VALUES (?,?,?,?,?)',(THREAD,TURN,'completed','item-a',0))
            db.execute('INSERT INTO thread_items VALUES (?,?,?,?)',(THREAD,TURN,'item-a',json.dumps({'type':'agentMessage','phase':'final_answer','text':'Reply'})))
        record=r.read_record(self.profile,THREAD)[1]
        self.assertEqual(r.turn_evidence(self.profile,record,THREAD,TURN)[1],'Reply')
        self.assertIsNone(r.turn_evidence(self.profile,record,THREAD,'different-turn')[1])

    def test_conflicts_are_not_replayed_without_the_conflicting_evidence(self):
        self.task()
        payload=dict(self.payload,threadSource='thread_title')
        self.assertFalse(q.defer(payload,self.classify(payload),self.profile,self.queue))

    def test_diagnostic_write_failure_does_not_disable_verification_queue(self):
        from codex_telegram_bridge.telegram import notification_diagnostics as diagnostics
        with patch.object(diagnostics,'record',side_effect=OSError('private-error-body')):
            self.assertIsNone(n.route_completion(self.payload))
        self.assertTrue(self.queue.is_file())
        self.assertNotIn('private-error-body',str(n.log_error.call_args_list))

    def test_verification_failure_does_not_abort_existing_receipt_worker(self):
        with patch.object(q,'ready',side_effect=sqlite3.DatabaseError('private-error-body')):
            n.retry_pending_completions()
        self.assertNotIn('private-error-body',str(n.log_error.call_args_list))

    def test_replay_failure_is_isolated_to_one_entry(self):
        with patch.object(q,'ready',return_value=[self.payload,self.payload]), \
             patch.object(n,'send_desktop_notification',side_effect=[OSError('private-error-body'),0]) as send:
            n.retry_pending_completions()
        self.assertEqual(send.call_count,2)
        self.assertNotIn('private-error-body',str(n.log_error.call_args_list))

    def test_pending_queue_has_expiry_and_limit(self):
        with patch.object(q,'MAX_PENDING',1):
            self.assertTrue(q.defer(self.payload,self.classify(),self.profile,self.queue))
            other=dict(self.payload, **{'turn-id':'different-turn'})
            self.assertFalse(q.defer(other,self.classify(other),self.profile,self.queue))
        self.task(text=self.payload['last-assistant-message'])
        with patch.object(q.time,'time',return_value=q.time.time()+q.TTL+1):
            self.assertEqual(q.ready(self.profile,self.queue),[])

    def delivery_patches(self, stack):
        for name in ('forward_computer_use_notification','cache_full_reply','resolve_desktop_submissions','attach_task_reply','complete_payload_reactions'):
            stack.enter_context(patch.object(n,name,return_value=None))
        stack.enter_context(patch.object(n,'notifications_muted',return_value=False))
        stack.enter_context(patch.object(n,'load_config',return_value=({'chat_id':'1','include_message_preview':True},'111:fixture')))
        return stack.enter_context(patch.object(n,'send_markdown_message',return_value={'message_id':42}))

    def test_both_ingresses_share_provenance_and_preserve_normal_json(self):
        body='{"title":"A normal JSON title","description":"requested"}'
        payload=dict(self.payload, **{'last-assistant-message':body})
        with ExitStack() as stack:
            send=self.delivery_patches(stack)
            n.send_desktop_notification(payload,'{}')
            send.assert_not_called()
            self.task(text=body)
            n.send_desktop_notification(payload,'{}')
            send.assert_called_once()
            self.assertIn(body,send.call_args.args[1]['text'])
            hook={'hook_event_name':'Stop','session_id':THREAD,'turn_id':TURN,'last_assistant_message':body}
            n.send_hook_notification(hook)
            send.assert_called_once()  # Same completion cannot send twice.

    def test_internal_and_unknown_do_not_touch_credentials_cache_or_reactions(self):
        with patch.object(n,'notifications_muted',return_value=False), patch.object(n,'load_config') as config, \
             patch.object(n,'cache_full_reply') as cache, patch.object(n,'complete_payload_reactions') as reactions, \
             patch.object(n,'forward_computer_use_notification') as cua:
            for fields in ({}, {'threadSource':'thread_title'}, {'threadSource':'composer_predictions'}):
                payload=dict(self.payload,**fields)
                n.send_desktop_notification(payload,'original')
            config.assert_not_called();cache.assert_not_called();reactions.assert_not_called()
            self.assertEqual(cua.call_count,3)


if __name__ == '__main__':
    unittest.main()
