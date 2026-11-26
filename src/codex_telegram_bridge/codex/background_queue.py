"""Background user input for the verified, local Desktop profile."""
from contextlib import contextmanager
import time

from .errors import DesktopBridgeSafetyError, DesktopIpcUnavailable, DesktopSubmissionUnknown
from ..i18n import tr as _t
from ..platforms.local_thread import validate_local_desktop_thread
from .receipts import prompt_digest


def target_status(control, endpoint, thread_id):
    validate_local_desktop_thread(endpoint, thread_id)
    result = control._reader(endpoint).read_thread(thread_id)
    thread = result.get('thread') if isinstance(result, dict) else None
    raw_status = thread.get('status') if isinstance(thread, dict) else None
    status = raw_status.get('type') if isinstance(raw_status, dict) else None
    if (not isinstance(thread, dict) or thread.get('id') != thread_id or thread.get('hostId') != 'local'
            or status not in ('active', 'idle', 'notLoaded')):
        raise DesktopBridgeSafetyError(_t('无法核实原任务的本机 Desktop 状态，未提交消息。'))
    control._check_instance(endpoint, thread_id)
    return status


@contextmanager
def open_queue(control, endpoint, thread_id):
    from ..core.bot import CodexAppServer

    executable, environment = control.access.queue_runtime(endpoint)
    client = CodexAppServer(lambda event: None, lambda request: None,
                            runtime={'codex_executable': str(executable)}, profile_env=environment,
                            stderr_callback=lambda line: None)
    try:
        try:
            client.ensure_started()
            result = client._request('thread/queue/list', {'threadId': thread_id, 'limit': 1},
                                     timeout=min(control.timeout, 10), ensure=False)
            if not isinstance(result.get('data'), list):
                raise ValueError('unknown queue list')
        except (RuntimeError, OSError, ValueError, TimeoutError) as exc:
            raise DesktopIpcUnavailable(_t('当前 Desktop 的后台队列不可用，未提交消息。')) from exc
        yield client
    finally:
        client.stop()


def _matches_digest(entry, client_id, digest):
    if not isinstance(entry, dict) or entry.get('clientUserMessageId') != client_id:
        return False
    items = entry.get('input')
    return (isinstance(entry.get('id'), str) and bool(entry['id'])
            and isinstance(items, list) and len(items) == 1 and isinstance(items[0], dict)
            and items[0].get('type') == 'text' and isinstance(items[0].get('text'), str)
            and prompt_digest('', items[0]['text']) == digest)


def _matches(entry, client_id, text):
    return _matches_digest(entry, client_id, prompt_digest('', text))


def lookup_entry(client, thread_id, client_id, digest, *, seconds=5):
    deadline = time.monotonic()+seconds
    cursor = None
    seen = set()
    found = set()
    while time.monotonic() < deadline:
        params = {'threadId': thread_id, 'limit': 100}
        if cursor is not None:
            params['cursor'] = cursor
        page = client._request('thread/queue/list', params, timeout=max(.01, deadline-time.monotonic()), ensure=False)
        if not isinstance(page, dict) or not isinstance(page.get('data'), list):
            return None
        for entry in page['data']:
            if not isinstance(entry, dict) or entry.get('clientUserMessageId') != client_id:
                continue
            if entry.get('threadId', thread_id) != thread_id or not _matches_digest(entry, client_id, digest):
                return None
            found.add(entry['id'])
        cursor = page.get('nextCursor')
        if cursor is None:
            return next(iter(found)) if len(found) == 1 else None
        if not isinstance(cursor, str) or not cursor or cursor in seen or len(found) > 1:
            return None
        seen.add(cursor)
    return None


def enqueue_once(client, thread_id, client_id, text):
    """The API accepts duplicate client IDs. Never repeat the add, even on disconnect."""
    try:
        result = client._request('thread/queue/add', {
            'threadId': thread_id, 'clientUserMessageId': client_id,
            'input': [{'type': 'text', 'text': text}],
        }, timeout=10, ensure=False)
        entry = result.get('queuedSubmission') if isinstance(result, dict) else None
        if _matches(entry, client_id, text) and entry.get('threadId', thread_id) == thread_id:
            return entry['id']
    except (RuntimeError, OSError, ValueError, TimeoutError):
        pass
    # An acknowledgement can be lost after persistence. A list cannot enqueue
    # or resume; a consumed entry will be reconciled from its actual user item.
    try:
        matched = lookup_entry(client, thread_id, client_id, prompt_digest(thread_id, text), seconds=2)
        if matched:
            return matched
    except (RuntimeError, OSError, ValueError, TypeError):
        pass
    raise DesktopSubmissionUnknown(_t('后台队列回执待核实，未重发。'))
