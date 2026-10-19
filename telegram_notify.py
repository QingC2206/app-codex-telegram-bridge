#!/usr/bin/env python3
"""Send a Telegram message when a Codex Stop hook fires.

The bot token is encrypted with Windows DPAPI and can only be decrypted by the
same Windows user account on this machine. The hook itself has no third-party
Python dependencies and never writes hook output to stdout.
"""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
from datetime import datetime
import getpass
import html
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import desktop_receipts
import local_images


HOOK_DIR = Path(__file__).resolve().parent
CONFIG_PATH = HOOK_DIR / "telegram_notify.credentials.json"
LOG_PATH = HOOK_DIR / "telegram_notify.log"
BOT_STATE_PATH = HOOK_DIR / "telegram_bot.state.json"
REPLY_CACHE_DIR = HOOK_DIR / "telegram_replies"
SESSION_ROOT = Path.home() / ".codex" / "sessions"
# An installed hook is bound to its own credentials/profile, including isolated
# Desktop instances. Development copies use the explicitly selected Codex home.
SOURCE_STATE_ROOT = (HOOK_DIR.parent if HOOK_DIR.name.lower() == "hooks"
                     else Path(os.environ.get("CODEX_HOME") or SESSION_ROOT.parent))
MAX_TELEGRAM_MESSAGE = 3900
REPLY_PREVIEW_LIMIT = 1600
REPLY_PAGE_LIMIT = 3300
REPLY_RETENTION_SECONDS = 7 * 24 * 60 * 60
MAX_CACHED_REPLY_BYTES = 48 * 1024 * 1024


REACTION_DB = HOOK_DIR / "telegram_reactions.sqlite3"


def _reaction_db():
    db = sqlite3.connect(REACTION_DB, timeout=30)
    db.execute("CREATE TABLE IF NOT EXISTS reactions (bot TEXT, chat TEXT, message INTEGER, thread TEXT, turn TEXT, emoji TEXT, updated REAL, PRIMARY KEY(bot, chat, message))")
    db.execute("CREATE TABLE IF NOT EXISTS reaction_delivered (bot TEXT, thread TEXT, turn TEXT, updated REAL, PRIMARY KEY(bot, thread, turn))")
    db.execute("CREATE TABLE IF NOT EXISTS task_reply_sources (bot TEXT, chat TEXT, thread TEXT, turn TEXT, message INTEGER, updated REAL, PRIMARY KEY(bot,chat,thread,turn))")
    db.execute("CREATE TABLE IF NOT EXISTS desktop_submissions (bot TEXT,chat TEXT,message INTEGER,thread TEXT,path TEXT,offset INTEGER,digest TEXT,created REAL,turn TEXT,item TEXT, PRIMARY KEY(bot,chat,message))")
    db.execute("CREATE TABLE IF NOT EXISTS reaction_retries (bot TEXT,chat TEXT,message INTEGER, PRIMARY KEY(bot,chat,message))")
    db.execute("CREATE TABLE IF NOT EXISTS navigation_roots (bot TEXT, chat TEXT, origin INTEGER, panel INTEGER, status TEXT, PRIMARY KEY(bot,chat,origin))")
    db.execute("CREATE TABLE IF NOT EXISTS navigation_views (bot TEXT, chat TEXT, panel INTEGER, origin INTEGER, status TEXT, revision INTEGER, PRIMARY KEY(bot,chat,panel))")
    db.execute("CREATE TABLE IF NOT EXISTS navigation_list_returns (bot TEXT, chat TEXT, panel INTEGER, target TEXT, PRIMARY KEY(bot,chat,panel))")
    return db


def navigation_view(token, chat, panel):
    db = _reaction_db()
    try:
        row = db.execute("SELECT origin,status,revision FROM navigation_views WHERE bot=? AND chat=? AND panel=?",
                         (token.split(':')[0], str(chat), panel)).fetchone()
        return dict(origin=row[0], status=row[1], revision=row[2]) if row else None
    finally:
        db.close()


def navigation_list_return(token, chat, panel, target=None):
    if not panel:
        return None
    db = _reaction_db()
    key = (token.split(':')[0], str(chat), panel)
    try:
        if target is not None:
            with db:
                db.execute('INSERT OR REPLACE INTO navigation_list_returns VALUES(?,?,?,?)',(*key,target))
        row = db.execute('SELECT target FROM navigation_list_returns WHERE bot=? AND chat=? AND panel=?',key).fetchone()
        return row[0] if row else None
    finally:
        db.close()


def reserve_navigation(token, chat, origin):
    db = _reaction_db()
    key = (token.split(':')[0], str(chat), origin)
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT panel,status FROM navigation_roots WHERE bot=? AND chat=? AND origin=?',key).fetchone()
            if row and row[1] != 'closed':
                return False, row[0]
            db.execute('INSERT OR REPLACE INTO navigation_roots VALUES(?,?,?,NULL,?)',(*key,'creating'))
            return True, None
    finally:
        db.close()


def bind_navigation(token, chat, origin, panel):
    db = _reaction_db()
    key = (token.split(':')[0], str(chat))
    try:
        with db:
            db.execute('UPDATE navigation_roots SET panel=?,status=? WHERE bot=? AND chat=? AND origin=?',
                       (panel,'open',*key,origin))
            db.execute('INSERT INTO navigation_views VALUES(?,?,?,?,?,0)',(*key,panel,origin,'open'))
    finally:
        db.close()


def release_navigation(token, chat, origin):
    # Only explicit API rejection permits another create; timeouts remain reserved.
    db = _reaction_db()
    try:
        with db:
            db.execute("DELETE FROM navigation_roots WHERE bot=? AND chat=? AND origin=? AND status='creating' AND panel IS NULL",
                       (token.split(':')[0],str(chat),origin))
    finally:
        db.close()


def advance_navigation(token, chat, panel, *, close=False):
    db = _reaction_db()
    key = (token.split(':')[0],str(chat),panel)
    try:
        with db:
            db.execute("UPDATE navigation_views SET revision=revision+1,status=? WHERE bot=? AND chat=? AND panel=? AND status='open'",
                       ('closed' if close else 'open',*key))
            if close:
                db.execute("UPDATE navigation_roots SET status='closed' WHERE bot=? AND chat=? AND panel=?",key)
    finally:
        db.close()


def remember_task_reply_source(token, chat_id, thread_id, turn_id, message_id):
    if not thread_id or not turn_id or not isinstance(message_id, int) or message_id <= 0:
        return
    db = _reaction_db()
    try:
        with db:
            db.execute("INSERT INTO task_reply_sources VALUES(?,?,?,?,?,?) "
                       "ON CONFLICT(bot,chat,thread,turn) DO UPDATE SET "
                       "message=excluded.message,updated=excluded.updated "
                       "WHERE excluded.message>=task_reply_sources.message",
                       (token.split(":")[0], str(chat_id), thread_id, turn_id, message_id, time.time()))
            db.execute("DELETE FROM task_reply_sources WHERE updated<?", (time.time()-30*86400,))
    finally:
        db.close()


def task_reply_source(token, chat_id, thread_id, turn_id):
    # Never fall back to a different turn, task, chat or bot.
    if not thread_id or not turn_id:
        return None
    db = _reaction_db()
    key = (token.split(":")[0], str(chat_id), thread_id, turn_id)
    try:
        row = db.execute("SELECT message FROM task_reply_sources WHERE bot=? AND chat=? AND thread=? AND turn=? AND updated>?",
                         (*key, time.time()-30*86400)).fetchone()
        if row:
            return int(row[0])
        # Compatible with a turn already running when this version was installed.
        row = db.execute("SELECT message FROM reactions WHERE bot=? AND chat=? AND thread=? AND turn=? AND updated>? ORDER BY message DESC LIMIT 1",
                         (*key, time.time()-30*86400)).fetchone()
        return int(row[0]) if row else None
    finally:
        db.close()


def attach_task_reply(token, request, thread_id, turn_id):
    if request.get("reply_parameters"):
        return
    try:
        message_id = task_reply_source(token, request.get("chat_id"), thread_id, turn_id)
        if message_id:
            request["reply_parameters"] = {"message_id": message_id, "allow_sending_without_reply": True}
    except Exception as exc:
        log_error(f"could not associate completion reply: {type(exc).__name__}")


def set_task_reaction(token, chat_id, message_id, emoji, thread_id="", turn_id="", *, _retry=False):
    """Serialize desired state and Telegram writes across bot and hook processes."""
    if not message_id:
        return False
    db = _reaction_db()
    bot = token.split(":")[0]
    try:
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute("SELECT thread,turn,emoji,updated FROM reactions WHERE bot=? AND chat=? AND message=?",
                              (bot, str(chat_id), message_id)).fetchone()
        if _retry and (not previous or previous[:3] != (thread_id, turn_id, emoji)):
            # A hook may have completed the message since the retry was queued.
            return False
        if emoji in {"👀", "🤔"} and turn_id and db.execute("SELECT 1 FROM reaction_delivered WHERE bot=? AND thread=? AND turn=?", (bot, thread_id, turn_id)).fetchone():
            emoji = "👌"
        if previous and previous[:3] == (thread_id, turn_id, emoji) and not db.execute(
                "SELECT 1 FROM reaction_retries WHERE bot=? AND chat=? AND message=?", (bot, str(chat_id), message_id)).fetchone():
            return True
        updated = previous[3] if previous and previous[:3] == (thread_id, turn_id, emoji) else time.time()
        db.execute("INSERT OR REPLACE INTO reactions VALUES (?,?,?,?,?,?,?)", (bot, str(chat_id), message_id, thread_id, turn_id, emoji, updated))
        try:
            telegram_call(token, "setMessageReaction", {
                "chat_id": str(chat_id), "message_id": message_id,
                "reaction": [{"type": "emoji", "emoji": emoji}] if emoji else [],
                "is_big": False,
            }, timeout=8)
        except Exception as exc:
            log_error(f"reaction update failed: {exc}", token)
            if 'Telegram API 400:' not in str(exc):
                db.execute("INSERT OR IGNORE INTO reaction_retries VALUES (?,?,?)", (bot, str(chat_id), message_id))
            else:
                db.execute("DELETE FROM reaction_retries WHERE bot=? AND chat=? AND message=?", (bot, str(chat_id), message_id))
                if emoji in {"", "👌", "👎"}:
                    db.execute("DELETE FROM reactions WHERE bot=? AND chat=? AND message=?", (bot, str(chat_id), message_id))
            db.commit()
            return False
        db.execute("DELETE FROM reaction_retries WHERE bot=? AND chat=? AND message=?", (bot, str(chat_id), message_id))
        if not emoji or emoji in {"👌", "👎"}:
            # Forget local work after success; Telegram keeps the OK reaction.
            db.execute("DELETE FROM reactions WHERE bot=? AND chat=? AND message=?", (bot, str(chat_id), message_id))
        db.commit()
        return True
    finally:
        db.close()


def task_reactions(token):
    db = _reaction_db()
    try:
        return db.execute("SELECT chat,message,thread,turn,emoji,updated FROM reactions WHERE bot=?", (token.split(":")[0],)).fetchall()
    finally:
        db.close()


def retry_task_reactions(token):
    db = _reaction_db()
    try:
        rows = db.execute("SELECT r.chat,r.message,r.thread,r.turn,r.emoji FROM reactions r JOIN reaction_retries q "
                          "ON r.bot=q.bot AND r.chat=q.chat AND r.message=q.message WHERE r.bot=?",
                          (token.split(':')[0],)).fetchall()
    finally:
        db.close()
    for chat, message, thread, turn, emoji in rows:
        set_task_reaction(token, chat, message, emoji, thread, turn, _retry=True)


def remember_desktop_submission(token, chat, message, thread, text):
    if not isinstance(message, int) or message <= 0:
        return False
    path, offset = desktop_receipts.checkpoint(thread, SESSION_ROOT)
    db = _reaction_db()
    try:
        with db:
            db.execute("INSERT OR REPLACE INTO desktop_submissions VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (token.split(':')[0], str(chat), message, thread, path, offset,
                        desktop_receipts.prompt_digest(thread, text), time.time(), '', ''))
            db.execute("DELETE FROM desktop_submissions WHERE created<?", (time.time()-30*86400,))
        return True
    finally:
        db.close()


def desktop_submission_turn(token, chat, message, thread):
    db = _reaction_db()
    try:
        row = db.execute("SELECT turn FROM desktop_submissions WHERE bot=? AND chat=? AND message=? AND thread=?",
                         (token.split(':')[0], str(chat), message, thread)).fetchone()
        return row[0] if row else None  # empty string denotes an unbound receipt
    finally:
        db.close()


def forget_desktop_submission(token, chat, message, thread):
    db = _reaction_db()
    try:
        with db:
            db.execute("DELETE FROM desktop_submissions WHERE bot=? AND chat=? AND message=? AND thread=? AND turn=''",
                       (token.split(':')[0], str(chat), message, thread))
    finally:
        db.close()


def bind_desktop_submission(token, chat, message, thread, turn, item=''):
    if not turn:
        return False
    bot = token.split(':')[0]
    db = _reaction_db()
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            if item and db.execute("SELECT 1 FROM desktop_submissions WHERE bot=? AND thread=? AND item=?",
                                   (bot, thread, item)).fetchone():
                return False
            changed = db.execute("UPDATE desktop_submissions SET turn=?,item=? WHERE bot=? AND chat=? AND message=? AND thread=? AND turn=''",
                                 (turn, item, bot, str(chat), message, thread)).rowcount
            if not changed:
                return False
            db.execute("INSERT INTO task_reply_sources VALUES(?,?,?,?,?,?) ON CONFLICT(bot,chat,thread,turn) "
                       "DO UPDATE SET message=excluded.message,updated=excluded.updated WHERE excluded.message>=task_reply_sources.message",
                       (bot, str(chat), thread, turn, message, time.time()))
            db.execute("UPDATE reactions SET thread=?,turn=? WHERE bot=? AND chat=? AND message=?",
                       (thread, turn, bot, str(chat), message))
        # The rollout receipt is proof of acceptance, even if the sender has
        # not received its acknowledgement or the completion hook is very fast.
        set_task_reaction(token, chat, message, '🤔', thread, turn)
        return True
    finally:
        db.close()


def resolve_desktop_submissions(token, thread_id=None):
    db = _reaction_db()
    bot = token.split(':')[0]
    try:
        query = "SELECT chat,message,thread,path,offset,digest,created FROM desktop_submissions WHERE bot=? AND turn='' AND created>?"
        params = [bot, time.time()-86400]
        if thread_id:
            query += " AND thread=?"; params.append(thread_id)
        rows = db.execute(query + " ORDER BY created,message LIMIT 50", params).fetchall()
    finally:
        db.close()
    for chat, message, thread, stored_path, offset, digest, created in rows:
        try:
            stored_offset = offset
            path = desktop_receipts.session_path(thread, SESSION_ROOT)
            if path is None:
                continue
            if stored_path and str(path) != stored_path:
                offset = 0  # e.g. Desktop unarchived the same task; require a fresh timestamp below.
            receipts, next_offset = desktop_receipts.read_receipts(path, offset)
            for receipt in receipts:
                if not isinstance(receipt['created'], (int, float)) or receipt['created'] < created:
                    continue
                if receipt['digest'] == digest and bind_desktop_submission(token, chat, message, thread, receipt['turn_id'], receipt['item_id']):
                    break
            else:
                db = _reaction_db()
                try:
                    with db:
                        db.execute("UPDATE desktop_submissions SET path=?,offset=? WHERE bot=? AND chat=? AND message=? AND turn='' AND offset=?",
                                   (str(path), next_offset, bot, str(chat), message, stored_offset))
                finally:
                    db.close()
        except (OSError, sqlite3.Error) as exc:
            log_error(f"Desktop reply association failed: {type(exc).__name__}")


def payload_turn_id(payload):
    explicit = str(payload.get('turn_id') or payload.get('turn-id') or '')
    if explicit:
        return explicit
    thread = str(payload.get('thread_id') or payload.get('thread-id') or '')
    final = payload.get('last_assistant_message') or payload.get('last-assistant-message')
    return desktop_receipts.completion_turn(thread, final, SESSION_ROOT)


def complete_task_reactions(token, thread_id, turn_id):
    # A thread alone cannot distinguish an old completion from a newer reply.
    if not thread_id or not turn_id:
        return
    bot = token.split(":")[0]
    db = _reaction_db()
    try:
        with db:
            db.execute("INSERT OR REPLACE INTO reaction_delivered VALUES (?,?,?,?)", (bot, thread_id, turn_id, time.time()))
            db.execute("DELETE FROM reaction_delivered WHERE updated<?", (time.time()-7*86400,))
    finally:
        db.close()
    for chat, message, thread, turn, emoji, updated in task_reactions(token):
        if thread == thread_id and turn == turn_id:
            set_task_reaction(token, chat, message, "👌", thread, turn)


def reaction_delivery_confirmed(token, thread_id, turn_id):
    db = _reaction_db()
    try:
        return bool(db.execute("SELECT 1 FROM reaction_delivered WHERE bot=? AND thread=? AND turn=?",
                               (token.split(":")[0], thread_id, turn_id)).fetchone())
    finally:
        db.close()


def complete_payload_reactions(payload):
    loaded = load_config()
    if loaded:
        _, token = loaded
        try:
            thread = str(payload.get("thread_id") or payload.get("thread-id") or "")
            resolve_desktop_submissions(token, thread)
            complete_task_reactions(token, str(payload.get("thread_id") or payload.get("thread-id") or ""),
                                    payload_turn_id(payload))
        except Exception as exc:
            log_error(f"reaction completion failed: {exc}", token)


class DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _dpapi_function(name: str):
    if os.name != "nt":
        raise RuntimeError("此配置方式仅支持 Windows DPAPI。")

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    function = getattr(crypt32, name)
    function.argtypes = [
        ctypes.POINTER(DataBlob),
        wintypes.LPCWSTR if name == "CryptProtectData" else ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DataBlob),
    ]
    function.restype = wintypes.BOOL
    return function


def _blob(data: bytes) -> tuple[DataBlob, Any]:
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    value = DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    return value, buffer


def _local_free(pointer: Any) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.LocalFree(ctypes.cast(pointer, ctypes.c_void_p))


def protect_secret(secret: str) -> str:
    raw = secret.encode("utf-8")
    input_blob, input_buffer = _blob(raw)
    output_blob = DataBlob()
    protect = _dpapi_function("CryptProtectData")
    if not protect(
        ctypes.byref(input_blob),
        "Codex Telegram bot token",
        None,
        None,
        None,
        0,
        ctypes.byref(output_blob),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    _ = input_buffer  # Keep the backing buffer alive through CryptProtectData.
    try:
        encrypted = ctypes.string_at(output_blob.pbData, output_blob.cbData)
        return base64.b64encode(encrypted).decode("ascii")
    finally:
        _local_free(output_blob.pbData)


def unprotect_secret(encoded: str) -> str:
    encrypted = base64.b64decode(encoded)
    input_blob, input_buffer = _blob(encrypted)
    output_blob = DataBlob()
    description = wintypes.LPWSTR()
    unprotect = _dpapi_function("CryptUnprotectData")
    if not unprotect(
        ctypes.byref(input_blob),
        ctypes.byref(description),
        None,
        None,
        None,
        0,
        ctypes.byref(output_blob),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    _ = input_buffer  # Keep the backing buffer alive through CryptUnprotectData.
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData).decode("utf-8")
    finally:
        _local_free(output_blob.pbData)
        if description:
            _local_free(description)


class TelegramApiError(RuntimeError):
    """A definite Telegram rejection, distinct from ambiguous transport failures."""
    def __init__(self, code: int, description: str):
        self.code = code
        self.description = description
        super().__init__(f"Telegram API {code}: {description}")


def telegram_call(
    token: str,
    method: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 20,
) -> Any:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
    request = Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "Codex-Telegram-Notify/1.0",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = str(exc)
        finally:
            exc.close()
        try:
            rejection = json.loads(detail)
        except (ValueError, TypeError):
            raise RuntimeError(f"Telegram HTTP {exc.code}") from exc
        if not isinstance(rejection, dict) or rejection.get("ok") is not False:
            raise RuntimeError(f"Telegram HTTP {exc.code}") from exc
        raise TelegramApiError(exc.code, str(rejection.get("description", ""))) from exc
    except URLError as exc:
        raise RuntimeError(f"无法连接 Telegram API: {exc.reason}") from exc

    if not result.get("ok"):
        raise TelegramApiError(int(result.get("error_code", 0)), str(result.get("description", "unknown error")))
    return result.get("result")


def send_message(token: str, payload: dict[str, Any]) -> Any:
    """Send a Telegram message with retries for slow proxy/TUN connections."""
    last_error: Exception | None = None
    for delay in (0, 2, 5):
        if delay:
            time.sleep(delay)
        try:
            return telegram_call(token, "sendMessage", payload, timeout=30)
        except Exception as exc:
            last_error = exc
    assert last_error is not None
    raise last_error


def telegram_multipart_call(
    token: str,
    method: str,
    fields: dict[str, Any],
    *,
    file_field: str = "",
    filename: str = "",
    content: bytes = b"",
    files: list[dict[str, Any]] | None = None,
    content_type: str = "application/octet-stream",
    timeout: float = 45,
) -> Any:
    """Call a Telegram upload method using a small standard-library multipart body."""
    boundary = f"CodexTelegram{secrets.token_hex(12)}"
    body = bytearray()

    def append(value: str | bytes) -> None:
        body.extend(value.encode("utf-8") if isinstance(value, str) else value)

    for name, value in fields.items():
        if value is None:
            continue
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, (dict, list)):
            rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        else:
            rendered = str(value)
        append(f"--{boundary}\r\n")
        append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n')
        append(rendered)
        append("\r\n")

    uploads = files if files is not None else [{"field": file_field, "filename": filename, "content": content, "content_type": content_type}]
    for upload in uploads:
        field = str(upload["field"])
        if not re.fullmatch(r"[A-Za-z0-9_]+", field):
            raise ValueError("Invalid multipart attachment name")
        safe_filename = re.sub(r"[^A-Za-z0-9._-]+", "-", upload["filename"]).strip(".-") or "screenshot.png"
        append(f"--{boundary}\r\n")
        append(f'Content-Disposition: form-data; name="{field}"; filename="{safe_filename}"\r\n')
        append(f"Content-Type: {upload.get('content_type', 'application/octet-stream')}\r\n\r\n")
        append(upload["content"])
        append("\r\n")
    append(f"--{boundary}--\r\n")

    request = Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=bytes(body),
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "Codex-Telegram-Notify/1.0",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = str(exc)
        finally:
            exc.close()
        try:
            rejection = json.loads(detail)
        except (ValueError, TypeError):
            raise RuntimeError(f"Telegram HTTP {exc.code}") from exc
        if not isinstance(rejection, dict) or rejection.get("ok") is not False:
            raise RuntimeError(f"Telegram HTTP {exc.code}") from exc
        raise TelegramApiError(exc.code, str(rejection.get("description", ""))) from exc
    except URLError as exc:
        raise RuntimeError(f"无法连接 Telegram API: {exc.reason}") from exc
    if not result.get("ok"):
        raise TelegramApiError(int(result.get("error_code", 0)), str(result.get("description", "unknown error")))
    return result.get("result")


def send_screenshot_album(token, chat_id, pictures, silent=False, reply_to=None):
    """Upload full-resolution photos; Bot API has no documented HD/SD switch."""
    if not pictures:
        raise ValueError("没有可发送的截图。")
    for picture in pictures:
        width, height = picture['width'], picture['height']
        if min(width, height) <= 0 or width+height > 10000 or max(width, height)/min(width, height) > 20 or len(picture['content']) > 10*1024*1024:
            raise ValueError("截图超出 Telegram 照片限制，未改为文件发送。")
    for offset in range(0, len(pictures), 10):
        batch = pictures[offset:offset+10]
        kind = "photo"
        fields = {"chat_id": chat_id, "disable_notification": silent}
        if reply_to:
            fields["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        if len(batch) == 1:
            picture = batch[0]
            fields['caption'] = picture['caption']
            telegram_multipart_call(token, "sendPhoto", fields,
                file_field=kind, filename=f"screenshot-{offset+1}.png", content=picture['content'], content_type="image/png")
        else:
            fields['media'] = [{"type": kind, "media": f"attach://shot{i}", "caption": x['caption']}
                               for i, x in enumerate(batch)]
            uploads = [{"field": f"shot{i}", "filename": f"screenshot-{offset+i+1}.png", "content": x['content'], "content_type": "image/png"}
                       for i, x in enumerate(batch)]
            telegram_multipart_call(token, "sendMediaGroup", fields, files=uploads)


def send_document(
    token: str,
    *,
    chat_id: str,
    filename: str,
    content: bytes,
    caption: str = "",
    silent: bool = False,
) -> Any:
    last_error: Exception | None = None
    fields: dict[str, Any] = {
        "chat_id": chat_id,
        "caption": caption,
        "disable_notification": silent,
    }
    for delay in (0, 2, 5):
        if delay:
            time.sleep(delay)
        try:
            return telegram_multipart_call(
                token,
                "sendDocument",
                fields,
                file_field="document",
                filename=filename,
                content=content,
                content_type="text/markdown; charset=utf-8",
            )
        except Exception as exc:
            last_error = exc
    assert last_error is not None
    raise last_error


_INLINE_MARKDOWN = re.compile(
    r"`[^`\n]+`|\*\*[^*\n]+\*\*|__[^_\n]+__|~~[^~\n]+~~|"
    r"\[[^\]\n]+\]\(https?://[^\s<>\)]+\)",
    re.IGNORECASE,
)


def _telegram_html_inline(text: str) -> str:
    """Render a safe, deliberately small Markdown subset as Telegram HTML."""
    output: list[str] = []
    position = 0
    for match in _INLINE_MARKDOWN.finditer(text):
        output.append(html.escape(text[position : match.start()]))
        token = match.group(0)
        if token.startswith("`"):
            output.append(f"<code>{html.escape(token[1:-1])}</code>")
        elif token.startswith("**") or token.startswith("__"):
            output.append(f"<b>{html.escape(token[2:-2])}</b>")
        elif token.startswith("~~"):
            output.append(f"<s>{html.escape(token[2:-2])}</s>")
        else:
            split_at = token.rfind("](")
            label = token[1:split_at]
            url = token[split_at + 2 : -1]
            output.append(
                f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'
            )
        position = match.end()
    output.append(html.escape(text[position:]))
    return "".join(output)


def _table_cells(line: str) -> list[str] | None:
    """Split structural pipes, preserving escaped pipes and inline code pipes."""
    value = line.strip()
    cells: list[str] = []
    cell: list[str] = []
    fence = 0
    pipes = 0
    index = 0
    leading = value.startswith("|")
    trailing = False
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            following = value[index + 1]
            cell.append(following if following == "|" else char + following)
            index += 2
            trailing = False
            continue
        if char == "`":
            end = index + 1
            while end < len(value) and value[end] == "`":
                end += 1
            width = end - index
            if fence == width:
                fence = 0
            elif not fence and value.find("`" * width, end) >= 0:
                fence = width
            cell.append(value[index:end])
            index = end
            trailing = False
            continue
        if char == "|" and not fence:
            cells.append("".join(cell).strip())
            cell = []
            pipes += 1
            trailing = True
        else:
            cell.append(char)
            trailing = False
        index += 1
    if not pipes:
        return None
    cells.append("".join(cell).strip())
    if leading:
        cells = cells[1:]
    if trailing:
        cells = cells[:-1]
    return cells


def format_markdown_tables(text: str) -> str:
    """Present GFM tables as readable field/value cards on narrow Telegram screens.

    This is a display transform only; reply caches and Markdown downloads stay raw.
    """
    lines = str(text or "").splitlines()
    output: list[str] = []
    index = 0
    code_fence = ""
    while index < len(lines):
        line = lines[index]
        fence_match = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if fence_match:
            marker = fence_match.group(1)
            if not code_fence:
                code_fence = marker
            elif marker[0] == code_fence[0] and len(marker) >= len(code_fence):
                code_fence = ""
            output.append(line)
            index += 1
            continue
        header = _table_cells(line) if not code_fence and not line.startswith(("    ", "\t")) else None
        divider = _table_cells(lines[index + 1]) if index + 1 < len(lines) else None
        if (not header or len(header) < 2 or not divider or len(divider) != len(header)
                or not all(re.fullmatch(r":?-{3,}:?", cell) for cell in divider)):
            output.append(line)
            index += 1
            continue
        row_index = index + 2
        rows: list[list[str]] = []
        while row_index < len(lines):
            row = _table_cells(lines[row_index])
            if row is None or len(row) != len(header):
                break
            rows.append(row)
            row_index += 1
        if not rows:
            output.append(line)
            index += 1
            continue
        for row in rows:
            if output and output[-1] != "":
                output.append("")
            for column, (label, value) in enumerate(zip(header, row), 1):
                label = label.strip().removeprefix("**").removesuffix("**") or f"第 {column} 列"
                value = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE) or "—"
                output.append(f"**{label}：** {value}")
            output.append("")
        index = row_index
    return "\n".join(output)


def markdown_to_telegram_html(text: str) -> str:
    """Convert common Codex Markdown without exposing Telegram's strict parser."""
    rendered: list[str] = []
    code_lines: list[str] = []
    in_code_block = False

    for line in format_markdown_tables(text).splitlines():
        if re.match(r"^\s*```", line):
            if in_code_block:
                rendered.append(f"<pre>{html.escape(chr(10).join(code_lines))}</pre>")
                code_lines = []
                in_code_block = False
            else:
                in_code_block = True
            continue
        if in_code_block:
            code_lines.append(line)
            continue

        heading = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        if heading:
            rendered.append(f"<b>{_telegram_html_inline(heading.group(1))}</b>")
            continue
        quote = re.match(r"^\s*>\s?(.*)$", line)
        if quote:
            rendered.append(f"<blockquote>{_telegram_html_inline(quote.group(1))}</blockquote>")
            continue
        bullet = re.match(r"^\s*[-+*]\s+(.+)$", line)
        if bullet:
            rendered.append(f"• {_telegram_html_inline(bullet.group(1))}")
            continue
        ordered = re.match(r"^\s*(\d+)[.)]\s+(.+)$", line)
        if ordered:
            rendered.append(
                f"{ordered.group(1)}. {_telegram_html_inline(ordered.group(2))}"
            )
            continue
        if re.match(r"^\s*([-*_])\1{2,}\s*$", line):
            rendered.append("────────")
            continue
        rendered.append(_telegram_html_inline(line))

    if in_code_block:
        rendered.append(f"<pre>{html.escape(chr(10).join(code_lines))}</pre>")
    return "\n".join(rendered)


def _split_long_line(line: str, limit: int) -> list[str]:
    if len(line) <= limit:
        return [line]
    parts: list[str] = []
    remaining = line
    while len(remaining) > limit:
        cut = max(
            remaining.rfind(" ", 0, limit + 1),
            remaining.rfind("，", 0, limit + 1),
            remaining.rfind("。", 0, limit + 1),
            remaining.rfind(",", 0, limit + 1),
        )
        if cut < limit // 2:
            cut = limit
        elif remaining[cut] == " ":
            cut += 1
        parts.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        parts.append(remaining)
    return parts


def _split_markdown_text_pages(text: str, limit: int = REPLY_PAGE_LIMIT) -> list[str]:
    """Split Markdown while closing and reopening fenced code blocks at page edges."""
    value = str(text or "").strip()
    if not value:
        return []
    if limit < 256:
        raise ValueError("reply page limit is too small")
    if len(value) <= limit:
        return [value]

    expanded_lines: list[str] = []
    for line in value.splitlines():
        if line.lstrip().startswith("```"):
            expanded_lines.append(line)
        else:
            expanded_lines.extend(_split_long_line(line, limit - 64))

    pages: list[str] = []
    current: list[str] = []
    current_length = 0
    in_fence = False
    fence_opener = "```"

    def flush() -> None:
        nonlocal current, current_length
        if not current:
            return
        page_lines = list(current)
        if in_fence:
            page_lines.append("```")
        page = "\n".join(page_lines).strip()
        if page:
            pages.append(page)
        current = [fence_opener] if in_fence else []
        current_length = len(fence_opener) if in_fence else 0

    for line in expanded_lines:
        stripped = line.lstrip()
        is_fence = stripped.startswith("```")
        next_in_fence = not in_fence if is_fence else in_fence
        separator = 1 if current else 0
        projected = current_length + separator + len(line)
        closing_reserve = 4 if next_in_fence else 0
        if current and projected + closing_reserve > limit:
            flush()
            separator = 1 if current else 0
        current.append(line)
        current_length += separator + len(line)
        if is_fence:
            if in_fence:
                in_fence = False
                fence_opener = "```"
            else:
                in_fence = True
                fence_opener = stripped or "```"

    flush()
    return pages


def split_markdown_pages(text: str, limit: int = REPLY_PAGE_LIMIT) -> list[str]:
    """Keep native tables intact and repeat their headers on continuation pages."""
    if limit < 256:
        raise ValueError("reply page limit is too small")
    lines = str(text or "").strip().splitlines()
    chunks: list[str] = []
    prose: list[str] = []
    fence = ""
    i = 0
    def flush_prose() -> None:
        if prose:
            chunks.extend(_split_markdown_text_pages("\n".join(prose), limit))
            prose.clear()
    while i < len(lines):
        line = lines[i]
        match = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if match:
            marker = match.group(1)
            if not fence:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = ""
        header = _table_cells(line) if not fence and not match and not line.startswith(("    ", "\t")) else None
        divider = _table_cells(lines[i + 1]) if i + 1 < len(lines) else None
        if not (header and len(header) >= 2 and divider and len(header) == len(divider)
                and all(re.fullmatch(r":?-{3,}:?", cell) for cell in divider)):
            prose.append(line)
            i += 1
            continue
        end = i + 2
        while end < len(lines):
            row = _table_cells(lines[end])
            if row is None or len(row) != len(header):
                break
            end += 1
        if end == i + 2:
            prose.append(line)
            i += 1
            continue
        flush_prose()
        heading = "\n".join(lines[i:i + 2])
        part = heading
        for row in lines[i + 2:end]:
            if len(heading) + 1 + len(row) > limit:
                if part != heading:
                    chunks.append(part)
                    part = heading
                # An individual enormous row cannot fit; preserve every field.
                chunks.extend(_split_markdown_text_pages(format_markdown_tables(heading + "\n" + row), limit))
            else:
                if len(part) + 1 + len(row) > limit:
                    chunks.append(part)
                    part = heading
                part += "\n" + row
        if part != heading:
            chunks.append(part)
        i = end
    flush_prose()
    pages: list[str] = []
    for chunk in chunks:
        if pages and len(pages[-1]) + 2 + len(chunk) <= limit:
            pages[-1] += "\n\n" + chunk
        else:
            pages.append(chunk)
    return pages


def markdown_preview(text: str, limit: int) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    pages = split_markdown_pages(value, limit - 32)
    return (pages[0] if pages else "") + "\n\n…（查看完整回复）"


def cleanup_reply_cache(now: float | None = None) -> None:
    if not REPLY_CACHE_DIR.exists():
        return
    cutoff = (time.time() if now is None else now) - REPLY_RETENTION_SECONDS
    for path in REPLY_CACHE_DIR.glob("*.json"):
        if not re.fullmatch(r"[0-9a-f]{16}\.json", path.name):
            continue
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError as exc:
            log_error(f"could not clean reply cache {path.name}: {exc}")


def cache_full_reply(payload: dict[str, Any], thread_id: str = "") -> str | None:
    value = payload.get("last_assistant_message")
    if not isinstance(value, str):
        return None
    text = value.strip()
    if max(len(text), len(format_markdown_tables(text))) <= REPLY_PREVIEW_LIMIT:
        return None
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_CACHED_REPLY_BYTES:
        log_error(f"reply too large to cache: {len(encoded)} bytes")
        return None
    try:
        REPLY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cleanup_reply_cache()
        cache_id = secrets.token_hex(8)
        cwd = str(payload.get("cwd") or "")
        record = {
            "version": 1,
            "id": cache_id,
            "created_at": int(time.time()),
            "thread_id": thread_id,
            "workspace": Path(cwd).name if cwd else "未知工作区",
            "text": text,
        }
        target = REPLY_CACHE_DIR / f"{cache_id}.json"
        temporary = REPLY_CACHE_DIR / f"{cache_id}.json.tmp.{os.getpid()}"
        temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, target)
        return cache_id
    except OSError as exc:
        log_error(f"could not cache full reply: {exc}")
        return None


def load_cached_reply(cache_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{16}", cache_id):
        raise ValueError("完整回复链接无效。")
    cleanup_reply_cache()
    path = REPLY_CACHE_DIR / f"{cache_id}.json"
    if not path.is_file():
        raise FileNotFoundError("完整回复已过期或不存在。")
    if path.stat().st_size > MAX_CACHED_REPLY_BYTES + 64 * 1024:
        raise ValueError("完整回复缓存文件异常。")
    record = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(record, dict) or not isinstance(record.get("text"), str):
        raise ValueError("完整回复缓存内容无效。")
    return record


def build_notification_keyboard(thread_id: str = "", cache_id: str | None = None) -> dict[str, Any] | None:
    rows: list[list[dict[str, str]]] = []
    if thread_id and len(thread_id) <= 52:
        rows.append([
            {"text": "查看任务详情", "callback_data": f"task|{thread_id}"},
            {"text": "💬 直接回复", "callback_data": f"direct|{thread_id}"},
        ])
    if cache_id:
        rows.append([
            {"text": "📖 查看完整回复", "callback_data": f"reply|{cache_id}|0"},
            {"text": "⬇️ 下载 Markdown", "callback_data": f"replyfile|{cache_id}"},
        ])
    return {"inline_keyboard": rows} if rows else None


def _format_rejected(exc: Exception, *, rich: bool = False) -> bool:
    if not isinstance(exc, TelegramApiError):
        return False
    description = exc.description.lower()
    if rich and exc.code == 404 and description in {"not found", "method not found"}:
        return True
    if exc.code != 400:
        return False
    # Rich media validation is a definite content rejection. In particular,
    # Codex replies can embed local image paths that Telegram cannot fetch.
    # Keep the reply deliverable as text without retrying uncertain sends.
    if rich and re.search(r"\brich_message_(?:[a-z0-9]+_)*invalid\b", description):
        return True
    markers = ["parse entities", "unsupported start tag", "can't find end"]
    if rich:
        markers += ["can't parse rich", "cannot parse rich", "can't parse markdown",
                    "rich messages are not supported", "rich_message is not supported",
                    "method not found", "unknown method"]
    return any(marker in description for marker in markers)


def visible_reply_markup(markup: Any) -> Any:
    # Keep direct actions in the internal payload for task association and old
    # callback compatibility, but no longer display the redundant UI button.
    if isinstance(markup, dict) and "inline_keyboard" in markup:
        rows = [[button for button in row
                 if not str(button.get("callback_data", "")).startswith("direct|")]
                for row in markup["inline_keyboard"]]
        return dict(markup, inline_keyboard=[row for row in rows if row])
    return markup


def prepare_local_image_reply(token: str, payload: dict[str, Any], *, upload: bool) -> dict[str, Any]:
    """Upload explicit local images once; the complete text remains a separate send."""
    original = str(payload.get("text") or "")
    base_dir = str(payload.get("_local_image_base") or "")
    images = local_images.find_local_images(original, base_dir)
    # A notification preview can end before the images in the final reply.
    # Read media references from the original reply, while rendering its preview.
    references = local_images.find_local_images(str(payload.get("_local_image_text") or original), base_dir) if upload else images
    if not images and not references:
        return payload
    outcomes: dict[str, str] = {}
    display_paths: dict[str, str] = {}
    attempts = 0
    used_bytes = 0
    for image in references + images:
        key = os.path.normcase(image.path)
        if key in outcomes:
            continue
        display_paths[key] = image.path
        outcome = "path"
        upload_started = False
        if upload and attempts < local_images.MAX_REPLY_PHOTOS:
            try:
                content, content_type = local_images.read_photo(
                    image.path, local_images.MAX_REPLY_PHOTO_BYTES - used_bytes)
                attempts += 1
                used_bytes += len(content)
                # Match the original destination, source quote and notification
                # preferences. Photos and final text share the same task route.
                fields = {key: payload[key] for key in (
                    "chat_id", "message_thread_id", "direct_messages_topic_id", "business_connection_id",
                    "disable_notification", "protect_content", "reply_parameters") if key in payload}
                fields["caption"] = ("📷 " + (image.label or Path(image.path).name))[:900]
                if payload.get("reply_markup"):
                    fields["reply_markup"] = visible_reply_markup(payload["reply_markup"])
                upload_started = True
                sent = telegram_multipart_call(token, "sendPhoto", fields, file_field="photo",
                    filename=Path(image.path).name, content=content, content_type=content_type, timeout=30)
                if not isinstance(sent, dict) or not isinstance(sent.get("message_id"), int):
                    raise RuntimeError("图片发送结果未确认")
                remember_task_message(token, payload, sent)
                outcome = "sent"
                log_error(f"local image delivered; message_id={sent['message_id']}", token)
            except (OSError, ValueError) as exc:
                if upload_started:
                    outcome = "uncertain"
                    log_error(f"local image delivery unconfirmed; using path button without retry: {type(exc).__name__}", token)
                else:
                    log_error(f"local image unavailable; using path button: {type(exc).__name__}", token)
            except TelegramApiError as exc:
                outcome = "failed"
                log_error(f"local image rejected; using path button: {exc}", token)
            except Exception as exc:
                outcome = "uncertain"
                log_error(f"local image delivery unconfirmed; using path button without retry: {type(exc).__name__}", token)
        outcomes[key] = outcome

    def escaped(value: str) -> str:
        return re.sub(r"([\\`*_{}\[\]()<>#+.!|~-])", r"\\\1", value)

    pieces: list[str] = []
    offset = 0
    for image in images:
        pieces.append(original[offset:image.start])
        label = escaped(image.label or Path(image.path).name)
        outcome = outcomes[os.path.normcase(image.path)]
        if outcome == "sent":
            pieces.append(f"**📷 图片已发送：** {label}")
        else:
            status = {"path": "", "failed": "（上传失败）", "uncertain": "（发送未确认）"}[outcome]
            width = max((len(run) for run in re.findall(r"`+", image.path)), default=0) + 1
            delimiter = "`" * max(3, width)
            pieces.append(f"**📂 图片路径{status}：** {label}\n\n{delimiter}text\n{image.path}\n{delimiter}")
        offset = image.end
    pieces.append(original[offset:])
    prepared = dict(payload, text="".join(pieces))
    markup = payload.get("reply_markup")
    if markup is None or (isinstance(markup, dict) and "inline_keyboard" in markup):
        rows = [list(row) for row in (markup or {}).get("inline_keyboard", [])]
        for index, (key, outcome) in enumerate(outcomes.items(), 1):
            if outcome == "sent":
                continue
            path = display_paths[key]
            try:
                button = local_images.path_button(token, str(payload.get("chat_id") or ""), path)
                if len(outcomes) > 1:
                    button = dict(button, text=button["text"] + f" · {index}")
                rows.append([button])
            except (OSError, ValueError) as exc:
                log_error(f"could not retain local image button: {type(exc).__name__}", token)
        if rows:
            prepared["reply_markup"] = dict(markup or {}, inline_keyboard=rows)
    return prepared


def _markdown_request(token: str, payload: dict[str, Any], *, edit: bool) -> Any:
    original = str(payload.get("text") or "")
    base = dict(payload)
    if "reply_markup" in base:
        base["reply_markup"] = visible_reply_markup(base["reply_markup"])
    for key in ("text", "parse_mode", "entities", "link_preview_options", "disable_web_page_preview", "_local_image_base", "_local_image_text"):
        base.pop(key, None)
    method = "editMessageText" if edit else "sendRichMessage"
    rich_payload = dict(base, rich_message={"markdown": original})
    try:
        return telegram_call(token, method, rich_payload, timeout=30)
    except Exception as exc:
        if not _format_rejected(exc, rich=True):
            raise
        log_error(f"{method} format rejected; using text fallback: {exc}", token)
    # Telegram explicitly rejected this request. Never downgrade on a timeout,
    # proxy failure, authentication error, or rate limit: delivery may be uncertain.
    fallback = format_markdown_tables(original)
    if len(fallback) > MAX_TELEGRAM_MESSAGE:
        fallback = fallback[:MAX_TELEGRAM_MESSAGE - 1].rstrip() + "…"
    method = "editMessageText" if edit else "sendMessage"
    legacy = dict(base, text=markdown_to_telegram_html(fallback), parse_mode="HTML")
    try:
        return telegram_call(token, method, legacy, timeout=30)
    except Exception as exc:
        if not _format_rejected(exc):
            raise
    return telegram_call(token, method, dict(base, text=fallback), timeout=30)


NATIVE_REPLY_DIR = Path(__file__).resolve().parent / "telegram_message_routes"
ATTACHMENT_DIR = Path(__file__).resolve().parent / "telegram_attachments"
ATTACHMENT_LIMIT = 20 * 1024 * 1024


def download_telegram_attachment(token: str, attachment: dict[str, Any]) -> Path:
    """Download only Telegram file metadata, with bounded size and safe local names."""
    from urllib.parse import quote
    file_id = attachment.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        raise ValueError("附件缺少有效文件 ID。")
    if int(attachment.get("file_size") or 0) > ATTACHMENT_LIMIT:
        raise ValueError("附件超过 20 MB，请拆分后发送。")
    metadata = telegram_call(token, "getFile", {"file_id": file_id}, timeout=30)
    remote = str(metadata.get("file_path") or "")
    if not remote or remote.startswith('/') or '..' in remote.split('/') or '\\' in remote:
        raise ValueError("Telegram 返回的附件路径无效。")
    if int(metadata.get("file_size") or 0) > ATTACHMENT_LIMIT:
        raise ValueError("附件超过 20 MB，请拆分后发送。")
    filename = str(attachment.get("file_name") or "attachment.bin").replace('\\', '/').split('/')[-1]
    filename = re.sub(r'[^\w.\- ]', '_', filename)[:120].strip(' .') or 'attachment.bin'
    folder = ATTACHMENT_DIR / secrets.token_hex(16)
    folder.mkdir(parents=True, exist_ok=False)
    path = folder / ("file-" + filename)
    try:
        with urlopen(f"https://api.telegram.org/file/bot{token}/{quote(remote, safe='/')}", timeout=45) as response:
            with path.open('xb') as output:
                total = 0
                while True:
                    chunk = response.read(min(65536, ATTACHMENT_LIMIT - total + 1))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > ATTACHMENT_LIMIT:
                        raise ValueError("附件超过 20 MB，请拆分后发送。")
                    output.write(chunk)
        return path.resolve()
    except Exception:
        path.unlink(missing_ok=True)
        folder.rmdir()
        # urllib exceptions can contain the credential-bearing URL.
        raise ValueError("附件下载失败或超过 20 MB；未转交任务，请稍后重试。") from None


def task_from_keyboard(markup: dict[str, Any] | None) -> str | None:
    """Only task cards with an explicit direct-reply action are routable."""
    targets = set()
    has_direct = False
    for row in (markup or {}).get("inline_keyboard", []):
        for button in row:
            action, _, value = str(button.get("callback_data", "")).partition("|")
            if action in {"direct", "task", "watch", "stop", "diff", "usage", "unwatch"} and value:
                targets.add(value)
                has_direct |= action == "direct"
            elif action in {"reply", "replypage", "replyfile"}:
                try:
                    record = load_cached_reply(value.split("|")[0])
                    if record.get("thread_id"):
                        targets.add(record["thread_id"])
                        has_direct = True
                except (OSError, ValueError):
                    pass
    return next(iter(targets)) if has_direct and len(targets) == 1 else None


def _route_path(token: str, chat_id: str, message_id: int) -> Path:
    import hashlib
    # Namespace by bot identity, chat and message; never persist the credential.
    key = f"{token.split(':')[0]}:{chat_id}:{message_id}"
    return NATIVE_REPLY_DIR / (hashlib.sha256(key.encode()).hexdigest() + ".json")


def remember_task_message(token: str, payload: dict[str, Any], result: Any) -> None:
    if not isinstance(result, dict) or not isinstance(result.get("message_id"), int):
        return
    thread_id = task_from_keyboard(payload.get("reply_markup"))
    try:
        chat_id = str((result.get("chat") or {}).get("id") or payload.get("chat_id") or "")
        path = _route_path(token, chat_id, result["message_id"])
        if not thread_id:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".tmp.{os.getpid()}.{time.time_ns()}")
        temporary.write_text(json.dumps({"thread_id": thread_id}), encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        log_error(f"could not store native reply route: {exc}", token)


def lookup_task_message(token: str, chat_id: str, message_id: int) -> str | None:
    try:
        record = json.loads(_route_path(token, chat_id, message_id).read_text(encoding="utf-8"))
        value = record.get("thread_id")
        return value if isinstance(value, str) and value else None
    except (OSError, ValueError, AttributeError):
        return None


def send_markdown_message(token: str, payload: dict[str, Any]) -> Any:
    """Prefer native rich Markdown, including tables and the original keyboard."""
    prepared = prepare_local_image_reply(token, payload, upload=True)
    result = _markdown_request(token, prepared, edit=False)
    remember_task_message(token, payload, result)
    return result


def edit_markdown_message(token: str, payload: dict[str, Any]) -> Any:
    try:
        # Paging and refreshing cards must not re-upload the same photos.
        prepared = prepare_local_image_reply(token, payload, upload=False)
        result = _markdown_request(token, prepared, edit=True)
        remember_task_message(token, payload, result)
        return result
    except TelegramApiError as exc:
        if exc.code == 400 and "message is not modified" in exc.description.lower():
            return None
        raise


def log_error(message: str, token: str | None = None) -> None:
    if token:
        message = message.replace(token, "<redacted-token>")
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 256 * 1024:
            LOG_PATH.replace(LOG_PATH.with_suffix(".log.old"))
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")
    except Exception:
        pass


def _chat_candidates(updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    for update in updates:
        message = (
            update.get("message")
            or update.get("edited_message")
            or update.get("channel_post")
        )
        if not isinstance(message, dict) or not isinstance(message.get("chat"), dict):
            continue
        chat = message["chat"]
        chat_id = str(chat.get("id", ""))
        if not chat_id:
            continue
        candidates[chat_id] = {
            "id": chat_id,
            "type": chat.get("type", "unknown"),
            "name": chat.get("title")
            or " ".join(
                part
                for part in [chat.get("first_name", ""), chat.get("last_name", "")]
                if part
            )
            or chat.get("username")
            or chat_id,
            "date": int(message.get("date", 0)),
        }
    return sorted(candidates.values(), key=lambda item: item["date"], reverse=True)


def choose_chat_id(token: str) -> str:
    while True:
        manual = input("Chat ID（直接回车可从你发给机器人的消息中自动识别）: ").strip()
        if manual:
            return manual

        input("请在 Telegram 打开机器人并发送 /start，然后回到这里按回车继续...")
        try:
            updates = telegram_call(token, "getUpdates", {"limit": 100, "timeout": 0})
        except Exception as exc:
            print(f"无法自动读取 Chat ID：{exc}")
            print("如果机器人配置过 Webhook，请手动输入 Chat ID。\n")
            continue

        candidates = _chat_candidates(updates or [])
        if not candidates:
            print("尚未找到聊天记录。请确认你已经向机器人发送 /start。\n")
            continue
        if len(candidates) == 1:
            selected = candidates[0]
            print(f"已识别：{selected['name']} ({selected['type']}, {selected['id']})")
            return selected["id"]

        print("检测到多个聊天，请选择：")
        for index, item in enumerate(candidates, start=1):
            print(f"  {index}. {item['name']} ({item['type']}, {item['id']})")
        selection = input("序号: ").strip()
        if selection.isdigit() and 1 <= int(selection) <= len(candidates):
            return candidates[int(selection) - 1]["id"]
        print("选择无效，请重试。\n")


def setup() -> int:
    print("Codex → Telegram 通知配置\n")
    print("1. 在 Telegram 找 @BotFather 创建机器人并取得 Bot Token。")
    print("2. Token 只会在本机输入，并使用 Windows DPAPI 加密保存。\n")

    token = getpass.getpass("Bot Token（输入时不会显示）: ").strip()
    if not token:
        print("未输入 Token，配置已取消。")
        return 1

    try:
        bot = telegram_call(token, "getMe")
        username = bot.get("username", "unknown") if isinstance(bot, dict) else "unknown"
        print(f"机器人验证成功：@{username}")
        chat_id = choose_chat_id(token)
        preview_answer = input("通知中包含 Codex 最后回复的预览？[y/N]: ").strip().lower()
        include_preview = preview_answer in {"y", "yes", "是"}
        subagent_answer = input("接收 Codex 子代理任务的完成通知？[y/N]: ").strip().lower()
        include_subagents = subagent_answer in {"y", "yes", "是"}
        silent_answer = input("以静默消息发送（不响铃）？[y/N]: ").strip().lower()
        silent = silent_answer in {"y", "yes", "是"}

        config = {
            "version": 1,
            "token_dpapi": protect_secret(token),
            "chat_id": chat_id,
            "include_message_preview": include_preview,
            "include_subagent_notifications": include_subagents,
            "silent": silent,
        }
        temporary = CONFIG_PATH.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, CONFIG_PATH)
    except Exception as exc:
        print(f"配置失败：{str(exc).replace(token, '<redacted-token>')}")
        return 1

    print("\n加密凭据已保存，正在发送测试消息（网络较慢时会自动重试）...")
    try:
        send_message(
            token,
            {
                "chat_id": chat_id,
                "text": "✅ Codex → Telegram 通知测试成功",
                "disable_notification": silent,
            },
        )
    except Exception as exc:
        safe_error = str(exc).replace(token, "<redacted-token>")
        print(f"测试消息发送失败，但配置已经保存：{safe_error}")
        print("稍后可重新运行向导，或使用 --test 再次测试。")
        return 2

    print("Telegram 中应已收到测试消息。")
    print("请完全退出并重新打开 Codex Desktop；无需 CLI，也无需 /hooks。")
    return 0


def load_config() -> tuple[dict[str, Any], str] | None:
    if not CONFIG_PATH.exists():
        return None
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    token = unprotect_secret(str(config["token_dpapi"]))
    return config, token


def subagent_notifications_enabled(config: dict[str, Any] | None = None) -> bool:
    """Return the opt-in flag; missing and malformed values stay safely disabled."""
    if config is None:
        if not CONFIG_PATH.exists():
            return False
        loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            return False
        config = loaded
    return config.get("include_subagent_notifications") is True


def set_subagent_notifications(enabled: bool) -> None:
    """Persist the subagent notification preference without touching the DPAPI token."""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError("Telegram 通知尚未配置。")
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("Telegram 通知配置无效。")
    config["include_subagent_notifications"] = bool(enabled)
    temporary = CONFIG_PATH.with_name(f"{CONFIG_PATH.name}.tmp.{os.getpid()}")
    temporary.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, CONFIG_PATH)


def _session_subagent_context(thread_id: str) -> dict[str, Any] | None:
    """Inspect the immutable session header used by the legacy Desktop notify path."""
    if not re.fullmatch(r"[A-Za-z0-9-]{6,64}", thread_id) or not SESSION_ROOT.exists():
        return None
    try:
        candidates = [
            path
            for path in SESSION_ROOT.glob(f"*/*/*/rollout-*-{thread_id}.jsonl")
            if path.is_file()
        ]
        if not candidates:
            return None
        path = max(candidates, key=lambda candidate: candidate.stat().st_mtime_ns)
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            first_line = handle.readline(2 * 1024 * 1024)
        metadata = json.loads(first_line)
        payload = metadata.get("payload") if isinstance(metadata, dict) else None
        source = payload.get("source") if isinstance(payload, dict) else None
        if isinstance(source, dict) and isinstance(source.get("subagent"), dict):
            return source["subagent"]
        if isinstance(source, str) and source.lower().replace("_", "") == "subagent":
            return {"source": source}
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        log_error(f"could not inspect session source {thread_id[:8]}: {exc}")
    return None


def subagent_completion_context(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Identify both official SubagentStop events and legacy Desktop completions."""
    if payload.get("hook_event_name") == "SubagentStop":
        return {
            "agent_id": payload.get("agent_id"),
            "agent_type": payload.get("agent_type"),
        }
    if payload.get("hook_event_name") in {"PreToolUse", "PostToolUse", "PermissionRequest"}:
        agent_id = payload.get("agent_id")
        if isinstance(agent_id, str) and agent_id:
            return {
                "agent_id": agent_id,
                "agent_type": payload.get("agent_type"),
            }
    source_kind = payload.get("sourceKind") or payload.get("source_kind")
    if isinstance(source_kind, str) and source_kind.lower().replace("_", "") == "subagent":
        return {"source": source_kind}
    thread_id = str(payload.get("thread_id") or payload.get("thread-id") or "")
    return _session_subagent_context(thread_id) if thread_id else None


def notifications_muted() -> bool:
    """Read the shared controller mute flag without requiring its process."""
    try:
        if not BOT_STATE_PATH.exists():
            return False
        state = json.loads(BOT_STATE_PATH.read_text(encoding="utf-8"))
        return bool(state.get("muted")) if isinstance(state, dict) else False
    except Exception as exc:
        log_error(f"could not read Telegram bot state: {exc}")
        return False


def _assistant_message(payload: dict[str, Any]) -> str | None:
    """Read the assistant text used by either legacy notify or lifecycle hooks."""
    value = payload.get("last-assistant-message")
    if not isinstance(value, str):
        value = payload.get("last_assistant_message")
    return value if isinstance(value, str) else None


def is_internal_task_metadata_completion(payload: dict[str, Any]) -> bool:
    """Only explicitly attributed metadata work may be suppressed.

    A normal user can request exactly the same title/description JSON.
    Unknown purpose must preserve the reply.
    """
    return completion_thread_source(payload) in INTERNAL_SUGGESTION_SOURCES


INTERNAL_SUGGESTION_SOURCES = frozenset({
    "composer_predictions", "ambient_suggestions", "ambient_suggestion_safety",
})
_THREAD_SOURCE_FIELDS = ("threadSource", "thread_source", "thread-source")
_TURN_TRIGGER_FIELDS = ("turnTrigger", "turn_trigger", "turn-trigger")


def _stored_thread_purpose(thread_id: str) -> tuple[str, str | None]:
    """Distinguish a missing temporary thread from a stored or unreadable thread."""
    if not re.fullmatch(r"[A-Za-z0-9-]{6,64}", thread_id):
        return "unavailable", None
    try:
        root = SOURCE_STATE_ROOT.resolve()
        databases = [path for path in root.glob("state_*.sqlite")
                     if re.fullmatch(r"state_[0-9]+\.sqlite", path.name)]
    except (OSError, RuntimeError):
        return "unavailable", None
    databases.sort(key=lambda path: int(path.stem.split("_")[1]), reverse=True)
    readable = False
    for path in databases:
        try:
            resolved = path.resolve()
            if not resolved.is_relative_to(root):
                continue
            db = sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True, timeout=0.2)
            try:
                row = db.execute("SELECT thread_source FROM threads WHERE id=?", (thread_id,)).fetchone()
            finally:
                db.close()
            readable = True
            if row is not None:
                # An existing newer record with unknown purpose must not inherit
                # stale attribution from an older state schema.
                return "stored", row[0] if isinstance(row[0], str) and row[0] else None
        except (OSError, sqlite3.Error):
            continue
    return ("missing" if readable else "unavailable"), None


def _stored_thread_source(thread_id: str) -> str | None:
    return _stored_thread_purpose(thread_id)[1]


_PREDICTION_REQUEST_HEADER = (
    "This is an app-generated request for a suggested next user message. "
    "The user did not write this message."
)
_PREDICTION_COMPOSER_HEADER = "Your suggestion will appear in the user's empty message composer."
_PREDICTION_SCHEMA_MARKER = "\n\nReturn only JSON matching this schema: "


def _composer_prediction_request(payload: dict[str, Any]) -> bool:
    """Match the observed Desktop generation request, never its generated reply.

    Only the latest input is relevant: older inherited inputs cannot label a
    subsequent normal turn. This compatibility check is used only for a thread
    proven absent from a readable profile database and lacking other attribution.
    """
    if payload.get("type") != "agent-turn-complete" or payload.get("client") != "Codex Desktop":
        return False
    inputs = payload.get("input-messages")
    if not isinstance(inputs, list) or not inputs or not isinstance(inputs[-1], str):
        return False
    text = inputs[-1].strip().replace("\r\n", "\n")
    if len(text) > 32768 or not text.startswith(_PREDICTION_REQUEST_HEADER + "\n\n"):
        return False
    paragraphs = text.split("\n\n", 2)
    if len(paragraphs) < 3 or not paragraphs[1].startswith(_PREDICTION_COMPOSER_HEADER):
        return False
    if text.count(_PREDICTION_SCHEMA_MARKER) != 1:
        return False
    schema_text = text.partition(_PREDICTION_SCHEMA_MARKER)[2]
    if not schema_text.endswith("."):
        return False
    try:
        schema = json.loads(schema_text[:-1])
    except (TypeError, ValueError):
        return False
    return schema == {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"suggestion": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
        "required": ["suggestion"], "additionalProperties": False,
    }


def completion_thread_source(payload: dict[str, Any]) -> str | None:
    """Use purpose metadata only; missing, invalid or conflicting evidence is unknown.

    Generic session source/SourceKind, Desktop originator, ephemeral status and
    reply JSON do not prove an internal suggestion's purpose. A captured app
    generation request can recover purpose for an otherwise unrecorded thread.
    """
    evidence: set[str] = set()
    containers = [payload]
    if isinstance(payload.get("thread"), dict):
        containers.append(payload["thread"])
    for container in containers:
        for name in _THREAD_SOURCE_FIELDS + _TURN_TRIGGER_FIELDS:
            value = container.get(name)
            if value is None:
                continue
            if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value):
                return None
            evidence.add(value)
    identifiers = {value for name in ("thread-id", "thread_id")
                   if isinstance(value := payload.get(name), str) and value}
    nested_id = payload.get("thread", {}).get("id") if isinstance(payload.get("thread"), dict) else None
    if isinstance(nested_id, str) and nested_id:
        identifiers.add(nested_id)
    if len(identifiers) > 1:
        return None
    status = "unavailable"
    if identifiers:
        status, stored = _stored_thread_purpose(next(iter(identifiers)))
        if stored:
            evidence.add(stored)
    if (not evidence and status == "missing" and len(identifiers) == 1
            and _composer_prediction_request(payload)):
        return "composer_predictions"
    return next(iter(evidence)) if len(evidence) == 1 else None


def _has_suggestion_envelope(payload: dict[str, Any]) -> bool:
    """A diagnostic hint only: a matching JSON shape never authorizes dropping it."""
    value = _assistant_message(payload)
    if not isinstance(value, str):
        return False
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return False
    if not isinstance(parsed, dict):
        return False
    if set(parsed) == {"suggestion"}:
        return parsed["suggestion"] is None or isinstance(parsed["suggestion"], str)
    if set(parsed) != {"suggestions"}:
        return False
    suggestions = parsed["suggestions"]
    if not isinstance(suggestions, list):
        return False
    fields = {"title", "description", "prompt", "appId", "pluginId"}
    return all(isinstance(item, dict) and set(item) == fields
               and all(isinstance(item[key], str) and item[key].strip()
                       for key in ("title", "description", "prompt"))
               and all(item[key] is None or isinstance(item[key], str)
                       for key in ("appId", "pluginId"))
               for item in suggestions)


def is_internal_suggestions_completion(payload: dict[str, Any]) -> bool:
    source = completion_thread_source(payload)
    if source in INTERNAL_SUGGESTION_SOURCES:
        return True
    if source is None and _has_suggestion_envelope(payload):
        thread = str(payload.get("thread-id") or payload.get("thread_id") or "")
        safe_id = thread[:8] if re.fullmatch(r"[A-Za-z0-9-]{6,64}", thread) else "unknown"
        log_error(f"preserved unclassified suggestion-shaped completion for {safe_id}; source=unknown")
    return False


def is_empty_internal_exclusion_completion(payload: dict[str, Any]) -> bool:
    """Suppress attributed safety-filter work, never a JSON-shaped user reply."""
    return completion_thread_source(payload) == 'ambient_suggestion_safety'


def message_sections(parts: list[str]) -> str:
    """Separate bot-authored UI fields as Markdown paragraphs.

    Only section boundaries are normalized; Markdown inside a section (including
    replies, tables, lists and code) is left intact. Do not apply to arbitrary text.
    """
    return "\n\n".join(part.strip("\n") for part in parts if part.strip("\n"))


def build_user_input_request_message(payload: dict[str, Any]) -> str:
    """Format a non-interactive alert for Desktop's request_user_input tool."""
    cwd = str(payload.get("cwd") or "")
    workspace = Path(cwd).name if cwd else "未知工作区"
    session_id = str(payload.get("session_id") or payload.get("thread_id") or "")
    tool_input = payload.get("tool_input")
    questions = tool_input.get("questions") if isinstance(tool_input, dict) else None
    lines = [
        "**❓ Codex 正在等待你的回答**",
        f"**工作区：** {workspace}",
    ]
    if session_id:
        lines.append(f"**任务：** `{session_id[:8]}`")
    if payload.get("permission_mode") == "plan":
        lines.append("**模式：** Plan")

    if isinstance(questions, list):
        for question_index, question in enumerate(questions[:3], start=1):
            if not isinstance(question, dict):
                continue
            header = str(question.get("header") or f"问题 {question_index}").strip()
            body = str(question.get("question") or "请输入回答").strip()
            lines.extend(["", f"**{question_index}. {header}**", body])
            options = question.get("options")
            if isinstance(options, list):
                for option_index, option in enumerate(options[:4], start=1):
                    if isinstance(option, dict):
                        label = str(option.get("label") or f"选项 {option_index}").strip()
                        description = str(option.get("description") or "").strip()
                    else:
                        label = str(option).strip()
                        description = ""
                    option_line = f"{option_index}. {label}"
                    if description:
                        option_line += f" — {description}"
                    lines.append(option_line)
    lines.extend(["", "请回到 Codex Desktop 选择或输入回答。"])
    message = message_sections(lines)
    if len(message) > MAX_TELEGRAM_MESSAGE:
        message = message[: MAX_TELEGRAM_MESSAGE - 1].rstrip() + "…"
    return message


def send_user_input_request_notification(payload: dict[str, Any]) -> int:
    """Alert for Desktop prompts without attempting to answer them from the hook."""
    if os.environ.get("CODEX_TELEGRAM_MANAGED_APP_SERVER") == "1":
        return 0
    if notifications_muted():
        return 0

    loaded = load_config()
    if loaded is None:
        return 0
    config, token = loaded
    try:
        subagent = subagent_completion_context(payload)
        if subagent is not None and not subagent_notifications_enabled(config):
            return 0
        session_id = str(payload.get("session_id") or payload.get("thread_id") or "")
        request: dict[str, Any] = {
            "chat_id": str(config["chat_id"]),
            "text": build_user_input_request_message(payload),
            "disable_notification": bool(config.get("silent")),
        }
        if session_id:
            request["reply_markup"] = build_notification_keyboard(session_id)
        send_markdown_message(token, request)
    except Exception as exc:
        log_error(f"user-input notification failed: {exc}", token)
    return 0


CODEX_STATE_ROOT = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def conversation_title(thread_id: str) -> str:
    """Read the Desktop display name, without model calls or stale title caching."""
    fallback = f"对话 {thread_id[:8]}" if thread_id else "Codex 对话"
    if not re.fullmatch(r"[A-Za-z0-9-]{6,64}", thread_id):
        return fallback
    try:
        databases = [p for p in CODEX_STATE_ROOT.glob("state_*.sqlite")
                     if re.fullmatch(r"state_[0-9]+\.sqlite", p.name)]
        databases.sort(key=lambda p: int(p.stem.split("_")[-1]), reverse=True)
        for path in databases:
            try:
                db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)
                try:
                    columns = {r[1] for r in db.execute("PRAGMA table_info(threads)")}
                    fields = [key for key in ("name", "title") if key in columns]
                    if "id" not in columns or not fields:
                        continue
                    row = db.execute("SELECT " + ",".join(fields) + " FROM threads WHERE id=?",
                                     (thread_id,)).fetchone()
                    if row:
                        for value in row:
                            if isinstance(value, str) and value.strip():
                                title = " ".join(value.split())
                                return title if len(title) <= 180 else title[:179] + "…"
                finally:
                    db.close()
            except (OSError, sqlite3.Error):
                continue
    except OSError:
        pass
    return fallback


def notification_heading(thread_id: str, icon: str = "✅") -> str:
    title = conversation_title(thread_id)
    # Titles are literal metadata, not Markdown supplied by the user.
    title = re.sub(r"([\\`*_{}\[\]()<>#+.!|~-])", r"\\\1", title)
    return f"**{icon} {title}**"


def build_message(payload: dict[str, Any], include_preview: bool) -> str:
    cwd = str(payload.get("cwd") or "")
    workspace = Path(cwd).name if cwd else "未知工作区"
    completed_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    thread_id = str(payload.get("thread_id") or payload.get("thread-id") or payload.get("session_id") or payload.get("agent_id") or "")
    title = notification_heading(thread_id)
    lines = [
        title,
        f"**工作区：** {workspace}",
        f"**时间：** {completed_at}",
    ]

    if payload.get("_is_subagent"):
        lines.append("**类型：** 子代理")

    model = payload.get("model")
    if isinstance(model, str) and model.strip():
        lines.insert(2, f"**模型：** {model.strip()}")

    last_message = payload.get("last_assistant_message")
    if include_preview and isinstance(last_message, str) and last_message.strip():
        preview = markdown_preview(last_message, REPLY_PREVIEW_LIMIT)
        if len(preview) > REPLY_PREVIEW_LIMIT:
            preview = preview[: REPLY_PREVIEW_LIMIT - 1].rstrip() + "…"
        lines.extend(["**最后回复：**", preview])

    message = "\n\n".join(lines)
    if len(message) > MAX_TELEGRAM_MESSAGE:
        message = message[: MAX_TELEGRAM_MESSAGE - 1].rstrip() + "…"
    return message


def send_hook_notification(payload: dict[str, Any]) -> int:
    if os.environ.get('CODEX_TELEGRAM_SIDE_CHAT') == '1':
        return 0  # The side-chat controller delivers and binds its own answer.
    hook_event = payload.get("hook_event_name")
    if hook_event == 'PreToolUse' and payload.get('tool_name') == 'request_user_input_async':
        # The observer reads the authoritative agentMessage item and its ID;
        # a pre-tool hook cannot safely invent the future question item ID.
        from async_inputs import hint_async_thread
        hint_async_thread(str(payload.get('session_id') or payload.get('thread_id') or ''), HOOK_DIR)
        return 0
    if hook_event == "PreToolUse" and payload.get("tool_name") == "request_user_input":
        return send_user_input_request_notification(payload)
    if hook_event not in {"Stop", "SubagentStop"}:
        return 0
    if (is_internal_task_metadata_completion(payload) or is_empty_internal_exclusion_completion(payload)
            or is_internal_suggestions_completion(payload)):
        return 0
    if notifications_muted():
        return 0

    loaded = load_config()
    if loaded is None:
        return 0
    config, token = loaded
    try:
        subagent = subagent_completion_context(payload)
        if subagent is not None and not subagent_notifications_enabled(config):
            agent_id = str(subagent.get("agent_id") or "")
            suffix = f" for {agent_id[:8]}" if agent_id else ""
            log_error(f"skipped subagent completion{suffix}")
            return 0
        notification_payload = dict(payload)
        notification_payload["_is_subagent"] = subagent is not None
        include_preview = bool(config.get("include_message_preview"))
        thread_id = str(
            payload.get("thread_id")
            or payload.get("thread-id")
            or payload.get("agent_id")
            or ""
        )
        cache_id = cache_full_reply(notification_payload, thread_id) if include_preview else None
        request: dict[str, Any] = {
            "chat_id": str(config["chat_id"]),
            "text": build_message(notification_payload, include_preview),
            "disable_notification": bool(config.get("silent")),
            "_local_image_base": str(payload.get("cwd") or ""),
            "_local_image_text": payload.get("last_assistant_message") if include_preview else "",
        }
        resolve_desktop_submissions(token, thread_id)
        attach_task_reply(token, request, thread_id, payload_turn_id(payload))
        reply_markup = build_notification_keyboard(thread_id, cache_id)
        if reply_markup:
            request["reply_markup"] = reply_markup
        sent = send_markdown_message(
            token,
            request,
        )
        message_id = sent.get("message_id", "unknown") if isinstance(sent, dict) else "unknown"
        log_error(f"notification delivered for {thread_id[:8]}; message_id={message_id}", token)
        complete_payload_reactions(payload)
    except Exception as exc:
        log_error(f"notification failed: {exc}", token)
    return 0


def find_computer_use_notifier() -> Path | None:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return None
    runtime_root = Path(local_app_data) / "OpenAI" / "Codex" / "runtimes" / "cua_node"
    pattern = "*/bin/node_modules/@oai/sky/bin/windows/codex-computer-use.exe"
    candidates = [path for path in runtime_root.glob(pattern) if path.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime_ns)


def forward_computer_use_notification(raw_payload: str) -> None:
    """Preserve the Codex Desktop Computer Use turn-ended callback."""
    executable = find_computer_use_notifier()
    if executable is None:
        log_error("computer-use notifier not found; Telegram notification still attempted")
        return

    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            [str(executable), "turn-ended", raw_payload],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
            creationflags=creation_flags,
        )
        if result.returncode != 0:
            log_error(f"computer-use notifier exited with code {result.returncode}")
    except Exception as exc:
        log_error(f"computer-use notifier failed: {exc}")


def send_desktop_notification(payload: dict[str, Any], raw_payload: str) -> int:
    forward_computer_use_notification(raw_payload)
    if payload.get("type") != "agent-turn-complete":
        return 0
    if is_internal_suggestions_completion(payload):
        thread_id = str(payload.get("thread-id") or "")
        safe_id = thread_id[:8] if re.fullmatch(r"[A-Za-z0-9-]{6,64}", thread_id) else "unknown"
        evidence = "app-generated-prediction-request" if _composer_prediction_request(payload) else "source-metadata"
        log_error(f"skipped internal suggestions completion for {safe_id}; evidence={evidence}")
        return 0
    if is_internal_task_metadata_completion(payload):
        thread_id = str(payload.get("thread-id") or "")
        suffix = f" for {thread_id[:8]}" if thread_id else ""
        log_error(f"skipped internal task metadata completion{suffix}")
        return 0
    if is_empty_internal_exclusion_completion(payload):
        thread_id = str(payload.get("thread-id") or "")
        suffix = f" for {thread_id[:8]}" if thread_id else ""
        log_error(f"skipped empty internal exclusion completion{suffix}")
        return 0
    if notifications_muted():
        return 0

    normalized = {
        "thread_id": payload.get("thread-id"),
        "cwd": payload.get("cwd"),
        "model": payload.get("model"),
        "last_assistant_message": payload.get("last-assistant-message"),
    }
    loaded = load_config()
    if loaded is None:
        return 0
    config, token = loaded
    try:
        subagent = subagent_completion_context(payload)
        if subagent is not None and not subagent_notifications_enabled(config):
            thread_id = str(payload.get("thread-id") or "")
            suffix = f" for {thread_id[:8]}" if thread_id else ""
            log_error(f"skipped subagent completion{suffix}")
            return 0
        include_preview = bool(config.get("include_message_preview"))
        thread_id = str(payload.get("thread-id") or "")
        normalized["_is_subagent"] = subagent is not None
        cache_id = cache_full_reply(normalized, thread_id) if include_preview else None
        request: dict[str, Any] = {
            "chat_id": str(config["chat_id"]),
            "text": build_message(
                normalized,
                include_preview,
            ),
            "disable_notification": bool(config.get("silent")),
            "_local_image_base": str(payload.get("cwd") or ""),
            "_local_image_text": payload.get("last-assistant-message") if include_preview else "",
        }
        resolve_desktop_submissions(token, thread_id)
        attach_task_reply(token, request, thread_id, payload_turn_id(payload))
        reply_markup = build_notification_keyboard(thread_id, cache_id)
        if reply_markup:
            request["reply_markup"] = reply_markup
        sent = send_markdown_message(
            token,
            request,
        )
        message_id = sent.get("message_id", "unknown") if isinstance(sent, dict) else "unknown"
        log_error(f"desktop notification delivered for {thread_id[:8]}; message_id={message_id}", token)
        complete_payload_reactions(payload)
    except Exception as exc:
        log_error(f"desktop notification failed: {exc}", token)
    return 0


def send_manual_test() -> int:
    loaded = load_config()
    if loaded is None:
        print("尚未配置，请先运行 --setup。")
        return 1
    config, token = loaded
    try:
        send_message(
            token,
            {
                "chat_id": str(config["chat_id"]),
                "text": "✅ Codex Telegram 手动测试成功",
                "disable_notification": bool(config.get("silent")),
            },
        )
    except Exception as exc:
        print(f"测试失败：{str(exc).replace(token, '<redacted-token>')}")
        return 1
    print("测试消息已发送。")
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--setup":
        return setup()
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        return send_manual_test()

    if len(sys.argv) > 1:
        raw_payload = sys.argv[1]
        try:
            payload = json.loads(raw_payload)
            if not isinstance(payload, dict):
                raise ValueError("desktop notify input must be a JSON object")
            return send_desktop_notification(payload, raw_payload)
        except Exception as exc:
            log_error(f"desktop notify input/configuration failed: {exc}")
            return 0

    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("hook input must be a JSON object")
        return send_hook_notification(payload)
    except Exception as exc:
        log_error(f"hook input/configuration failed: {exc}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
