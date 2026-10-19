"""Length-prefixed Desktop IPC over a verified same-user Unix socket."""
import ctypes
import json
import os
from pathlib import Path
import socket
import stat
import struct
import sys
import time

from ...codex.errors import DesktopBridgeSafetyError, DesktopIpcError, DesktopIpcUnavailable


def validate_socket_path(path):
    path = Path(path)
    if not path.is_absolute():
        raise DesktopBridgeSafetyError('Desktop socket must be an absolute path.')
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise DesktopBridgeSafetyError('Desktop socket ownership or permissions are unsafe.')
    parent = path.parent.stat()
    # /tmp itself is shared and sticky; a private native-pipe directory must
    # belong to this user. The peer PID/UID is still verified after connecting.
    if parent.st_mode & 0o022 and not parent.st_mode & stat.S_ISVTX:
        raise DesktopBridgeSafetyError('Desktop socket directory is writable by others.')
    return (info.st_dev, info.st_ino)


def validate_peer(handle, expected_pid):
    if sys.platform == 'darwin':
        # Darwin LOCAL_PEERPID is 2 at SOL_LOCAL (0).
        pid = struct.unpack('i', handle.getsockopt(0, 2, 4))[0]
        libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
        uid, gid = ctypes.c_uint(), ctypes.c_uint()
        libc.getpeereid.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
        if libc.getpeereid(handle.fileno(), ctypes.byref(uid), ctypes.byref(gid)):
            raise DesktopBridgeSafetyError('Cannot verify Desktop socket user.')
        owner = uid.value
    else:
        pid, owner, _ = struct.unpack('3i', handle.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    if owner != os.getuid() or pid != expected_pid:
        raise DesktopBridgeSafetyError('Desktop socket belongs to another process or user.')


class UnixTransport:
    def __init__(self, timeout=35):
        self.timeout = timeout
        self.expected_server_pid = None
        self._buffer = bytearray()

    @staticmethod
    def encode_frame(message):
        payload = json.dumps(message, ensure_ascii=False, separators=(',', ':')).encode()
        return struct.pack('<I', len(payload)) + payload

    def _open(self):
        handle = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            identity = validate_socket_path(self.PIPE_PATH)
            handle.settimeout(min(self.timeout, 10))
            handle.connect(self.PIPE_PATH)
            if validate_socket_path(self.PIPE_PATH) != identity:
                raise DesktopBridgeSafetyError('Desktop socket was replaced during connection.')
            if self.expected_server_pid is not None:
                validate_peer(handle, self.expected_server_pid)
            self._buffer.clear()
            return handle
        except Exception as exc:
            handle.close()
            if isinstance(exc, DesktopBridgeSafetyError):
                raise
            raise DesktopIpcUnavailable('Desktop Unix socket is unavailable.') from exc

    def _close(self, handle):
        handle.close()

    def _write(self, handle, message):
        handle.settimeout(self.timeout)
        handle.sendall(self.encode_frame(message))

    def _next_message(self, handle, deadline):
        while True:
            if len(self._buffer) >= 4:
                length = struct.unpack('<I', self._buffer[:4])[0]
                if not 0 < length <= 256 * 1024 * 1024:
                    raise DesktopIpcError('Invalid Desktop frame length.')
                if len(self._buffer) >= length + 4:
                    raw = bytes(self._buffer[4:4+length]); del self._buffer[:4+length]
                    value = json.loads(raw)
                    if not isinstance(value, dict):
                        raise DesktopIpcError('Invalid Desktop frame content.')
                    return value
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DesktopIpcError('Desktop response timed out.')
            handle.settimeout(remaining)
            chunk = handle.recv(1024 * 1024)
            if not chunk:
                raise DesktopIpcError('Desktop closed the connection (check app version and local peer authorization).')
            self._buffer.extend(chunk)
