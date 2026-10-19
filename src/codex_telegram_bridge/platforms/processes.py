"""Only children launched by this service are eligible for cleanup."""
import os
import signal


def spawn_options() -> dict:
    return {} if os.name == 'nt' else {'start_new_session': True}


class PosixChildProcessJob:
    def __init__(self):
        self.processes = []
        self.watchdogs = []

    def attach(self, process):
        if os.getpgid(process.pid) != process.pid:
            raise RuntimeError('Owned child was not started in its own process group.')
        self.processes.append(process)
        import psutil
        import subprocess
        import sys
        watcher=subprocess.Popen([sys.executable,'-m','codex_telegram_bridge.platforms.posix.watchdog',
            str(os.getpid()),str(psutil.Process().create_time()),str(process.pid),str(psutil.Process(process.pid).create_time())],
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
        self.watchdogs.append(watcher)

    def close(self):
        for process in self.processes:
            # Popen retains the unreaped PID, so it cannot be recycled here.
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.processes.clear()
        for watcher in self.watchdogs:
            if watcher.poll() is None:
                watcher.terminate()
            watcher.wait(timeout=5)
        self.watchdogs.clear()


if os.name == 'nt':
    from .windows.processes import ChildProcessJob
else:
    ChildProcessJob = PosixChildProcessJob
