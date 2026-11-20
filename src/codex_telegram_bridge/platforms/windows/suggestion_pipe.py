"""Read-only, profile-scoped access to a live Desktop relay. No text on disk."""
import json
import os
from pathlib import Path
import re
import secrets
import threading

import psutil

from ...codex.suggestion_source import ID
from ...management.transaction import atomic_write
from ...paths import codex_home, data_dir
from .credentials import protect_secret, unprotect_secret
from .discovery import validate_pipe_server


def _directory():
    return data_dir() / 'desktop-suggestion-sources'


def _address(nonce):
    if not isinstance(nonce, str) or not re.fullmatch('[0-9a-f]{32}', nonce):
        raise ValueError('Invalid suggestion pipe')
    return r'\\.\pipe\codex-telegram-suggestions-' + nonce


def _connect(address):
    # multiprocessing's default PipeClient can retry for 20 seconds.
    import _winapi as win
    from multiprocessing.connection import PipeConnection
    win.WaitNamedPipe(address, 350)
    handle = win.CreateFile(address, win.GENERIC_READ | win.GENERIC_WRITE, 0, win.NULL,
                            win.OPEN_EXISTING, win.FILE_FLAG_OVERLAPPED, win.NULL)
    try:
        win.SetNamedPipeHandleState(handle, win.PIPE_READMODE_MESSAGE, None, None)
        return PipeConnection(handle)
    except BaseException:
        win.CloseHandle(handle)
        raise


class Publisher:
    def __init__(self, capture):
        from multiprocessing.connection import Listener
        self.capture = capture
        self.secret = secrets.token_hex(32)
        nonce = secrets.token_hex(16)
        self.address = _address(nonce)
        self.listener = Listener(self.address, family='AF_PIPE', authkey=None)
        self.closed = threading.Event()
        pid = os.getpid()
        self.path = _directory() / f'{capture.desktop_pid}-{pid}-{nonce}.json'
        try:
            row = dict(profile=str(capture.profile), desktop=capture.desktop_pid, relay=pid,
                       desktopBorn=psutil.Process(capture.desktop_pid).create_time(),
                       relayBorn=psutil.Process(pid).create_time(), nonce=nonce,
                       capability=protect_secret(self.secret))
            atomic_write(self.path, json.dumps(row).encode())
            self.thread = threading.Thread(target=self._serve, daemon=True)
            self.thread.start()
        except BaseException:
            self.path.unlink(missing_ok=True)
            self.listener.close()
            raise

    def _serve(self):
        try:
            while not self.closed.is_set():
                try:
                    with self.listener.accept() as conn:
                        if self.closed.is_set() or not conn.poll(0.5):
                            continue
                        q = json.loads(conn.recv_bytes(2048))
                        result = None
                        if (isinstance(q, dict) and isinstance(q.get('key'), str)
                                and q['key'].isascii() and secrets.compare_digest(self.secret, q['key'])):
                            result = self.capture.lookup(q.get('thread'), q.get('turn'))
                        conn.send_bytes(json.dumps(result, ensure_ascii=True).encode())
                except (EOFError, OSError, ValueError, RecursionError):
                    continue
        finally:
            self.listener.close()

    def close(self):
        self.closed.set()
        try:
            self.path.unlink(missing_ok=True)
        finally:
            try:
                with _connect(self.address):
                    pass
            except OSError:
                pass
            self.thread.join(timeout=1)


def locator(endpoint):
    if os.name != 'nt' or endpoint.profile != codex_home():
        return None
    matches = []
    for path in _directory().glob(f'{endpoint.pid}-*.json'):
        try:
            if path.stat().st_size > 8192:
                continue
            row = json.loads(path.read_bytes())
            pid = row['relay']
            if (row['desktop'] != endpoint.pid or Path(row['profile']) != endpoint.profile
                    or type(pid) is not int or pid <= 0 or
                    path.name != f"{endpoint.pid}-{pid}-{row['nonce']}.json"
                    or psutil.Process(pid).create_time() != row['relayBorn']
                    or psutil.Process(endpoint.pid).create_time() != row['desktopBorn']
                    or not any(p.pid == endpoint.pid for p in psutil.Process(pid).parents())):
                continue
            _address(row['nonce'])
            matches.append(row)
        except (OSError, ValueError, KeyError, TypeError, RecursionError, psutil.Error):
            continue
    return matches[0] if len(matches) == 1 else None


def read_approved(endpoint, thread, turn):
    if not all(isinstance(s, str) and ID.fullmatch(s) for s in (thread, turn)):
        return None
    row = locator(endpoint)
    if row is None:
        return None
    with _connect(_address(row['nonce'])) as conn:
        validate_pipe_server(conn.fileno(), row['relay'])
        key = unprotect_secret(row['capability'])
        conn.send_bytes(json.dumps(dict(key=key, thread=thread, turn=turn)).encode())
        return json.loads(conn.recv_bytes(64000)) if conn.poll(1.0) else None
