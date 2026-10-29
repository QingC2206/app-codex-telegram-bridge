"""Host-wide exclusion, crash recovery and controller startup contracts."""
import errno
import os
import queue
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from codex_telegram_bridge.platforms import locking
from codex_telegram_bridge.core.bot import TelegramCodexBot
from codex_telegram_bridge.backends.app_server.runtime import AppServerBot


class MachineLockTests(unittest.TestCase):
    def setUp(self):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            self.endpoint = probe.getsockname()
        self.address_patch = patch.object(locking, '_MACHINE_ENDPOINT', self.endpoint)
        self.address_patch.start()
        self.addCleanup(self.address_patch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_names_profiles_data_and_bot_identity_cannot_partition_lock(self):
        first = locking.SingleInstance('first-profile')
        self.addCleanup(first.close)
        self.assertTrue(first.acquire())
        for name in ('second-profile', 'other-bot', 'Local\\LegacyBot'):
            with self.subTest(name=name), patch.dict(os.environ, {
                'CODEX_HOME':self.temp.name+'/other-profile',
                'CODEX_TELEGRAM_DATA_DIR':self.temp.name+'/other-data',
                'USER':'another-user', 'USERNAME':'another-user'}):
                other = locking.SingleInstance(name)
                self.addCleanup(other.close)
                self.assertFalse(other.acquire())
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_reservation_does_not_listen_and_is_not_inherited_by_exec(self):
        with locking.SingleInstance() as first:
            with socket.socket() as probe:
                probe.settimeout(0.2)
                self.assertNotEqual(probe.connect_ex(self.endpoint),0)
            self.assertFalse(first.handle.get_inheritable())
            descriptor = first.handle.fileno()
            # A child holding ordinary inherited handles must not own this one.
            code = 'import socket,sys\ntry: socket.socket(fileno=int(sys.argv[1])).getsockname()\nexcept OSError: sys.exit(0)\nsys.exit(1)'
            child = subprocess.run([sys.executable,'-c',code,str(descriptor)],close_fds=False,
                                   capture_output=True,timeout=10)
            self.assertEqual(child.returncode,0,child.stderr)

    def test_conflicting_socket_cannot_force_address_reuse(self):
        with locking.SingleInstance():
            with socket.socket() as other:
                other.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
                with self.assertRaises(OSError):other.bind(self.endpoint)

    def test_process_with_other_configuration_is_blocked_then_acquires_after_crash(self):
        code = '''import os,sys
from codex_telegram_bridge.platforms import locking
locking._MACHINE_ENDPOINT=('127.0.0.1',int(sys.argv[1]))
lock=locking.SingleInstance(sys.argv[2])
print('acquired' if lock.acquire() else 'blocked',flush=True)
if sys.argv[2]=='holder':sys.stdin.readline()
lock.close()
'''
        first = subprocess.Popen([sys.executable,'-u','-c',code,str(self.endpoint[1]),'holder'],
                                 stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.addCleanup(lambda: first.kill() if first.poll() is None else None)
        self.assertEqual(first.stdout.readline().strip(),'acquired')
        other_env={**os.environ,'CODEX_HOME':self.temp.name+'/profile-b',
                   'CODEX_TELEGRAM_DATA_DIR':self.temp.name+'/data-b'}
        second=subprocess.run([sys.executable,'-c',code,str(self.endpoint[1]),'different-profile-and-bot'],
                              env=other_env,capture_output=True,text=True,timeout=10)
        self.assertEqual(second.stdout.strip(),'blocked',second.stderr)
        first.kill();first.communicate(timeout=10)
        with locking.SingleInstance() as recovered:self.assertIsNotNone(recovered.handle)

    @unittest.skipUnless(hasattr(os,'fork'),'POSIX fork inheritance')
    def test_fork_child_cannot_keep_parent_reservation(self):
        with locking.SingleInstance() as holder:
            read_fd,write_fd=os.pipe()
            child=os.fork()
            if child==0:
                os.close(read_fd)
                os.write(write_fd,b'closed' if holder.handle is None else b'inherited')
                os.close(write_fd);os._exit(0)
            os.close(write_fd)
            try:self.assertEqual(os.read(read_fd,32),b'closed')
            finally:os.close(read_fd);os.waitpid(child,0)
            self.assertIsNotNone(holder.handle)

    def test_windows_requests_exclusive_binding_before_bind(self):
        handle=Mock();factory=Mock(return_value=handle)
        with patch.object(locking.socket,'socket',factory),patch.object(locking.os,'name','nt'), \
             patch.object(locking.socket,'SO_EXCLUSIVEADDRUSE',-5,create=True):
            instance=locking.SingleInstance()
            self.assertTrue(instance.acquire());instance.close()
        self.assertEqual(handle.method_calls[:3],[
            unittest.mock.call.set_inheritable(False),
            unittest.mock.call.setsockopt(socket.SOL_SOCKET,-5,1),
            unittest.mock.call.bind(self.endpoint)])
        handle.listen.assert_not_called()

    def test_unexpected_bind_failure_closes_handle_and_is_not_silenced(self):
        handle=Mock();handle.bind.side_effect=OSError(errno.ENETDOWN,'network unavailable')
        with patch.object(locking.socket,'socket',return_value=handle):
            instance=locking.SingleInstance()
            with self.assertRaises(OSError):instance.acquire()
        handle.close.assert_called_once();self.assertIsNone(instance.handle)

    def test_both_controller_entrypoints_reject_before_workers_or_delivery(self):
        with locking.SingleInstance():
            for cls in (TelegramCodexBot,AppServerBot):
                with self.subTest(backend=cls.__name__):
                    bot=object.__new__(cls);bot._run_service=Mock();bot.shutdown=Mock()
                    with patch('codex_telegram_bridge.backends.app_server.runtime.migrate_controller') as migration:
                        with self.assertRaisesRegex(RuntimeError,'27321'):bot.run(quiet_start=True)
                    bot._run_service.assert_not_called();migration.assert_not_called()

    def test_controller_holds_reservation_until_shutdown_finishes(self):
        bot=object.__new__(TelegramCodexBot)
        bot._run_service=Mock(return_value=0)
        observed=[]
        def shutdown():
            other=locking.SingleInstance();observed.append(other.acquire());other.close()
        bot.shutdown=shutdown
        self.assertEqual(bot.run(quiet_start=True),0)
        self.assertEqual(observed,[False])
        with locking.SingleInstance():pass

    def test_context_releases_after_startup_exception(self):
        with self.assertRaisesRegex(ValueError,'startup'):
            with locking.SingleInstance():raise ValueError('startup')
        with locking.SingleInstance():pass

    def test_hook_entrypoints_remain_available_while_controller_holds_lock(self):
        from codex_telegram_bridge import cli
        with locking.SingleInstance(), patch('codex_telegram_bridge.paths.ensure_data_dir',return_value=Path(self.temp.name)), \
             patch('codex_telegram_bridge.management.desktop_hook.main',return_value=0) as desktop_hook, \
             patch('codex_telegram_bridge.telegram.notifications.main',return_value=0) as notify, \
             patch('codex_telegram_bridge.management.runtime.load_settings',return_value={'backend':'desktop'}) as settings, \
             patch.object(sys,'argv',['isolated-cli']):
            self.assertEqual(cli.main(['desktop-notify']),0)
            self.assertEqual(cli.main(['notify']),0)
            settings.return_value={'backend':'app-server'}
            self.assertEqual(cli.main(['notify']),0)
        self.assertEqual(desktop_hook.call_count,2);notify.assert_called_once()

    def test_shared_controller_waits_for_inflight_worker_before_releasing_lock(self):
        bot=object.__new__(TelegramCodexBot)
        started, stopping, release = threading.Event(), threading.Event(), threading.Event()
        results=[]
        def network_work():
            started.set();release.wait(5)
        def service(**kwargs):
            bot._start_service_worker(network_work, 'isolated-inflight-send')
            self.assertTrue(started.wait(2))
            return 0
        bot._run_service=service
        bot.shutdown=lambda:stopping.set()
        runner=threading.Thread(target=lambda:results.append(bot.run(quiet_start=True)))
        runner.start()
        try:
            self.assertTrue(stopping.wait(2))
            self.assertTrue(runner.is_alive())
            second=locking.SingleInstance()
            try:self.assertFalse(second.acquire())
            finally:second.close()
        finally:
            release.set();runner.join(3)
        self.assertFalse(runner.is_alive())
        self.assertEqual(results,[0])
        with locking.SingleInstance():pass

    def test_cancelled_but_inflight_timer_keeps_machine_lock_until_exit(self):
        bot=object.__new__(TelegramCodexBot)
        bot.stop_event=threading.Event();bot.live_lock=threading.RLock()
        bot.quota_monitor_lock=threading.RLock()
        bot.watch_timers={};bot.watch_poll_timers={};bot.outbox=queue.Queue()
        bot.sides=Mock();bot.app=Mock()
        entered,release,stopping=threading.Event(),threading.Event(),threading.Event()
        bot.sides.stop.side_effect=stopping.set
        def network():entered.set();release.wait(5)
        def service(**kwargs):
            bot.quota_monitor_timer=threading.Timer(0,network)
            bot.quota_monitor_timer.start()
            self.assertTrue(entered.wait(2))
            return 0
        bot._run_service=service
        runner=threading.Thread(target=bot.run)
        runner.start()
        try:
            self.assertTrue(stopping.wait(2));runner.join(.05)
            self.assertTrue(runner.is_alive())
            second=locking.SingleInstance()
            try:self.assertFalse(second.acquire())
            finally:second.close()
        finally:release.set();runner.join(3)
        self.assertFalse(runner.is_alive())
        with locking.SingleInstance():pass

    def test_client_restarted_by_inflight_request_is_closed_before_unlock(self):
        bot=object.__new__(TelegramCodexBot)
        bot.app=Mock(process=None)
        stopped=threading.Event()
        def close():bot.app.process=None;stopped.set()
        bot.app.stop.side_effect=close
        bot.shutdown=bot.app.stop
        def pending_request():
            if not stopped.wait(2):raise AssertionError('shutdown did not start')
            bot.app.process=Mock();bot.app.process.poll.return_value=None
        def service(**kwargs):
            bot._start_service_worker(pending_request,'isolated-admitted-request')
            return 0
        bot._run_service=service
        self.assertEqual(bot.run(),0)
        self.assertEqual(bot.app.stop.call_count,2)
        self.assertIsNone(bot.app.process)
        with locking.SingleInstance():pass

    def test_shared_shutdown_error_cannot_skip_pending_hook_writer(self):
        bot=object.__new__(AppServerBot)
        bot.legacy=Mock();bot.settings={};bot.state=Mock();bot.state.snapshot.return_value={}
        bot.token='111:isolated';bot.hook_worker=None
        entered,release,stopping=threading.Event(),threading.Event(),threading.Event()
        def hook():entered.set();release.wait(5)
        bot._hook_loop=hook;bot.shutdown=stopping.set
        bot._wait_service_workers=Mock(side_effect=RuntimeError('shared cleanup failed'))
        def service(*args,**kwargs):
            if not entered.wait(2):raise AssertionError('hook worker did not start')
            return 0
        errors=[]
        def run():
            try:bot.run()
            except BaseException as exc:errors.append(exc)
        with patch('codex_telegram_bridge.backends.app_server.runtime.migrate_controller'), \
             patch('codex_telegram_bridge.telegram.controller_delivery.ControllerDelivery'), \
             patch('codex_telegram_bridge.backends.app_server.runtime.notify.telegram_call',return_value={}), \
             patch.object(TelegramCodexBot,'_run_service',side_effect=service):
            runner=threading.Thread(target=run);runner.start()
            try:
                self.assertTrue(stopping.wait(2));runner.join(.05)
                self.assertTrue(runner.is_alive())
                other=locking.SingleInstance()
                try:self.assertFalse(other.acquire())
                finally:other.close()
                bot.legacy.close.assert_not_called()
            finally:release.set();runner.join(3)
        self.assertFalse(runner.is_alive());bot.legacy.close.assert_called_once()
        self.assertEqual([str(exc) for exc in errors],['shared cleanup failed'])
        with locking.SingleInstance():pass


if __name__=='__main__':unittest.main()
