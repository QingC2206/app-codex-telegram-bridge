"""Durable Telegram source identities for at-most-once Desktop queue submissions."""
from contextlib import closing
from pathlib import Path
import sqlite3
import time
import uuid

from ..codex import receipts
from ..codex.errors import DesktopBridgeSafetyError, DesktopSubmissionUnknown
from ..i18n import tr as _t
from ..telegram import notifications as notify


def key(token, chat, message):
    return token.split(':', 1)[0], str(chat), message


def existing(source, thread, text):
    with closing(notify._reaction_db()) as db:
        row = db.execute("SELECT s.thread,s.digest,s.turn,q.queue_id FROM desktop_queue_sends q "
                         "JOIN desktop_submissions s USING(bot,chat,message) WHERE (q.bot,q.chat,q.message)=(?,?,?)",
                         source).fetchone()
    if not row:
        return None
    if row[:2] != (thread, receipts.prompt_digest(thread, text)):
        raise DesktopBridgeSafetyError(_t('该 Telegram 消息已有其他提交关联。'))
    return 'received' if row[2] else ('queued' if row[3] else 'unknown')


def claim(source, thread, text, *, before_write=None):
    if type(source[2]) is not int or source[2] <= 0:
        raise DesktopBridgeSafetyError(_t('无法核实 Telegram 消息编号，未提交消息。'))
    path, offset = receipts.checkpoint(thread, notify.SESSION_ROOT)
    if not path:
        raise DesktopBridgeSafetyError(_t('无法定位原任务的接收记录，未提交消息。'))
    client_id = str(uuid.uuid4())
    with closing(notify._reaction_db()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM desktop_queue_sends WHERE (bot,chat,message)=(?,?,?)', source).fetchone():
            return None
        if db.execute('SELECT 1 FROM desktop_submissions WHERE (bot,chat,message)=(?,?,?)', source).fetchone():
            raise DesktopSubmissionUnknown(_t('该 Telegram 消息已有提交记录，未重发。'))
        if before_write is not None:
            before_write(db)
        db.execute('INSERT INTO desktop_submissions VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (*source, thread, path, offset, receipts.prompt_digest(thread, text), time.time(), '', ''))
        db.execute('INSERT INTO desktop_queue_sends(bot,chat,message,client) VALUES(?,?,?,?)', (*source, client_id))
    return client_id


def acknowledge(source, queue_id):
    with closing(notify._reaction_db()) as db, db:
        db.execute('UPDATE desktop_queue_sends SET queue_id=? WHERE (bot,chat,message)=(?,?,?)', (queue_id, *source))


def recheck_uncertain(bot):
    """A fresh verified reader can see a persisted item even while its chat is unloaded."""
    from ..codex import background_queue
    now = time.time()
    with closing(notify._reaction_db()) as db:
        row = db.execute("SELECT q.client,s.thread,s.digest,s.created FROM desktop_queue_sends q "
                         "JOIN desktop_submissions s USING(bot,chat,message) LEFT JOIN desktop_queue_retry f USING(client) "
                         "WHERE q.bot=? AND q.chat=? AND q.queue_id='' AND s.turn='' AND s.created<? "
                         "AND coalesce(f.probe_after,0)<=? ORDER BY coalesce(f.probe_after,0),s.created LIMIT 1",
                         (bot.token.split(':', 1)[0], str(bot.chat_id), now-10, now)).fetchone()
        if not row:
            return
        client_id, thread, digest, created = row
        with db:
            db.execute("INSERT INTO desktop_queue_retry(client,probe_after) VALUES(?,?) ON CONFLICT(client) "
                       "DO UPDATE SET probe_after=excluded.probe_after", (client_id, now+min(600,max(15,(now-created)/4))))
        try:
            endpoint = bot.desktop.native.discover()
            background_queue.target_status(bot.desktop, endpoint, thread)
            with background_queue.open_queue(bot.desktop, endpoint, thread) as client:
                match = background_queue.lookup_entry(client, thread, client_id, digest)
            bot.desktop._check_instance(endpoint, thread)
        except (OSError, RuntimeError, ValueError, TimeoutError):
            return
        if match:
            with db:
                changed = db.execute("UPDATE desktop_queue_sends SET queue_id=? WHERE client=? AND queue_id=''",
                                     (match, client_id)).rowcount
                if changed:
                    db.execute("UPDATE desktop_queue_retry SET queue_notice=1 WHERE client=?", (client_id,))


def release_before_send(source, client_id, *, durable=False):
    """Only the claimant may release, and only before invoking the mutating RPC."""
    with closing(notify._reaction_db()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        removed = db.execute("DELETE FROM desktop_queue_sends WHERE (bot,chat,message)=(?,?,?) AND client=? AND queue_id=''",
                             (*source, client_id)).rowcount
        if removed:
            db.execute("DELETE FROM desktop_submissions WHERE (bot,chat,message)=(?,?,?) AND turn=''", source)
            if durable:
                db.execute("UPDATE desktop_inbox SET phase='ready' WHERE (bot,chat,message)=(?,?,?) AND phase='sending'", source)


def remember_card(source, message_id):
    if type(message_id) is int and message_id > 0:
        with closing(notify._reaction_db()) as db, db:
            db.execute('UPDATE desktop_queue_sends SET card=?,checked=0 WHERE (bot,chat,message)=(?,?,?)',
                       (message_id, *source))


def card_limited(source, error, *, sending):
    from ..telegram.delivery import delivery_error
    due = time.time()+delivery_error(error)[1]
    with closing(notify._reaction_db()) as db, db:
        db.execute("INSERT INTO desktop_queue_retry(client,card_after) SELECT client,? FROM desktop_queue_sends "
                   "WHERE (bot,chat,message)=(?,?,?) ON CONFLICT(client) DO UPDATE SET card_after=excluded.card_after",
                   (due, *source))
        if sending:
            db.execute("UPDATE desktop_queue_sends SET card=NULL WHERE (bot,chat,message)=(?,?,?) AND card=-1", source)


def reconcile(token, thread=None, *, update_reactions=True, deadline=None):
    timeout = 30 if update_reactions else 0
    with closing(notify._reaction_db(timeout=timeout)) as db:
        sql = ("SELECT q.chat,q.message,q.client,s.thread,s.path,s.offset,s.digest FROM desktop_queue_sends q "
               "JOIN desktop_submissions s USING(bot,chat,message) WHERE q.bot=? AND s.turn=''")
        params = [token.split(':', 1)[0]]
        if thread:
            sql += ' AND s.thread=?'
            params.append(thread)
        rows = db.execute(sql + ' ORDER BY q.checked,q.message LIMIT 32', params).fetchall()
        for chat, message, client_id, target, old_path, old_offset, digest in rows:
            if deadline is not None and time.monotonic() >= deadline:
                break
            source = key(token, chat, message)
            with db:
                db.execute('UPDATE desktop_queue_sends SET checked=? WHERE client=?', (time.time(), client_id))
            try:
                path = Path(old_path)
                if not path.is_file():
                    path = receipts.session_path(target, notify.SESSION_ROOT)
                if not path:
                    continue
                offset = old_offset if str(path) == old_path and old_offset <= path.stat().st_size else 0
                items, end = receipts.read_receipts(path, offset, thread_id=target, client_id=client_id)
                for item in items:
                    if item['digest'] == digest and notify.bind_desktop_submission(
                            token, chat, message, target, item['turn_id'], item['item_id'],
                            client_id=client_id, update_reaction=update_reactions):
                        break
                else:
                    if (str(path), end) != (old_path, old_offset):
                        with db:
                            db.execute("UPDATE desktop_submissions SET path=?,offset=? WHERE (bot,chat,message)=(?,?,?) "
                                       "AND turn='' AND path=? AND offset=?", (str(path), end, *source, old_path, old_offset))
            except (OSError, sqlite3.Error) as exc:
                notify.log_error('Desktop queue receipt unavailable: ' + type(exc).__name__)


def update_cards(bot):
    with closing(notify._reaction_db()) as db:
        # A restart can land between the durable claim and the first Telegram
        # status card. Recover feedback without ever submitting another input.
        now = time.time()
        missing = db.execute("SELECT q.message,s.thread,s.turn,q.queue_id FROM desktop_queue_sends q "
                             "JOIN desktop_submissions s USING(bot,chat,message) LEFT JOIN desktop_queue_retry f USING(client) "
                             "WHERE q.bot=? AND q.chat=? AND q.card IS NULL AND coalesce(f.card_after,0)<=? "
                             "AND (s.created<? OR f.card_after>0) ORDER BY s.created LIMIT 16",
                             (bot.token.split(':', 1)[0], str(bot.chat_id), now, now-90)).fetchall()
        for message, thread, turn, queue_id in missing:
            source = key(bot.token, bot.chat_id, message)
            with db:
                claimed = db.execute('UPDATE desktop_queue_sends SET card=-1 WHERE (bot,chat,message)=(?,?,?) AND card IS NULL',
                                     source).rowcount
            if not claimed:
                continue
            state = 'received' if turn else ('queued' if queue_id else 'unknown')
            try:
                result = bot.send(status_text(thread, state), task_keyboard(thread), reply_to=message)
            except Exception as exc:
                if isinstance(exc, notify.TelegramApiError) and exc.code == 429:
                    card_limited(source, exc, sending=True)
                notify.log_error('Desktop queue recovery card unavailable: ' + type(exc).__name__)
                continue
            if state == 'received':
                with db:
                    db.execute('UPDATE desktop_queue_sends SET card=0 WHERE (bot,chat,message)=(?,?,?) AND card=-1', source)
            elif isinstance(result, dict):
                remember_card(source, result.get('message_id'))
        rows = db.execute("SELECT q.chat,q.message,q.card,s.thread,s.turn,q.client FROM desktop_queue_sends q "
                          "JOIN desktop_submissions s USING(bot,chat,message) LEFT JOIN desktop_queue_retry f USING(client) "
                          "WHERE q.bot=? AND q.chat=? AND q.card>0 AND coalesce(f.card_after,0)<=? "
                          "AND ((s.turn<>'' AND q.checked<?) OR f.queue_notice=1) ORDER BY q.checked LIMIT 16",
                          (bot.token.split(':', 1)[0], str(bot.chat_id), now, now-5)).fetchall()
        for chat, message, card, thread, turn, client_id in rows:
            source = key(bot.token, chat, message)
            with db:
                db.execute('UPDATE desktop_queue_sends SET checked=? WHERE (bot,chat,message)=(?,?,?)', (time.time(), *source))
            try:
                bot.edit_message(card, status_text(thread, 'received' if turn else 'queued'), task_keyboard(thread), chat_id=chat)
            except notify.TelegramApiError as exc:
                if exc.code == 429:
                    card_limited(source, exc, sending=False)
                    continue
                if exc.code != 400 or not any(s in exc.description.lower() for s in ('message is not modified', 'message to edit not found')):
                    continue
            except Exception as exc:
                notify.log_error('Desktop queue card edit unavailable: ' + type(exc).__name__)
                continue
            with db:
                db.execute('UPDATE desktop_queue_retry SET queue_notice=0,card_after=0 WHERE client=?', (client_id,))
                if turn:
                    db.execute('UPDATE desktop_queue_sends SET card=0 WHERE (bot,chat,message)=(?,?,?) AND card=?', (*source, card))


def status_text(thread, state, status=''):
    if state == 'received':
        return _t('💬 原任务 {task} 已接收。', task=thread[:8])
    if state == 'unknown':
        return _t('⚠️ 任务 {task} 的排队回执待核实，未重发。', task=thread[:8])
    if status == 'notLoaded':
        return _t('📥 已排队 · {task}。Desktop 加载原任务后处理。', task=thread[:8])
    if status == 'active':
        return _t('📥 已排队 · {task}。当前轮结束后处理。', task=thread[:8])
    return _t('📥 已排队 · {task}。等待原任务接收。', task=thread[:8])


def task_keyboard(thread):
    return {'inline_keyboard': [[{'text': _t('查看详情'), 'callback_data': 'task|' + thread},
                                 {'text': _t('💬 再次回复'), 'callback_data': 'direct|' + thread}]]}
