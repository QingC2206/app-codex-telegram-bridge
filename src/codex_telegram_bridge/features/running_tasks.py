"""Read-only enumeration and live verification of running local Codex tasks."""
from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Callable


@dataclass
class RunningTasks:
    threads: list[dict[str, Any]]
    notes: list[str]


def _active(status: Any) -> bool:
    return (status.get('type') if isinstance(status, dict) else status) == 'active'


def collect_running_tasks(app: Any, native: Any, *, source_kinds: list[str],
                          owns_thread: Callable[[str], bool],
                          persisted_active: Callable[[str], bool],
                          timeout: float = 20, max_pages: int = 50) -> RunningTasks:
    """Use lifecycle records only to discover candidates, never as live proof.

    Native sidebar state has a 50-item cap, so page through local metadata and
    check older unclosed Turns against the owning Desktop. Never load/resume a
    thread. A failed or bounded scan is visibly incomplete, not an empty list.
    """
    deadline = time.monotonic() + timeout
    metadata = {}
    notes = []
    cursor = None
    cursors = set()
    try:
        for _ in range(max_pages):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('任务范围查询超时')
            params = {'limit': 100, 'sortKey': 'updated_at', 'sortDirection': 'desc',
                      'sourceKinds': source_kinds, 'useStateDbOnly': True}
            if cursor:
                params['cursor'] = cursor
            page = app.request('thread/list', params, timeout=min(8, remaining))
            data = page.get('data')
            if not isinstance(data, list):
                raise ValueError('任务元数据格式无效')
            for entry in data:
                if isinstance(entry, dict) and isinstance(entry.get('id'), str) and entry['id']:
                    metadata.setdefault(entry['id'], entry)
            cursor = page.get('nextCursor')
            if not cursor:
                break
            if not isinstance(cursor, str) or cursor in cursors:
                raise ValueError('任务分页游标重复或无效')
            cursors.add(cursor)
        else:
            raise ValueError('任务范围超过本次核验上限')
    except Exception:
        notes.append('历史任务范围未完整核验，列表可能有遗漏；请刷新重试。')

    if not metadata:
        if notes:
            raise ValueError('任务范围查询失败，无法核验运行状态；请稍后重试。')
        return RunningTasks([], [])
    # Use a real, local thread ID as tool context; Desktop validates this ID.
    native = native.for_current_instance()
    sidebar = native.list_threads(next(iter(metadata)), timeout=min(8, max(0.1, deadline-time.monotonic())))
    local = {}
    for group in ('pinnedThreads', 'threads'):
        entries = sidebar.get(group)
        if not isinstance(entries, list):
            raise ValueError('Desktop 任务列表格式无效，无法核验运行状态。')
        for entry in entries:
            if (isinstance(entry, dict) and entry.get('kind') == 'codex'
                    and entry.get('hostId') == 'local' and isinstance(entry.get('id'), str) and entry['id']):
                local[entry['id']] = entry
    if any(host == 'local' or isinstance(host, dict) and host.get('hostId') == 'local'
           for host in sidebar.get('unavailableHosts', [])):
        raise ValueError('当前 Desktop 本机连接不可用，无法核验运行状态。')
    if any(source == 'codex' or isinstance(source, dict) and source.get('source') == 'codex'
           for source in sidebar.get('unavailableSources', [])):
        raise ValueError('Desktop 的 Codex 任务来源不可用，无法核验运行状态。')

    running = []
    unverified = 0
    for tid in dict.fromkeys([*local, *metadata]):
        if time.monotonic() >= deadline:
            notes.append('本次核验超时，列表可能有遗漏；请刷新重试。')
            break
        entry = metadata.get(tid, {})
        live = local.get(tid)
        own_active = owns_thread(tid) and _active(entry.get('status'))
        if own_active:
            running.append(dict(entry))
            continue
        if live is not None and not _active(live.get('status')):
            continue
        if live is None:
            try:
                if not persisted_active(tid):
                    continue
            except OSError:
                # Migrated history can retain metadata after its rollout was
                # removed. Query Desktop instead of assuming idle or active.
                pass
            except Exception:
                unverified += 1
                continue
        try:
            state = native.read_thread(tid, timeout=min(5, max(0.1, deadline - time.monotonic())))
            thread = state.get('thread')
            if not isinstance(thread, dict) or thread.get('id') != tid or not isinstance(thread.get('status'), dict):
                raise ValueError('Desktop 返回了不匹配的任务状态')
            if not _active(thread['status']):
                continue
            merged = {**entry, **thread}
            if live:
                merged['name'] = live.get('title') or merged.get('name') or merged.get('title')
                merged['cwd'] = live.get('cwd') or merged.get('cwd')
                merged['updatedAt'] = live.get('updatedAt') or merged.get('updatedAt')
            running.append(merged)
        except Exception:
            unverified += 1
    if unverified:
        notes.append(f'另有 {unverified} 项状态未能核实，未计入运行中；请刷新重试。')
    def order(thread: dict[str, Any]) -> tuple[float, str]:
        updated = thread.get('updatedAt')
        return (float(updated) if isinstance(updated, (int, float)) else 0, thread['id'])
    running.sort(key=order, reverse=True)
    return RunningTasks(running, notes)
