"""Attach an approved Desktop suggestion to its original completion card."""
from contextlib import closing, contextmanager
import hashlib
import html
import json
import os
import queue
import re
import secrets
import sqlite3
import threading
import time

from ..codex import suggestion_source as source
from ..codex.composer_approval import auth_stamp
from ..codex.desktop import DesktopControl, DesktopIpcInactive, DesktopIpcUnavailable, DesktopSubmissionUnknown
from ..codex.ipc import CodexDesktopIpc
from ..i18n import tr as _t
from ..telegram import notifications as notify
from ..telegram.delivery import TelegramDeliveryUnknown, delivery_error, verified_message


RETENTION = 47 * 3600
POLL_INTERVAL = 15
DELETE_LEASE = 90


def _path():
    return notify.HOOK_DIR / 'telegram_suggested_replies.sqlite3'


@contextmanager
def _database():
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise OSError('Invalid suggestion journal path')
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
    with closing(sqlite3.connect(path, timeout=2)) as db:
        db.row_factory = sqlite3.Row
        with db:
            db.execute('''CREATE TABLE IF NOT EXISTS cards(
                button TEXT PRIMARY KEY, scope TEXT NOT NULL, thread TEXT NOT NULL, turn TEXT NOT NULL,
                created REAL NOT NULL, generation INTEGER NOT NULL, body TEXT NOT NULL,
                state TEXT NOT NULL, due REAL NOT NULL, old_id INTEGER, new_id INTEGER,
                owner TEXT, fingerprint TEXT, touched INTEGER NOT NULL DEFAULT 0, UNIQUE(scope, thread, turn))''')
            db.execute('DELETE FROM cards WHERE created <= ?', (time.time() - RETENTION,))
            yield db


def _provider(config):
    name = config.get('desktop_suggestion_source', source.HOOK_SOURCE)
    if (config.get('include_message_preview') is True and isinstance(name, str)
            and re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}', name)):
        return name
    return ''


def configured(config):
    name = _provider(config)
    try:
        return name == source.HOOK_SOURCE or bool(name and source.installed(name))
    except Exception as exc:
        notify.log_error('Desktop suggestion adapter unavailable: ' + type(exc).__name__)
        return False


def _scope(config, token):
    fields = [str(notify.SOURCE_STATE_ROOT.resolve()), token.partition(':')[0],
              str(config['chat_id']), _provider(config)]
    return hashlib.sha256(json.dumps(fields).encode()).hexdigest()


def _live(config, token, row):
    loaded = notify.load_config()
    return (loaded is not None and loaded[1] == token and _provider(loaded[0]) and
            _scope(*loaded) == row['scope'] == _scope(config, token)
            and not notify.notifications_muted() and row['generation'] == notify.completion_generation()
            and row['created'] + RETENTION > time.time())


def _set(button, previous, state, **fields):
    columns = ['state=?', *(name + '=?' for name in fields)]
    with _database() as db:
        changed = db.execute('UPDATE cards SET ' + ', '.join(columns) + ' WHERE button=? AND state=?',
                             (state, *fields.values(), button, previous))
        return changed.rowcount == 1


def note_user_input(token, chat, thread):
    if not _path().is_file():
        return
    try:
        loaded = notify.load_config()
        if loaded and loaded[1] == token and str(loaded[0].get('chat_id')) == str(chat):
            with _database() as db:
                db.execute('UPDATE cards SET touched=1 WHERE scope=? AND thread=?',
                           (_scope(*loaded), thread))
    except (OSError, sqlite3.Error, ValueError) as exc:
        notify.log_error('Suggestion invalidation unavailable: ' + type(exc).__name__)


def _untouched(key):
    with _database() as db:
        return db.execute('SELECT 1 FROM cards WHERE button=? AND touched=0', (key,)).fetchone() is not None


def _payload(prepared):
    return {key: value for key, value in prepared.items() if not key.startswith('_')}


def _rich_card(body, suggestion, button):
    # Escape all Markdown punctuation, including the start of each quoted line.
    # Telegram may also reject rich Markdown. Prepare its literal HTML fallback
    # before deleting anything and retain the unmodified text for submission.
    title = _t('建议回复')
    escaped = re.sub(r'([!"#$%&\x27()*+,./:;<=>?@\[\\\]^_`{|}~\-])', r'\\\1', suggestion.text)
    quoted = '\n'.join('> ' + line for line in escaped.split('\n'))
    text = body['text'] + '\n\n**' + title + '**\n\n' + quoted
    if notify._telegram_units(text) > notify.MAX_TELEGRAM_MESSAGE:
        return None  # Never show a button that sends more than its visible quote.
    rows = body.get('reply_markup', {}).get('inline_keyboard', [])
    return dict(body, text=text, reply_markup={'inline_keyboard': [*rows, [
        {'text': _t('发送建议回复'), 'callback_data': 'suggest|' + button}]]},
        _suggestion_html=notify.markdown_to_telegram_html(body['text'])
            + '\n\n<b>' + html.escape(title) + '</b>\n<blockquote>' + html.escape(suggestion.text) + '</blockquote>',
        _suggestion_plain=body['text'] + '\n\n' + title + '\n' + suggestion.text)


def _send(token, payload, *, allow_inherited_topic=False):
    outgoing = dict(payload)
    literal = None
    if '_suggestion_html' in outgoing:
        literal = outgoing.pop('_suggestion_html'), outgoing.pop('_suggestion_plain')
    result = verified_message(notify._markdown_request(token, outgoing, edit=False, literal_fallback=literal))
    if not _matches_message(outgoing, token, result, allow_inherited_topic=allow_inherited_topic):
        raise TelegramDeliveryUnknown('Telegram returned an unverified suggestion card destination')
    notify.remember_task_message(token, payload, result)
    return result


def _probe(name, thread, turn, result):
    try:
        result.put_nowait(source.observe(DesktopControl(CodexDesktopIpc), name, thread, turn, keep_pending_owner=True))
    except Exception as exc:
        notify.log_error('Desktop suggestion unavailable: ' + type(exc).__name__)
        result.put_nowait(None)


def send_completion(config, token, request, decision, turn, *, subagent=None):
    """Common hook, notify and deferred delivery. Never wait for generation."""
    name, thread = _provider(config), decision.thread_id
    if (subagent is not None or not name or decision.kind != 'conversation' or not source.ID.fullmatch(thread or '')
            or not source.ID.fullmatch(turn or '') or not configured(config)
            or (name == source.HOOK_SOURCE and auth_stamp(notify.SOURCE_STATE_ROOT) is None)):
        return notify.send_markdown_message(token, request)
    result = queue.Queue(1)
    generation = notify.completion_generation()
    try:
        # Only a cache read, concurrent with media preparation. Slow reads
        # follow the late path on a separate worker.
        threading.Thread(target=_probe, args=(name, thread, turn, result), daemon=True).start()
    except RuntimeError:
        return notify.send_markdown_message(token, request)
    prepared = _payload(notify.prepare_local_image_reply(token, request, upload=True))
    pending = False
    try:
        observation = result.get(timeout=0.05)
    except queue.Empty:
        observation, pending = None, True
    suggestion = observation.suggestion if observation else None
    button = secrets.token_hex(16)
    rich = _rich_card(prepared, suggestion, button) if suggestion else None
    if notify.notifications_muted() or generation != notify.completion_generation():
        return None
    if name == source.HOOK_SOURCE and observation is None and not pending:
        return _send(token, prepared, allow_inherited_topic=True)
    now = time.time()
    try:
        with _database() as db:
            db.execute('''INSERT INTO cards
                (button,scope,thread,turn,created,generation,body,state,due,owner,fingerprint)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)''', (button, _scope(config, token), thread, turn, now,
                generation, json.dumps(prepared, ensure_ascii=False), 'initial', now,
                observation.owner if observation else None, suggestion.fingerprint if rich else None))
    except sqlite3.IntegrityError:
        # The common completion reservation normally prevents this. A previous
        # network outcome may still be unknown, so do not send a second card.
        return None
    except (OSError, sqlite3.Error, ValueError) as exc:
        notify.log_error('Suggestion journal unavailable: ' + type(exc).__name__)
        return _send(token, prepared, allow_inherited_topic=True)
    sent = _send(token, rich or prepared, allow_inherited_topic=True)
    for name, value in _topics(sent).items():
        if value is not None:
            prepared[name] = value
    if pending:
        # A cold Desktop discovery can outlast the initial send. Finish only
        # the observation already started for this completion. A future account
        # can never claim a notification whose original owner was unverified.
        try:
            observation = result.get(timeout=2.5)
        except queue.Empty:
            observation = None
    try:
        state = 'ready' if rich else 'plain' if observation else 'expired'
        _set(button, 'initial', state, owner=observation.owner if observation else None,
             body=json.dumps(prepared, ensure_ascii=False),
             **({'new_id': sent['message_id']} if rich else {'old_id': sent['message_id']}))
    except (OSError, sqlite3.Error) as exc:
        notify.log_error('Delivered completion could not be journaled: ' + type(exc).__name__)
    return sent


def _backoff(row):
    age = time.time() - row['created']
    return time.time() + (POLL_INTERVAL if age < 300 else 120 if age < 3600 else 900)


def wake_hook_card(thread, turn):
    if _path().is_file():
        with _database() as db:
            db.execute("""UPDATE cards SET due=0 WHERE (? IS NULL OR thread=?) AND turn=? AND state='plain'
                          AND owner LIKE 'hook:%' AND touched=0""", (thread, thread, turn))


def poll(config, token, desktop):
    """Bound each pass to one due card. Never run on a Telegram/RPC reader."""
    from ..codex import hook_suggestions
    try:
        hook_suggestions.prune_if_due()
    except (OSError, sqlite3.Error) as exc:
        notify.log_error('Hook suggestion cleanup unavailable: ' + type(exc).__name__)
    if not _path().is_file():
        return
    with _database() as db:
        row = db.execute('''SELECT * FROM cards WHERE scope=? AND state IN ('plain','deleting','deleted')
            AND due<=? ORDER BY due,created LIMIT 1''', (_scope(config, token), time.time())).fetchone()
    if row is None:
        return
    key, state = row['button'], row['state']
    if not _live(config, token, row) or (row['touched'] and state != 'deleted'):
        _set(key, state, 'expired')
        return
    try:
        if state == 'plain' and (row['owner'] or '').startswith('hook:'):
            if not hook_suggestions.has_target(row['thread'], row['turn']):
                _set(key, state, state, due=row['created'] + RETENTION)
                # A hook could have committed and woken the card just as it was parked.
                if hook_suggestions.has_target(row['thread'], row['turn']):
                    wake_hook_card(row['thread'], row['turn'])
                return
        suggestion = source.read_current(desktop, _provider(config), row['thread'], row['turn'], owner=row['owner'])
    except DesktopIpcInactive:
        suggestion = None
        if state == 'plain':
            _set(key, state, 'expired')
            return
    except Exception:
        if state == 'plain':
            _set(key, state, state, due=_backoff(row))
            return
        # On a transient owner read failure keep the old card if deletion has
        # not begun. After a confirmed deletion restore at least the original.
        if state == 'deleting':
            _set(key, state, state, due=_backoff(row))
            return
        suggestion = None
    body = json.loads(row['body'])
    rich = _rich_card(body, suggestion, key) if suggestion else None
    if state == 'plain' and rich is None:
        _set(key, state, state, due=_backoff(row))
        return
    if state == 'plain':
        if not _set(key, state, 'deleting', fingerprint=suggestion.fingerprint):
            return
        state = 'deleting'
    elif suggestion is None or suggestion.fingerprint != row['fingerprint']:
        rich = None
    if state == 'deleting':
        now = time.time()
        with _database() as db:
            claimed = db.execute("""UPDATE cards SET due=? WHERE button=? AND state='deleting' AND due<=?""",
                                 (now + DELETE_LEASE, key, now)).rowcount
        if not claimed or not _live(config, token, row) or not _untouched(key):
            return
        if rich is None:
            # After a lost delete receipt and a changed turn the old message
            # may still be visible. Sending or deleting again would be unsafe.
            _set(key, 'deleting', 'expired')
            return
        try:
            confirmed = notify.telegram_call(token, 'deleteMessages',
                {'chat_id': body['chat_id'], 'message_ids': [row['old_id']]}, timeout=15)
            if confirmed is not True:
                raise RuntimeError('Unconfirmed deletion')
        except Exception as exc:
            outcome, delay = delivery_error(exc)
            _set(key, state, 'expired' if outcome == 'failed' else 'deleting',
                 due=time.time() + (delay or DELETE_LEASE))
            notify.log_error('Suggestion card deletion: ' + outcome)
            return
        if not _set(key, state, 'deleted', due=0):
            return
    if rich is not None:
        try:
            if (not _untouched(key) or
                    source.read_current(desktop, _provider(config), row['thread'], row['turn'], owner=row['owner']) != suggestion):
                rich = None
        except Exception:
            rich = None
    if not _live(config, token, row) or not _set(key, 'deleted', 'sending',
                                                fingerprint=suggestion.fingerprint if rich else None):
        return
    try:
        sent = _send(token, rich or body)
    except Exception as exc:
        outcome, delay = delivery_error(exc)
        if outcome == 'retry':
            _set(key, 'sending', 'deleted', due=time.time() + delay)
        elif outcome == 'failed' and rich:
            # A definitive rejection permits a single restoration of the plain
            # card. A lost acknowledgement never permits another send.
            _set(key, 'sending', 'deleted', fingerprint=None, due=_backoff(row))
        else:
            _set(key, 'sending', 'uncertain' if outcome == 'unknown' else 'failed')
        notify.log_error('Suggestion card delivery: ' + outcome)
        return
    _set(key, 'sending', 'ready' if rich else 'restored', new_id=sent['message_id'])


def _topics(message):
    direct = message.get('direct_messages_topic') or {}
    return {'message_thread_id': message.get('message_thread_id'), 'direct_messages_topic_id': direct.get('topic_id')}


def _matches_message(body, token, message, *, allow_inherited_topic=False):
    chat = message.get('chat') or {}
    author = message.get('from') or {}
    return (str(chat.get('id')) == str(body['chat_id']) and author.get('is_bot') is True
            and str(author.get('id')) == token.partition(':')[0]
            and message.get('business_connection_id') == body.get('business_connection_id')
            and all(value == body.get(field) or (allow_inherited_topic and field not in body)
                    for field, value in _topics(message).items()))


def handle_click(bot, message, button):
    expired = _t('这条建议已发送或失效。')
    if not _path().is_file() or not re.fullmatch(r'[a-f0-9]{32}', button):
        raise ValueError(expired)
    loaded = notify.load_config()
    if not loaded or loaded[1] != bot.token:
        raise ValueError(expired)
    config, token = loaded
    with _database() as db:
        row = db.execute('SELECT * FROM cards WHERE button=? AND scope=?',
                         (button, _scope(config, token))).fetchone()
    message_id = message.get('message_id')
    if (row is None or type(message_id) is not int or message_id <= 0 or
            not row['fingerprint'] or row['touched'] or not _matches_message(json.loads(row['body']), token, message)
            or not _live(config, token, row)):
        raise ValueError(expired)
    if row['state'] in {'initial', 'sending', 'uncertain'} and message_id != row['old_id']:
        buttons = (message.get('reply_markup') or {}).get('inline_keyboard', [])
        if not any(b.get('callback_data') == 'suggest|' + button for group in buttons for b in group):
            raise ValueError(expired)
        if _set(button, row['state'], 'ready', new_id=message_id):
            notify.remember_task_message(token, json.loads(row['body']), message)
    with _database() as db:
        ready = db.execute("SELECT 1 FROM cards WHERE button=? AND state='ready' AND new_id=?",
                           (button, message_id)).fetchone()
    if not ready:
        raise ValueError(expired)
    bot._require_task_actions()
    suggestion = source.read_current(bot.desktop, _provider(config), row['thread'], row['turn'], owner=row['owner'])
    if suggestion is None or suggestion.fingerprint != row['fingerprint']:
        raise ValueError(expired)

    owned = False
    def claim(endpoint):
        nonlocal owned
        # Called after owner discovery and immediately before its single start
        # request. All reads can fail without spending the user's one click.
        bot._require_task_actions()
        current = source.read_current(bot.desktop, _provider(config), row['thread'], row['turn'],
                                      owner=row['owner'], endpoint=endpoint)
        if not _live(config, token, row) or current != suggestion:
            raise ValueError(expired)
        with _database() as db:
            if not db.execute("UPDATE cards SET state='claimed' WHERE button=? AND state='ready' AND touched=0",
                              (button,)).rowcount:
                raise ValueError(expired)
        owned = True

    try:
        result = bot._desktop_follow_up(row['thread'], suggestion.text, '', expected_completed_turn_id=row['turn'],
                                        before_start=claim)
    except DesktopSubmissionUnknown:
        if owned:
            _set(button, 'claimed', 'submission_unknown')
        raise
    except DesktopIpcUnavailable:
        if owned:
            _set(button, 'claimed', 'ready')
        raise
    if result is None:
        raise ValueError(_t('原任务的 Desktop 回复通道尚未就绪，请稍后再试。'))
    _set(button, 'claimed', 'submitted')
    bot._submission_feedback(row['thread'], result.get('turn_id') or '', _t('已发回原对话。'))
