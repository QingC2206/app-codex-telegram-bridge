"""Durable routing for Desktop's asynchronous agent-message questions.

Observation uses read-only history queries. Answer submission is journaled
before dispatch and never retried after an uncertain result.
"""
from __future__ import annotations
from codex_telegram_bridge.i18n import tr as _t

from contextlib import contextmanager
import json
from pathlib import Path
from codex_telegram_bridge.paths import data_dir
import sqlite3
import threading
import time
import uuid

from codex_telegram_bridge.codex.desktop import DesktopBridgeSafetyError, DesktopIpcInactive, DesktopIpcUnavailable
from codex_telegram_bridge.codex.history import read_thread_history, question_answers, REPLY_OPEN, REPLY_CLOSE


def question_id(item_id: str, index: int) -> str:
    return json.dumps(['request_user_input_async', item_id, index], separators=(',', ':'))


def hint_async_thread(thread_id: str, directory: Path) -> None:
    import re
    if not re.fullmatch(r'[A-Za-z0-9-]{6,64}', thread_id):
        return
    with sqlite3.connect(directory / 'telegram_async_inputs.sqlite3', timeout=2) as db:
        db.execute('CREATE TABLE IF NOT EXISTS hints(thread TEXT PRIMARY KEY,updated REAL)')
        db.execute('INSERT OR REPLACE INTO hints VALUES(?,?)', (thread_id, time.time()))


class AsyncInputs:
    def __init__(self, bot, database=None):
        self.bot = bot
        self.database = Path(database or (data_dir() / 'telegram_async_inputs.sqlite3'))
        self.identity = (bot.token.split(':')[0], bot.chat_id)
        self.updated = {}
        self.failures = {}
        self.worker = None

    @contextmanager
    def db(self):
        self.database.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.database, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            db.execute('CREATE TABLE IF NOT EXISTS hints(thread TEXT PRIMARY KEY,updated REAL)')
            db.execute('''CREATE TABLE IF NOT EXISTS questions(
                bot TEXT,chat TEXT,key TEXT,thread TEXT,turn TEXT,item TEXT,qindex INTEGER,
                title TEXT,options TEXT,status TEXT,message INTEGER,created REAL,
                PRIMARY KEY(bot,chat,thread,item,qindex),UNIQUE(bot,chat,key))''')
            with db:
                db.execute('BEGIN IMMEDIATE')
                if 'answer' not in {row[1] for row in db.execute('PRAGMA table_info(questions)')}:
                    db.execute('ALTER TABLE questions ADD COLUMN answer TEXT')
                yield db
        finally:
            db.close()

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        self.worker = threading.Thread(target=self._loop, name='telegram-async-question-observer', daemon=True)
        self.worker.start()

    def observe(self, thread, turn, item):
        questions = item.get('questions') if isinstance(item, dict) else None
        if (not isinstance(item, dict) or item.get('type') != 'agentMessage' or not isinstance(item.get('id'), str)
                or not item['id'] or not isinstance(questions, list) or not 1 <= len(questions) <= 8):
            return
        with self.db() as db:
            for index, question in enumerate(questions):
                if not isinstance(question, dict) or not isinstance(question.get('title'), str):
                    continue
                options = question.get('options')
                if options is not None and (not isinstance(options, list) or len(options) > 12
                        or any(not isinstance(option, str) or len(option) > 1000 for option in options)):
                    continue
                if not question['title'].strip() or len(question['title']) > 12000:
                    continue
                db.execute('INSERT OR IGNORE INTO questions(bot,chat,key,thread,turn,item,qindex,title,options,status,message,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (*self.identity, uuid.uuid4().hex, thread, turn, item['id'], index,
                     question['title'], json.dumps(options or [], ensure_ascii=False),
                     'suppressed' if self.bot.quiet_mode_enabled() is True else 'new', None, time.time()))

    def reconcile(self, thread_id, history):
        thread = history.get('thread') or {}
        turns = thread.get('turns') or []
        latest = turns[-1] if turns else {}
        turn = latest.get('id')
        active = latest.get('status') == 'inProgress'
        items = latest.get('items') or []
        answered = question_answers([item for entry in turns for item in entry.get('items', [])])
        with self.db() as db:
            previous = [dict(row) for row in db.execute('SELECT key,status,message,answer FROM questions WHERE bot=? AND chat=? AND thread=?',
                                                       (*self.identity, thread_id))]
            if active:
                db.execute("UPDATE questions SET status='expired' WHERE bot=? AND chat=? AND thread=? AND turn<>? AND status IN ('new','waiting','publishing','submitted','uncertain','submitting')",
                           (*self.identity, thread_id, turn))
            else:
                db.execute("UPDATE questions SET status='expired' WHERE bot=? AND chat=? AND thread=? AND status IN ('new','waiting','publishing','submitted','uncertain','submitting')",
                           (*self.identity, thread_id))
            for row in db.execute('SELECT key,item,qindex FROM questions WHERE bot=? AND chat=? AND thread=?',
                                  (*self.identity, thread_id)).fetchall():
                if question_id(row['item'], row['qindex']) in answered:
                    db.execute("UPDATE questions SET status='answered',answer=COALESCE(?,answer) WHERE bot=? AND chat=? AND key=?",
                               (answered[question_id(row['item'], row['qindex'])], *self.identity, row['key']))
        if active:
            for item in items:
                if isinstance(item, dict) and item.get('questions'):
                    self.observe(thread_id, turn, item)
            # A newly observed item might already be answered in the same page.
            if answered:
                with self.db() as db:
                    for row in db.execute('SELECT key,item,qindex FROM questions WHERE bot=? AND chat=? AND thread=? AND turn=?',
                                          (*self.identity, thread_id, turn)).fetchall():
                        if question_id(row['item'], row['qindex']) in answered:
                            db.execute("UPDATE questions SET status='answered',answer=COALESCE(?,answer) WHERE bot=? AND chat=? AND key=?",
                                       (answered[question_id(row['item'], row['qindex'])], *self.identity, row['key']))
        self._clear_expired_input()
        for row in previous:
            current = self.get(row['key'])
            if (current and current['status'] in {'answered', 'expired'}
                    and (current['status'] != row['status'] or current['answer'] != row['answer'])
                    and isinstance(current.get('message'), int)):
                self.refresh_card(current)

    def refresh_card(self, row):
        """Retain the question and answer; only retire the answering buttons."""
        try:
            self._refresh_card(row)
        except Exception as exc:
            from codex_telegram_bridge.app import log
            log('async answer card update postponed: ' + type(exc).__name__)

    @staticmethod
    def _preview(value, units):
        # Telegram counts UTF-16 units. Cut raw text before HTML escaping so
        # entities and tags stay valid even for emoji-heavy or long answers.
        encoded = value.encode('utf-16-le')
        return value if len(encoded) <= units * 2 else encoded[:(units-1)*2].decode('utf-16-le', errors='ignore') + '…'

    def _refresh_card(self, row):
        if not row or not isinstance(row.get('message'), int):
            return
        from codex_telegram_bridge.telegram.notifications import cache_full_reply, build_notification_keyboard
        text, full_text = self.render_card(row)
        truncated = any(self._preview(value, 1400) != value for value in (row['title'], row.get('answer') or ''))
        cache_id = cache_full_reply({'last_assistant_message': full_text}, row['thread'], force=truncated)
        keyboard = build_notification_keyboard(cache_id=cache_id) or {'inline_keyboard': []}
        self.bot.enqueue_edit(row['message'], text, keyboard, parse_mode='HTML')

    def card_heading(self, row, status):
        from html import escape
        from codex_telegram_bridge.telegram.notifications import conversation_title, conversation_workspace
        return (_t('✒️ <b>问题 · {p0}</b> ({p1})', p0=escape(self._preview(conversation_title(row['thread']), 180)), p1=escape(status))
                + '\n\n📁 <code>' + escape(self._preview(conversation_workspace(row['thread']), 100))
                + '</code>   ·   <code>' + escape(row['thread'][:8]) + '</code>')

    def render_card(self, row, *, pending=False):
        from html import escape, unescape
        import re
        status = row['status']
        label = (_t('待回答') if pending else _t('已回答') if status == 'answered' else
                 _t('已提交') if status == 'submitted' else
                 _t('待确认') if status in {'submitting', 'uncertain'} else _t('已结束'))
        heading = self.card_heading(row, label)
        question_label = _t('❓ 原问题')
        text = heading + '\n\n<b>' + question_label + '</b>\n<blockquote>'
        text += escape(self._preview(row['title'], 2000 if pending else 1400)) + '</blockquote>'
        full = unescape(re.sub(r'<[^>]*>', '', heading)) + '\n\n' + question_label + '\n' + row['title']
        if pending:
            hint = _t('任务可以继续运行。请选择选项，或回复此消息填写答案。')
            text += '\n\n<i>' + hint + '</i>'
        elif row.get('answer') is not None:
            answer_label = _t('💬 用户回答')
            text += '\n\n<b>' + answer_label + '</b>\n<blockquote><b>'
            text += escape(self._preview(row['answer'], 1400)) + '</b></blockquote>'
            full += '\n\n' + answer_label + '\n' + row['answer']
        else:
            hint = (_t('已确认作答，暂未读取到回答内容。') if status == 'answered' else _t('未记录到回答。'))
            text += '\n\n<i>' + hint + '</i>'
            full += '\n\n' + hint
        return text, full

    def current_history(self, thread_id, *, item_limit=240):
        history = read_thread_history(self.bot.app, thread_id, turn_limit=1, item_limit=item_limit,
                                      cancelled=self.bot.stop_event.is_set)
        if self.bot._owns_thread(thread_id) and thread_id in self.bot.active_turns:
            latest = ((history.get('thread') or {}).get('turns') or [{}])[-1]
            if latest.get('id') == self.bot.active_turns[thread_id]:
                latest['status'] = 'inProgress'
            return history
        # An independent read-only app-server labels another owner's persisted
        # in-flight turn as interrupted. Runtime status must come from Desktop.
        live = self.bot.desktop.native.read_thread(thread_id)
        thread = live.get('thread') or {}
        summaries = live.get('turns') or []
        if thread.get('id') != thread_id or not summaries:
            raise ValueError(_t('Desktop 未返回可验证的当前任务状态。'))
        latest = ((history.get('thread') or {}).get('turns') or [{}])[-1]
        if latest.get('id') != summaries[0].get('id'):
            raise ValueError(_t('当前 Turn 尚未写入可读历史，暂缓异步问题处理。'))
        latest['status'] = summaries[0].get('status')
        return history

    def _clear_expired_input(self):
        selection = self.bot.pending_free_text.get(self.bot.chat_id, '')
        if selection.startswith('async:'):
            row = self.get(selection[6:])
            if not row or row['status'] not in {'waiting', 'publishing'}:
                self.bot.pending_free_text.pop(self.bot.chat_id, None)

    def get(self, key):
        with self.db() as db:
            db.execute('DELETE FROM questions WHERE bot=? AND chat=? AND created<?', (*self.identity, time.time()-30*86400))
            row = db.execute('SELECT * FROM questions WHERE bot=? AND chat=? AND key=?',
                             (*self.identity, key)).fetchone()
            return dict(row) if row else None

    def suppress_unpublished(self):
        with self.db() as db:
            db.execute("UPDATE questions SET status='suppressed' WHERE bot=? AND chat=? AND status='new'", self.identity)

    def publish(self):
        generation = self.bot.state.snapshot().get("quiet_generation", 0)
        if self.bot.quiet_mode_enabled() is True:
            self.suppress_unpublished()
            return
        with self.db() as db:
            db.execute("UPDATE questions SET status='expired' WHERE bot=? AND chat=? AND created<? AND status IN ('new','waiting','publishing','submitted','uncertain','submitting')",
                       (*self.identity, time.time() - 6 * 3600))
            rows = db.execute("SELECT * FROM questions WHERE bot=? AND chat=? AND status='new' ORDER BY created LIMIT 8",
                              self.identity).fetchall()
        for saved in rows:
            if self.bot.quiet_mode_enabled() is True:
                self.suppress_unpublished()
                return
            if self.bot.stop_event.is_set():
                return
            row = dict(saved)
            with self.db() as db:
                claimed = db.execute("UPDATE questions SET status='publishing' WHERE bot=? AND chat=? AND key=? AND status='new'",
                                     (*self.identity, row['key'])).rowcount
            if not claimed:
                continue
            if self.bot._background_allowed(generation) is False:
                with self.db() as db:
                    db.execute("UPDATE questions SET status='suppressed' WHERE bot=? AND chat=? AND key=?", (*self.identity, row['key']))
                continue
            options = json.loads(row['options'])
            keyboard = [[{'text': label[:50], 'callback_data': f"asyncq|{row['key']}:{index}"}]
                        for index, label in enumerate(options)]
            keyboard.append([{'text': _t('文字回答'), 'callback_data': f"asyncq|{row['key']}:other"}])
            try:
                from codex_telegram_bridge.telegram.notifications import cache_full_reply, build_notification_keyboard
                text, full_text = self.render_card(row, pending=True)
                cache_id = cache_full_reply({'last_assistant_message': full_text}, row['thread'],
                                           force=self._preview(row['title'], 2000) != row['title'])
                if cache_id:
                    keyboard.extend(build_notification_keyboard(cache_id=cache_id)['inline_keyboard'])
                sent = self.bot.send(text, {'inline_keyboard': keyboard}, parse_mode='HTML')
                message = sent.get('message_id') if isinstance(sent, dict) else None
                if not isinstance(message, int) or message <= 0:
                    raise ValueError(_t('Telegram 未确认提问消息 ID；不会重复发送。'))
                with self.db() as db:
                    db.execute('UPDATE questions SET message=? WHERE bot=? AND chat=? AND key=?',
                               (message, *self.identity, row['key']))
            except Exception as exc:
                from codex_telegram_bridge.app import log
                log('async question delivery unconfirmed: ' + type(exc).__name__)
            # A lost HTTP acknowledgement remains publishing, never resends.

    def reply_to(self, message_id, answer):
        if not self.database.is_file():
            return False
        with self.db() as db:
            row = db.execute('SELECT key FROM questions WHERE bot=? AND chat=? AND message=?',
                             (*self.identity, message_id)).fetchone()
        if row is None:
            return False
        self.submit(row['key'], answer)
        return True

    def callback(self, value):
        key, separator, choice = value.partition(':')
        row = self.get(key)
        if not separator or not row or row['status'] not in {'waiting', 'publishing'}:
            raise ValueError(_t('这条异步问题已回答、失效或提交结果未确认。'))
        if choice == 'other':
            self.bot.pending_free_text[self.bot.chat_id] = 'async:' + key
            self.bot.send(_t('请发送文字回答，或使用 Telegram 的“回复”回复原问题。'))
            return
        options = json.loads(row['options'])
        if not choice.isdecimal() or not 0 <= int(choice) < len(options):
            raise ValueError(_t('选项无效。'))
        self.submit(key, options[int(choice)])

    def submit(self, key, answer):
        self.bot._require_task_actions()
        row = self.get(key)
        if not row or row['status'] not in {'waiting', 'publishing'}:
            raise ValueError(_t('这条异步问题已回答、失效或提交结果未确认。'))
        if not isinstance(answer, str) or not answer.strip() or len(answer) > 20000:
            raise ValueError(_t('请提供不超过 20000 字符的回答。'))
        history = self.current_history(row['thread'], item_limit=400)
        self.reconcile(row['thread'], history)
        latest = ((history.get('thread') or {}).get('turns') or [{}])[-1]
        item = next((item for item in latest.get('items', []) if item.get('id') == row['item']), None)
        row = self.get(key)
        if (row['status'] not in {'waiting', 'publishing'} or latest.get('id') != row['turn']
                or latest.get('status') != 'inProgress' or item is None):
            raise ValueError(_t('无法确认原问题仍在当前 Turn 中待回答，请回 Desktop 核对；未提交消息。'))
        questions = item.get('questions') or []
        if (row['qindex'] >= len(questions) or questions[row['qindex']].get('title') != row['title']):
            raise ValueError(_t('原问题内容已改变，未提交消息。'))
        payload = [{'questionItemId': question_id(row['item'], row['qindex']),
                    'question': row['title'], 'answer': answer}]
        prompt = REPLY_OPEN + '\n' + json.dumps(payload, ensure_ascii=False) + '\n' + REPLY_CLOSE
        with self.db() as db:
            changed = db.execute("UPDATE questions SET status='submitting',answer=? WHERE bot=? AND chat=? AND key=? AND status IN ('waiting','publishing')",
                                 (answer, *self.identity, key)).rowcount
        if not changed:
            raise ValueError(_t('该回答已进入提交流程，不会重复发送。'))
        try:
            if self.bot._owns_thread(row['thread']) and self.bot.active_turns.get(row['thread']) == row['turn']:
                self.bot.app.request('turn/steer', {'threadId': row['thread'],
                    'expectedTurnId': row['turn'], 'input': [{'type': 'text', 'text': prompt}]}, timeout=30)
                self.bot._remember_reply_source(row['thread'], row['turn'])
            else:
                result = self.bot._desktop_follow_up(row['thread'], prompt, '', start_if_inactive=False,
                                                     expected_turn_id=row['turn'])
                if result is None:
                    raise DesktopIpcUnavailable(_t('暂时找不到持有该任务的 Codex Desktop 窗口。'))
        except (DesktopBridgeSafetyError, DesktopIpcInactive, DesktopIpcUnavailable):
            with self.db() as db:
                db.execute("UPDATE questions SET status='waiting',answer=NULL WHERE bot=? AND chat=? AND key=? AND status='submitting'", (*self.identity, key))
            raise
        except BaseException:
            with self.db() as db:
                db.execute("UPDATE questions SET status='uncertain' WHERE bot=? AND chat=? AND key=? AND status='submitting'", (*self.identity, key))
            self.refresh_card(self.get(key))
            raise
        try:
            with self.db() as db:
                db.execute("UPDATE questions SET status='submitted' WHERE bot=? AND chat=? AND key=? AND status='submitting'", (*self.identity, key))
        except Exception as exc:
            # The pre-dispatch journal still blocks a second submission.
            from codex_telegram_bridge.app import log
            log('async answer accepted but journal confirmation failed: ' + type(exc).__name__)
        if self.bot.pending_free_text.get(self.bot.chat_id) == 'async:' + key:
            self.bot.pending_free_text.pop(self.bot.chat_id, None)
        # Generic submission feedback replaces choice cards; this card retains
        # the actual question/answer instead, including when reactions succeed.
        panel = getattr(self.bot._ui_local, 'message_id', None)
        try:
            self.bot._ui_local.message_id = None
            self.bot._submission_feedback(row['thread'], row['turn'],
                _t('🥳 已将回答关联到原问题并提交。'), {'inline_keyboard': []})
        finally:
            self.bot._ui_local.message_id = panel
        self.refresh_card(self.get(key))

    def poll(self):
        threads = self.bot._list_threads(limit=30)
        with self.db() as db:
            db.execute('DELETE FROM hints WHERE updated<?', (time.time()-1800,))
            hinted = {row[0] for row in db.execute('SELECT thread FROM hints WHERE updated>?', (time.time()-1800,))}
            pending = {row[0] for row in db.execute("SELECT DISTINCT thread FROM questions WHERE bot=? AND chat=? AND status IN ('new','waiting','publishing','submitted','uncertain')", self.identity)}
        from codex_telegram_bridge.app import persisted_thread_is_active, SESSION_ROOT
        from codex_telegram_bridge.codex.receipts import checkpoint
        present = {thread['id'] for thread in threads}
        threads += [{'id': tid} for tid in hinted | pending if tid not in present]
        for thread in threads:
            if self.bot.stop_event.is_set():
                return
            tid = thread['id']
            if tid not in hinted | pending and not persisted_thread_is_active(tid):
                continue
            path, size = checkpoint(tid, SESSION_ROOT)
            stamp = (thread.get('updatedAt'), path, size if path else int(time.monotonic() // 30))
            if tid not in hinted | pending and self.updated.get(tid) == stamp:
                continue
            try:
                history = self.current_history(tid)
                self.reconcile(tid, history)
                self.updated[tid] = stamp
                self.failures.pop(tid, None)
            except Exception as exc:
                if time.monotonic() - self.failures.get(tid, -1000) > 60:
                    from codex_telegram_bridge.app import log
                    log('async task observation postponed: ' + type(exc).__name__)
                    self.failures[tid] = time.monotonic()
        self.publish()

    def _loop(self):
        while not self.bot.stop_event.is_set():
            try:
                self.poll()
            except Exception as exc:
                from codex_telegram_bridge.app import log
                log('async question observation failed: ' + type(exc).__name__)
            self.bot.stop_event.wait(5)
