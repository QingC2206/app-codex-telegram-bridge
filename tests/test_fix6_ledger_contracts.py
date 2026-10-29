"""Previously unbound ledger rules, with isolated files and mocked task APIs."""
from pathlib import Path
import io
import json
import queue
import time
from unittest.mock import Mock, patch
import unittest
import test_interactions as fixture
import test_side_chat as side_fixture
import test_async_inputs as async_fixture
from codex_telegram_bridge.core import bot as b
from codex_telegram_bridge.i18n import using_language


class LedgerContracts(unittest.TestCase):
    def setUp(self):
        fixture.InteractionTests.setUp(self)
        self.bot.send=Mock(return_value={'message_id':100})
        self.bot._reaction=Mock(return_value=False)
        self.bot._begin_turn=Mock(return_value='turn-new')
        self.root=Path(self.folder.name)

    def test_rotated_log_preserves_previous_file_and_redacts_new_credentials(self):
        path=self.root/'bridge.log';previous='old-log\n'*(65537)
        path.write_text(previous)
        with patch.object(b,'LOG_PATH',path):b.log('received 111:secret','111:secret')
        self.assertEqual(path.with_suffix('.log.old').read_text(),previous)
        self.assertNotIn('111:secret',path.read_text())
        self.assertIn('<redacted-token>',path.read_text())

    def test_message_projection_excludes_tool_and_reasoning_but_preserves_original_text(self):
        original='中文 /language en **raw**'
        thread={'turns':[{'items':[{'type':'userMessage','content':[{'type':'text','text':original}]},
            {'type':'commandExecution','aggregatedOutput':'private tool output'},
            {'type':'agentMessage','phase':'commentary','text':'progress'},
            {'type':'agentMessage','phase':'final_answer','text':original}]}]}
        with using_language('en'):
            messages=b.extract_messages(thread)
        self.assertEqual([text for _,text in messages],[original,original])
        self.assertEqual(b.extract_messages(thread,limit=1),[('Codex',original)])
        self.assertEqual(b.extract_messages({'turns':None}),[])

    def test_command_parser_separates_suffix_arguments_and_preserves_plain_text(self):
        self.assertEqual(b.parse_command('/SEND@MyRobot task Original Text'),('/send','task Original Text'))
        self.assertEqual(b.parse_command('  Ordinary /send literal  '),('','Ordinary /send literal'))
        self.assertEqual(b.parse_command('/TASKS'),('/tasks',''))

    def test_diff_extracts_only_latest_changes_and_empty_data_is_not_progress(self):
        thread={'turns':[{'items':[{'type':'fileChange','changes':[{'path':'old','diff':'old'}]}]},
            {'items':[{'type':'fileChange','changes':[{'path':'new','diff':'@@ actual diff'}]},
                      {'type':'commandExecution','command':'ignore'}]}]}
        self.assertEqual(b.extract_latest_diff(thread),('@@ actual diff',['new']))
        self.assertEqual(b.extract_latest_diff({}),('',[]))
        self.assertIn('等待',b.describe_item(None))
        for item,expected in [({'type':'commandExecution','command':'echo hello'},'echo hello'),
                              ({'type':'mcpToolCall','server':'reader','tool':'inspect'},'reader/inspect'),
                              ({'type':'fileChange','changes':[{'path':'a.py'}]},'a.py')]:
            self.assertIn(expected,b.describe_item(item))

    def test_new_task_validates_directory_and_binds_first_input_only_to_created_thread(self):
        self.bot.state.update(default_cwd=str(self.root/'missing'))
        with self.assertRaises(ValueError):self.bot.command_new('original input')
        self.bot.app.request.assert_not_called()
        self.bot.state.update(default_cwd=str(self.root),managed_threads={'existing':{'origin':'bot_created'}})
        self.bot.app.request.return_value={'thread':{'id':'created'}}
        original='unchanged **input** 中文'
        self.bot.command_new(original)
        self.bot._begin_turn.assert_called_once_with('created',original)
        self.assertEqual(self.bot.state.snapshot()['managed_threads']['created']['origin'],'bot_created')
        self.assertIn('existing',self.bot.state.snapshot()['managed_threads'])

    def test_cwd_rejects_relative_or_missing_and_preserves_existing_tasks(self):
        before=self.bot.state.snapshot()
        for path in ('relative',str(self.root/'missing')):
            with self.assertRaises(ValueError):self.bot.command_cwd(path)
        self.assertEqual(self.bot.state.snapshot(),before)
        folder=self.root/'with spaces';folder.mkdir();self.bot.command_cwd(str(folder))
        self.assertEqual(self.bot.state.snapshot()['default_cwd'],str(folder.resolve()))
        self.bot.app.request.assert_not_called();self.bot._begin_turn.assert_not_called()

    def test_aliases_use_current_page_and_ambiguous_id_prefix_is_rejected(self):
        self.bot.state.update(task_aliases=['current-page-thread'])
        self.assertEqual(self.bot.resolve_thread('1'),'current-page-thread')
        with self.assertRaises(ValueError):self.bot.resolve_thread('2')
        self.bot._list_threads=Mock(return_value=[{'id':'abcdef-one'},{'id':'abcdef-two'}])
        with self.assertRaises(ValueError):self.bot.resolve_thread('abcdef')
        self.assertEqual(self.bot.resolve_thread('abcdef-one'),'abcdef-one')

    def test_usage_separates_cumulative_latest_context_time_and_supported_cost(self):
        self.bot.resolve_thread=Mock(return_value='task')
        self.bot.token_usage['task']={'total':{'totalTokens':1000},'last':{'totalTokens':100},'modelContextWindow':200}
        self.bot.turn_elapsed['task']=65
        self.bot.app.request.side_effect=lambda method,*a,**k: ({'thread':{'id':'task'}} if method=='thread/read'
            else {'threadUsage':{'estimatedUsageUsdMicros':1250000,'estimatedUsageCreditsMicros':2500000}})
        self.bot.command_usage('task');text=self.bot.send.call_args.args[0]
        for expected in ('累计','1,000','100 Token','50.0%','1分5秒','$1.250000','2.500000'):
            self.assertIn(expected,text)

    def test_log_query_is_bounded_and_read_only(self):
        path=self.root/'telegram_notify.log';path.write_text('\n'.join(f'line-{i:04d}' for i in range(100)))
        before=path.read_bytes()
        with patch.object(b,'HOOK_DIR',self.root),patch.object(b,'LOG_PATH',self.root/'missing'):
            self.bot.command_logs()
        text=self.bot.send.call_args.args[0]
        self.assertNotIn('line-0087',text);self.assertIn('line-0088',text);self.assertIn('line-0099',text)
        self.assertEqual(path.read_bytes(),before);self.bot.app.request.assert_not_called()

    def test_jsonl_out_of_order_responses_and_exit_wake_each_waiter(self):
        app=b.CodexAppServer(Mock(),Mock())
        process=Mock(stdout=io.StringIO('\n'.join(json.dumps(x) for x in [
            {'id':2,'result':{'value':'second'}},{'id':1,'result':{'value':'first'}}])))
        app.process=process
        waiting={ident:queue.Queue(maxsize=1) for ident in (1,2,3)}
        app.pending.update(waiting);app._read_stdout(process)
        self.assertEqual(waiting[1].get_nowait()['result'],{'value':'first'})
        self.assertEqual(waiting[2].get_nowait()['result'],{'value':'second'})
        self.assertTrue(waiting[3].get_nowait()['transportFailure'])
        self.assertTrue(app.closed.is_set())


class SidePolicyContracts(unittest.TestCase):
    setUp=side_fixture.SideTests.setUp

    def test_side_permissions_are_never_and_approval_requests_are_declined(self):
        self.ctl._run(self.route,'question',42)
        fork=self.ctl.app.calls[0][1]
        self.assertEqual(fork['approvalPolicy'],'never')
        self.assertEqual(fork['sandbox'],'read-only')
        self.assertIn('Do not contact or create subagents',fork['developerInstructions'])
        self.ctl.app.answer=Mock()
        for method in ('item/commandExecution/requestApproval','item/fileChange/requestApproval'):
            self.ctl.server_request({'id':1,'method':method})
            self.ctl.app.answer.assert_called_with(1,{'decision':'decline'})
        self.ctl.server_request({'id':2,'method':'item/permissions/requestApproval'})
        self.ctl.app.answer.assert_called_with(2,{'permissions':{},'scope':'turn'})

    def test_side_idle_lifetime_is_three_hours_then_only_child_is_unsubscribed(self):
        with patch.object(side_fixture.side.time,'time',return_value=100000):
            self.ctl._run(self.route,'question',42)
        self.assertEqual(self.ctl.sessions['side-thread']['expires'],110800)
        with patch.object(side_fixture.side.time,'time',return_value=110801):self.ctl.cleanup_expired()
        self.assertEqual(self.ctl.app.calls[-1],('thread/unsubscribe',{'threadId':'side-thread'}))
        self.assertFalse(self.ctl.sessions)


class AsyncEvidenceContracts(unittest.TestCase):
    setUp=async_fixture.AsyncInputTests.setUp
    observed=async_fixture.AsyncInputTests.observed

    def test_answer_is_persisted_submitting_before_desktop_input(self):
        row=self.observed();observed=[]
        def submit(*args,**kwargs):
            current=self.manager.get(row['key']);observed.append((current['status'],current['answer']))
            return {'route':'desktop-user-input'}
        self.bot._desktop_follow_up.side_effect=submit
        self.manager.submit(row['key'],'Second')
        self.assertEqual(observed,[('submitting','Second')])

    def test_question_retention_cleans_only_own_bot_chat_after_thirty_days(self):
        row=self.observed()
        with self.manager.db() as db:
            db.execute('UPDATE questions SET created=? WHERE key=?',(time.time()-31*86400,row['key']))
            other=dict(row);other.update(bot='222',key='foreign',created=time.time()-31*86400)
            keys=list(other)
            db.execute('INSERT INTO questions ('+','.join(keys)+') VALUES ('+','.join('?' for _ in keys)+')',
                       [other[key] for key in keys])
        self.assertIsNone(self.manager.get(row['key']))
        with self.manager.db() as db:self.assertEqual(db.execute("SELECT count(*) FROM questions WHERE bot='222'").fetchone()[0],1)
        self.bot._desktop_follow_up.assert_not_called()


if __name__=='__main__':unittest.main()
