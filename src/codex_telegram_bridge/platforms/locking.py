"""Per-profile lock, released by the OS even after a crash."""
import hashlib
import os
from ..paths import ensure_data_dir


class SingleInstance:
    def __init__(self, name: str):
        self.name = name
        self.handle = None

    def acquire(self) -> bool:
        path = ensure_data_dir() / ('service-' + hashlib.sha256(self.name.encode()).hexdigest()[:12] + '.lock')
        self.handle = open(path, 'a+b')
        if not os.fstat(self.handle.fileno()).st_size:
            self.handle.write(b'0'); self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (OSError, BlockingIOError):
            self.handle.close(); self.handle = None
            return False

    def close(self) -> None:
        if self.handle:
            self.handle.close(); self.handle = None
