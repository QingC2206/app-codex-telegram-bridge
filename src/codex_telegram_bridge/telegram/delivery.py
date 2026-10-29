"""Durable outbound delivery and bounded attachments for the app-server client."""
import logging
import json
import math
import mimetypes
from pathlib import Path
import re
import threading
import time

from codex_telegram_bridge.telegram.notifications import TelegramApiError, telegram_multipart_call, _markdown_request
from ..i18n import tr as _t

LOG = logging.getLogger(__name__)
MAX_FILE = 20 * 1024 * 1024


class TelegramDeliveryUnknown(RuntimeError):
    """Telegram may have accepted output; neither fail nor replay the task."""


def delivery_error(exc):
    """Shared transport policy; only an explicit rate limit is retryable."""
    if isinstance(exc, TelegramApiError):
        if exc.code == 429:
            match = re.search(r'retry after (\d+)', exc.description)
            try:
                delay = float(exc.retry_after if exc.retry_after is not None else (match[1] if match else 30))
                if not math.isfinite(delay) or delay < 0:
                    raise ValueError('invalid retry_after')
            except (TypeError, ValueError, OverflowError):
                delay = 30
            return 'retry', max(1, delay)
        return ('failed' if 400 <= exc.code < 500 else 'unknown'), None
    return ('unknown' if isinstance(exc, (OSError, RuntimeError)) else 'failed'), None


def verified_message(result):
    if not isinstance(result, dict) or type(result.get('message_id')) is not int or result['message_id'] <= 0:
        raise TelegramDeliveryUnknown('Telegram returned no verifiable message ID')
    return result


def validate_document(content):
    from .notifications import MAX_CACHED_REPLY_BYTES
    if not isinstance(content, bytes) or len(content) > MAX_CACHED_REPLY_BYTES:
        raise ValueError(_t('文件过大或内容无效，无法发送。'))


class Messenger:
    def __init__(self, state, token, chat, api, *, valid_route=None, route_context=None, upload=telegram_multipart_call):
        self.state, self.token, self.chat, self.api = state, token, chat, api
        self.valid_route = valid_route or (lambda route: True)
        self.route_context = route_context or (lambda route: route)
        self.upload = upload
        self.lock = threading.Lock()

    def flush(self, identifier=None):
        # The only network writer. A 429 pauses later sends as well.
        with self.lock:
            if self.state.get('telegram_retry_at', 0) > time.time():
                return False
            row = self.state.take_message(identifier)
            if row is None:
                return False
            if not self.valid_route(row['route']):
                self.state.message_done(row['id'], 'expired', error='请求已结束')
                return True
            try:
                payload = dict(row['payload'])
                if row['route'].get('completion'):
                    from .notifications import attach_task_reply
                    attach_task_reply(self.token, payload, *row['route']['completion'])
                if payload.pop('_prepare_images', False):
                    from .notifications import prepare_local_image_reply
                    payload = prepare_local_image_reply(self.token, payload, upload=True)
                    # An explicit 429 must not upload the same images again.
                    with self.state.lock, self.state.db:
                        self.state.db.execute('UPDATE outbox SET payload=? WHERE id=?',
                            (json.dumps(payload), row['id']))
                upload = payload.pop('_upload', False)
                if upload:
                    saved = self.state.message_upload(row['id'])
                    if saved is None:
                        raise ValueError('Queued attachment content is unavailable')
                    result = self.upload(self.token, row['method'], payload,
                        file_field='document', filename=saved['filename'], content=saved['content'],
                        content_type=saved['content_type'])
                elif '_file' in payload:
                    path = Path(payload.pop('_file'))
                    if path.stat().st_size > MAX_FILE:
                        raise ValueError('附件过大')
                    result = self.upload(self.token, row['method'], payload,
                        file_field='photo' if row['method']=='sendPhoto' else 'document',
                        filename=path.name, content=path.read_bytes(), content_type=mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
                elif payload.pop('_markdown', False):
                    result = _markdown_request(self.token, payload, edit=row['method']=='editMessageText', api=self.api)
                else:
                    result = self.api(row['method'], payload)
                if row['route'].get('controller') or upload:
                    verified_message(result)
                self.state.message_done(row['id'], 'sent', message_id=(result or {}).get('message_id'))
            except TelegramApiError as exc:
                if row['method'] == 'editMessageText' and exc.code == 400 and 'message is not modified' in exc.description.lower():
                    self.state.message_done(row['id'], 'sent', message_id=row['payload']['message_id'])
                elif exc.code == 429:
                    _, delay = delivery_error(exc)
                    due = time.time()+delay
                    self.state.set('telegram_retry_at', due)
                    self.state.message_done(row['id'], 'retry', due=due, error='Telegram 429')
                else:
                    self.state.message_done(row['id'], delivery_error(exc)[0], error='Telegram '+str(exc.code))
            except (OSError, TimeoutError, RuntimeError) as exc:
                self.state.message_done(row['id'], 'unknown', error=type(exc).__name__)
                LOG.error('Telegram delivery outcome unknown: %s', type(exc).__name__)
            except Exception as exc:
                self.state.message_done(row['id'], 'failed', error=type(exc).__name__)
            return True

    def flush_batch(self, *, limit=8, seconds=1, stop=None):
        """Bound each drain so lifecycle work can run between network batches."""
        started = time.monotonic()
        count = 0
        while count < limit and (count == 0 or time.monotonic()-started < seconds):
            if stop is not None and stop.is_set():
                break
            if not self.flush():
                break
            count += 1
        return count
