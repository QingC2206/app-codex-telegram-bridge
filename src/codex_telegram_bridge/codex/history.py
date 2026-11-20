"""Bounded, read-only hydration through the current paginated history API."""
from __future__ import annotations
from codex_telegram_bridge.i18n import tr as _t

from functools import partial
from typing import Any


def _item_page(request, thread_id, turn_id, cursor, limit, visited):
    params = {'threadId': thread_id, 'turnId': turn_id, 'limit': limit, 'sortDirection': 'desc'}
    if cursor is not None:
        params['cursor'] = cursor
    page = request('thread/items/list', params)
    if not isinstance(page.get('data'), list):
        raise ValueError(_t('任务消息分页返回格式无效。'))
    items = []
    for entry in page['data']:
        if (not isinstance(entry, dict) or entry.get('turnId') != turn_id
                or not isinstance(entry.get('item'), dict)):
            raise ValueError(_t('任务消息与请求的 Turn 不一致。'))
        items.append(entry['item'])
    next_cursor = page.get('nextCursor')
    if next_cursor:
        if not isinstance(next_cursor, str) or next_cursor in visited:
            raise ValueError(_t('任务消息分页游标无效或重复。'))
        visited.add(next_cursor)
    return items, next_cursor


def read_thread_history(app: Any, thread_id: str, *, turn_limit: int = 3,
                        item_limit: int = 160, max_pages: int = 4,
                        timeout: float = 30, cancelled=None) -> dict[str, Any]:
    def request(method, params):
        if cancelled is not None and cancelled():
            raise RuntimeError(_t('任务历史读取已取消。'))
        return app.request(method, params, timeout=timeout)
    result = request('thread/read', {'threadId': thread_id, 'includeTurns': False})
    thread = result.get('thread')
    if not isinstance(thread, dict):
        return result
    thread = dict(thread)
    # Older servers can already supply bounded/legacy history in a summary.
    # Never request includeTurns=True, including on the compatibility path.
    if 'historyMode' not in thread or (thread.get('historyMode') != 'paginated' and isinstance(thread.get('turns'), list)):
        return dict(result, thread=thread)
    page = request('thread/turns/list', {
        'threadId': thread_id, 'limit': turn_limit, 'sortDirection': 'desc',
        'itemsView': 'notLoaded'})
    turns = page.get('data')
    if not isinstance(turns, list):
        raise ValueError(_t('任务历史分页返回格式无效。'))
    hydrated = []
    truncated = False
    for original in turns[:turn_limit]:
        if not isinstance(original, dict) or not isinstance(original.get('id'), str):
            raise ValueError(_t('任务历史缺少明确的 Turn ID。'))
        turn = dict(original)
        collected = []
        cursor = None
        visited = set()
        for _ in range(max_pages):
            items, cursor = _item_page(request, thread_id, turn['id'], cursor,
                                       min(100, item_limit - len(collected)), visited)
            collected.extend(items)
            if not cursor:
                break
            if len(collected) >= item_limit:
                truncated = True
                break
        else:
            truncated = truncated or bool(cursor)
        turn['items'] = list(reversed(collected[:item_limit]))
        turn['_itemsTruncated'] = bool(cursor)
        if cursor:
            turn['_olderItemsCursor'] = cursor
        hydrated.append(turn)
    thread['turns'] = list(reversed(hydrated))
    thread['_historyTruncated'] = truncated
    return dict(result, thread=thread)


def complete_question_history(app, thread_id, turn, cancelled, *, wanted=None):
    """Read backward to the requested questions, keeping questions and typed answers only."""
    if not turn.get('_itemsTruncated'):
        return
    missing = None if wanted is None else set(wanted) - {
        item.get('id') for item in turn['items'] if item.get('type') == 'agentMessage' and item.get('questions')}
    if missing == set():
        return
    cursor = turn.get('_olderItemsCursor')
    if not isinstance(cursor, str) or not cursor:
        raise ValueError(_t('任务消息分页游标无效或重复。'))
    visited = {cursor}
    older = []
    request = partial(app.request, timeout=30)
    while cursor:
        if cancelled():
            raise RuntimeError(_t('任务历史读取已取消。'))
        items, cursor = _item_page(request, thread_id, turn['id'], cursor, 100, visited)
        for item in items:
            if item.get('type') == 'agentMessage' and item.get('questions'):
                if missing is not None:
                    missing.discard(item.get('id'))
            elif not question_answers([item]):
                continue
            older.append(item)
        if missing == set():
            break
    turn['items'][:0] = reversed(older)


def superseded_turns(app, thread_id, current, candidates, cancelled):
    """Confirm that an observed turn precedes the current Desktop turn, even after a long outage."""
    found = set()
    params = {'threadId': thread_id, 'limit': 100, 'sortDirection': 'desc', 'itemsView': 'notLoaded'}
    visited = set()
    while candidates - found:
        if cancelled():
            raise RuntimeError(_t('任务历史读取已取消。'))
        page = app.request('thread/turns/list', params.copy(), timeout=30)
        data = page.get('data')
        if not isinstance(data, list) or any(not isinstance(turn, dict) for turn in data):
            raise ValueError(_t('任务历史分页返回格式无效。'))
        if 'cursor' not in params:
            if not data or data[0].get('id') != current:
                return set()
            data = data[1:]
        found.update(candidates.intersection(turn.get('id') for turn in data))
        cursor = page.get('nextCursor')
        if not cursor:
            break
        if not isinstance(cursor, str) or cursor in visited:
            raise ValueError(_t('任务消息分页游标无效或重复。'))
        visited.add(cursor)
        params['cursor'] = cursor
    return found


REPLY_OPEN = '<send_user_message_question_reply>'
REPLY_CLOSE = '</send_user_message_question_reply>'


def answered_question_ids(items: list[dict[str, Any]]) -> set[str]:
    return set(question_answers(items))


def question_answers(items: list[dict[str, Any]]) -> dict[str, str | None]:
    """Recognize typed Desktop answers, including App Tools delegation receipts."""
    import json
    result = {}
    for item in items:
        if item.get('type') in {'userMessage', 'steeringUserMessage'}:
            content = item.get('content', item.get('input', []))
            texts = [part.get('text', '') for part in content
                     if isinstance(part, dict) and part.get('type') == 'text']
        elif (item.get('type') == 'functionCallOutput' and item.get('namespace') == 'codex_app'
              and item.get('name') == 'send_message_to_thread'):
            output = item.get('output', '')
            if not isinstance(output, str) or '<input>' not in output:
                continue
            body = output.partition('<input>')[2].rpartition('</input>')[0]
            # Match Desktop's one-layer delegation decoder. Do not decode
            # arbitrary HTML entities or decode normal user text twice.
            texts = [body.replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')]
        else:
            continue
        for text in texts:
            text = text.strip()
            if not text.startswith(REPLY_OPEN) or not text.endswith(REPLY_CLOSE):
                continue
            try:
                parsed = json.loads(text[len(REPLY_OPEN):-len(REPLY_CLOSE)])
            except (ValueError, TypeError):
                continue
            for entry in parsed if isinstance(parsed, list) else [parsed]:
                if isinstance(entry, dict) and isinstance(entry.get('questionItemId'), str):
                    answer = entry.get('answer')
                    result[entry['questionItemId']] = answer if isinstance(answer, str) else None
    return result
