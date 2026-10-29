"""Protocol client contracts retained after controller consolidation."""
from pathlib import Path
import sys
import tempfile
import unittest
from codex_telegram_bridge.app_server_bot import thread_options
from codex_telegram_bridge.codex.app_server import AppServerClient, AppServerError

class ClientTests(unittest.TestCase):
    def test_jsonl_initialize_responses_notifications_and_error(self):
        script = '''import sys,json
for line in sys.stdin:
 m=json.loads(line)
 if 'id' not in m: continue
 if m['method']=='bad':
  print(json.dumps({'id':m['id'],'error':{'message':'expected failure'}}),flush=True)
 else:
  print(json.dumps({'id':m['id'],'result':{'ok':True}}),flush=True)
  print(json.dumps({'method':'test/event','params':{}}),flush=True)
'''
        with tempfile.TemporaryDirectory() as directory:
            client = AppServerClient(sys.executable, Path(directory)/'stderr.log', argv=[sys.executable, '-u', '-c', script])
            try:
                self.assertEqual(client.start(), {'ok': True})
                self.assertEqual(client.events.get(timeout=2)['method'], 'test/event')
                with self.assertRaisesRegex(AppServerError, 'expected failure'):
                    client.request('bad')
            finally:
                client.close()
            self.assertIsNotNone(client.process.returncode)

    def test_safe_thread_options_override_global_unrestricted_policy(self):
        options = thread_options('/workspace', {})
        self.assertEqual(options['sandbox'], 'workspace-write')
        self.assertEqual(options['approvalPolicy'], 'on-request')
        self.assertEqual(options['config']['notify'], [])
