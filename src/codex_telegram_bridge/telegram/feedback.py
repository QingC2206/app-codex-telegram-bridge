"""Durable operation feedback using the existing native reaction journal.

Backend code supplies task state. This layer owns message-scoped presentation,
delivery gating and restart recovery; it never submits or replays a task.
"""
from contextlib import contextmanager
import logging
import threading
import time

from . import notifications

LOG = logging.getLogger(__name__)
UNKNOWN_RETENTION = 7 * 24 * 3600


def delivery_feedback(statuses):
    """An uncertain page makes the aggregate uncertain, even if another failed."""
    states = set(statuses)
    if 'unknown' in states:
        return '👀', False
    if states & {'pending', 'retry', 'sending'}:
        return '🤔', False
    if states & {'failed', 'expired'}:
        return '😢', True
    return '🎉', True


class Feedback:
    def __init__(self, state, token, chat, api, task_status):
        self.state,self.token,self.chat,self.api,self.task_status=state,token,chat,api,task_status
        self.local=threading.local()
        self.lock=threading.RLock()
        self.started=time.time()
        self.last={}

    @property
    def current(self):
        return getattr(self.local,'identifier',None)

    def route(self, route):
        return {**(route or {'origin':'system'}), **({'_feedback':self.current} if self.current else {})}

    def _set(self, record, emoji, thread='', turn=''):
        key=record['id'];desired=(emoji,thread,turn)
        if self.last.get(key,(None,None))[0]==desired:
            return self.last[key][1]
        try:
            result=notifications.set_task_reaction(self.token,self.chat,record['message'],emoji,
                thread or 'telegram-operation',turn,api=self.api)
        except Exception as exc:
            LOG.warning('Reaction feedback unavailable: %s',type(exc).__name__)
            return None
        self.last[key]=(desired,result)
        return result

    @contextmanager
    def operation(self, update_id, message_id):
        previous=self.current
        self.local.identifier='update:'+str(update_id) if isinstance(message_id,int) and message_id>0 else None
        key=self.current
        if key:
            with self.lock:
                self.state.start_feedback(key,message_id)
                self.reconcile(key,initial=True)
        try:
            yield
        except Exception as exc:
            from .delivery import TelegramDeliveryUnknown
            self.fail(uncertain=isinstance(exc, TelegramDeliveryUnknown))
            raise
        finally:
            if key:
                self.state.update_feedback(key,ready=True)
                self.reconcile(key)
            self.local.identifier=previous

    def processing(self):
        if self.current:
            rows=self.state.feedback_records(self.current)
            if rows:self._set(rows[0],'🤔')

    def fail(self, *, uncertain=False):
        if self.current:self.state.update_feedback(self.current,outcome='uncertain' if uncertain else 'failed')

    def fail_source(self, message_id):
        """Settle the original input when its later choice is definitely rejected."""
        with self.lock:
            keys = [row['id'] for row in self.state.feedback_records()
                    if row['message'] == message_id]
            for key in keys:
                self.state.update_feedback(key, ready=True, outcome='failed')
                self.reconcile(key)
            return bool(keys)

    def bind(self, binding, *, source=None, key=None):
        key=key or self.current
        if not key:return False
        if source:self.state.start_feedback(key,source)
        self.state.update_feedback(key,binding=binding,ready=True,outcome='')
        return bool(self.reconcile(key))

    def reconcile(self, identifier=None, *, initial=False):
        # Serialize projections so a delayed processing stage cannot overwrite completion.
        with self.lock:
            result=False
            for record in self.state.feedback_records(identifier):
                if initial:
                    result=self._set(record,'👀');continue
                if not record['ready']:
                    if record['created']>=self.started:continue
                    update_id=int(record['id'].partition(':')[2]) if record['id'].startswith('update:') else None
                    if any(u['update_id']==update_id for u in self.state.pending_updates()):continue
                    emoji,terminal,thread,turn='😢',True,'',''
                elif record['outcome']:
                    emoji,terminal,thread,turn=('👀',False,'','') if record['outcome']=='uncertain' else ('😢',True,'','')
                    if record['outcome']=='uncertain' and record['binding']:
                        resolved=self.task_status(record['binding'])
                        if resolved[1]:emoji,terminal,thread,turn=resolved
                elif record['binding']:
                    emoji,terminal,thread,turn=self.task_status(record['binding'])
                else:
                    delivery=self.state.feedback_delivery(record['id'])
                    emoji,terminal=delivery_feedback([delivery])
                    thread,turn='',''
                result=self._set(record,emoji,thread,turn)
                if terminal and result is not None:
                    # The shared journal retains transient network failures for retry.
                    self.state.finish_feedback(record['id']);self.last.pop(record['id'],None)
                elif not terminal and emoji == '👀' and time.time()-record['created'] >= UNKNOWN_RETENTION:
                    # Expiry drops tracking only; never changes unknown to failed.
                    self.state.finish_feedback(record['id']);self.last.pop(record['id'],None)
            return result

    def retry(self):
        try:notifications.retry_task_reactions(self.token,api=self.api)
        except Exception as exc:LOG.warning('Reaction retry unavailable: %s',type(exc).__name__)
