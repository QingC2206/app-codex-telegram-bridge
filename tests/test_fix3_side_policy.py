"""B11 policy enforcement through the real client with isolated transports."""
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from codex_telegram_bridge.core import bot as b
from codex_telegram_bridge.core import side_policy as policy
import test_side_chat as fixture


def features(enabled=False, **changes):
    return {'data':[{'name':'multi_agent','enabled':enabled,'stage':'stable',**changes}], 'nextCursor':None}


class SidePolicyTests(unittest.TestCase):
    def ready(self):
        client=b.CodexAppServer(Mock(),Mock());client.side_chat=True
        client.process=Mock();client.process.poll.return_value=None
        client._connection_generation=1;client._side_verified_generation=1
        self.enabled=False;self.calls=[]
        def request(method,params,**kwargs):
            self.calls.append((method,params))
            if method=='experimentalFeature/list':return features(self.enabled)
            if method=='thread/fork':return {'thread':{'id':'child','ephemeral':True},'approvalPolicy':'never','sandbox':{'type':'readOnly'}}
            return {'turn':{'id':'turn'}}
        client._request=Mock(side_effect=request)
        return client

    def test_missing_enabled_removed_and_alternate_multi_agent_flags_fail_closed(self):
        bad=[{}, {'data':[]}, {'data':features()['data']}, features(True), features(False,stage='removed'),
             {'data':features()['data']+[{'name':'multi_agent_v2','enabled':True}], 'nextCursor':None}]
        for result in bad:
            with self.subTest(result=result),self.assertRaises(policy.SidePolicyError):
                policy.verify_features(Mock(return_value=result),'thread')
        policy.verify_features(Mock(return_value=features()),'thread')

    def test_feature_pagination_must_complete_and_use_exact_thread(self):
        call=Mock(side_effect=[{'data':[{'name':'other','enabled':True}],'nextCursor':'next'},features()])
        policy.verify_features(call,'thread')
        self.assertEqual([c.args[1] for c in call.call_args_list],
            [{'limit':100,'threadId':'thread'},{'limit':100,'threadId':'thread','cursor':'next'}])
        with self.assertRaises(policy.SidePolicyError):
            policy.verify_features(Mock(return_value={'data':[],'nextCursor':'cycle'}))

    def test_feature_pages_share_one_deadline(self):
        request=Mock(return_value={'data':[],'nextCursor':'next'})
        with patch.object(policy.time,'monotonic',side_effect=[0,1,16]),self.assertRaises(policy.SidePolicyError):
            policy.verify_features(request)
        request.assert_called_once();self.assertEqual(request.call_args.kwargs['timeout'],14)

    def test_fork_overrides_parent_and_rechecks_effective_thread_before_input(self):
        client=self.ready()
        client.request('thread/fork',{'threadId':'parent','sandbox':'danger-full-access',
            'approvalPolicy':'on-request','config':{'agents.enabled':True,'features.multi_agent':True,'notify':[]}})
        self.assertEqual(self.calls[0][1]['config'],{**policy.OVERRIDES,'notify':[]})
        self.assertEqual(self.calls[0][1]['sandbox'],'read-only')
        self.assertEqual(self.calls[0][1]['approvalPolicy'],'never')
        self.assertEqual(self.calls[1],('experimentalFeature/list',{'limit':100,'threadId':'child'}))
        client.request('turn/start',{'threadId':'child','approvalPolicy':'on-request',
            'sandboxPolicy':{'type':'dangerFullAccess'}},expected_process=client.process)
        self.assertEqual(self.calls[-2][0],'experimentalFeature/list')
        self.assertEqual(self.calls[-1][1]['sandboxPolicy'],{'type':'readOnly'})
        self.assertEqual(self.calls[-1][1]['approvalPolicy'],'never')

    def test_reenabled_thread_never_receives_next_turn_and_stops_side_only(self):
        client=self.ready();client.request('thread/fork',{'threadId':'parent'})
        self.enabled=True;client.stop=Mock()
        with self.assertRaises(policy.SidePolicyError):
            client.request('turn/start',{'threadId':'child'},expected_process=client.process)
        self.assertNotIn('turn/start',[m for m,p in self.calls]);client.stop.assert_called_once()

    def test_bad_fork_policy_cannot_enter_verified_threads(self):
        for field,value in [('approvalPolicy','on-request'),('sandbox',{'type':'workspaceWrite'}),
                            ('thread',{'id':'parent','ephemeral':True}),('thread',{'id':'child','ephemeral':False}),
                            ('thread','invalid'),('sandbox','invalid')]:
            client=self.ready();client.stop=Mock()
            bad={'thread':{'id':'child','ephemeral':True},'approvalPolicy':'never','sandbox':{'type':'readOnly'}};bad[field]=value
            client._request=Mock(return_value=bad)
            with self.subTest(field=field),self.assertRaises(policy.SidePolicyError):client.request('thread/fork',{'threadId':'parent'})
            self.assertEqual(client._side_threads,set());client.stop.assert_called_once()

    def test_main_parent_resume_and_unknown_threads_never_receive_side_calls(self):
        client=self.ready()
        for method in ('turn/start','thread/inject_items','thread/resume','turn/steer'):
            with self.subTest(method=method),self.assertRaises(policy.SidePolicyError):
                client.request(method,{'threadId':'parent'},expected_process=client.process)
        client._request.assert_not_called()

    def test_dead_or_changed_process_is_never_restarted_for_old_session(self):
        client=self.ready();old=client.process;client.ensure_started=Mock()
        client.process=Mock();client.process.poll.return_value=None
        with self.assertRaises(policy.SidePolicyError):client.request('turn/start',{'threadId':'child'},expected_process=old)
        client.process=old;old.poll.return_value=1
        with self.assertRaises(policy.SidePolicyError):client.request('turn/start',{'threadId':'child'},expected_process=old)
        client.ensure_started.assert_not_called();client._request.assert_not_called()

    def test_startup_probe_has_no_stdin_is_bounded_and_uses_platform_options(self):
        result=Mock(returncode=0,stdout=json.dumps({'appServerVersion':'fixture'}))
        with patch.object(policy,'validate_schema'),patch.object(policy,'spawn_options',return_value={'creationflags':123}),patch.object(policy.subprocess,'run',return_value=result) as run:
            policy.validate_startup(['/codex','app-server'],{'fixture':'value'})
        self.assertEqual(run.call_args.kwargs['stdin'],subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs['creationflags'],123)
        self.assertEqual(run.call_args.kwargs['timeout'],15)
        with patch.object(policy.subprocess,'run',side_effect=subprocess.TimeoutExpired('probe',15)):
            with self.assertRaises(policy.SidePolicyError):policy.validate_startup(['/codex','app-server'],{})

    def test_unrecognized_probe_or_effective_capability_never_reaches_model_turn(self):
        for response in (Mock(returncode=1,stdout='private diagnostics'),Mock(returncode=0,stdout='{}'),
                         Mock(returncode=0,stdout='invalid-json')):
            with patch.object(policy.subprocess,'run',return_value=response),self.assertRaises(policy.SidePolicyError):
                policy.validate_startup(['/codex','app-server'],{})
        with tempfile.TemporaryDirectory() as folder:
            executable=Path(folder)/'codex';executable.write_text('fixture')
            client=b.CodexAppServer(Mock(),Mock(),runtime={'codex_executable':str(executable)});client.side_chat=True
            with patch.object(policy,'validate_startup',side_effect=policy.SidePolicyError('unsupported')), \
                 patch.object(b.subprocess,'Popen') as spawn:
                with self.assertRaises(policy.SidePolicyError):client.request('thread/fork',{'threadId':'parent'})
                spawn.assert_not_called()
            process=Mock();process.poll.return_value=None
            client._request=Mock(side_effect=lambda method,*a,**kw: features(True) if method=='experimentalFeature/list' else {})
            with patch.object(policy,'validate_startup'),patch.object(b.subprocess,'Popen',return_value=process), \
                 patch.object(b,'ChildProcessJob'),patch.object(b.threading,'Thread'),patch.object(client,'stop') as stop:
                with self.assertRaises(policy.SidePolicyError):client.request('thread/fork',{'threadId':'parent'})
                stop.assert_called_once()
                self.assertNotIn('thread/fork',[c.args[0] for c in client._request.call_args_list])

    def test_schema_must_declare_scoped_thread_and_object_overrides_not_unrelated_fields(self):
        policy._SCHEMA_VERIFIED.clear()
        with tempfile.TemporaryDirectory() as folder:
            executable=Path(folder)/'codex';executable.write_text('fixture')
            seen=[];mode=['valid']
            def generate(arguments,**kwargs):
                target=Path(arguments[-1]);seen.append(target)
                (target/'v2').mkdir()
                feature={'properties':{'threadId':{'type':['string','null']}}}
                fork={'properties':{'config':{'type':['object','null'],'additionalProperties':True}}}
                if mode[0]=='unrelated':feature={'properties':{},'definitions':{'unrelated':{'properties':{'threadId':{'type':'string'}}}}}
                if mode[0]=='wrong-type':fork['properties']['config']['type']='string'
                if mode[0]=='closed-config':fork['properties']['config']['additionalProperties']=False
                (target/'v2'/'ExperimentalFeatureListParams.json').write_text(json.dumps(feature))
                (target/'v2'/'ThreadForkParams.json').write_text(json.dumps(fork))
                self.assertEqual(kwargs['stdin'],subprocess.DEVNULL);self.assertEqual(kwargs['timeout'],15)
                return Mock(returncode=0)
            with patch.object(policy.subprocess,'run',side_effect=generate) as run:
                policy.validate_schema(executable,{},'v1');policy.validate_schema(executable,{},'v1')
                self.assertEqual(run.call_count,1)
                # A new binary signature cannot inherit the old capability result.
                executable.write_text('different binary')
                for value in ('unrelated','wrong-type','closed-config'):
                    mode[0]=value
                    with self.subTest(mode=value),self.assertRaises(policy.SidePolicyError):policy.validate_schema(executable,{},'v1')
                mode[0]='valid';policy.validate_schema(executable,{},'v1')
                self.assertEqual(run.call_count,5)
            self.assertTrue(all(not p.exists() for p in seen))

    def test_custom_side_arguments_cannot_enable_agents_or_replace_binary(self):
        for arguments in (['/other','app-server'],['/codex','--enable','multi_agent','app-server'],['/codex','exec']):
            with self.assertRaises(policy.SidePolicyError):policy.server_arguments('/codex',arguments)
        arguments=policy.server_arguments('/codex',['/codex','-c','features.multi_agent=true','app-server'])
        self.assertEqual(arguments[-6:],['--strict-config','-c','agents.enabled=false','-c','features.multi_agent=false','app-server'])

    def test_every_new_side_process_probes_and_verifies_while_main_is_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            executable=Path(folder)/'codex';executable.write_text('fixture')
            client=b.CodexAppServer(Mock(),Mock(),runtime={'codex_executable':str(executable)})
            process=Mock();process.poll.return_value=None
            client._request=Mock(side_effect=lambda method,*a,**kw: features() if method=='experimentalFeature/list' else {})
            with patch.object(b.subprocess,'Popen',return_value=process) as spawn,patch.object(b,'ChildProcessJob'), \
                 patch.object(b.threading,'Thread'),patch.object(policy,'validate_startup') as validate:
                client.ensure_started();main_args=spawn.call_args.args[0]
                validate.assert_not_called();self.assertNotIn('--strict-config',main_args)
                client.process=None;client.side_chat=True;client.ensure_started()
                self.assertIn('agents.enabled=false',spawn.call_args.args[0]);self.assertEqual(client.connection_generation,2)
                self.assertEqual(client.side_policy_identity,(policy.POLICY_VERSION,2))
                client.process=None;client.ensure_started()
                self.assertEqual(client.connection_generation,3);self.assertEqual(validate.call_count,2)
                self.assertEqual(client.side_policy_identity,(policy.POLICY_VERSION,3))

    def test_old_reader_generation_cannot_impersonate_new_connection(self):
        events=[];requests=[];client=b.CodexAppServer(events.append,requests.append)
        frames=[{'method':'account/rateLimits/updated','params':{},'_bridge_connection_generation':999},
                {'id':7,'method':'currentTime/read','params':{},'_bridge_connection_generation':999}]
        process=Mock(stdout=io.StringIO('\n'.join(map(json.dumps,frames))));client.process=process
        client._connection_generation=2;client._read_stdout(process,1)
        self.assertEqual([m['_bridge_connection_generation'] for m in events+requests],[1,1])
        with self.assertRaises(AttributeError):client.connection_generation=99

    def test_owner_cleanup_errors_cannot_skip_process_readers_or_stream_cleanup(self):
        for error in (subprocess.TimeoutExpired('watchdog',5),OSError('job handle close failed')):
            client=b.CodexAppServer(Mock(),Mock());process=Mock();process.poll.return_value=None
            client.process=process;client.child_job=Mock();client.child_job.close.side_effect=error
            reader=Mock();client.readers=[reader]
            process.stdin.close.side_effect=OSError('stream close failed')
            with self.subTest(error=type(error).__name__),self.assertRaises(type(error)) as caught:client.stop()
            self.assertIs(caught.exception,error)
            process.terminate.assert_called_once();process.wait.assert_called_once_with(timeout=5)
            reader.join.assert_called_once_with(timeout=1)
            for stream in (process.stdin,process.stdout,process.stderr):stream.close.assert_called_once()
            self.assertIsNone(client.process);self.assertIsNone(client.child_job);self.assertEqual(client.readers,[])

    def test_old_owner_cleanup_cannot_clear_or_close_a_replacement_process(self):
        client=b.CodexAppServer(Mock(),Mock());old=Mock();old.poll.return_value=0
        replacement=Mock();new_reader=Mock();old_reader=Mock()
        client.process=old;client.readers=[old_reader];client.child_job=Mock()
        def replace():
            client.process=replacement;client.readers=[new_reader]
            raise OSError('old owner failed')
        client.child_job.close.side_effect=replace
        with self.assertRaises(OSError):client.stop()
        self.assertIs(client.process,replacement);self.assertEqual(client.readers,[new_reader])
        old_reader.join.assert_called_once();new_reader.join.assert_not_called()
        replacement.terminate.assert_not_called();replacement.stdin.close.assert_not_called()


class SideSessionPolicyTests(unittest.TestCase):
    setUp=fixture.SideTests.setUp

    def test_written_submission_lost_ack_is_unknown_without_retry_or_parent_fallback(self):
        original=self.ctl.app.request
        def request(method,params,**kwargs):
            if method=='turn/start':
                self.ctl.app.calls.append((method,params))
                raise b.AppServerSubmissionUnknown('written request lost acknowledgement')
            return original(method,params,**kwargs)
        self.ctl.app.request=request
        self.ctl._run(self.route,'question',42)
        self.assertEqual([m for m,p in self.ctl.app.calls].count('turn/start'),1)
        self.assertEqual(self.bot._reaction.call_args.args,('👀',))
        self.assertNotIn(('😢',),[c.args for c in self.bot._reaction.call_args_list])
        text=self.bot.send.call_args.args[0]
        self.assertIn('问题提交结果未确认',text);self.assertIn('未自动重试',text)
        self.assertNotIn('答复投递结果未确认',text)
        self.assertNotIn('side-thread',self.ctl.sessions)
        self.bot.start_turn.assert_not_called();self.bot.steer_turn.assert_not_called()

    def test_delivery_unknown_is_distinguished_from_question_submission_unknown(self):
        from codex_telegram_bridge.telegram.delivery import TelegramDeliveryUnknown
        self.bot.send.side_effect=[TelegramDeliveryUnknown('lost delivery acknowledgement'),{'message_id':100}]
        self.ctl._run(self.route,'question',42)
        self.assertEqual([m for m,p in self.ctl.app.calls].count('turn/start'),1)
        self.assertEqual(self.bot._reaction.call_args.args,('👀',))
        self.assertIn('答复投递结果未确认',self.bot.send.call_args.args[0])
        self.assertNotIn('问题提交结果未确认',self.bot.send.call_args.args[0])
        self.assertNotIn('side-thread',self.ctl.sessions)

    def test_policy_change_invalidates_existing_side_without_parent_fallback(self):
        self.ctl._run(self.route,'first',42)
        self.ctl.app.side_policy_identity=('changed-policy',1)
        before=list(self.ctl.app.calls)
        self.ctl.submit(self.ctl.lookup(99),'follow-up')
        self.assertEqual(self.ctl.app.calls,before)
        self.bot.start_turn.assert_not_called();self.assertIn('过期',self.bot.send.call_args.args[0])

    def test_uncertified_client_never_sends_question_or_inherited_boundary(self):
        self.ctl.app.side_policy_identity=None
        self.ctl._run(self.route,'question',42)
        self.assertEqual([m for m,p in self.ctl.app.calls],['thread/fork'])
        self.assertIn('内置子代理',self.bot.send.call_args.args[0])
        self.bot.start_turn.assert_not_called()


if __name__=='__main__':unittest.main()
