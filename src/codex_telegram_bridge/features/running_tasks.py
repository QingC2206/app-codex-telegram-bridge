"""Read-only enumeration and live verification of running local Codex tasks."""
from __future__ import annotations
from codex_telegram_bridge.i18n import tr as _t

from collections import deque
from contextlib import closing
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Callable

RUNNING_SCAN_TIMEOUT = 20.0


@dataclass
class RunningTasks:
    threads: list[dict[str, Any]]
    notes: list[str]


@dataclass
class RunningScanState:
    """Discovery hints and a fair ID queue, never authoritative running state."""
    metadata: dict[str, dict] = field(default_factory=dict)
    cursor: str | None = None
    ordinary: deque = field(default_factory=deque)
    fresh: deque = field(default_factory=deque)
    prefer_fresh: bool = True
    confirmed: dict[str, float] = field(default_factory=dict)
    priority_after: dict[int, str] = field(default_factory=dict)
    priority_level: int = 0
    short_priority_turn: bool = True


class LifecycleCandidates:
    """Read a bounded, rotating batch of rollout tails via the state DB index.

    Missing DB rows/tails are unknown. There is deliberately no recursive glob
    fallback per task, and no lifecycle value is evidence of current activity.
    """
    def __init__(self, profile: Path):
        self.profile = profile.resolve()
        self.queue = deque()
        self.active = set()

    def __call__(self, identifiers, deadline):
        identifiers = set(identifiers)
        self.active.intersection_update(identifiers)
        self.queue = deque(tid for tid in self.queue if tid in identifiers)
        queued = set(self.queue)
        self.queue.extend(sorted(identifiers - queued))
        databases = [p for p in self.profile.glob('state_*.sqlite')
                     if re.fullmatch(r'state_[0-9]+\.sqlite', p.name)]
        if not databases or time.monotonic() >= deadline:
            return set(self.active)
        path = max(databases, key=lambda p: int(p.stem.split('_')[1])).resolve()
        if not path.is_relative_to(self.profile):
            return set(self.active)
        batch = list(self.queue)[:128]
        if not batch:
            return set()
        try:
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True,
                         timeout=min(0.2, max(0.001, deadline-time.monotonic())))) as db:
                db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
                rows = dict(db.execute('SELECT id, rollout_path FROM threads WHERE id IN (' +
                                      ','.join('?' for _ in batch) + ')', batch))
                for tid in batch:
                    if time.monotonic() >= deadline:
                        break
                    self.queue.rotate(-1)
                    active = self._tail_active(tid, rows.get(tid))
                    if active:
                        self.active.add(tid)
                    else:
                        self.active.discard(tid)
        except (OSError, sqlite3.Error, ValueError, RuntimeError):
            pass
        return set(self.active)

    def _tail_active(self, tid, value):
        if not isinstance(value, str) or not value:
            return False
        path = Path(value).resolve()
        if (not path.is_relative_to(self.profile) or not path.name.startswith('rollout-')
                or not path.name.endswith(tid + '.jsonl')):
            return False
        try:
            with path.open('rb') as stream:
                start = max(0, path.stat().st_size - 256 * 1024)
                stream.seek(start)
                data = stream.read(256 * 1024)
            if start:
                data = data.partition(b'\n')[2]
            for line in reversed(data.splitlines()):
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(event, dict) or event.get('type') != 'event_msg':
                    continue
                payload = event.get('payload')
                kind = payload.get('type') if isinstance(payload, dict) else None
                if kind in {'task_started', 'task_complete', 'turn_aborted'}:
                    return kind == 'task_started'
        except OSError:
            pass
        return False


def _rotate_after(values, previous):
    values = list(dict.fromkeys(values))
    if previous in values:
        offset = values.index(previous) + 1
        return values[offset:] + values[:offset]
    return values


def _active(status: Any) -> bool:
    return (status.get('type') if isinstance(status, dict) else status) == 'active'


def collect_running_tasks(app: Any, native: Any, *, source_kinds: list[str],
                          owns_thread: Callable[[str], bool],
                          persisted_active: Callable[[str], bool] | None = None,
                          known_active=(), lifecycle_candidates=None,
                          scan_state: RunningScanState | None = None,
                          timeout: float = RUNNING_SCAN_TIMEOUT, max_pages: int = 50) -> RunningTasks:
    """Prioritize live candidates, reserving time for fair ordinary discovery.

    Metadata gets at most one fifth of the budget. Every refresh checks the
    newest page, then continues an unfinished historical range. Only this
    refresh's live reads are returned; cached hints never prove activity.
    """
    started = time.monotonic()
    deadline = started + timeout
    discovery_deadline = started + min(4, timeout * 0.2)
    state = scan_state if scan_state is not None else RunningScanState()
    notes = []
    metadata = state.metadata
    before = set(metadata) | set(state.ordinary)
    listed = {}
    resume = state.cursor
    cursor = None
    cursors = set()
    complete = False
    resumed = False
    try:
        for page_index in range(max_pages):
            remaining = discovery_deadline - time.monotonic()
            if remaining <= 0:
                break
            params = {'limit': 100, 'sortKey': 'updated_at', 'sortDirection': 'desc',
                      'sourceKinds': source_kinds, 'useStateDbOnly': True}
            if cursor:
                params['cursor'] = cursor
                cursors.add(cursor)
            try:
                # Fresh discovery cannot consume the continuation's whole
                # slice on every refresh, starving historical pages forever.
                page = app.request('thread/list', params,
                                   timeout=min(2 if page_index == 0 else 4, remaining))
            except Exception:
                if page_index == 0 and resume:
                    cursor, resumed = resume, True
                    continue
                raise
            data = page.get('data')
            if not isinstance(data, list):
                raise ValueError(_t('任务元数据格式无效'))
            for entry in data:
                if isinstance(entry, dict) and isinstance(entry.get('id'), str) and entry['id']:
                    listed[entry['id']] = entry
                    metadata[entry['id']] = entry
            following = page.get('nextCursor')
            if not following:
                complete = not resumed
                state.cursor = None
                break
            if not isinstance(following, str) or following in cursors:
                state.cursor = None
                raise ValueError(_t('任务分页游标重复或无效'))
            if page_index == 0 and resume:
                cursor = resume
                resumed = resume != following
            else:
                cursor = following
            state.cursor = cursor
        if complete:
            # A complete fresh enumeration can retire deleted/archived rows.
            metadata.clear()
            metadata.update(listed)
            state.ordinary = deque(tid for tid in state.ordinary if tid in listed)
    except Exception:
        state.cursor = None  # Retry from the head after an invalid/expired cursor.
    if not complete:
        notes.append(_t('历史任务范围未完整核验，列表可能有遗漏；请刷新重试。'))
    # Bound heavyweight rows, retaining discovered IDs in the fair queue so
    # cache eviction cannot silently discard unvisited older candidates.
    while len(metadata) > 5000:
        metadata.pop(next(iter(metadata)))
    known = list(dict.fromkeys(tid for tid in known_active if isinstance(tid, str) and tid))
    if not metadata and not known:
        if notes:
            raise ValueError(_t('任务范围查询失败，无法核验运行状态；请稍后重试。'))
        return RunningTasks([], [])

    native = native.for_current_instance()
    has_sidebar = getattr(native, 'has_sidebar_catalogue', True)
    context = next(iter(metadata), known[0] if known else '')
    sidebar = (native.list_threads(context, timeout=min(4, max(0.1, deadline-time.monotonic())))
               if has_sidebar else {'threads': [], 'pinnedThreads': []})
    local = {}
    for group in ('pinnedThreads', 'threads'):
        entries = sidebar.get(group)
        if not isinstance(entries, list):
            raise ValueError(_t('Desktop 任务列表格式无效，无法核验运行状态。'))
        for entry in entries:
            if (isinstance(entry, dict) and entry.get('kind') == 'codex'
                    and entry.get('hostId') == 'local' and isinstance(entry.get('id'), str) and entry['id']):
                local[entry['id']] = entry
    if any(host == 'local' or isinstance(host, dict) and host.get('hostId') == 'local'
           for host in sidebar.get('unavailableHosts', [])):
        raise ValueError(_t('当前 Desktop 本机连接不可用，无法核验运行状态。'))
    if any(source == 'codex' or isinstance(source, dict) and source.get('source') == 'codex'
           for source in sidebar.get('unavailableSources', [])):
        raise ValueError(_t('Desktop 的 Codex 任务来源不可用，无法核验运行状态。'))

    candidates = dict.fromkeys([*known,
        *(tid for tid, live in local.items() if _active(live.get('status'))),
        *state.ordinary, *listed, *metadata])
    now = time.monotonic()
    state.confirmed = {tid: at for tid, at in state.confirmed.items()
                       if tid in candidates and now-at < 60}
    hint_deadline = min(deadline, now + min(1, timeout * 0.05))
    hints = set()
    if lifecycle_candidates and now < hint_deadline:
        try:
            hints = set(lifecycle_candidates(candidates, hint_deadline)) & candidates.keys()
        except (OSError, ValueError, sqlite3.Error):
            pass  # Hint lookup failure must not hide a live candidate.
    elif persisted_active:
        # Compatibility injection for callers without a batched index. The
        # controller uses LifecycleCandidates, never a recursive lookup per ID.
        for tid in candidates:
            if time.monotonic() >= hint_deadline:
                break
            try:
                if persisted_active(tid):
                    hints.add(tid)
            except Exception:
                pass
    tiers = [known,
             [tid for tid, live in local.items() if _active(live.get('status'))],
             list(state.confirmed),
             [tid for tid in candidates if tid in hints],
             [tid for tid, entry in metadata.items() if _active(entry.get('status'))]]
    priority_queues = []
    priority_ids = set()
    for level, ids in enumerate(tiers):
        group = deque()
        for tid in _rotate_after(ids, state.priority_after.get(level)):
            if tid not in priority_ids:
                group.append(tid)
                priority_ids.add(tid)
        priority_queues.append(group)
    # Round-robin both within and across priority sources. A large set of
    # locally known tasks cannot indefinitely starve Desktop/lifecycle hints.
    priority = []
    level = state.priority_level
    while len(priority) < len(priority_ids):
        if priority_queues[level]:
            priority.append((level, priority_queues[level].popleft()))
        level = (level + 1) % len(tiers)
    retained = set(candidates)
    state.ordinary = deque(tid for tid in state.ordinary if tid in retained)
    queued = set(state.ordinary)
    state.ordinary.extend(tid for tid in candidates if tid not in queued)
    state.fresh = deque(tid for tid in state.fresh if tid in retained)
    fresh_queued = set(state.fresh)
    for tid in reversed(list(listed)):
        if tid not in before and tid not in fresh_queued:
            state.fresh.appendleft(tid)

    running = []
    checked = set()
    unverified = 0

    def verify(tid, until):
        nonlocal unverified
        checked.add(tid)
        if tid in state.fresh:
            state.fresh.remove(tid)
        if tid in state.ordinary:
            state.ordinary.remove(tid)
            state.ordinary.append(tid)
        entry = metadata.get(tid, {})
        live = local.get(tid)
        owned = owns_thread(tid)
        if owned and tid in listed and _active(entry.get('status')):
            running.append(dict(entry))
            state.confirmed[tid] = time.monotonic()
            return
        status = entry.get('status')
        if owned and tid in listed and (status.get('type') if isinstance(status, dict) else status) == 'idle':
            state.confirmed.pop(tid, None)
            return
        try:
            state_read = native.read_thread(tid, timeout=min(5, max(0.001, until-time.monotonic())))
            thread = state_read.get('thread')
            if not isinstance(thread, dict) or thread.get('id') != tid or not isinstance(thread.get('status'), dict):
                raise ValueError(_t('Desktop 返回了不匹配的任务状态'))
            if not _active(thread['status']):
                state.confirmed.pop(tid, None)
                return
            merged = {**entry, **thread}
            if live:
                merged['name'] = live.get('title') or merged.get('name') or merged.get('title')
                merged['cwd'] = live.get('cwd') or merged.get('cwd')
                merged['updatedAt'] = live.get('updatedAt') or merged.get('updatedAt')
            running.append(merged)
            state.confirmed[tid] = time.monotonic()
        except Exception:
            state.confirmed.pop(tid, None)
            unverified += 1

    # Even an indefinitely busy priority set must leave an exploration slot.
    ordinary_ids = retained - priority_ids
    remaining = max(0, deadline-time.monotonic())
    if priority and ordinary_ids and remaining < 10:
        # Two potentially five-second reads cannot fit. Alternate which side
        # gets a full chance, instead of timing one side out on every refresh.
        priority_deadline = deadline if state.short_priority_turn else time.monotonic()
        state.short_priority_turn = not state.short_priority_turn
    else:
        priority_deadline = deadline - min(5, remaining) if ordinary_ids else deadline
    for level, tid in priority:
        if time.monotonic() >= priority_deadline:
            break
        state.priority_after[level] = tid
        state.priority_level = (level + 1) % len(tiers)
        verify(tid, priority_deadline)
    while time.monotonic() < deadline:
        eligible = ordinary_ids - checked
        if not eligible:
            break
        # Alternate new discoveries and the persistent FIFO. This discovers
        # unflushed new activity without starving older unknown candidates.
        while state.fresh and state.fresh[0] not in eligible:
            state.fresh.popleft()
        if state.prefer_fresh and state.fresh:
            tid = state.fresh.popleft()
        else:
            tid = None
            for _ in range(len(state.ordinary)):
                candidate = state.ordinary.popleft()
                state.ordinary.append(candidate)
                if candidate in eligible:
                    tid = candidate
                    break
            if tid is None:
                break
        state.prefer_fresh = not state.prefer_fresh
        verify(tid, deadline)
    # If exploration was cheap, spend its unused budget on deferred priorities.
    for level, tid in priority:
        if tid in checked:
            continue
        if time.monotonic() >= deadline:
            break
        state.priority_after[level] = tid
        state.priority_level = (level + 1) % len(tiers)
        verify(tid, deadline)
    if retained - checked:
        notes.append(_t('本次核验超时，列表可能有遗漏；请刷新重试。'))
    if unverified:
        notes.append(_t('另有 {p0} 项状态未能核实，未计入运行中；请刷新重试。', p0=format(unverified, '')))
    def order(thread: dict[str, Any]) -> tuple[float, str]:
        updated = thread.get('updatedAt')
        return (float(updated) if isinstance(updated, (int, float)) else 0, thread['id'])
    running.sort(key=order, reverse=True)
    return RunningTasks(running, notes)
