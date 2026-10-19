"""Offline plugin tests: real isolated Python workers, no Telegram or model calls."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import codex_telegram_bridge.plugins.host as h
from codex_telegram_bridge.plugins.api import Context
import codex_telegram_bridge.app as b
class PluginTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.bot=Mock(token='111:secret',chat_id='123',silent=False)
        self.bot._ui_local=threading.local()
        self.bot._reaction=Mock(return_value=True)
        self.bot.send=Mock(return_value={'message_id':99})
        self.host=h.PluginHost(self.bot,self.root/'plugins',self.root/'state')
        self.host.root.mkdir()

    def add(self,ident='demo',code=None,**options):
        folder=self.host.root/ident;folder.mkdir(exist_ok=True)
        manifest=dict(api_version=1,id=ident,version='1',entrypoint='main.py',commands={ident:'测试插件'},**options)
        (folder/'plugin.json').write_text(json.dumps(manifest),encoding='utf-8')
        (folder/'main.py').write_text(code or "from codex_telegram_bridge.plugins.api import *\ndef handle(request, ctx): return result(message('你好'))\n",encoding='utf-8')
        self.host.discover()
        return self.host.plugins.get(ident)

    def execute(self,plugin,request=None):
        with tempfile.TemporaryDirectory(dir=self.root) as folder:
            return self.host.execute(plugin,request or {'kind':'command','argument':''},Path(folder))

    def test_worker_round_trip_without_model_or_secret_environment(self):
        p=self.add(code="import os\nfrom plugin_api import *\ndef handle(request,ctx): return result(message(str(os.environ.get('TEST_PLUGIN_SECRET'))+'|'+request['argument']))")
        with patch.dict(os.environ,TEST_PLUGIN_SECRET='private'):
            value=self.execute(p,{'kind':'command','argument':'hello'})
        self.assertEqual(value['actions'][0]['text'],'None|hello')
        self.bot.app.request.assert_not_called()

    def test_worker_language_and_translated_menu_with_legacy_fallback(self):
        from codex_telegram_bridge.i18n import using_language
        p=self.add(code="from plugin_api import *\ndef handle(request,ctx): return result(message(ctx.language+'|'+ctx.tr('你好','Hello')+'|'+request['argument']))",
                   command_descriptions={'en':{'demo':'Demo plugin'}})
        with using_language('en'):
            self.assertEqual(self.host.menu()[0]['description'],'Demo plugin')
            value=self.execute(p,{'kind':'command','argument':'你好 {p0}'})
            self.assertEqual(value['actions'][0]['text'],'en|Hello|你好 {p0}')
        with using_language('zh'):
            self.assertEqual(self.host.menu()[0]['description'],'测试插件')
        path=self.host.root/'demo/plugin.json';data=json.loads(path.read_text())
        data['command_descriptions']={'en':{'unknown':'Bad'}}
        path.write_text(json.dumps(data));self.host.discover()
        self.assertNotIn('demo',self.host.plugins)

    def test_hot_update_uses_new_code_and_inflight_snapshot_is_immutable(self):
        old=self.add();new=self.add(code="from codex_telegram_bridge.plugins.api import *\ndef handle(request,ctx): return result(message('新版'))")
        self.assertNotEqual(old.revision,new.revision)
        self.assertEqual(self.execute(old)['actions'][0]['text'],'你好')
        self.assertEqual(self.execute(new)['actions'][0]['text'],'新版')

    def test_worker_keeps_bound_profile_when_inherited_environment_changes(self):
        p=self.add(code="import os\nfrom plugin_api import *\ndef handle(request,ctx): return result(message(os.environ['CODEX_HOME']))")
        with patch.object(h,'ROOT',self.root),patch.dict(os.environ,CODEX_HOME=str(self.root/'other-profile')):
            value=self.execute(p)
        self.assertEqual(value['actions'][0]['text'],str(self.root.resolve()))

    def test_disabled_removed_syntax_invalid_and_conflicting_plugins_isolated(self):
        self.add('good');self.add('bad',code='syntax invalid !')
        self.assertIn('good',self.host.plugins);self.assertIn('bad',self.host.errors)
        folder=self.host.root/'good';data=json.loads((folder/'plugin.json').read_text());data['enabled']=False
        (folder/'plugin.json').write_text(json.dumps(data));self.host.discover()
        self.assertNotIn('good',self.host.plugins)
        shutil.rmtree(folder);self.host.discover();self.assertNotIn('good',self.host.plugins)
        self.add('one');self.add('two')
        path=self.host.root/'two/plugin.json';data=json.loads(path.read_text());data['commands']={'one':'冲突'};path.write_text(json.dumps(data))
        self.host.discover();self.assertNotIn('one',self.host.plugins);self.assertNotIn('two',self.host.plugins)

    def test_reserved_commands_and_unknown_api_rejected(self):
        self.add()
        path=self.host.root/'demo/plugin.json';data=json.loads(path.read_text())
        for command in ('quota', 'running', 'language'):
            data['commands']={command:'覆盖'};path.write_text(json.dumps(data));self.host.discover()
            self.assertNotIn('demo',self.host.plugins)
        data['commands']={'demo':'测试'};data['api_version']=99;path.write_text(json.dumps(data));self.host.discover()
        self.assertNotIn('demo',self.host.plugins)

    def test_timeout_terminates_worker_and_exception_does_not_escape(self):
        p=self.add(code='import time\ndef handle(request,ctx): time.sleep(20)',timeout_seconds=1)
        started=time.monotonic()
        with self.assertRaises(TimeoutError):self.execute(p)
        self.assertLess(time.monotonic()-started,10)
        p=self.add(code="def handle(request,ctx): raise RuntimeError('broken')")
        with self.assertRaisesRegex(ValueError,'插件执行失败'):self.execute(p)
        self.assertIn('broken',(self.host.state_root/'demo/last-error.txt').read_text())

    def test_invalid_output_and_attachment_escape_rejected_before_delivery(self):
        p=self.add(code="from codex_telegram_bridge.plugins.api import *\ndef handle(request,ctx): return result({'type':'shell','command':'oops'})")
        with self.assertRaises(ValueError):self.execute(p)
        outside=self.root/'outside.md';outside.write_text('private')
        value={'actions':[{'type':'document','path':str(outside),'filename':'outside.md'}]}
        with self.assertRaisesRegex(ValueError,'output_dir'):self.host.validate(value,self.root/'output')
        self.bot.send.assert_not_called()

    def test_callback_bound_to_chat_panel_revision_and_expiry(self):
        p=self.add();markup,tokens=self.host._buttons(p,[[{'text':'下一页','action':{'page':2}}]],42,99)
        token=tokens[0];self.host.launch=Mock()
        self.host.callback('plug|'+token,99);self.assertEqual(self.host.launch.call_args.args[1]['action'],{'page':2})
        with self.assertRaises(ValueError):self.host.callback('plug|'+token,98)
        self.bot.chat_id='456'
        with self.assertRaises(ValueError):self.host.callback('plug|'+token,99)
        self.bot.chat_id='123'
        with self.host.db() as db:db.execute('UPDATE callbacks SET expires=0 WHERE token=?',(token,))
        with self.assertRaises(ValueError):self.host.callback('plug|'+token,99)
        _,tokens=self.host._buttons(p,[[{'text':'下一页','action':{}}]],42,99)
        self.add(code='def handle(request,ctx): return {}')
        with self.assertRaisesRegex(ValueError,'更新'):self.host.callback('plug|'+tokens[0],99)

    def test_duplicate_request_and_busy_clicks_cannot_start_two_jobs(self):
        p=self.add()
        with patch.object(h.threading,'Thread') as thread:
            self.host.launch(p,{'kind':'command'},42,None,'cmd:123:42')
            with self.assertRaisesRegex(ValueError,'正在处理'):self.host.launch(p,{},42,None,'other')
            self.assertEqual(thread.return_value.start.call_count,1)
        self.host.running.clear()
        with self.assertRaisesRegex(ValueError,'重复'):self.host.launch(p,{},42,None,'cmd:123:42')
        # Persists across service restarts.
        other=h.PluginHost(self.bot,self.host.root,self.host.state_root)
        with self.assertRaisesRegex(ValueError,'重复'):other.launch(p,{},42,None,'cmd:123:42')

    def test_panel_navigation_invalidates_sibling_buttons(self):
        p=self.add();_,tokens=self.host._buttons(p,[[{'text':'下一页','action':{'page':2}},
            {'text':'发送文件','action':{'file':True}}]],42,99)
        with patch.object(h.threading,'Thread'):
            self.host.callback('plug|'+tokens[0],99)
        self.host.running.clear()
        with self.assertRaisesRegex(ValueError,'失效'):self.host.callback('plug|'+tokens[1],99)

    def test_local_config_change_is_loaded_and_changes_callback_revision(self):
        old=self.add();folder=self.host.state_root/'demo';folder.mkdir(parents=True)
        (folder/'config.json').write_text('{"prefix":"changed"}',encoding='utf-8')
        self.host.discover();new=self.host.plugins['demo']
        self.assertNotEqual(old.revision,new.revision);self.assertEqual(new.config['prefix'],'changed')

    def test_bundled_screenshot_discovered_and_markdown_retired(self):
        self.add('markdown')
        self.host.seed_bundled();self.host.discover()
        self.assertIn('screenshot',self.host.plugins)
        self.assertNotIn('markdown',self.host.plugins)
        self.assertNotIn('md',self.host.commands)
        self.assertTrue(list((self.host.state_root/'bundled-backups').glob('markdown-*')))

    def test_ok_reaction_only_after_delivery_failure_not_retried_and_lock_released(self):
        p=self.add();events=[]
        self.bot._reaction.side_effect=lambda emoji:events.append(emoji) or True
        self.host.execute=Mock(return_value={'actions':[{'type':'message','text':'done'}]})
        self.host.deliver=Mock(side_effect=lambda *args:events.append('delivered'))
        self.host.running.add('demo');self.host._run(p,{},42,None)
        self.assertEqual(events,['🤔','delivered','👌']);self.assertFalse(self.host.running)
        events.clear();self.host.deliver=Mock(side_effect=TimeoutError('unknown'))
        self.host._run(p,{},42,None)
        self.assertEqual(events,['🤔','👎']);self.host.deliver.assert_called_once()
        self.assertIn('未自动重试',self.bot.send.call_args.args[0])

    def test_document_and_album_reply_to_command_without_automatic_retry(self):
        p=self.add();path=self.root/'test.md';path.write_bytes(b'original')
        value={'actions':[{'type':'document','path':str(path),'filename':'test.md'}]}
        with patch.object(h.notify,'telegram_multipart_call') as send:
            self.host.deliver(p,value,42,None)
            self.assertEqual(send.call_args.args[2]['reply_parameters']['message_id'],42)
            self.assertEqual(send.call_args.kwargs['content'],b'original')
        with patch.object(h.notify,'telegram_multipart_call',side_effect=TimeoutError()) as send:
            with self.assertRaises(TimeoutError):self.host.deliver(p,value,42,None)
            send.assert_called_once()
        with patch.object(h.notify,'send_screenshot_album') as send:
            self.host.deliver(p,{'actions':[{'type':'photos','items':[{'path':str(path),'width':1,'height':1,'caption':''}]}]},42,None)
            self.assertEqual(send.call_args.args[4],42)

    def test_plugin_command_and_callback_authorization_stays_in_core(self):
        bot=b.TelegramCodexBot('111:secret',{'chat_id':'123'});bot.plugins=Mock();bot.send=Mock()
        with patch.object(b,'telegram_call'),patch.object(b,'log'):
            bot.handle_update({'message':{'chat':{'id':999},'message_id':1,'text':'/demo'}})
            bot.handle_callback({'message':{'chat':{'id':999},'message_id':2},'data':'plug|fake'})
        bot.plugins.command.assert_not_called();bot.plugins.callback.assert_not_called()


if __name__=='__main__':unittest.main(verbosity=2)
