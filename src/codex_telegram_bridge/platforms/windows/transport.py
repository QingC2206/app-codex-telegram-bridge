from __future__ import annotations
from codex_telegram_bridge.i18n import tr as _t
import ctypes,json,os,struct,time
from ctypes import wintypes
from typing import Any
from ...codex.errors import DesktopIpcError,DesktopIpcUnavailable
from .discovery import validate_pipe_server

class WindowsTransport:
    def __init__(self, timeout: float = 35):
        self.timeout = timeout
        self.expected_server_pid: int | None = None
        self._buffer = bytearray()
        if os.name != "nt":
            self.kernel32 = None
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
        kernel32.WaitNamedPipeW.restype = wintypes.BOOL
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.WriteFile.argtypes = [
            wintypes.HANDLE,
            wintypes.LPCVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        kernel32.WriteFile.restype = wintypes.BOOL
        kernel32.ReadFile.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        kernel32.ReadFile.restype = wintypes.BOOL
        kernel32.PeekNamedPipe.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.PeekNamedPipe.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        self.kernel32 = kernel32

    @staticmethod
    def encode_frame(message: dict[str, Any]) -> bytes:
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return struct.pack("<I", len(payload)) + payload

    def _open(self) -> int:
        if self.kernel32 is None:
            raise DesktopIpcUnavailable(_t('Codex Desktop IPC 仅支持 Windows。'))
        wait_ms = max(1, int(min(self.timeout, 10) * 1000))
        if not self.kernel32.WaitNamedPipeW(self.PIPE_PATH, wait_ms):
            error = ctypes.get_last_error()
            raise DesktopIpcUnavailable(_t('Codex Desktop IPC 不可用（Windows 错误 {p0}）。', p0=format(error, '')))
        handle = self.kernel32.CreateFileW(
            self.PIPE_PATH,
            0xC0000000,  # GENERIC_READ | GENERIC_WRITE
            0,
            None,
            3,  # OPEN_EXISTING
            0,
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if handle == invalid_handle:
            error = ctypes.get_last_error()
            raise DesktopIpcUnavailable(_t('无法连接 Codex Desktop IPC（Windows 错误 {p0}）。', p0=format(error, '')))
        if self.expected_server_pid is not None:
            try:
                validate_pipe_server(int(handle), self.expected_server_pid)
            except Exception:
                self.kernel32.CloseHandle(handle)
                raise
        self._buffer.clear()
        return int(handle)

    def _close(self, handle: int) -> None:
        if self.kernel32 is not None:
            self.kernel32.CloseHandle(handle)

    def _write(self, handle: int, message: dict[str, Any]) -> None:
        if self.kernel32 is None:
            raise DesktopIpcError(_t('Codex Desktop IPC 不可用。'))
        frame = self.encode_frame(message)
        buffer = ctypes.create_string_buffer(frame)
        written = wintypes.DWORD()
        if not self.kernel32.WriteFile(
            handle,
            buffer,
            len(frame),
            ctypes.byref(written),
            None,
        ):
            raise DesktopIpcError(_t('写入 Codex Desktop IPC 失败（Windows 错误 {p0}）。', p0=format(ctypes.get_last_error(), '')))
        if written.value != len(frame):
            raise DesktopIpcError(_t('Codex Desktop IPC 消息未完整写入。'))

    def _next_message(self, handle: int, deadline: float) -> dict[str, Any]:
        if self.kernel32 is None:
            raise DesktopIpcError(_t('Codex Desktop IPC 不可用。'))
        while time.monotonic() < deadline:
            if len(self._buffer) >= 4:
                length = struct.unpack("<I", self._buffer[:4])[0]
                if length <= 0 or length > 256 * 1024 * 1024:
                    raise DesktopIpcError(_t('Codex Desktop IPC 返回了无效帧长度：{p0}', p0=format(length, '')))
                if len(self._buffer) >= 4 + length:
                    raw = bytes(self._buffer[4 : 4 + length])
                    del self._buffer[: 4 + length]
                    value = json.loads(raw.decode("utf-8"))
                    if not isinstance(value, dict):
                        raise DesktopIpcError(_t('Codex Desktop IPC 返回了无效消息。'))
                    return value

            available = wintypes.DWORD()
            if not self.kernel32.PeekNamedPipe(
                handle,
                None,
                0,
                None,
                ctypes.byref(available),
                None,
            ):
                raise DesktopIpcError(
                    _t('读取 Codex Desktop IPC 失败（Windows 错误 {p0}）。', p0=format(ctypes.get_last_error(), ''))
                )
            if available.value:
                size = min(available.value, 1024 * 1024)
                chunk = ctypes.create_string_buffer(size)
                read = wintypes.DWORD()
                if not self.kernel32.ReadFile(
                    handle,
                    chunk,
                    size,
                    ctypes.byref(read),
                    None,
                ):
                    raise DesktopIpcError(
                        _t('读取 Codex Desktop IPC 失败（Windows 错误 {p0}）。', p0=format(ctypes.get_last_error(), ''))
                    )
                self._buffer.extend(chunk.raw[: read.value])
                continue
            time.sleep(0.03)
        raise DesktopIpcError(_t('等待 Codex Desktop 响应超时。'))
