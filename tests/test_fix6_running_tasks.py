"""R18 scheduling tests: fake clocks/RPCs and temporary read-only DB inputs."""
from contextlib import closing
from collections import deque
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from codex_telegram_bridge.features.running_tasks import (
    LifecycleCandidates, RunningScanState, collect_running_tasks,
)


def task(tid, status='notLoaded'):
    return {'id': tid, 'status': {'type': status}, 'updatedAt': 1}


class ScanSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.clock = 0.0
        self.reads = []
        self.active = set()
        self.cost = 5
        self.state = RunningScanState()
        self.app, self.native = Mock(), Mock()
        self.native.has_sidebar_catalogue = False
        self.native.for_current_instance.return_value = self.native
        self.native.read_thread.side_effect = self.read
        self.app.request.return_value = {'data': [task(str(i)) for i in range(6)]}
        timer = patch('codex_telegram_bridge.features.running_tasks.time.monotonic',
                      side_effect=lambda: self.clock)
        timer.start(); self.addCleanup(timer.stop)

    def read(self, tid, *, timeout):
        self.reads.append((tid, timeout))
        self.clock += min(self.cost, timeout)
        return {'thread': task(tid, 'active' if tid in self.active else 'idle')}

    def query(self, **kwargs):
        return collect_running_tasks(self.app, self.native, source_kinds=['appServer'],
            owns_thread=lambda _: False, scan_state=self.state, **kwargs)

    def test_old_known_activity_precedes_recent_idle_rows(self):
        self.active = {'5'}
        result = self.query(known_active=['5'])
        self.assertEqual(self.reads[0][0], '5')
        self.assertEqual([t['id'] for t in result.threads], ['5'])
        self.assertLessEqual(self.clock, 20)

    def test_unclosed_lifecycle_is_prioritized_but_does_not_prove_activity(self):
        hints = Mock(return_value={'5'})
        result = self.query(lifecycle_candidates=hints)
        self.assertEqual(self.reads[0][0], '5')
        self.assertEqual(result.threads, [])
        self.assertLessEqual(hints.call_args.args[1], 1)

    def test_recent_confirmation_is_rechecked_and_cleared_when_idle(self):
        self.active = {'5'}
        self.query(known_active=['5'])
        self.active.clear(); self.reads.clear()
        result = self.query()
        self.assertEqual(self.reads[0][0], '5')
        self.assertEqual(result.threads, [])
        self.assertNotIn('5', self.state.confirmed)

    def test_ordinary_candidates_make_progress_across_short_refreshes(self):
        for _ in range(6):
            self.query(timeout=5)
        self.assertEqual([tid for tid, _ in self.reads], list(map(str, range(6))))

    def test_new_unflushed_task_is_found_without_starving_older_candidates(self):
        self.query(timeout=5)
        self.app.request.return_value = {'data': [task('new'), *[task(str(i)) for i in range(6)]]}
        self.active = {'new'}
        results = [self.query(timeout=5), self.query(timeout=5)]
        self.assertTrue(any(t['id'] == 'new' for r in results for t in r.threads))
        self.assertIn('1', [tid for tid, _ in self.reads])
        self.native.list_threads.assert_not_called()

    def test_hot_priority_set_leaves_one_full_read_for_ordinary_discovery(self):
        self.app.request.return_value = {'data': [task('p1'), task('p2'), task('ordinary')]}
        self.query(known_active=['p1', 'p2'], timeout=10)
        self.assertEqual(self.reads, [('p1', 5), ('ordinary', 5)])
        self.reads.clear()
        self.query(known_active=['p1', 'p2'], timeout=10)
        self.assertEqual(self.reads[0][0], 'p2')

    def test_busy_known_tier_cannot_starve_lifecycle_candidates(self):
        self.app.request.return_value = {'data': [*[task(f'p{i}') for i in range(20)],
                                                task('hint'), task('ordinary')]}
        known = [f'p{i}' for i in range(20)]
        self.query(known_active=known, lifecycle_candidates=lambda *_: {'hint'}, timeout=10)
        self.reads.clear()
        self.query(known_active=known, lifecycle_candidates=lambda *_: {'hint'}, timeout=10)
        self.assertEqual(self.reads[0][0], 'hint')

    def test_five_second_budget_still_prioritizes_known_activity(self):
        self.active = {'5'}
        result = self.query(known_active=['5'], timeout=5)
        self.assertEqual([tid for tid, _ in self.reads], ['5'])
        self.assertEqual([t['id'] for t in result.threads], ['5'])

    def test_short_budget_alternates_full_read_opportunities_instead_of_starvation(self):
        self.app.request.return_value = {'data': [task('known'), task('ordinary')]}
        self.active = {'known', 'ordinary'}
        def strict_read(tid, *, timeout):
            result = self.read(tid, timeout=timeout)
            if timeout < 5:
                raise TimeoutError('needs full read budget')
            return result
        self.native.read_thread.side_effect = strict_read
        first = self.query(known_active=['known'], timeout=7)
        second = self.query(known_active=['known'], timeout=7)
        self.assertEqual([t['id'] for t in first.threads], ['known'])
        self.assertEqual([t['id'] for t in second.threads], ['ordinary'])
        self.assertEqual(self.clock, 14)

    def test_pagination_cannot_exhaust_live_verification_budget(self):
        requested = []
        def pages(method, params, *, timeout):
            requested.append(params.get('cursor'))
            self.clock += min(2, timeout)
            cursor = params.get('cursor')
            return {'data': [task(cursor or 'head')], 'nextCursor': str(len(requested))}
        self.app.request.side_effect = pages
        self.active = {'known-old'}
        result = self.query(known_active=['known-old'])
        self.assertEqual(requested, [None, '1'])
        self.assertEqual(self.reads[0][0], 'known-old')
        self.assertIn('known-old', [t['id'] for t in result.threads])
        self.assertTrue(result.notes)
        self.reads.clear(); requested.clear()
        self.query()
        self.assertEqual(requested[:2], [None, '2'])

    def test_slow_fresh_page_does_not_erase_historical_continuation(self):
        self.state.cursor = 'older'
        def pages(method, params, *, timeout):
            if 'cursor' not in params:
                self.clock += timeout
                raise TimeoutError('fresh page slow')
            self.assertEqual(params['cursor'], 'older')
            return {'data': [task('old')], 'nextCursor': None}
        self.app.request.side_effect = pages
        self.active = {'old'}
        result = self.query()
        self.assertEqual([t['id'] for t in result.threads], ['old'])
        self.assertTrue(result.notes)

    def test_cached_owned_active_metadata_is_not_live_proof(self):
        self.state.metadata = {'old': task('old', 'active')}
        self.app.request.side_effect = TimeoutError('metadata unavailable')
        result = collect_running_tasks(self.app, self.native, source_kinds=['appServer'],
            owns_thread=lambda _: True, scan_state=self.state)
        self.assertEqual(result.threads, [])
        self.assertEqual(self.reads[0][0], 'old')

    def test_metadata_eviction_keeps_unvisited_ids_in_fair_rotation(self):
        self.state.metadata = {f'old-{i}': task(f'old-{i}') for i in range(5000)}
        self.state.ordinary = deque(self.state.metadata)
        self.state.prefer_fresh = False
        def page(method, params, *, timeout):
            self.clock += timeout
            return {'data': [task('new')], 'nextCursor': 'later'}
        self.app.request.side_effect = page
        self.query(timeout=5)
        self.assertNotIn('old-0', self.state.metadata)
        self.assertIn('old-0', self.state.ordinary)
        self.assertEqual(self.reads[0][0], 'old-0')

    def test_complete_fresh_enumeration_retires_deleted_cached_ids(self):
        self.state.metadata = {'deleted': task('deleted')}
        self.state.ordinary = deque(['deleted'])
        self.app.request.return_value = {'data': [task('current')]}
        self.query()
        self.assertNotIn('deleted', [tid for tid, _ in self.reads])
        self.assertNotIn('deleted', self.state.ordinary)

    def test_windows_sidebar_activity_precedes_metadata_and_preserves_title(self):
        self.native.has_sidebar_catalogue = True
        self.native.list_threads.return_value = {'pinnedThreads': [{'id': 'pinned',
            'kind': 'codex', 'hostId': 'local', 'status': 'active', 'title': 'Pinned title'}],
            'threads': [{'id': 'sidebar', 'kind': 'codex', 'hostId': 'local', 'status': 'active'}]}
        self.active = {'pinned', 'sidebar'}
        result = self.query()
        self.assertEqual([tid for tid, _ in self.reads[:2]], ['pinned', 'sidebar'])
        self.assertEqual(next(t for t in result.threads if t['id'] == 'pinned')['name'], 'Pinned title')
        self.assertEqual({call.args[0] for call in self.app.request.call_args_list}, {'thread/list'})


class LifecycleCandidateTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.profile = Path(folder.name).resolve()
        self.dbpath = self.profile/'state_5.sqlite'
        with closing(sqlite3.connect(self.dbpath)) as db:
            db.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT)')
        self.reader = LifecycleCandidates(self.profile)

    def put(self, tid, events, *, foreign=False):
        path = self.profile/('rollout-' + tid + '.jsonl')
        path.write_text('\n'.join(json.dumps(event) for event in events) + '\n')
        with closing(sqlite3.connect(self.dbpath)) as db:
            with db:
                db.execute('INSERT INTO threads VALUES (?, ?)', (tid, str(path)))
        return path

    def test_indexed_tails_are_bounded_rotating_and_do_not_recurse(self):
        ids = [f'task-{n:03}' for n in range(129)]
        for tid in ids:
            self.put(tid, [{'type': 'event_msg', 'payload': {'type': 'task_started'}}])
        glob = Path.glob
        patterns = []
        def record(path, pattern):
            patterns.append(pattern)
            return glob(path, pattern)
        with patch.object(Path, 'glob', record):
            first = self.reader(ids, 10**20)
            second = self.reader(ids, 10**20)
        self.assertEqual(len(first), 128)
        self.assertEqual(second, set(ids))
        self.assertEqual(patterns, ['state_*.sqlite', 'state_*.sqlite'])

    def test_completed_and_quoted_start_events_do_not_create_active_hints(self):
        self.put('complete', [{'type': 'event_msg', 'payload': {'type': 'task_started'}},
                              {'type': 'event_msg', 'payload': {'type': 'task_complete'}}])
        self.put('quoted', [{'type': 'response_item', 'payload': {'type': 'task_started'}}])
        self.assertEqual(self.reader(['complete', 'quoted', 'missing'], 10**20), set())

    def test_newest_invalid_database_does_not_fall_back_to_old_active_rows(self):
        self.put('old-task', [{'type': 'event_msg', 'payload': {'type': 'task_started'}}])
        (self.profile/'state_6.sqlite').write_text('not a database')
        self.assertEqual(self.reader(['old-task'], 10**20), set())

    def test_removed_candidate_is_not_retained_as_priority(self):
        self.put('old-task', [{'type': 'event_msg', 'payload': {'type': 'task_started'}}])
        self.assertEqual(self.reader(['old-task'], 10**20), {'old-task'})
        self.assertEqual(self.reader([], 10**20), set())


class ControllerCandidateTests(unittest.TestCase):
    def make_bot(self):
        from codex_telegram_bridge.core import bot as core
        bot = object.__new__(core.TelegramCodexBot)
        bot.live_lock = threading.RLock()
        bot.active_turns = {'owned': 'turn'}
        bot.runtime_status = {'desktop': {'type': 'active'}, 'idle': {'type': 'idle'}}
        bot.pending_questions = {'q': {'params': {'threadId': 'question'}}}
        bot.pending_approvals = {}
        bot.pending_direct_replies = {'d': {'thread_id': 'direct'}}
        bot.app, bot.desktop = Mock(), Mock()
        return core, bot

    def test_controller_shares_scan_state_and_uses_batched_history_hints(self):
        core, bot = self.make_bot()
        with patch.object(core, 'collect_running_tasks') as collect:
            bot._running_tasks()
            bot._running_tasks()
        first, second = (call.kwargs for call in collect.call_args_list)
        self.assertIs(first['scan_state'], second['scan_state'])
        self.assertIs(first['lifecycle_candidates'], second['lifecycle_candidates'])
        self.assertEqual(set(first['known_active']), {'owned', 'desktop', 'question', 'direct'})
        self.assertNotIn('persisted_active', first)

    def test_serial_scan_wait_is_part_of_total_twenty_second_budget(self):
        core, bot = self.make_bot()
        bot._running_scan_state = RunningScanState()
        bot._running_lifecycle = Mock()
        bot._running_scan_lock = Mock()
        clock = [0]
        def acquire(**kwargs):
            self.assertEqual(kwargs['timeout'], 20)
            clock[0] = 15
            return True
        bot._running_scan_lock.acquire.side_effect = acquire
        with patch.object(core.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(core, 'collect_running_tasks') as collect:
            bot._running_tasks()
        self.assertEqual(collect.call_args.kwargs['timeout'], 5)
        bot._running_scan_lock.release.assert_called_once()

    def test_expired_serial_scan_wait_does_not_start_another_scan(self):
        core, bot = self.make_bot()
        bot._running_scan_state = RunningScanState()
        bot._running_lifecycle = Mock()
        bot._running_scan_lock = Mock()
        bot._running_scan_lock.acquire.return_value = False
        with patch.object(core, 'collect_running_tasks') as collect:
            with self.assertRaisesRegex(ValueError, '超时'):
                bot._running_tasks()
        collect.assert_not_called()
        bot._running_scan_lock.release.assert_not_called()


if __name__ == '__main__':
    unittest.main()
