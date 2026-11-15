"""Opt-in Windows launcher that observes Codex Desktop's composer sample replies."""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

from .runtime import store_desktop_runtime


def prediction_request(line):
    if b'"thread/sample"' not in line or b'composer_predictions' not in line:
        return None
    try:
        message = json.loads(line)
        params = message.get("params") or {}
        request_id = message.get("id")
        thread, turn = params.get("threadId"), params.get("expectedTurnId")
        metadata = params.get("responsesapiClientMetadata") or {}
        if (message.get("method") == "thread/sample"
                and isinstance(request_id, (str, int))
                and params.get("threadSource") == "composer_predictions"
                and isinstance(thread, str)
                and isinstance(turn, str)
                and metadata.get("prediction_source_thread_id") == thread
                and metadata.get("prediction_source_turn_id") == turn):
            return request_id, (thread, turn, time.time())
    except (ValueError, TypeError, AttributeError):
        return None
    return None


def prediction_response(line, pending, lock):
    if b'"thread/sample:' not in line:
        return None
    try:
        message = json.loads(line)
        if "result" not in message and "error" not in message:
            return None
        with lock:
            request = pending.pop(message.get("id"), None)
        if request is None:
            return None
        thread, turn, requested = request
        result = message.get("result") or {}
        if result.get("sourceTurnId") != turn or not isinstance(result.get("text"), str):
            return None
        text = json.loads(result["text"]).get("suggestion")
        if isinstance(text, str) and text.strip():
            return {"threadId": thread, "turnId": turn, "text": text, "requested": requested}
    except (ValueError, TypeError, AttributeError):
        return None
    return None


def _publish(record, desktop_pid):
    try:
        from ...features import composer_replies
        composer_replies.record_prediction(record, desktop_pid)
    except Exception as exc:
        print(f"Composer prediction unavailable: {type(exc).__name__}", file=sys.stderr)


def main():
    if os.name != "nt":
        print("The Codex Telegram Desktop proxy requires Windows.", file=sys.stderr)
        return 1
    for parent in psutil.Process().parents():
        try:
            executable = store_desktop_runtime(Path(parent.exe()))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if executable is not None:
            desktop_pid = parent.pid
            break
    else:
        print("Start the proxy through the Microsoft Store Codex Desktop app.", file=sys.stderr)
        return 1

    args = sys.argv[1:]
    env = os.environ.copy()
    env.pop("CODEX_CLI_PATH", None)
    env["CODEX_APP_RUNTIME_CLI_PATH"] = str(executable)
    if "app-server" not in args:
        return subprocess.call([str(executable), *args], env=env)
    env["CODEX_TELEGRAM_COMPOSER_REPLIES"] = "1"
    child = subprocess.Popen([str(executable), *args], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, env=env)
    pending = {}
    lock = threading.Lock()

    def forward_input():
        try:
            for line in sys.stdin.buffer:
                request = prediction_request(line)
                if request is not None:
                    request_id, identifiers = request
                    with lock:
                        pending[request_id] = identifiers
                        if len(pending) > 32:
                            pending.pop(next(iter(pending)))
                child.stdin.write(line)
                child.stdin.flush()
        except (OSError, ValueError):
            pass
        finally:
            child.stdin.close()

    threading.Thread(target=forward_input, daemon=True).start()
    try:
        for line in child.stdout:
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()
            record = prediction_response(line, pending, lock)
            if record is not None:
                threading.Thread(target=_publish, args=(record, desktop_pid), daemon=True).start()
    except (OSError, ValueError):
        if child.poll() is None:
            child.terminate()
    finally:
        child.stdout.close()
    return child.wait()
