"""In-flight plugin host shutdown contracts; no model, Telegram, or service."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from codex_telegram_bridge.plugins import host as plugins
from codex_telegram_bridge.features.side_chat import SideChats


class PluginShutdownTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.bot = Mock(token='123:fixture', chat_id='456', silent=False)
        self.bot.stop_event = threading.Event()
        self.bot._ui_local = threading.local()
        self.bot._reaction.return_value = True
        self.host = plugins.PluginHost(self.bot, Path(folder.name)/'plugins', Path(folder.name)/'state')
        self.plugin = plugins.Plugin('demo', {}, {}, {}, 'revision')
        self.events = []
        self.threads = []
        self.addCleanup(self.cleanup_threads)

    def event(self):
        event = threading.Event()
        self.events.append(event)
        return event

    def thread(self, target):
        worker = threading.Thread(target=target, daemon=True)
        self.threads.append(worker)
        return worker

    def cleanup_threads(self):
        self.bot.stop_event.set()
        for event in self.events:
            event.set()
        for worker in [*self.threads, *self.host.jobs, self.host.worker]:
            if worker is not None and worker.ident is not None:
                worker.join(timeout=3)

    def test_waits_for_inflight_job_and_watcher_without_holding_host_lock(self):
        job_entered, release_job = self.event(), self.event()
        watch_entered, release_watch = self.event(), self.event()
        stopped = self.event()
        def execute(*args):
            job_entered.set()
            release_job.wait(3)
            return {'actions': []}
        def watch():
            watch_entered.set()
            release_watch.wait(3)
            with self.host.lock:
                pass
        def stop():
            self.host.wait_for_shutdown()
            stopped.set()
        with patch.object(self.host, 'seed_bundled'), patch.object(self.host, 'discover'), \
                patch.object(self.host, '_watch', side_effect=watch), \
                patch.object(self.host, 'execute', side_effect=execute), \
                patch.object(self.host, 'deliver') as deliver:
            self.host.start()
            self.assertTrue(watch_entered.wait(1))
            self.host.launch(self.plugin, {}, 1, None, 'accepted')
            self.assertTrue(job_entered.wait(1))
            self.bot.stop_event.set()
            waiter = self.thread(stop); waiter.start()
            self.assertFalse(stopped.wait(.05))
            release_job.set()
            for job in list(self.host.jobs):
                job.join(timeout=1)
            self.assertEqual(self.host.jobs, set())
            self.assertFalse(stopped.wait(.05))
            release_watch.set()
            self.assertTrue(stopped.wait(1))
            self.assertFalse(self.host.worker.is_alive())
            self.assertEqual(self.host.running, set())
            deliver.assert_called_once()
            with self.assertRaisesRegex(ValueError, '停止'):
                self.host.launch(self.plugin, {}, 2, None, 'rejected')

    def test_shutdown_cannot_miss_job_paused_between_admission_and_thread_start(self):
        original_thread = threading.Thread
        starting, allow_start = self.event(), self.event()
        job_entered, release_job = self.event(), self.event()
        stopping, stopped = self.event(), self.event()
        errors = []
        class DelayedStart(original_thread):
            def start(worker):
                starting.set()
                allow_start.wait(3)
                super().start()
        def execute(*args):
            job_entered.set()
            release_job.wait(3)
            return {'actions': []}
        def launch():
            try:
                self.host.launch(self.plugin, {}, 1, None, 'accepted')
            except Exception as exc:
                errors.append(exc)
        def stop():
            stopping.set()
            self.host.wait_for_shutdown()
            stopped.set()
        launcher, waiter = self.thread(launch), self.thread(stop)
        with patch.object(plugins.threading, 'Thread', DelayedStart), \
                patch.object(self.host, 'execute', side_effect=execute), \
                patch.object(self.host, 'deliver'):
            launcher.start()
            self.assertTrue(starting.wait(1))
            self.bot.stop_event.set()
            waiter.start()
            self.assertTrue(stopping.wait(1))
            self.assertFalse(stopped.wait(.05))
            allow_start.set()
            self.assertTrue(job_entered.wait(1))
            self.assertFalse(stopped.wait(.05))
            release_job.set()
            self.assertTrue(stopped.wait(1))
            launcher.join(timeout=1)
            self.assertEqual(errors, [])
            self.assertEqual(self.host.jobs, set())
            self.assertEqual(self.host.running, set())
            with self.assertRaisesRegex(ValueError, '停止'):
                self.host.launch(self.plugin, {}, 2, None, 'rejected')
            with self.host.db() as db:
                self.assertIsNone(db.execute('SELECT id FROM requests WHERE id=?', ('rejected',)).fetchone())

    def test_side_chat_stop_waits_for_worker_final_reaction_after_app_exit(self):
        request_entered, release_request = self.event(), self.event()
        reaction_entered, release_reaction = self.event(), self.event()
        stopped = self.event()
        app = Mock()
        app.stop.side_effect = release_request.set
        def request(*args, **kwargs):
            request_entered.set()
            release_request.wait(3)
            raise RuntimeError('fixture app stopped')
        def reaction(emoji):
            if emoji == '😢':
                reaction_entered.set()
                release_reaction.wait(3)
            return True
        app.request.side_effect = request
        self.bot._reaction.side_effect = reaction
        side = SideChats(self.bot, lambda *args: app, self.host.state_root/'side.sqlite3')
        self.bot._ui_local.source_message_id = 1
        side.submit({'kind':'parent', 'target':'parent-task', 'parent':'parent-task'}, 'question')
        self.threads.extend(list(side.jobs))
        self.assertTrue(request_entered.wait(1))
        def stop():
            side.stop()
            stopped.set()
        waiter = self.thread(stop); waiter.start()
        self.assertTrue(reaction_entered.wait(1))
        app.stop.assert_called_once()
        self.assertFalse(stopped.wait(.05))
        release_reaction.set()
        self.assertTrue(stopped.wait(1))
        self.assertEqual(side.jobs, set())
        self.bot.send.assert_not_called()
        self.assertEqual([call.args for call in self.bot._reaction.call_args_list], [('🤔',), ('😢',)])
        with self.assertRaisesRegex(ValueError, '停止'):
            side.submit({'kind':'parent', 'target':'parent-task', 'parent':'parent-task'}, 'later')
