"""Running-list regressions; external reads and Telegram sends are mocked."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import ANY, Mock, patch

import codex_telegram_bridge.codex.desktop as d
import codex_telegram_bridge.app as b
import codex_telegram_bridge.telegram.notifications as n
from codex_telegram_bridge.features.running_tasks import RunningTasks, collect_running_tasks


def task(tid, status='active', **fields):
    return {'id': tid, 'name': 'Task ' + tid, 'cwd': 'D:/repo',
            'status': status if isinstance(status, dict) else {'type': status}, **fields}


class RunningQueryTests(unittest.TestCase):
    def setUp(self):
        self.app, self.native = Mock(), Mock()
        self.native.for_current_instance.return_value = self.native
        self.app.request.return_value = {'data': [task('current', 'notLoaded')]}
        self.sidebar = {'threads': [{'id': 'current', 'title': 'Current title', 'status': 'active',
            'kind': 'codex', 'hostId': 'local', 'cwd': 'D:/current'}], 'pinnedThreads': []}
        self.native.list_threads.return_value = self.sidebar
        self.native.read_thread.side_effect = lambda tid, **kw: {'thread': task(tid)}
        self.persisted = Mock(return_value=False)
        self.owns = Mock(return_value=False)

    def query(self, **kwargs):
        return collect_running_tasks(self.app, self.native, source_kinds=['appServer'],
            owns_thread=self.owns, persisted_active=self.persisted, **kwargs)

    def test_live_desktop_overrides_unloaded_metadata_and_keeps_real_title(self):
        result = self.query()
        self.assertEqual([t['id'] for t in result.threads], ['current'])
        self.assertEqual(result.threads[0]['name'], 'Current title')
        self.assertEqual(result.threads[0]['cwd'], 'D:/current')
        self.assertEqual(result.notes, [])
        self.assertTrue(self.app.request.call_args.args[1]['useStateDbOnly'])
        self.native.list_threads.assert_called_once_with('current', timeout=ANY)

    def test_older_running_task_on_later_page_is_not_lost_at_sidebar_limit(self):
        self.sidebar['pinnedThreads'] = [dict(self.sidebar['threads'][0])]
        self.app.request.side_effect = [{'data': [task('current')], 'nextCursor': 'page-2'},
                                        {'data': [task('old', 'notLoaded')]}]
        self.persisted.side_effect = lambda tid: tid == 'old'
        result = self.query()
        self.assertEqual({t['id'] for t in result.threads}, {'current', 'old'})
        self.assertEqual(len(result.threads), 2)
        self.assertEqual(self.app.request.call_args.args[1]['cursor'], 'page-2')
        self.assertEqual([call.args[0] for call in self.app.request.call_args_list], ['thread/list', 'thread/list'])

    def test_unclosed_crashed_turn_is_only_a_candidate_not_live_proof(self):
        self.app.request.return_value = {'data': [task('current'), task('crashed', 'notLoaded')]}
        self.persisted.return_value = True
        self.native.read_thread.side_effect = lambda tid, **kw: {'thread': task(tid, 'idle')}
        result = self.query()
        self.assertEqual(result.threads, [])
        self.assertEqual(result.notes, [])

    def test_ios_background_turn_is_checked_even_when_sidebar_is_not_loaded(self):
        self.sidebar['threads'][0]['status'] = 'notLoaded'
        self.persisted.return_value = True
        result = self.query()
        self.assertEqual([t['id'] for t in result.threads], ['current'])
        self.native.read_thread.assert_called_once()

    def test_follower_without_sidebar_checks_live_state_even_before_history_flush(self):
        self.native.has_sidebar_catalogue = False
        self.persisted.return_value = False
        result = self.query()
        self.assertEqual([t['id'] for t in result.threads], ['current'])
        self.native.list_threads.assert_not_called()
        self.native.read_thread.assert_called_once()

    def test_remote_other_source_and_idle_tasks_are_not_running_local_codex(self):
        self.sidebar['threads'] += [
            {'id': 'remote', 'kind': 'codex', 'hostId': 'other', 'status': 'active'},
            {'id': 'chatgpt', 'kind': 'chatgpt', 'status': 'active'},
            {'id': 'idle', 'kind': 'codex', 'hostId': 'local', 'status': 'idle'}]
        self.query()
        self.assertEqual([c.args[0] for c in self.native.read_thread.call_args_list], ['current'])

    def test_bot_owned_turn_requires_active_status_from_current_backend(self):
        self.sidebar['threads'] = []
        self.app.request.return_value = {'data': [task('owned'), task('stale', 'idle')]}
        self.owns.return_value = True
        self.assertEqual([t['id'] for t in self.query().threads], ['owned'])
        self.native.read_thread.assert_not_called()

    def test_failed_or_mismatched_live_read_is_not_reported_as_no_tasks(self):
        for response in (TimeoutError('offline'), {'thread': task('wrong-task')}):
            with self.subTest(response=response):
                self.native.read_thread.side_effect = response if isinstance(response, Exception) else None
                self.native.read_thread.return_value = response
                result = self.query()
                self.assertEqual(result.threads, [])
                self.assertIn('1 项状态未能核实', ' '.join(result.notes))

    def test_pagination_cycle_retains_confirmed_results_and_warns_about_coverage(self):
        self.app.request.return_value = {'data': [task('current')], 'nextCursor': 'same'}
        result = self.query()
        self.assertEqual([t['id'] for t in result.threads], ['current'])
        self.assertIn('未完整核验', ' '.join(result.notes))
        self.assertEqual(self.app.request.call_count, 2)

    def test_native_disconnect_propagates_instead_of_becoming_an_empty_list(self):
        self.native.list_threads.side_effect = d.DesktopIpcUnavailable('Desktop offline')
        with self.assertRaises(d.DesktopIpcUnavailable): self.query()

    def test_missing_lifecycle_record_is_checked_in_desktop_instead_of_assuming_idle(self):
        self.app.request.return_value = {'data': [task('current'), task('unreadable')]}
        self.persisted.side_effect = OSError('record unavailable')
        self.native.read_thread.side_effect = lambda tid, **kw: {'thread': task(tid, 'active' if tid == 'current' else 'notLoaded')}
        result = self.query()
        self.assertEqual([t['id'] for t in result.threads], ['current'])
        self.assertEqual(result.notes, [])
        self.assertIn('unreadable', [c.args[0] for c in self.native.read_thread.call_args_list])
        with patch.object(b, 'persisted_thread_path', return_value=None):
            self.assertFalse(b.persisted_thread_is_active('missing-record'))
            with self.assertRaises(OSError): b.persisted_thread_is_active('missing-record', strict=True)

    def test_unavailable_local_runtime_does_not_claim_to_be_an_empty_list(self):
        self.sidebar['unavailableHosts'] = ['local']
        with self.assertRaises(ValueError): self.query()

    def test_empty_history_does_not_create_a_fake_task_to_query_desktop(self):
        self.app.request.return_value = {'data': []}
        self.assertEqual(self.query().threads, [])
        self.native.list_threads.assert_not_called()

    def test_budget_expiry_is_marked_incomplete(self):
        clock = [0]
        def sidebar(*args, **kwargs):
            clock[0] = 30
            return self.sidebar
        self.native.list_threads.side_effect = sidebar
        with patch('codex_telegram_bridge.features.running_tasks.time.monotonic', side_effect=lambda: clock[0]):
            result = self.query(timeout=20)
        self.assertEqual(result.threads, [])
        self.assertIn('超时', ' '.join(result.notes))


class RunningPanelTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        for owner, key, value in ((n, 'REACTION_DB', self.root/'reactions.db'),
                                   (b, 'LOG_PATH', self.root/'bot.log')):
            context = patch.object(owner, key, value); context.start(); self.addCleanup(context.stop)
        self.bot = b.TelegramCodexBot('111:fixture', {'chat_id': '123'})
        self.bot.state = b.StateStore(self.root/'state.json')
        self.bot.app, self.bot.desktop, self.bot.send = Mock(), Mock(), Mock()
        context = patch.object(b, 'collect_running_tasks')
        self.collect = context.start(); self.addCleanup(context.stop)
        self.collect.return_value = RunningTasks([task('task-a')], [])

    def test_command_dispatch_and_menu_discoverability(self):
        self.bot.show_task_list = Mock()
        self.bot.handle_text('/running')
        self.bot.show_task_list.assert_called_once_with('active', 0)
        self.bot.plugins.menu = Mock(return_value=[])
        with patch.object(b, 'telegram_call', return_value={}) as api:
            self.assertTrue(self.bot._set_commands())
        commands = api.call_args.args[2]['commands']
        self.assertIn('running', [item['command'] for item in commands])

    def test_waiting_flags_workspace_and_direct_reply_are_shown(self):
        self.collect.return_value = RunningTasks([
            task('approval', status={'type': 'active', 'activeFlags': ['waitingOnApproval']}),
            task('input', status={'type': 'active', 'activeFlags': ['waitingOnUserInput']})], [])
        self.bot.show_task_list('active')
        text, markup = self.bot.send.call_args.args
        self.assertIn('2 项', text); self.assertIn('等待审批', text); self.assertIn('等待输入', text)
        self.assertIn('`repo`', text)
        self.assertIn('direct|approval', str(markup))
        self.assertIn('task|input', str(markup))

    def test_refresh_rechecks_runtime_and_clears_stale_cached_active_task(self):
        self.bot.runtime_status['task-a'] = {'type': 'active'}
        self.bot.active_turns['task-a'] = 'stale-turn'
        self.collect.side_effect = [RunningTasks([task('task-a')], []), RunningTasks([], [])]
        self.bot.show_task_list('active'); self.bot.show_task_list('active')
        self.assertEqual(self.collect.call_count, 2)
        self.assertEqual(self.bot.state.snapshot()['task_aliases'], [])
        self.assertIn('当前没有正在运行', self.bot.send.call_args.args[0])

    def test_pagination_preserves_active_filter_and_aliases(self):
        self.collect.return_value = RunningTasks([task('task-'+str(i)) for i in range(9)], [])
        self.bot.show_task_list('active', 1)
        self.assertEqual(self.bot.state.snapshot()['task_aliases'], ['task-8'])
        markup = str(self.bot.send.call_args.args[1])
        self.assertIn('tasklist|active:1', markup); self.assertIn('tasklist|active:0', markup)

    def test_incomplete_empty_result_does_not_claim_that_no_task_is_running(self):
        self.collect.return_value = RunningTasks([], ['另有 1 项状态未能核实。'])
        self.bot.show_task_list('active')
        text = self.bot.send.call_args.args[0]
        self.assertIn('本次未确认', text); self.assertNotIn('当前没有正在运行', text)

    def test_detail_returns_to_running_page_and_rechecks_desktop_status(self):
        message = {'message_id': 8, 'reply_markup': {'inline_keyboard': [[
            {'text': '刷新', 'callback_data': 'tasklist|active:1'}]]}}
        with patch.object(b, 'send_markdown_message', return_value={'message_id': 80}):
            self.assertEqual(self.bot._open_navigation(message), 80)
        self.assertEqual(n.navigation_list_return(self.bot.token, self.bot.chat_id, 80), 'active:1')
        self.bot._ui_local.message_id = 80
        self.bot.app.request.return_value = {'thread': task('task-a', 'notLoaded')}
        self.bot.desktop.native.read_thread.return_value = {'thread': task('task-a')}
        self.bot.show_task('task-a')
        text, markup = self.bot.send.call_args.args
        self.assertIn('状态：** 运行中', text)
        self.assertIn('tasklist|active:1', str(markup))

    def test_stop_and_steer_selectors_share_live_running_scan(self):
        self.collect.return_value = RunningTasks([task('ios-background')], [])
        for action in ('stop', 'steerpick'):
            self.bot._prompt_task_selection(action, 'select', active_only=True)
            self.assertIn(action + '|ios-background', str(self.bot.send.call_args))
        self.assertEqual(self.collect.call_count, 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
