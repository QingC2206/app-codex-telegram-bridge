"""Durable Telegram transport for the shared controller, with no task writer.

Rendering and interaction decisions remain in core.bot. This adapter persists
only outbound messages and feedback, retries explicit rate limits and never
replays a request whose delivery is ambiguous.
"""
import json
import threading
import time

from .delivery import Messenger, TelegramDeliveryUnknown, validate_document
from .feedback import Feedback, delivery_feedback, UNKNOWN_RETENTION
from . import notifications as n
from ..i18n import tr as _t


class ControllerDelivery:
    def __init__(self, bot, state):
        self.bot, self.state = bot, state
        self.apply_lock = threading.RLock()
        self.feedback = Feedback(state, bot.token, bot.chat_id, n.telegram_call, self.task_status)
        self.messenger = Messenger(state, bot.token, bot.chat_id,
            lambda method, payload, timeout=30: n.telegram_call(bot.token, method, payload, timeout=timeout),
            valid_route=self.valid_route, upload=lambda *args, **kwargs: n.telegram_multipart_call(*args, **kwargs))
        # A previous process may have died after writing to Telegram.
        with state.lock, state.db:
            state.db.execute("UPDATE outbox SET status='unknown',error='process exited during delivery' WHERE status='sending'")
            state.db.execute("DELETE FROM outbox_uploads WHERE outbox IN (SELECT id FROM outbox WHERE status='unknown')")
            if not state.get('quota_scope_transport_v1'):
                count = 0
                for row in state.db.execute("SELECT id,route FROM outbox WHERE status IN ('pending','retry')").fetchall():
                    route = json.loads(row['route'])
                    after = route.get('after')
                    quota_state = after and after[0] == 'state' and str(after[1].get('key', '')).startswith('quota_')
                    ambiguous = route.get('background') and not any(route.get(key) for key in
                        ('completion', 'interaction', 'progress', 'navigation', 'after'))
                    if not route.get('quota_scope') and (quota_state or ambiguous):
                        state.db.execute("UPDATE outbox SET status='quarantined',error=? WHERE id=?",
                            ('Legacy notification has no verified quota account ownership; retained for review.', row['id']))
                        count += 1
                state.set('quota_scope_transport_v1', {'quarantined': count, 'migrated_at': time.time()})

    def valid_route(self, route):
        if route.get('quota_scope') and not self.bot.quota_state.valid(route['quota_scope']):
            return False
        if not route.get('controller'):
            # Compatibility outbox: only replies/notifications survive. Old
            # process-bound approvals and questions cannot become actionable.
            return (not route.get('request') and not self.bot.quiet_mode_enabled()
                    and not (route.get('notice') and self.bot.state.snapshot().get('muted')))
        if route.get('background') and not self.bot._background_allowed(route.get('quiet_generation')):
            return False
        if route.get('completion') and self.bot.state.snapshot().get('muted'):
            return False
        if route.get('navigation'):
            message, revision = route['navigation']
            view = n.navigation_view(self.bot.token, self.bot.chat_id, message)
            if not view or view['status'] != 'open' or view['revision'] != revision:
                return False
        if route.get('progress'):
            progress = route['progress']
            # Identity fields do not change after binding. Do not acquire
            # live_lock while holding the network writer lock: UI code can
            # send a foreground response while it owns live_lock.
            watch = self.bot.watches.get(progress['thread'])
            if (not watch or watch.get('delivery_version') != progress['version']
                    or watch.get('message_id') != progress['message_id']
                    or str(watch.get('chat_id') or self.bot.chat_id) != progress['chat_id']):
                return False
        if route.get('interaction'):
            group, sequence = route['interaction']
            pending = (self.bot.pending_questions if group == 'question' else self.bot.pending_approvals).get(sequence)
            if not pending or not self.bot._pending_interaction_valid(pending):
                if pending:
                    self.bot._resolve_interaction(str((pending.get('params') or {}).get('threadId') or ''), pending.get('id'))
                return False
        after = route.get('after')
        if after and after[0] == 'question_text' and not self.bot._question_text_valid(after[1]):
            return False
        if after and after[0] == 'async':
            with self.bot.async_inputs.db() as db:
                row = db.execute('SELECT status,created FROM questions WHERE bot=? AND chat=? AND key=?',
                                 (*self.bot.async_inputs.identity, after[1])).fetchone()
            if not row or row['status'] not in {'publishing', 'waiting'} or row['created'] < time.time()-21600:
                return False
        if after and after[0] == 'state':
            value = after[1]
            quota = value['key'].startswith('quota_')
            if quota and not self.bot.quota_state.valid(route.get('quota_scope')):
                return False
            snapshot = self.bot.quota_state.snapshot(route.get('quota_scope')) if quota else self.bot.state.snapshot()
            current = snapshot.get(value['key'], {})
            if current.get(value['identity_key']) != value['identity'] or current.get('expires_at', 0) < time.time():
                return False
        if after and after[0] == 'side' and after[1]['kind'] == 'session':
            if after[1]['target'] not in self.bot.sides.sessions:
                return False
        return True

    def context(self):
        item = getattr(self.bot._ui_local, 'delivery_context', None)
        route = {'controller': True, 'background': item is not None,
                 'quiet_generation': self.bot.state.snapshot().get('quiet_generation', 0)}
        if item:
            route.update({key: item[key] for key in ('completion', 'interaction', 'outcome', 'quota_scope') if item.get(key)})
        if scope := self.bot._quota_output_scope():
            route['quota_scope'] = scope
        return self.feedback.route(route)

    def enqueue(self, item):
        """Persist background work before returning to the app-server reader."""
        route = {'controller': True, 'background': True,
                 'quiet_generation': item.get('quiet_generation', 0)}
        route.update({key: item[key] for key in ('completion', 'interaction', 'outcome', 'quota_scope') if item.get(key)})
        payload = {'chat_id': item.get('chat_id') or self.bot.chat_id,
                   'text': item['text'].replace(self.bot.token, '<redacted-token>'),
                   'reply_markup': item.get('reply_markup') or {'inline_keyboard': []},
                   'disable_notification': self.bot.silent}
        edit = item.get('kind') == 'edit'
        if edit:
            payload['message_id'] = item['message_id']
            if item.get('navigation_revision') is not None:
                route['navigation'] = [item['message_id'], item['navigation_revision']]
        progress_key = None
        if edit and item.get('progress'):
            route['progress'] = {**item['progress'], 'message_id': item['message_id'], 'chat_id': str(payload['chat_id'])}
            progress_key = json.dumps([str(payload['chat_id']), item['message_id'], item['progress'],
                                       item.get('navigation_revision'), route['quiet_generation']], sort_keys=True)
        if item.get('parse_mode') == 'HTML':
            payload['parse_mode'] = 'HTML'
        else:
            payload['_markdown'] = True
            payload['_prepare_images'] = not edit
        key = ('controller:completion:'+':'.join(item['completion'])) if item.get('completion') else None
        return self.state.queue_message('editMessageText' if edit else 'sendMessage', payload, route, key, progress_key=progress_key)

    def send(self, payload, *, edit=False):
        route = self.context()
        if edit:
            view = n.navigation_view(self.bot.token, self.bot.chat_id, payload['message_id'])
            if view:
                route['navigation'] = [payload['message_id'], view['revision']]
        if payload.get('parse_mode') != 'HTML':
            payload = dict(n.prepare_local_image_reply(self.bot.token, payload, upload=not edit), _markdown=True)
        identifier = self.state.queue_message('editMessageText' if edit else 'sendMessage', payload, route)
        return self._send_result(identifier)

    def send_document(self, *, filename, content, caption='', silent=False):
        validate_document(content)
        payload = {'chat_id': self.bot.chat_id, 'caption': caption, 'disable_notification': silent, '_upload': True}
        identifier = self.state.queue_message('sendDocument', payload, self.context(), upload={
            'filename': filename, 'content': content, 'content_type': 'text/markdown; charset=utf-8'})
        return self._send_result(identifier)

    def _send_result(self, identifier):
        self.messenger.flush(identifier)
        self.apply_deliveries()
        with self.state.lock:
            row = self.state.db.execute('SELECT status,message_id FROM outbox WHERE id=?', (identifier,)).fetchone()
        if row['status'] == 'sent':
            return {'message_id': row['message_id'], '_outbox_id': identifier}
        if row['status'] in {'pending', 'retry'}:
            return {'_pending_outbox': identifier}
        if row['status'] == 'unknown':
            raise TelegramDeliveryUnknown(_t('Telegram 未确认本次消息投递；不会重复执行任务。'))
        raise RuntimeError(_t('Telegram 未确认本次消息投递；不会重复执行任务。'))

    def replace_pending(self, identifier, text, markup):
        # A quota progress placeholder rejected with 429 is still unsent.
        # Replace it with the final result instead of later sending both.
        with self.state.lock, self.state.db:
            row = self.state.db.execute('SELECT payload,status,message_id FROM outbox WHERE id=?', (identifier,)).fetchone()
            if not row or row['status'] not in {'pending', 'retry'}:
                if row and row['status'] == 'sent' and row['message_id']:
                    message_id = row['message_id']
                else:
                    # An uncertain placeholder must not cause another send.
                    raise RuntimeError(_t('Telegram 未确认本次消息投递；不会重复执行任务。'))
            else:
                payload = json.loads(row['payload']); payload['text'] = text
                payload['reply_markup'] = markup or {'inline_keyboard': []}
                self.state.db.execute('UPDATE outbox SET payload=? WHERE id=?', (json.dumps(payload), identifier))
                return True
        self.bot.edit_message(message_id, text, markup)
        return True

    def tracks_feedback(self, message):
        return any(row['message'] == message for row in self.state.feedback_records())

    def after_delivery(self, identifier, kind, thread):
        with self.state.lock, self.state.db:
            row = self.state.db.execute('SELECT route FROM outbox WHERE id=?', (identifier,)).fetchone()
            route = json.loads(row[0]); route['after'] = [kind, thread]
            if kind in {'async', 'side', 'question_text'}:
                route['background'] = True
            route['applied'] = False
            self.state.db.execute('UPDATE outbox SET route=? WHERE id=?', (json.dumps(route), identifier))
        self.apply_deliveries()

    def defer_reaction(self, identifiers, source, generation):
        key = 'delivery-reaction:'+str(source)+':'+str(identifiers[-1])
        self.state.set(key, {'ids': identifiers, 'source': source, 'generation': generation, 'created': time.time()})

    def finish_deferred_reactions(self):
        with self.state.lock:
            rows = list(self.state.db.execute("SELECT key,value FROM meta WHERE key LIKE 'delivery-reaction:%'"))
        for key, raw in rows:
            group = json.loads(raw)
            statuses = [self.state.message_status(i) for i in group['ids']]
            emoji, terminal = delivery_feedback(statuses)
            if emoji == '🤔':
                continue
            if 'created' not in group:
                group['created'] = time.time()
                self.state.set(key, group)
            result = None
            if self.bot._background_allowed(group['generation']):
                result = n.set_task_reaction(self.bot.token, self.bot.chat_id, group['source'], emoji)
            if terminal and result is not None or time.time()-group['created'] >= UNKNOWN_RETENTION:
                with self.state.lock, self.state.db:
                    self.state.db.execute('DELETE FROM meta WHERE key=?', (key,))

    def apply_deliveries(self):
        with self.apply_lock:
            self._apply_deliveries()

    def _apply_deliveries(self):
        with self.state.lock:
            rows = [dict(r) for r in self.state.db.execute("SELECT id,payload,route,message_id FROM outbox WHERE status='sent' AND json_extract(route,'$.controller')=1 AND COALESCE(json_extract(route,'$.applied'),0)=0 ORDER BY id")]
        for row in rows:
            route = json.loads(row['route'])
            if not route.get('controller') or route.get('applied'):
                continue
            payload = json.loads(row['payload'])
            n.remember_task_message(self.bot.token, payload, {'message_id': row['message_id']})
            if route.get('legacy_thread'):
                n.remember_task_message(self.bot.token, {'chat_id': self.bot.chat_id,
                    'reply_markup': {'inline_keyboard': [[{'text': 'Reply', 'callback_data': 'direct|'+route['legacy_thread']}]]}},
                    {'message_id': row['message_id']})
            if route.get('completion'):
                thread, turn = route['completion']
                group = route.get('completion_group')
                if not group or self.state.delivery_for(group) == 'sent':
                    n.complete_task_reactions(self.bot.token, thread, turn, outcome=route.get('outcome'))
            if route.get('interaction'):
                group, key = route['interaction']
                pending = (self.bot.pending_questions if group == 'question' else self.bot.pending_approvals).get(key)
                if pending:
                    pending['message_id'] = row['message_id']
            if route.get('after'):
                kind, tid = route['after']
                if kind == 'direct':
                    self.bot._bind_direct_reply_prompt(row['message_id'], tid)
                elif kind == 'question_text':
                    self.bot._bind_question_text_prompt(tid, row['message_id'])
                elif kind == 'watch' and tid in self.bot.watches:
                    self.bot.watches[tid]['message_id'] = row['message_id']
                    self.bot._refresh_watch(tid, force=True)
                    if not self.bot._owns_thread(tid):
                        self.bot._schedule_external_watch_poll(tid)
                elif kind == 'async':
                    with self.bot.async_inputs.db() as db:
                        db.execute('UPDATE questions SET message=? WHERE bot=? AND chat=? AND key=?',
                                   (row['message_id'], *self.bot.async_inputs.identity, tid))
                elif kind == 'side':
                    self.bot.sides.remember(row['message_id'], tid['kind'], tid['target'], tid['parent'])
                elif kind == 'state':
                    if tid['key'].startswith('quota_'):
                        self.bot.quota_state.bind_message(route.get('quota_scope'), tid['key'],
                            tid['identity_key'], tid['identity'], row['message_id'])
                    else:
                        def bind(state):
                            current = state.get(tid['key'], {})
                            if current.get(tid['identity_key']) == tid['identity']:
                                current['message_id'] = row['message_id']
                        self.bot.state.mutate(bind)
            route['applied'] = True
            with self.state.lock, self.state.db:
                self.state.db.execute('UPDATE outbox SET route=? WHERE id=?', (json.dumps(route), row['id']))

    def task_status(self, binding):
        thread, turn = binding.get('thread_id', ''), binding.get('turn_id', '')
        source = binding.get('source')
        if not turn and source:
            turn = n.desktop_submission_turn(self.bot.token, self.bot.chat_id, source, thread) or ''
        if thread and turn and n.reaction_delivery_confirmed(self.bot.token, thread, turn):
            db = n._reaction_db()
            try:
                row = db.execute('SELECT emoji FROM reaction_outcomes WHERE bot=? AND thread=? AND turn=?',
                                 (self.bot.token.split(':')[0], thread, turn)).fetchone()
            finally:
                db.close()
            return (row[0] if row else '🎉'), True, thread, turn
        if thread and turn:
            with self.state.lock:
                row = self.state.db.execute("SELECT status FROM outbox WHERE json_extract(route,'$.completion[0]')=? AND json_extract(route,'$.completion[1]')=? ORDER BY id DESC LIMIT 1", (thread, turn)).fetchone()
            if row and row[0] in {'unknown', 'failed', 'expired'}:
                return '👀', False, thread, turn
        return ('🤔' if turn else '👀'), False, thread, turn

    def tick(self, *, limit=1, seconds=1):
        self.bot._expire_interactions()
        count = self.messenger.flush_batch(limit=limit, seconds=seconds, stop=self.bot.stop_event)
        self.apply_deliveries()
        self.finish_deferred_reactions()
        self.feedback.reconcile()
        self.feedback.retry()
        return count

    def run(self, stop):
        """Wake on committed output; keep network work out of hook scanning."""
        while not stop.is_set():
            self.state.outbox_event.clear()
            try:
                self.tick(limit=8)
            except Exception as exc:
                n.log_error('Telegram delivery worker postponed: '+type(exc).__name__, self.bot.token)
                # Do not spin on a broken local record or housekeeping error.
                stop.wait(1)
            if not stop.is_set():
                self.state.outbox_event.wait(self.state.delivery_wait())
