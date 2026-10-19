"""No model or Telegram calls: side routing, credits, and packaged plugin upgrades."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import bundle_install as bundles
import side_chat as side
import telegram_bot as b


class FakeApp:
    def __init__(self,event,request):
        self.event=event;self.request_handler=request;self.calls=[];self.process=Mock()
        self.process.poll.return_value=None;self.count=0;self.fail=None
    def request(self,method,params,**kwargs):
        self.calls.append((method,params))
        if method==self.fail:raise TimeoutError('ambiguous')
        if method=='thread/fork':return {'thread':{'id':'side-thread','ephemeral':True}}
        if method=='thread/inject_items' or method=='thread/unsubscribe':return {}
        if method=='turn/start':
            self.count+=1;turn='turn-'+str(self.count)
            for event,data in [('turn/started',{'turn':{'id':turn}}),
                ('item/completed',{'turnId':turn,'item':{'type':'agentMessage','phase':'final_answer','text':'侧边答复'}}),
                ('turn/completed',{'turn':{'id':turn,'status':'completed'}})]:
                self.event({'method':event,'params':{'threadId':params['threadId'],**data}})
            return {'turn':{'id':turn}}
        raise AssertionError(method)
    def stop(self):pass
    def answer(self,*args):pass
    def release_thread(self,tid,process):
        if process is not self.process or self.process.poll() is not None:return False
        self.request('thread/unsubscribe',{'threadId':tid})
        return True


class SideTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.bot=Mock(token='111:secret',chat_id='123')
        self.bot._ui_local=threading.local();self.bot._ui_local.source_message_id=42
        self.bot._reaction.return_value=True;self.bot.send.return_value={'message_id':99}
        self.ctl=side.SideChats(self.bot,FakeApp,Path(self.tmp.name)/'side.db')
        self.route={'kind':'parent','target':'parent-thread','parent':'parent-thread'}
        self.title=patch.object(side.n,'conversation_title',return_value='主任务');self.title.start();self.addCleanup(self.title.stop)

    def test_fork_boundary_readonly_and_only_child_receives_question(self):
        self.ctl._run(self.route,'为什么这样实现',42)
        calls=self.ctl.app.calls
        self.assertEqual([m for m,p in calls],['thread/fork','thread/inject_items','turn/start'])
        self.assertTrue(calls[0][1]['ephemeral']);self.assertEqual(calls[0][1]['sandbox'],'read-only')
        self.assertEqual(calls[1][1]['threadId'],'side-thread')
        self.assertEqual(calls[2][1]['threadId'],'side-thread')
        self.assertEqual(calls[2][1]['input'][0]['text'],'为什么这样实现')
        self.assertIn('reference material only',calls[0][1]['developerInstructions'])
        self.bot.start_turn.assert_not_called();self.bot.steer_turn.assert_not_called();self.bot.interrupt_and_send.assert_not_called()
        self.assertEqual(self.bot.send.call_args.kwargs['reply_to'],42)
        self.assertEqual(self.ctl.lookup(99),{'kind':'session','target':'side-thread','parent':'parent-thread'})
        self.assertEqual(self.bot._reaction.call_args.args,('👌',))

    def test_reply_continues_same_side_without_forking_or_touching_parent(self):
        self.ctl._run(self.route,'first',42)
        self.ctl._run(self.ctl.lookup(99),'follow-up',43)
        self.assertEqual([m for m,p in self.ctl.app.calls].count('thread/fork'),1)
        self.assertEqual([p['threadId'] for m,p in self.ctl.app.calls if m=='turn/start'],['side-thread']*2)
        self.assertEqual(self.bot.send.call_args.kwargs['reply_to'],43)

    def test_restart_expiry_never_redirects_to_parent(self):
        self.ctl._run(self.route,'first',42)
        restarted=side.SideChats(self.bot,FakeApp,self.ctl.database)
        route=restarted.lookup(99);restarted.submit(route,'follow-up')
        self.assertEqual(restarted.app.calls,[])
        self.assertIn('过期',self.bot.send.call_args.args[0])
        self.assertEqual(self.bot.send.call_args.args[1]['inline_keyboard'][0][0]['callback_data'],'side|parent-thread')

    def test_sending_failure_keeps_non_ok_and_does_not_retry_model(self):
        self.bot.send.side_effect=TimeoutError('unknown')
        self.ctl._run(self.route,'q',42)
        self.assertNotIn(('👌',),[x.args for x in self.bot._reaction.call_args_list])
        self.assertEqual([m for m,p in self.ctl.app.calls].count('turn/start'),1)
        self.assertNotIn('side-thread',self.ctl.sessions)

    def test_ambiguous_submission_and_old_completion_do_not_leak_to_main(self):
        self.ctl.app.fail='turn/start';self.ctl._run(self.route,'q',42)
        self.assertNotIn('side-thread',self.ctl.sessions)
        self.assertIn('未自动重试',self.bot.send.call_args.args[0])
        self.bot.start_turn.assert_not_called()

    def test_duplicate_click_submits_only_once_and_busy_side_rejects_steering(self):
        with patch.object(side.threading,'Thread') as worker:
            self.ctl.submit(self.route,'q')
            with self.assertRaisesRegex(ValueError,'重复'):self.ctl.submit(self.route,'q')
            worker.return_value.start.assert_called_once()
        self.ctl.jobs.clear();self.ctl._run(self.route,'first',42)
        self.ctl.sessions['side-thread']['busy']=True
        self.bot._ui_local.source_message_id=43
        with self.assertRaisesRegex(ValueError,'正在回答'):self.ctl.submit(self.ctl.lookup(99),'second')

    def test_callback_branch_consumes_choice_without_interrupt_or_steer(self):
        bot=b.TelegramCodexBot('111:secret',{'chat_id':'123'})
        bot.pending_direct_replies['1']={'thread_id':'parent-thread','text':'question','source_message_id':42}
        bot.sides=Mock();bot.steer_turn=Mock();bot.interrupt_and_send=Mock()
        bot._handle_direct_reply_choice('1:side')
        bot.sides.submit.assert_called_once_with(self.route,'question')
        bot.steer_turn.assert_not_called();bot.interrupt_and_send.assert_not_called()
        self.assertNotIn('1',bot.pending_direct_replies)

    def test_reply_router_prefers_side_route_and_command_resolves_reply_target(self):
        bot=b.TelegramCodexBot('111:secret',{'chat_id':'123'})
        bot.sides=self.ctl;bot.submit_direct_reply=Mock();self.ctl.submit=Mock()
        self.ctl.remember(99,'session','side-thread','parent-thread')
        bot.handle_text('继续解释',reply_to_message_id=99)
        self.ctl.submit.assert_called_once_with(self.ctl.lookup(99),'继续解释')
        bot.submit_direct_reply.assert_not_called()
        self.ctl.submit.reset_mock()
        self.ctl.command('第二个问题',99)
        self.ctl.submit.assert_called_once_with(self.ctl.lookup(99),'第二个问题')

    def test_closed_and_other_chat_routes_are_not_reused(self):
        self.ctl._run(self.route,'q',42)
        self.ctl.close('side-thread');self.assertNotIn('side-thread',self.ctl.sessions)
        self.bot.chat_id='456';self.assertIsNone(self.ctl.lookup(99))
        self.bot.chat_id='123';self.bot.token='222:other';self.assertIsNone(self.ctl.lookup(99))

    def test_expired_idle_sessions_release_only_their_original_backend(self):
        process=self.ctl.app.process
        self.ctl.sessions={
            'expired':{'busy':False,'expires':0,'process':process},
            'busy':{'busy':True,'expires':0,'process':process},
            'valid':{'busy':False,'expires':float('inf'),'process':process},
            'stale':{'busy':False,'expires':0,'process':Mock()},
        }
        self.ctl.cleanup_expired()
        self.assertEqual(set(self.ctl.sessions),{'busy','valid'})
        self.assertEqual(self.ctl.app.calls,[('thread/unsubscribe',{'threadId':'expired'})])


class CreditTests(unittest.TestCase):
    def test_credit_balance_separate_from_reset_cards_and_limits(self):
        value={'rateLimitsByLimitId':{'codex':{'credits':{'hasCredits':True,'unlimited':False,'balance':'12345.6789'}}},
               'rateLimitResetCredits':{'availableCount':2}}
        self.assertIn('**余额：** 12,345.68 积分',b.format_account_credits(value))
    def test_missing_invalid_zero_unlimited_and_other_bucket(self):
        self.assertIn('**余额：** 暂未返回',b.format_account_credits({}))
        for raw in (None,'NaN','Infinity','-1','abc'):
            self.assertIn('**余额：** 暂未返回',b.format_account_credits({'rateLimits':{'credits':{'balance':raw}}}))
        self.assertIn('**余额：** 0.00 积分',b.format_account_credits({'rateLimits':{'credits':{'balance':'0','hasCredits':False}}}))
        self.assertIn('**余额：** 不限量',b.format_account_credits({'rateLimits':{'credits':{'unlimited':True}}}))
        self.assertIn('**余额：** 暂未返回',b.format_account_credits({'rateLimits':{'limitId':'other','credits':{'balance':'99'}}}))


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name);self.package=root/'package';self.root=root/'plugins';self.state=root/'state'
        self.source=self.package/'bundled_plugins/demo';self.source.mkdir(parents=True)
        (self.package/'bundled_plugins.json').write_text(json.dumps({'plugins':{'demo':['plugin.json','main.py']},'retired':['markdown']}))
        (self.source/'plugin.json').write_text('{}');(self.source/'main.py').write_text('first')
    def test_install_upgrade_retire_and_preserve_unrelated_plugin(self):
        old=self.root/'markdown';old.mkdir(parents=True);(old/'main.py').write_text('old')
        custom=self.root/'custom';custom.mkdir();(custom/'main.py').write_text('custom')
        bundles.install_bundled(self.package,self.root,self.state)
        self.assertFalse(old.exists());self.assertTrue(custom.exists())
        (self.source/'main.py').write_text('second')
        bundles.install_bundled(self.package,self.root,self.state)
        self.assertEqual((self.root/'demo/main.py').read_text(),'second')
        self.assertTrue(list((self.state/'bundled-backups').glob('demo-*')))
    def test_modified_plugin_preserved_and_new_package_not_overwriting(self):
        bundles.install_bundled(self.package,self.root,self.state)
        (self.root/'demo/main.py').write_text('customized');(self.source/'main.py').write_text('upgrade')
        notes=bundles.install_bundled(self.package,self.root,self.state)
        self.assertEqual((self.root/'demo/main.py').read_text(),'customized')
        self.assertTrue(any('本地修改' in x for x in notes))


if __name__=='__main__':unittest.main(verbosity=2)
