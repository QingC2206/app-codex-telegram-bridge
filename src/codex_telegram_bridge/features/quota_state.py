"""Account-owned quota history and process/connection-bound authorizations.

The RPC has no account-bound rate-limit transaction. Reading identity on both
sides detects observed switches, but cannot prove an unreported A/B/A switch
inside those reads. Unidentified pushes are refresh hints, never account data.
"""
from functools import wraps
import hashlib
import json
import threading
import uuid

from ..i18n import tr as _t
from ..storage.quota_schema import ROOT, legacy_quota_key

TRANSIENT = ('quota_reset_buttons', 'quota_reset_confirmation', 'quota_reset_selection')


class QuotaSnapshot(dict):
    def __init__(self, result, scope):
        super().__init__(result)
        self.scope = dict(scope)


def quota_operation(method):
    @wraps(method)
    def call(owner, *args, **kwargs):
        bot = getattr(owner, 'bot', owner)
        with bot.quota_state.lock:
            previous = getattr(bot._ui_local, 'quota_operation', False)
            previous_scope = getattr(bot._ui_local, 'quota_scope', None)
            bot._ui_local.quota_operation = True
            bot._ui_local.quota_scope = bot.quota_state.scope()
            try:
                return method(owner, *args, **kwargs)
            finally:
                bot._ui_local.quota_operation = previous
                bot._ui_local.quota_scope = previous_scope
    return call


class QuotaState:
    def __init__(self, bot):
        self.bot = bot
        self.lock = threading.RLock()
        self._scope = None
        self.identity_generation = 0

    def note_identity_change(self):
        # Called by the RPC reader: no lock, disk IO or nested RPC.
        self._scope = None
        self.identity_generation += 1

    def connection(self):
        value = getattr(self.bot.app, 'connection_generation', 0)
        return f'{id(self.bot.app)}:{value if type(value) is int else 0}'

    @staticmethod
    def account_key(account):
        if (not isinstance(account, dict) or account.get('type') != 'chatgpt'
                or not isinstance(account.get('email'), str) or not account['email'].strip()):
            raise ValueError(_t('无法确认当前账户，未执行重置。'))
        # The current integration only establishes type/email as identity fields.
        # Plan/display attributes are not stable identity. This does not claim
        # to distinguish workspaces sharing the same email without an RPC ID.
        identity = {'type': 'chatgpt', 'email': account['email'].strip().casefold()}
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()

    def identity(self):
        result = self.bot.app.request('account/read', {'refreshToken': False}, timeout=30)
        return self.account_key(result.get('account'))

    @staticmethod
    def _cancel(bucket, reason):
        for key in TRANSIENT:
            bucket[key] = {}
        reservation = bucket.get('quota_reset_reservation') or {}
        if reservation.get('status') == 'armed':
            reservation.update(status='cancelled', reason=reason)

    @staticmethod
    def _root(state):
        value = state.get(ROOT)
        if value is not None and (not isinstance(value, dict) or value.get('version') != 1):
            raise ValueError('Unsupported quota account schema; preserve existing history before migration.')
        if not isinstance(value, dict) or value.get('version') != 1:
            legacy = {key: state.pop(key) for key in list(state) if legacy_quota_key(key)}
            value = {'version': 1, 'accounts': {}, 'legacy_unassigned': legacy}
            state[ROOT] = value
        return value

    def startup(self):
        with self.lock:
            self._scope = None
            def change(state):
                root = self._root(state)
                for bucket in root['accounts'].values():
                    self._cancel(bucket, _t('机器人已重启，无法确认离线期间额度变化；原预约不再执行。'))
                root['epoch'] = uuid.uuid4().hex
            self.bot.state.mutate(change)

    def activate(self, key, connection, *, expected_generation=None):
        with self.lock:
            generation = self.identity_generation if expected_generation is None else expected_generation
            if generation != self.identity_generation:
                raise ValueError(_t('查询期间账户或连接已变化，请重新查询额度。'))
            current = self._scope
            if self.valid(current) and current['account'] == key and current['connection'] == connection:
                if getattr(self.bot._ui_local, 'quota_operation', False):
                    self.bot._ui_local.quota_scope = dict(current)
                return dict(current)
            # A failed disk migration must not leave the old account authorized.
            self._scope = None
            epoch = uuid.uuid4().hex
            def change(state):
                root = self._root(state)
                for bucket in root['accounts'].values():
                    self._cancel(bucket, _t('账户或连接已变化，原额度授权已失效。'))
                root['accounts'].setdefault(key, {})
                root.update(active=key, epoch=epoch)
            self.bot.state.mutate(change)
            scope = {'account': key, 'epoch': epoch, 'connection': connection,
                     'identity_generation': generation}
            self._scope = scope
            if getattr(self.bot._ui_local, 'quota_operation', False):
                self.bot._ui_local.quota_scope = dict(scope)
            if not self.valid(scope):
                raise ValueError(_t('查询期间账户或连接已变化，请重新查询额度。'))
            return dict(scope)

    def retire_authorizations(self, reason):
        with self.lock:
            scope = self.scope()
            if not self.valid(scope):
                return
            self._scope = None
            epoch = uuid.uuid4().hex
            def change(state):
                root = self._root(state)
                self._cancel(root['accounts'][scope['account']], reason)
                root['epoch'] = epoch
            self.bot.state.mutate(change)
            updated = {**scope, 'epoch': epoch}
            self._scope = updated
            if getattr(self.bot._ui_local, 'quota_operation', False):
                self.bot._ui_local.quota_scope = dict(updated)

    def invalidate(self):
        with self.lock:
            self._scope = None
            def change(state):
                root = self._root(state)
                for bucket in root['accounts'].values():
                    self._cancel(bucket, _t('无法确认额度所属账户，原额度授权已失效。'))
                root['epoch'] = uuid.uuid4().hex
            self.bot.state.mutate(change)

    def fetch(self):
        with self.lock:
            try:
                generation = self.identity_generation
                before = self.identity()
                connection = self.connection()
                result = self.bot.app.request('account/rateLimits/read', {}, timeout=45)
                after = self.identity()
                if (before != after or connection != self.connection()
                        or generation != self.identity_generation):
                    raise ValueError(_t('查询期间账户或连接已变化，请重新查询额度。'))
            except Exception:
                self.invalidate()
                raise
            return QuotaSnapshot(result, self.activate(after, connection, expected_generation=generation))

    def scope(self):
        current = self._scope
        return dict(current) if self.valid(current) else None

    def valid(self, scope):
        # No quota lock here: transport may already own its writer lock while
        # an account operation waits for delivery. The scope is replaced whole.
        current = self._scope
        return bool(scope and scope == current and scope.get('connection') == self.connection()
                    and scope.get('identity_generation') == self.identity_generation)

    def snapshot(self, scope=None):
        scope = scope or self.scope()
        if not self.valid(scope):
            return {}
        root = self.bot.state.snapshot().get(ROOT) or {}
        if root.get('active') != scope['account'] or root.get('epoch') != scope['epoch']:
            return {}
        return root.get('accounts', {}).get(scope['account'], {})

    def update(self, **values):
        with self.lock:
            scope = self.scope()
            if not self.valid(scope):
                raise ValueError(_t('无法确认额度所属账户，未更新额度状态。'))
            def change(state):
                root = self._root(state)
                root['accounts'][scope['account']].update(values)
            self.bot.state.mutate(change)

    def bind_message(self, scope, key, identity_key, identity, message_id):
        def change(state):
            root = state.get(ROOT) or {}
            if (not self.valid(scope) or root.get('active') != scope['account']
                    or root.get('epoch') != scope['epoch']):
                return
            current = root['accounts'][scope['account']].get(key) or {}
            if current.get(identity_key) == identity:
                current['message_id'] = message_id
        self.bot.state.mutate(change)

    def legacy_blocked(self):
        root = self.bot.state.snapshot().get(ROOT) or {}
        attempt = (root.get('legacy_unassigned') or {}).get('quota_reset_attempt') or {}
        return bool(attempt.get('status') in {'consuming', 'uncertain'} or (
            attempt.get('request_sent') and not attempt.get('recovery_observed')))
