"""Compatibility facade over the one shared JSONL/process implementation."""
from pathlib import Path
import queue

from ...core.bot import CodexAppServer, RpcError

AppServerError = RpcError


class AppServerClient:
    def __init__(self, executable, log_path, *, argv=None):
        self.log_path = Path(log_path)
        self.events = queue.Queue()
        self.process = None
        self._closed = False
        self._client = CodexAppServer(self.events.put, self.events.put,
            runtime={'codex_executable': str(executable)}, argv=argv,
            stderr_callback=self._stderr)

    def _stderr(self, value):
        self.log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.log_path.open('a', encoding='utf-8') as output:
            self.log_path.chmod(0o600)
            output.write(value+'\n')

    def start(self):
        if self._closed:
            raise AppServerError('Client is closed')
        self._client.ensure_started()
        self.process = self._client.process
        return self._client.initialize_result

    def request(self, method, params=None, timeout=45):
        if self._closed:
            raise AppServerError('Client is closed')
        return self._client.request(method, params, timeout)

    def send(self, message):
        if self._closed:
            raise AppServerError('Client is closed')
        self._client._send(message)

    def respond(self, request_id, result=None, error=None):
        self.send({'id': request_id, **({'error': error} if error else {'result': result or {}})})

    def close(self):
        self._closed = True
        self._client.stop()
