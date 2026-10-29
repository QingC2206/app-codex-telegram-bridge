"""One controller for the host OS, regardless of account or configuration.

A bound, non-listening TCP socket is a kernel-owned machine-wide reservation.
It needs no shared writable directory or elevation, and is released on exit.
The endpoint is deliberately not configurable by profile, bot, user or env.
"""
import errno
import os
import socket
import weakref

from ..i18n import tr as _t


_MACHINE_ENDPOINT = ('127.0.0.1', 27321)
_held = weakref.WeakSet()


def _close_after_fork():
    # A child must not keep its parent's reservation alive after that parent exits.
    for instance in list(_held):
        instance.close()


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_close_after_fork)


class SingleInstance:
    def __init__(self, name: str = ''):
        # Kept for import compatibility; a name can never partition this lock.
        self.name = name
        self.handle = None

    def acquire(self) -> bool:
        if self.handle is not None:
            return True
        handle = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            handle.set_inheritable(False)
            if os.name == 'nt':
                # Windows otherwise allows another socket to force address reuse.
                handle.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            handle.bind(_MACHINE_ENDPOINT)
            # Never listen, accept, connect or enable SO_REUSEADDR/SO_REUSEPORT.
            self.handle = handle
            _held.add(self)
            return True
        except OSError as exc:
            handle.close()
            if exc.errno in {errno.EADDRINUSE, errno.EACCES} or getattr(exc, 'winerror', None) in {10048, 10013}:
                return False
            raise
        except BaseException:
            handle.close()
            raise

    def close(self) -> None:
        if self.handle:
            self.handle.close(); self.handle = None
        _held.discard(self)

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError(_t('整机启动锁已被占用，本机只允许一个机器人主服务。请先停止已有服务；若没有运行中的机器人，请检查本机端口 27321 是否被其他程序占用。'))
        return self

    def __exit__(self, *_):
        self.close()
