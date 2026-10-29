"""Read fresh state from the verified Desktop owner without taking ownership.

The follower stream is the same broker used for human replies. A subscription
does not navigate, resume, start or mutate a conversation. Unknown versions,
owners and partial histories fail closed; persisted history never proves idle.
"""
from __future__ import annotations

import time

from .desktop import (DesktopAppTools, DesktopBridgeSafetyError,
                      DesktopIpcUnavailable, validate_thread_profile)
from ..platforms.transport import default_broker_path


def ordered_turns(state):
    history = state.get('turnHistory') or {}
    if history.get('kind') == 'canonical':
        canonical = history.get('history') or {}
        islands = canonical.get('islands')
        entities = canonical.get('entitiesByKey')
        if not isinstance(islands, list) or not isinstance(entities, dict):
            raise DesktopIpcUnavailable('Desktop returned an invalid canonical history.')
        # The final island must reach the newest boundary, otherwise the
        # visible page could be old and cannot guard a turn-specific action.
        if islands and (islands[-1].get('newerBoundary') or {}).get('status') != 'exhausted':
            raise DesktopIpcUnavailable('Desktop latest turn is not available yet.')
        turns = []
        for island in islands:
            for entry in island.get('entries', []):
                value = entities.get(entry.get('value'))
                if not isinstance(value, dict):
                    raise DesktopIpcUnavailable('Desktop history contains an unresolved entry.')
                turns.append(value)
    else:
        turns = state.get('turns')
        if not isinstance(turns, list):
            raise DesktopIpcUnavailable('Desktop returned no readable history.')
    result = []
    for turn in turns:
        identifier = turn.get('turnId') or turn.get('id')
        if not isinstance(identifier, str) or not identifier:
            # A local optimistic input has no server Turn ID yet. Never use
            # the previous turn for an interrupt/answer during this interval.
            if turn is turns[-1]:
                raise DesktopIpcUnavailable('Desktop is still confirming the latest turn.')
            continue
        result.append({**turn, 'id': identifier})
    return result


def normalize_snapshot(state, thread_id):
    if state.get('id') != thread_id or state.get('hostId') != 'local':
        raise DesktopBridgeSafetyError('Desktop snapshot belongs to a different task or host.')
    status = state.get('threadRuntimeStatus')
    if not isinstance(status, dict) or status.get('type') not in {'active', 'idle'}:
        raise DesktopIpcUnavailable('Desktop current runtime state is not confirmed.')
    turns = ordered_turns(state)
    if status['type'] == 'active' and (not turns or turns[-1].get('status') != 'inProgress'):
        raise DesktopIpcUnavailable('Desktop active turn is not confirmed yet.')
    thread = {'id': thread_id, 'status': status, 'title': state.get('title'),
              'name': state.get('title'), 'cwd': state.get('cwd'),
              'updatedAt': state.get('updatedAt'), 'turns': turns}
    return {'thread': thread, 'turns': list(reversed(turns))}


class FollowerReader(DesktopAppTools):
    # The broker exposes per-task streams, not a sidebar catalogue. The common
    # running-task scanner uses its complete metadata pages on this adapter.
    has_sidebar_catalogue = False

    def __init__(self, transport_factory, timeout, discover, *, validate_thread=None):
        super().__init__(transport_factory, timeout, discover)
        self.validate_local_thread = validate_thread

    def for_current_instance(self):
        endpoint = self.discover()
        return type(self)(self.transport_factory, self.timeout, lambda: endpoint,
                          validate_thread=self.validate_local_thread)

    def read_thread(self, thread_id, *, timeout=None):
        return normalize_snapshot(self.snapshot(thread_id, timeout=timeout), thread_id)

    def read_submission_history(self, thread_id, *, timeout):
        return self.read_thread(thread_id, timeout=timeout)

    def _open_follower(self, endpoint, deadline):
        transport = self.transport_factory(timeout=max(0.1, deadline-time.monotonic()))
        transport.expected_server_pid = endpoint.pid
        transport.PIPE_PATH = default_broker_path(endpoint.profile)
        transport._deadline = deadline
        handle = transport._open()
        try:
            initialized = transport._request(handle, transport._INITIAL_CLIENT_ID,
                'initialize', {'clientType': 'telegram-bot'})
            client_id = (initialized.get('result') or {}).get('clientId')
            if not isinstance(client_id, str) or not client_id:
                raise DesktopIpcUnavailable('Desktop did not identify the reader.')
            return transport, handle, client_id
        except BaseException:
            transport._close(handle)
            raise

    def probe(self):
        endpoint = self.discover()
        transport, handle, _ = self._open_follower(endpoint, time.monotonic()+self.timeout)
        try:
            if self.discover() != endpoint:
                raise DesktopBridgeSafetyError('Desktop restarted during the probe.')
        finally:
            transport._close(handle)

    def snapshot(self, thread_id, *, timeout=None):
        budget = min(self.timeout, timeout) if timeout is not None else self.timeout
        if budget <= 0:
            raise TimeoutError('Desktop read deadline exceeded')
        deadline = time.monotonic() + budget
        endpoint = self.discover()
        validate_thread_profile(endpoint, thread_id)
        # A local metadata check is platform policy, not an ownership claim.
        if self.validate_local_thread:
            self.validate_local_thread(endpoint, thread_id)
        transport, handle, client_id = self._open_follower(endpoint, deadline)
        owner_id = None
        def following(value):
            transport._write(handle, {'type': 'broadcast',
                'method': 'thread-stream-following-changed', 'version': 1,
                'sourceClientId': client_id, 'targetClientIds': [owner_id],
                'params': {'hostId': 'local', 'conversationId': thread_id, 'following': value}})
        try:
            owner = transport._request(handle, client_id, 'thread-owner-discovery',
                {'hostId': 'local', 'conversationId': thread_id})
            owner_id = owner.get('handledByClientId')
            if not isinstance(owner_id, str) or not owner_id:
                raise DesktopIpcUnavailable('Desktop has no live owner for this task.')
            following(True)
            while time.monotonic() < deadline:
                message = transport._next_message(handle, deadline)
                if message.get('type') == 'client-discovery-request':
                    transport._write(handle, {'type': 'client-discovery-response',
                        'requestId': message.get('requestId'), 'response': {'canHandle': False}})
                    continue
                if (message.get('type') != 'broadcast'
                        or message.get('method') != 'thread-stream-state-changed'
                        or message.get('sourceClientId') != owner_id):
                    continue
                params = message.get('params') or {}
                if params.get('conversationId') != thread_id or params.get('hostId') != 'local':
                    continue
                if message.get('version') != 11:
                    raise DesktopIpcUnavailable('Desktop follower stream version is unsupported.')
                change = params.get('change') or {}
                if change.get('type') != 'snapshot':
                    continue  # Deltas cannot establish an initial state.
                if not isinstance(change.get('revision'), int) or change['revision'] < 0:
                    raise DesktopIpcUnavailable('Desktop snapshot revision is invalid.')
                state = change.get('conversationState')
                if not isinstance(state, dict):
                    raise DesktopIpcUnavailable('Desktop snapshot is invalid.')
                if self.discover() != endpoint:
                    raise DesktopBridgeSafetyError('Desktop restarted during the read.')
                return state
            raise DesktopIpcUnavailable('Desktop live snapshot timed out.')
        finally:
            if owner_id and client_id:
                try:
                    transport.timeout = min(0.5, budget)
                    following(False)
                except Exception:
                    pass
            transport._close(handle)
