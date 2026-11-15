"""Attach a verified Desktop prediction to its existing Telegram message."""
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import time

from ..i18n import tr as _t
from ..telegram import notifications as notify

DB = notify.HOOK_DIR / "telegram_composer_replies.sqlite3"
MAX_AGE = 24 * 60 * 60
EDIT_LEASE = 120
ENABLED_ENV = "CODEX_TELEGRAM_COMPOSER_REPLIES"
UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
SAFETY = re.compile(
    r"^(\S+) info \[cumberbatch\] cumberbatch_safety_decision "
    r"sourceThreadId=(\S+) sourceTurnId=(\S+) suppressed=(true|false)\b"
)


def _open():
    db = sqlite3.connect(DB, timeout=2)
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS composer_replies(
        key TEXT PRIMARY KEY, chat TEXT NOT NULL, thread TEXT NOT NULL,
        turn TEXT NOT NULL, prediction TEXT, notice TEXT, message INTEGER,
        status TEXT NOT NULL DEFAULT 'waiting', updated REAL NOT NULL,
        retry_at REAL NOT NULL DEFAULT 0
    )""")
    if not any(column["name"] == "retry_at" for column in db.execute("PRAGMA table_info(composer_replies)")):
        with db:
            db.execute("BEGIN IMMEDIATE")
            if not any(column["name"] == "retry_at" for column in db.execute("PRAGMA table_info(composer_replies)")):
                db.execute("ALTER TABLE composer_replies ADD COLUMN retry_at REAL NOT NULL DEFAULT 0")
    return db


def _key(token, chat, thread, turn):
    return sha256(f"{token.split(':', 1)[0]}:{chat}:{thread}:{turn}".encode()).hexdigest()[:32]


def _save(token, chat, thread, turn, *, text=None, notice=None, message=None):
    if not UUID.fullmatch(thread) or not UUID.fullmatch(turn):
        return
    key = _key(token, chat, thread, turn)
    now = time.time()
    with closing(_open()) as db, db:
        db.execute("DELETE FROM composer_replies WHERE updated < ?", (now - MAX_AGE,))
        db.execute("""INSERT INTO composer_replies
            (key, chat, thread, turn, prediction, notice, message, updated)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(key) DO UPDATE SET
              prediction=COALESCE(composer_replies.prediction,excluded.prediction),
              notice=COALESCE(composer_replies.notice,excluded.notice),
              message=COALESCE(composer_replies.message,excluded.message),
              updated=excluded.updated
            WHERE composer_replies.status='waiting'""",
            (key, chat, thread, turn, text, notice, message, now))
    _attach(token, chat, key)


def _attach(token, chat, key):
    if notify.notifications_muted():
        return
    now = time.time()
    # Keep exclusive ownership through the Telegram request and its formatting fallbacks.
    lease = now + EDIT_LEASE
    with closing(_open()) as db, db:
        row = db.execute("""UPDATE composer_replies SET status='editing', retry_at=?
            WHERE key=? AND chat=? AND status IN ('waiting','editing') AND retry_at<=?
              AND updated>=? AND prediction IS NOT NULL AND notice IS NOT NULL AND message IS NOT NULL
            RETURNING thread,prediction,notice,message,updated""",
            (lease, key, chat, now, now - MAX_AGE)).fetchone()
    if row is None:
        return
    request = json.loads(row["notice"])
    quoted = "\n".join("> " + re.sub(r"([\\\x60*_{}\[\]()#+.!>|~\-])", r"\\\1", line)
                       for line in row["prediction"].split("\n"))
    body = request["text"] + "\n\n**" + _t("建议回复") + "**\n" + quoted
    status, retry_at, failure = "available", 0, None
    if len(body.encode("utf-16-le")) // 2 > notify.MAX_TELEGRAM_MESSAGE:
        status, failure = "failed", "message size limit"
    else:
        rows = request.get("reply_markup", {}).get("inline_keyboard", [])
        request.update(text=body, chat_id=chat, message_id=row["message"],
            reply_markup={"inline_keyboard": [
                *rows, [{"text": _t("发送建议回复"), "callback_data": f"composer|{key}"}],
            ]})
        try:
            notify.edit_markdown_message(token, request)
        except Exception as exc:
            failure = type(exc).__name__
            rejected = isinstance(exc, notify.TelegramApiError) and 400 <= exc.code < 500
            if rejected and exc.code not in (408, 409, 425, 429):
                status = "failed"
            else:
                age = time.time() - row["updated"]
                delay = 15
                if age >= 600:
                    delay = 300
                elif age >= 60 or (rejected and exc.code == 429):
                    delay = 60
                status, retry_at = "editing", time.time() + delay
    with closing(_open()) as db, db:
        db.execute("""UPDATE composer_replies SET status=?, retry_at=?
            WHERE key=? AND status='editing' AND retry_at=?""",
            (status, retry_at, key, lease))
    if failure is None:
        notify.log_error(f"composer reply attached to {row['thread'][:8]}; message_id={row['message']}", token)
    elif status == "editing":
        notify.log_error(f"composer reply attachment will retry; message_id={row['message']}; {failure}", token)
    else:
        notify.log_error(f"composer reply attachment failed; message_id={row['message']}; {failure}", token)


def retry_pending(token, chat):
    if not DB.is_file() or notify.notifications_muted():
        return
    now = time.time()
    with closing(_open()) as db:
        rows = db.execute("""SELECT key,thread,turn FROM composer_replies
            WHERE chat=? AND status IN ('waiting','editing') AND retry_at<=?
              AND updated>=? AND prediction IS NOT NULL AND notice IS NOT NULL AND message IS NOT NULL
            ORDER BY retry_at,updated""", (chat, now, now - MAX_AGE)).fetchall()
    for row in rows:
        if row["key"] == _key(token, chat, row["thread"], row["turn"]):
            _attach(token, chat, row["key"])
            break


def record_notification(token, request, sent, thread, turn):
    if not isinstance(sent, dict) or type(sent.get("message_id")) is not int:
        return
    chat = str(request["chat_id"])
    content = {name: request[name] for name in
               ("text", "reply_markup", "_local_image_base", "_local_image_text")
               if name in request}
    try:
        _save(token, chat, thread, turn, notice=json.dumps(content), message=sent["message_id"])
    except Exception as exc:
        notify.log_error(f"composer completion state unavailable: {type(exc).__name__}", token)


def _desktop_safety(pid, thread, turn, requested):
    local = Path(os.environ["LOCALAPPDATA"])
    roots = (local / "Packages" / "OpenAI.Codex_2p2nqsd0c76g0" /
             "LocalCache" / "Local" / "Codex" / "Logs", local / "Codex" / "Logs")
    dates = {datetime.fromtimestamp(ts).strftime("%Y/%m/%d")
             for ts in (time.time(), requested)}
    dates.update(datetime.fromtimestamp(ts, timezone.utc).strftime("%Y/%m/%d")
                 for ts in (time.time(), requested))
    files = [file for root in roots for day in dates
             for file in (root / day).glob(f"codex-desktop-*-{pid}-t0-i*-*.log")]
    for file in files:
        try:
            with file.open("rb") as handle:
                handle.seek(max(0, file.stat().st_size - 256 * 1024))
                lines = handle.read().decode("utf-8", errors="replace").splitlines()
            for line in reversed(lines):
                match = SAFETY.match(line)
                if match is None or match[2] != thread or match[3] != turn:
                    continue
                if datetime.fromisoformat(match[1]).timestamp() >= requested:
                    return match[4] == "false"
        except (OSError, ValueError):
            continue
    return None


def record_prediction(record, desktop_pid):
    if desktop_pid <= 0 or not record["text"].strip():
        return
    for attempt in range(10):
        decision = _desktop_safety(desktop_pid, record["threadId"], record["turnId"], record["requested"])
        if decision is not None:
            break
        time.sleep(0.35)
    if decision is not True or notify.notifications_muted():
        return
    loaded = notify.load_config()
    if loaded is None:
        return
    config, token = loaded
    if not config.get("include_message_preview"):
        return
    _save(token, str(config["chat_id"]), record["threadId"], record["turnId"], text=record["text"])


def handle_click(bot, callback, key):
    callback_id = str(callback.get("id") or "")
    message = callback.get("message") or {}
    chat = str((message.get("chat") or {}).get("id"))
    message_id = message.get("message_id")
    try:
        if chat != bot.chat_id or type(message_id) is not int:
            raise ValueError(_t("这条建议回复已失效。"))
        bot._require_task_actions()
        # Telegram may have applied the edit before the acknowledgement was lost.
        with closing(_open()) as db, db:
            row = db.execute("""UPDATE composer_replies SET status='claimed', retry_at=0
                WHERE key=? AND chat=? AND message=? AND status IN ('available','editing')
                  AND updated >= ? RETURNING thread,turn,prediction""",
                (key, chat, message_id, time.time() - MAX_AGE)).fetchone()
        if row is None or key != _key(bot.token, chat, row["thread"], row["turn"]):
            raise ValueError(_t("这条建议回复已发送或失效。"))
        target = bot.desktop.native.read_thread(row["thread"], timeout=8)
        thread = target.get("thread") or {}
        if thread.get("id") != row["thread"]:
            raise ValueError(_t("原对话无法核对，建议回复未提交。"))
        previous = getattr(bot._ui_local, "source_message_id", None)
        bot._ui_local.source_message_id = message_id
        try:
            result = bot._desktop_follow_up(row["thread"], row["prediction"], thread.get("cwd") or "",
                                            expected_completed_turn_id=row["turn"])
        finally:
            bot._ui_local.source_message_id = previous
        if result is None:
            raise ValueError(_t("原对话暂未在桌面打开，建议回复未提交。"))
    except Exception as exc:
        if callback_id:
            bot._answer_callback(callback_id, str(exc)[:160], True)
        return
    if callback_id:
        bot._answer_callback(callback_id, _t("已发回原对话"), False)
