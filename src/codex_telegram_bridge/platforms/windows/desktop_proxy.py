"""Optional stdio relay for the official Windows Desktop's own app-server."""
import os
from pathlib import Path
import subprocess
import sys
import threading

import psutil

from ...paths import codex_home
from .composer_capture import Capture, MAX_FRAME
from .runtime import _packaged_runtime_root, _registered_package_path


PROXY = 'codex-telegram-desktop-proxy.exe'
PROFILE_MARKER = 'CODEX_TELEGRAM_SUGGESTIONS_PROFILE'


def bundled_runtime(desktop):
    desktop = Path(desktop).resolve()
    candidate = desktop.parent / 'resources' / 'codex.exe'
    if desktop.name.lower() != 'chatgpt.exe' or not desktop.is_file() or not candidate.is_file():
        return None
    package = _packaged_runtime_root(candidate)
    if (package != desktop.parent.parent or candidate.resolve() != candidate
            or _registered_package_path(package.name) != package):
        return None
    return candidate


def desktop_parent():
    # The pip console launcher and Windows venv redirector can both be parents.
    # A shell, arbitrary CLI or renderer in the middle is a different caller.
    scripts = Path(sys.executable).resolve().parent
    for parent in psutil.Process().parents():
        exe = Path(parent.exe()).resolve()
        native = bundled_runtime(exe) if exe.name.lower() == 'chatgpt.exe' else None
        if native and not any(a == '--type' or a.startswith('--type=') for a in parent.cmdline()):
            return parent.pid, native
        if exe not in (scripts / PROXY, scripts / 'python.exe', scripts / 'pythonw.exe'):
            break
    raise RuntimeError('Launch the relay from the registered Codex Desktop.')


def pump(source, target, consume, reset):
    read = getattr(source, 'read1', source.read)
    line = bytearray()
    skip = False
    while chunk := read(65536):
        # Observe before releasing a request to the child, including if its
        # response immediately follows. All bytes are always forwarded unchanged.
        if consume is not None:
            try:
                start = 0
                while start < len(chunk):
                    end = chunk.find(b'\n', start)
                    stop = end if end >= 0 else len(chunk)
                    if not skip:
                        if len(line) + stop - start > MAX_FRAME:
                            reset()
                            skip = True
                            line.clear()
                        else:
                            line.extend(chunk[start:stop])
                    if end < 0:
                        break
                    if not skip:
                        consume(bytes(line))
                    skip = False
                    line.clear()
                    start = end + 1
            except Exception:
                reset()
                consume = None
        target.write(chunk)
        target.flush()


def relay(executable, args, env, capture):
    child = subprocess.Popen([str(executable), *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             env=env, creationflags=subprocess.CREATE_NO_WINDOW)
    def forward(source, target, incoming):
        observe = (lambda line: capture.consume(line, incoming=incoming)) if capture else None
        pump(source, target, observe, capture.reset if capture else None)

    def forward_input():
        try:
            with os.fdopen(os.dup(sys.stdin.fileno()), 'rb', buffering=0) as input_stream:
                forward(input_stream, child.stdin, True)
        except (OSError, ValueError):
            pass
        finally:
            try:
                child.stdin.close()
            except OSError:
                pass

    threading.Thread(target=forward_input, daemon=True).start()
    try:
        forward(child.stdout, sys.stdout.buffer, False)
    except (OSError, ValueError):
        if child.poll() is None:
            child.terminate()
    finally:
        child.stdout.close()
    return child.wait()


def main():
    if os.name != 'nt':
        return 1
    try:
        pid, native = desktop_parent()
    except (OSError, RuntimeError, psutil.Error):
        print('A verified Windows Codex Desktop parent is required.', file=sys.stderr)
        return 1
    args = sys.argv[1:]
    env = os.environ.copy()
    marker = env.pop(PROFILE_MARKER, '')
    env['CODEX_CLI_PATH'] = env['CODEX_APP_RUNTIME_CLI_PATH'] = str(native)
    from .desktop_launch import enabled
    profile = codex_home()
    if ('app-server' not in args or '--remote' in args or not marker
            or Path(marker) != profile or not enabled()):
        return subprocess.call([str(native), *args], env=env)
    # Source setup is optional. An unavailable publisher must not interrupt Desktop.
    publisher = None
    try:
        from .suggestion_pipe import Publisher
        capture = Capture(profile, pid)
        publisher = Publisher(capture)
    except Exception as exc:
        print('Suggestion source unavailable: ' + type(exc).__name__, file=sys.stderr)
        return subprocess.call([str(native), *args], env=env)
    try:
        return relay(native, args, env, capture)
    finally:
        publisher.close()
