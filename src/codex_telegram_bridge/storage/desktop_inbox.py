"""Transactional intake for ordinary Windows Desktop messages. No plaintext is journaled."""
from contextlib import closing
import secrets
import sqlite3
import time

from ..telegram import notifications as notify


AUTO_SECONDS = 600
RETAIN_SECONDS = 86400


def connect():
    db = notify._reaction_db()
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE IF NOT EXISTS desktop_inbox (bot TEXT,chat TEXT,message INTEGER,update_id INTEGER,"
               "sender TEXT NOT NULL,thread TEXT NOT NULL,kind TEXT NOT NULL,body TEXT NOT NULL,scope TEXT NOT NULL,"
               "generation INTEGER NOT NULL,created REAL NOT NULL,expires REAL NOT NULL,phase TEXT NOT NULL DEFAULT 'ready',"
               "due REAL NOT NULL DEFAULT 0,nonce TEXT UNIQUE,notice TEXT NOT NULL DEFAULT '',notice_due REAL NOT NULL DEFAULT 0,"
               "attempts INTEGER NOT NULL DEFAULT 0,"
               "PRIMARY KEY(bot,chat,message))")
    db.execute("CREATE TABLE IF NOT EXISTS desktop_poll_offsets (bot TEXT PRIMARY KEY,offset INTEGER NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS desktop_send_selection (bot TEXT,chat TEXT,sender TEXT,thread TEXT NOT NULL,"
               "nonce TEXT NOT NULL,expires REAL NOT NULL,scope TEXT NOT NULL,generation INTEGER NOT NULL,PRIMARY KEY(bot,chat,sender))")
    return db


def offset(bot_id):
    with closing(connect()) as db:
        row = db.execute('SELECT offset FROM desktop_poll_offsets WHERE bot=?', (bot_id,)).fetchone()
        return row[0] if row else 0


def receive(key, update_id, sender, thread, kind, ciphertext, scope, generation, created, selection=''):
    with closing(connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        inserted = db.execute("INSERT INTO desktop_inbox(bot,chat,message,update_id,sender,thread,kind,body,scope,generation,created,expires)"
                              " SELECT ?,?,?,?,?,?,?,?,?,?,?,? WHERE NOT EXISTS (SELECT 1 FROM desktop_submissions "
                              "WHERE (bot,chat,message)=(?,?,?)) ON CONFLICT(bot,chat,message) DO NOTHING",
                              (*key, update_id, sender, thread, kind, ciphertext, scope, generation, created, created+AUTO_SECONDS, *key)).rowcount
        db.execute("INSERT INTO desktop_poll_offsets VALUES(?,?) ON CONFLICT(bot) DO UPDATE SET offset=max(offset,excluded.offset)",
                   (key[0], update_id+1))
        if inserted and not scope:
            db.execute("UPDATE desktop_inbox SET phase='done',body='',notice='unbound' WHERE (bot,chat,message)=(?,?,?)", key)
        if selection and inserted:
            chosen = db.execute('SELECT thread,scope,generation FROM desktop_send_selection WHERE '
                                '(bot,chat,sender,nonce)=(?,?,?,?) AND expires>?', (*key[:2], sender, selection, time.time())).fetchone()
            if chosen is None or tuple(chosen) != (thread, scope, generation):
                db.execute("UPDATE desktop_inbox SET phase='done',body='',notice='stopped' WHERE (bot,chat,message)=(?,?,?)", key)
            db.execute('DELETE FROM desktop_send_selection WHERE bot=? AND chat=? AND sender=? AND nonce=?',
                       (*key[:2], sender, selection))


def save_selection(identity, thread, nonce, scope, generation):
    with closing(connect()) as db, db:
        db.execute('INSERT OR REPLACE INTO desktop_send_selection VALUES(?,?,?,?,?,?,?,?)',
                   (*identity, thread, nonce, time.time()+AUTO_SECONDS, scope, generation))


def selection(identity):
    with closing(connect()) as db:
        row = db.execute('SELECT thread,nonce FROM desktop_send_selection WHERE (bot,chat,sender)=(?,?,?) AND expires>?',
                         (*identity, time.time())).fetchone()
        return dict(row) if row else None


def clear_selection(identity, nonce=None):
    with closing(connect()) as db, db:
        db.execute('DELETE FROM desktop_send_selection WHERE (bot,chat,sender)=(?,?,?) AND (? IS NULL OR nonce=?)',
                   (*identity, nonce, nonce))


def writing(key, db=None):
    if db is None:
        with closing(connect()) as own, own:
            writing(key, own)
        return
    if db.execute("UPDATE desktop_inbox SET phase='sending' WHERE (bot,chat,message)=(?,?,?) AND phase='ready' AND expires>?",
                  (*key, time.time())).rowcount != 1:
        raise RuntimeError('Desktop intake is no longer eligible for submission')


def finished(key, notice=''):
    with closing(connect()) as db, db:
        db.execute("UPDATE desktop_inbox SET phase='done',body='',notice=?,nonce=NULL,due=0,notice_due=0 WHERE (bot,chat,message)=(?,?,?)",
                   (notice, *key))


def retry(key, now):
    with closing(connect()) as db, db:
        db.execute("UPDATE desktop_inbox SET phase='done',body='',notice='unknown',notice_due=0 "
                   "WHERE (bot,chat,message)=(?,?,?) AND phase='sending'", key)
        db.execute("UPDATE desktop_inbox SET due=?,notice=CASE WHEN attempts=0 THEN 'waiting' ELSE notice END,attempts=attempts+1 "
                   "WHERE (bot,chat,message)=(?,?,?) AND phase='ready'", (now+5, *key))


def finish_error(key):
    with closing(connect()) as db, db:
        db.execute("UPDATE desktop_inbox SET notice=CASE WHEN phase='sending' THEN 'unknown' ELSE 'stopped' END,"
                   "phase='done',body='',nonce=NULL,due=0,notice_due=0 WHERE (bot,chat,message)=(?,?,?)", key)


def pending(bot_id, chat):
    with closing(connect()) as db:
        return db.execute("SELECT 1 FROM desktop_inbox WHERE bot=? AND chat=? AND (phase='ready' OR notice<>'') LIMIT 1",
                          (bot_id, chat)).fetchone() is not None


def expire(key):
    with closing(connect()) as db, db:
        db.execute("UPDATE desktop_inbox SET phase='confirm',nonce=?,notice='confirm',notice_due=0,due=0 "
                   "WHERE (bot,chat,message)=(?,?,?) AND phase='ready'", (secrets.token_hex(12), *key))


def confirmable(identity, nonce):
    with closing(connect()) as db:
        row = db.execute("SELECT * FROM desktop_inbox WHERE (bot,chat,sender)=(?,?,?) AND phase='confirm' AND nonce=? AND created>?",
                         (*identity, nonce, time.time()-RETAIN_SECONDS)).fetchone()
        return dict(row) if row else None


def decide(identity, nonce, send):
    with closing(connect()) as db, db:
        now = time.time()
        return db.execute("UPDATE desktop_inbox SET phase=?,body=CASE WHEN ? THEN body ELSE '' END,expires=?,"
                          "notice='',nonce=NULL,due=0,notice_due=0,attempts=0 WHERE (bot,chat,sender)=(?,?,?) AND nonce=? AND phase='confirm' AND created>?",
                          ('ready' if send else 'done', send, now+AUTO_SECONDS, *identity, nonce, now-RETAIN_SECONDS)).rowcount == 1


def recover(bot_id, chat):
    """A process that exited in 'sending' may have written. Never dispatch that row again."""
    with closing(connect()) as db, db:
        db.execute("UPDATE desktop_inbox AS i SET phase='done',body='',due=0,notice_due=0,notice=CASE WHEN EXISTS "
                   "(SELECT 1 FROM desktop_queue_sends q WHERE (q.bot,q.chat,q.message)=(i.bot,i.chat,i.message)) "
                   "OR EXISTS (SELECT 1 FROM desktop_submissions s WHERE (s.bot,s.chat,s.message)=(i.bot,i.chat,i.message) AND s.turn<>'') "
                   "THEN '' ELSE 'unknown' END WHERE bot=? AND chat=? AND phase='sending'", (bot_id, chat))


def due(bot_id, chat):
    with closing(connect()) as db, db:
        now = time.time()
        db.execute("DELETE FROM desktop_send_selection WHERE bot=? AND chat=? AND expires<=?", (bot_id, chat, now))
        db.execute("UPDATE desktop_inbox SET phase='done',body='',nonce=NULL,notice='expired',notice_due=0,due=0 "
                   "WHERE bot=? AND chat=? AND phase IN ('ready','confirm') AND created<=?", (bot_id, chat, now-RETAIN_SECONDS))
        db.execute("DELETE FROM desktop_inbox WHERE bot=? AND chat=? AND phase='done' AND notice='' AND created<?",
                   (bot_id, chat, now-7*RETAIN_SECONDS))
        return [dict(row) for row in db.execute(
            "SELECT * FROM desktop_inbox i WHERE bot=? AND chat=? AND due<=? AND phase='ready' AND NOT EXISTS "
            "(SELECT 1 FROM desktop_inbox p WHERE (p.bot,p.chat,p.thread)=(i.bot,i.chat,i.thread) AND p.phase IN ('ready','sending') "
            "AND p.update_id<i.update_id) ORDER BY update_id LIMIT 16", (bot_id, chat, now))]


def notices(bot_id, chat):
    with closing(connect()) as db:
        return [dict(row) for row in db.execute("SELECT * FROM desktop_inbox WHERE bot=? AND chat=? AND notice<>'' "
                                                "AND notice_due<=? ORDER BY update_id LIMIT 16", (bot_id, chat, time.time()))]


def notice_result(key, notice, retry_at=0):
    with closing(connect()) as db, db:
        db.execute("UPDATE desktop_inbox SET notice_due=?,notice=CASE WHEN ?>0 THEN notice ELSE '' END "
                   "WHERE (bot,chat,message)=(?,?,?) AND notice=?", (retry_at, retry_at, *key, notice))
