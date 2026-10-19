"""Desktop-owned user input via the app's local follower IPC.

Discovery reads only the Desktop process environment. No task is resumed by
this module, and a possibly submitted call is never retried on another route.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import sqlite3
import struct
import subprocess
import time
from typing import Any, Callable
import uuid


class DesktopIpcError(RuntimeError):
    pass


class DesktopIpcUnavailable(DesktopIpcError):
    """The route failed before a mutating request could be submitted."""


class DesktopIpcInactive(DesktopIpcError):
    pass


class DesktopBridgeSafetyError(DesktopIpcError):
    """The Desktop instance or profile cannot be identified safely."""


class DesktopSubmissionUnknown(DesktopIpcError):
    """A mutating request may have been accepted; never fall back or retry."""


@dataclass(frozen=True)
class DesktopEndpoint:
    pid: int
    pipe: str | None
    profile: Path | None = field(default=None, compare=False)
    other_profiles: tuple[Path, ...] = field(default=(), compare=False)


_APP_TOOLS_PIPE = re.compile(
    r"\\\\\.\\pipe\\codex-(?:app-tools|browser-use)-[A-Za-z0-9][A-Za-z0-9-]{0,127}"
)


def _desktop_roots() -> list[dict[str, Any]]:
    if os.name != "nt":
        raise DesktopIpcUnavailable("Desktop 连接仅支持 Windows。")
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    command = (
        "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); "
        "@(Get-CimInstance Win32_Process -Filter \"Name = 'ChatGPT.exe'\" "
        "| Select-Object ProcessId,ExecutablePath,CommandLine) | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, encoding="utf-8", check=True,
            timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        records = json.loads(result.stdout or "[]")
        if isinstance(records, dict):
            records = [records]
        roots = []
        for record in records:
            path = Path(record.get("ExecutablePath") or "").resolve()
            if (path.name.lower() == "chatgpt.exe"
                    and path.parent.name.lower() == "app"
                    and path.parent.parent.name.lower().startswith("openai.codex_")
                    and path.parent.parent.parent.name.lower() == "windowsapps"
                    and "--type=" not in str(record.get("CommandLine") or "")
                    and path.is_file()):
                roots.append(record)
        return roots
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
        raise DesktopIpcUnavailable("无法查询正在运行的 Codex Desktop。") from exc


def _process_environment(pid: int) -> dict[str, str]:
    """Read a 64-bit Desktop's environment; return only profile/pipe values.

    Keep the Windows PEB layout isolated here. A changed layout, denied read,
    or truncated environment must fail closed instead of guessing a profile.
    """
    if os.name != "nt" or struct.calcsize("P") != 8:
        raise DesktopBridgeSafetyError("无法验证 Desktop 环境，请使用 64 位 Windows Python。")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                          ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    kernel32.ReadProcessMemory.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    class BasicInformation(ctypes.Structure):
        _fields_ = [("reserved1", ctypes.c_void_p), ("peb", ctypes.c_void_p),
                    ("reserved2", ctypes.c_void_p * 2), ("pid", ctypes.c_size_t),
                    ("reserved3", ctypes.c_void_p)]

    ntdll.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p,
                                               wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
    ntdll.NtQueryInformationProcess.restype = wintypes.LONG
    handle = kernel32.OpenProcess(0x410, False, pid)  # QUERY_INFORMATION | VM_READ
    if not handle:
        raise DesktopBridgeSafetyError("无法验证 Desktop 使用的环境目录。")
    try:
        def read(address: int, size: int) -> bytes:
            buffer = ctypes.create_string_buffer(size)
            received = ctypes.c_size_t()
            ok = kernel32.ReadProcessMemory(handle, address, buffer, size, ctypes.byref(received))
            if not ok and not received.value:
                raise DesktopBridgeSafetyError("读取 Desktop 环境失败，未提交消息。")
            return buffer.raw[:received.value]

        def pointer(address: int) -> int:
            raw = read(address, 8)
            if len(raw) != 8:
                raise DesktopBridgeSafetyError("Desktop 环境指针不完整，未提交消息。")
            value = struct.unpack("<Q", raw)[0]
            if value < 0x10000:
                raise DesktopBridgeSafetyError("Desktop 环境指针无效，未提交消息。")
            return value

        info = BasicInformation()
        size = wintypes.ULONG()
        status = ntdll.NtQueryInformationProcess(handle, 0, ctypes.byref(info), ctypes.sizeof(info), ctypes.byref(size))
        if status != 0 or not info.peb:
            raise DesktopBridgeSafetyError("无法读取 Desktop 环境信息，未提交消息。")
        parameters = pointer(int(info.peb) + 0x20)
        environment = pointer(parameters + 0x80)
        raw = bytearray()
        for _ in range(256):
            chunk = read(environment + len(raw), 1024)
            if not chunk or len(chunk) % 2:
                break
            raw.extend(chunk)
            decoded = raw.decode("utf-16-le", errors="surrogatepass")
            if "\0\0" in decoded:
                values = {}
                for line in decoded.split("\0\0", 1)[0].split("\0"):
                    key, _, value = line.partition("=")
                    if key.upper() in ("CODEX_HOME", "CODEX_APP_TOOLS_PIPE_PATH", "USERPROFILE", "HOME"):
                        values[key.upper()] = value
                return values
        raise DesktopBridgeSafetyError("Desktop 环境不完整，未提交消息。")
    finally:
        kernel32.CloseHandle(handle)


def discover_desktop(expected_home: Path | None = None) -> DesktopEndpoint:
    expected = (expected_home or Path.home() / ".codex").resolve()
    roots = _desktop_roots()
    candidates = []
    other_profiles = set()
    for record in roots:
        environment = _process_environment(int(record["ProcessId"]))
        profile_hint = environment.get("CODEX_HOME")
        if not profile_hint:
            user_profile, home = environment.get("USERPROFILE"), environment.get("HOME")
            if user_profile and home and Path(user_profile).resolve() != Path(home).resolve():
                raise DesktopBridgeSafetyError("Desktop 的用户目录存在歧义，未提交消息。")
            # A launcher can isolate USERPROFILE without setting CODEX_HOME.
            # Never substitute the bot's own home for another process's home.
            desktop_home = Path(user_profile or home or Path.home())
            profile_hint = str(desktop_home / ".codex")
        profile = Path(profile_hint).expanduser()
        if not profile.is_absolute():
            raise DesktopBridgeSafetyError("Desktop 的环境目录不是绝对路径，未提交消息。")
        if profile.resolve() != expected:
            other_profiles.add(profile.resolve())
            continue
        pipe = environment.get("CODEX_APP_TOOLS_PIPE_PATH") or None
        if pipe and not _APP_TOOLS_PIPE.fullmatch(pipe):
            raise DesktopBridgeSafetyError("Desktop 原生连接地址无法验证，未提交消息。")
        candidates.append(DesktopEndpoint(int(record["ProcessId"]), pipe))
    if len(candidates) > 1:
        raise DesktopBridgeSafetyError("同一环境有多个 Codex Desktop 实例，请关闭重复实例后再发送。")
    if not candidates:
        if roots:
            raise DesktopBridgeSafetyError("运行中的 Desktop 使用另一个环境，未向其他订阅实例提交消息。")
        raise DesktopIpcUnavailable("Codex Desktop 未运行，请启动 Desktop 后再发送。")
    chosen = candidates[0]
    return DesktopEndpoint(chosen.pid, chosen.pipe, expected, tuple(other_profiles))


def validate_thread_profile(endpoint: DesktopEndpoint, thread_id: str) -> None:
    """The global broker also has clients from other Desktop profiles.

    A copied thread ID in two running profiles cannot be safely routed by ID.
    The native, profile-specific read below proves the target exists here;
    this check rejects an ambiguous copy in another running profile.
    """
    for profile in endpoint.other_profiles:
        databases = [p for p in profile.glob('state_*.sqlite')
                     if re.fullmatch(r'state_[0-9]+\.sqlite', p.name)]
        databases.sort(key=lambda p: int(p.stem.split('_')[1]), reverse=True)
        if not databases:
            continue
        try:
            db = sqlite3.connect(databases[0].resolve().as_uri() + '?mode=ro',
                                 uri=True, timeout=0.2)
            try:
                duplicate = db.execute('SELECT 1 FROM threads WHERE id=?', (thread_id,)).fetchone()
            finally:
                db.close()
        except (OSError, sqlite3.Error) as exc:
            raise DesktopBridgeSafetyError('无法排除另一 Desktop 环境中的同名任务，未提交消息。') from exc
        if duplicate:
            raise DesktopBridgeSafetyError('同一任务编号存在于多个运行中的 Desktop 环境，未提交消息。')


def validate_pipe_server(handle: int, expected_pid: int) -> None:
    if os.name != "nt":
        raise DesktopBridgeSafetyError("无法验证 Desktop 连接进程。")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetNamedPipeServerProcessId.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
    kernel32.GetNamedPipeServerProcessId.restype = wintypes.BOOL
    pid = wintypes.ULONG()
    if not kernel32.GetNamedPipeServerProcessId(handle, ctypes.byref(pid)) or pid.value != expected_pid:
        raise DesktopBridgeSafetyError("Desktop 连接进程已改变或不属于当前环境，未提交消息。")


def _content_json(result: dict[str, Any]) -> dict[str, Any]:
    for item in result.get("contentItems", []):
        if not isinstance(item, dict) or item.get("type") != "inputText":
            continue
        try:
            value = json.loads(item.get("text", ""))
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            return value
    return {}


def _turn_id(result: dict[str, Any]) -> str:
    for key in ("turnId", "turn_id"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return value
    turn = result.get("turn")
    if isinstance(turn, dict) and isinstance(turn.get("id"), str):
        return turn["id"]
    nested = result.get("result")
    return _turn_id(nested) if isinstance(nested, dict) else ""


class DesktopAppTools:
    def __init__(self, transport_factory: Callable[..., Any], timeout: float = 35,
                 discover: Callable[[], DesktopEndpoint] = discover_desktop):
        self.transport_factory = transport_factory
        self.timeout = timeout
        self.discover = discover

    def for_current_instance(self) -> DesktopAppTools:
        """Pin a read batch to one verified profile/process, with new pipes.

        Every connection still verifies its server PID. A Desktop restart
        invalidates this batch instead of mixing snapshots from two apps.
        """
        endpoint = self.discover()
        return DesktopAppTools(self.transport_factory, self.timeout, lambda: endpoint)

    def _connect(self) -> tuple[Any, int]:
        endpoint = self.discover()
        if not endpoint.pipe:
            raise DesktopIpcUnavailable("当前 Desktop 未提供原生任务接口。")
        transport = self.transport_factory(timeout=self.timeout)
        transport.PIPE_PATH = endpoint.pipe
        handle = transport._open()
        try:
            validate_pipe_server(handle, endpoint.pid)
        except Exception:
            transport._close(handle)
            raise
        return transport, handle

    def _rpc(self, transport: Any, handle: int, method: str, params: dict[str, Any], *, mutating: bool = False) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        try:
            # Once a mutating frame is written (even partially), its outcome may
            # be unknown. Never make a second writer or retry through legacy IPC.
            transport._write(handle, request)
            deadline = time.monotonic() + self.timeout
            while True:
                response = transport._next_message(handle, deadline)
                if response.get("id") == request_id:
                    break
        except Exception as exc:
            if mutating:
                raise DesktopSubmissionUnknown("Desktop 提交结果未知：连接在发送期间中断。请先查看任务最新消息，避免重复发送。") from exc
            raise DesktopIpcUnavailable("Desktop 原生接口查询失败，消息尚未发送。") from exc
        error = response.get("error")
        if error:
            # Invalid parameters/method are rejected before dispatch. A generic
            # application error could occur after queuing, so keep it uncertain.
            rejected = isinstance(error, dict) and error.get("code") in (-32600, -32601, -32602)
            if mutating and not rejected:
                raise DesktopSubmissionUnknown("Desktop 未能确认提交结果。请先查看任务最新消息，避免重复发送。")
            raise DesktopIpcError("Desktop 拒绝了任务请求，消息未提交。")
        result = response.get("result")
        if not isinstance(result, dict):
            if mutating:
                raise DesktopSubmissionUnknown("Desktop 返回的提交结果不完整。请先查看任务最新消息，避免重复发送。")
            raise DesktopIpcUnavailable("Desktop 原生接口返回无效查询结果，消息尚未发送。")
        return result

    def _catalog(self, transport: Any, handle: int, required: set[str]) -> None:
        result = self._rpc(transport, handle, "tools/list", {"threadStartKind": "all"})
        names = {tool.get("name") for tool in result.get("tools", [])
                 if isinstance(tool, dict) and tool.get("namespace") == "codex_app"}
        if not required.issubset(names):
            raise DesktopIpcUnavailable("Desktop 原生接口缺少所需任务功能，消息尚未发送。")

    def _call(self, transport: Any, handle: int, tool: str, thread_id: str,
              arguments: dict[str, Any], *, mutating: bool = False) -> dict[str, Any]:
        call_id = str(uuid.uuid4())
        # These are transport correlation IDs, not model-issued tool/turn IDs.
        # The official MCP client uses the same mcp-turn fallback when absent.
        result = self._rpc(transport, handle, "tools/call", {
            "namespace": "codex_app", "tool": tool, "arguments": arguments,
            "callerSource": "codex", "callId": "telegram-" + call_id,
            "hostId": "local", "threadId": thread_id, "turnId": "mcp-turn-" + call_id,
        }, mutating=mutating)
        if result.get("success") is not True:
            text = " ".join(str(item.get("text", "")) for item in result.get("contentItems", [])
                            if isinstance(item, dict) and item.get("type") == "inputText")
            raise DesktopIpcError("Desktop 未接受任务请求：" + (text[:500] or "未返回成功状态。"))
        return result

    def read_thread(self, thread_id: str, *, timeout: float | None = None) -> dict[str, Any]:
        if timeout is not None:
            if timeout <= 0:
                raise ValueError('查询超时必须为正数。')
            return DesktopAppTools(self.transport_factory, min(timeout, self.timeout), self.discover).read_thread(thread_id)
        transport, handle = self._connect()
        try:
            self._catalog(transport, handle, {"read_thread"})
            result = self._call(transport, handle, "read_thread", thread_id,
                                {"threadId": thread_id, "hostId": "local", "turnLimit": 1,
                                 "includeOutputs": False, "maxOutputCharsPerItem": 0})
            return _content_json(result)
        finally:
            transport._close(handle)

    def list_threads(self, context_thread_id: str, *, timeout: float | None = None) -> dict[str, Any]:
        """Read live sidebar state, including every pinned task.

        This endpoint caps recent non-pinned items at 50. Callers must search
        the local metadata pages too when they need older running tasks.
        """
        if timeout is not None:
            if timeout <= 0:
                raise ValueError('查询超时必须为正数。')
            return DesktopAppTools(self.transport_factory, min(timeout, self.timeout), self.discover).list_threads(context_thread_id)
        transport, handle = self._connect()
        try:
            self._catalog(transport, handle, {'list_threads'})
            result = self._call(transport, handle, 'list_threads', context_thread_id, {'limit': 50})
            content = _content_json(result)
            if not isinstance(content.get('threads'), list) or not isinstance(content.get('pinnedThreads'), list):
                raise DesktopIpcUnavailable('Desktop 未返回有效任务列表，无法确认运行状态。')
            return content
        finally:
            transport._close(handle)

class DesktopControl:
    """Read through App Tools; submit human input only through its owner.

    send_message_to_thread deliberately represents an agent delegation. It is
    never a fallback for human Telegram input, even when no owner is found.
    """
    def __init__(self, transport_factory: Callable[..., Any], timeout: float = 35):
        self.native = DesktopAppTools(transport_factory, timeout)
        self.transport_factory = transport_factory
        self.timeout = timeout
        self._interrupt_capabilities: dict[str, tuple[float, bool]] = {}

    def can_interrupt(self, thread_id: str, *, probe: bool = True) -> bool:
        cached = self._interrupt_capabilities.get(thread_id)
        if cached and time.monotonic() - cached[0] < 30:
            return cached[1]
        if not probe:
            return False
        available = False
        try:
            available = self._legacy_for_current_desktop(thread_id).find_thread_owner(thread_id)
        except (DesktopIpcError, OSError, TimeoutError, ValueError):
            pass
        self._interrupt_capabilities[thread_id] = (time.monotonic(), bool(available))
        return bool(available)

    def _legacy_for_current_desktop(self, thread_id: str = '') -> Any:
        # The old broker is global. Verify its server is the same-profile app,
        # so a Cockpit/second-subscription instance cannot receive the message.
        endpoint = self.native.discover()
        if thread_id:
            validate_thread_profile(endpoint, thread_id)
        # Each operation has its own pipe handle and receive buffer. Observer
        # capability probes must not clear a concurrent submission's buffer.
        transport = self.transport_factory(timeout=self.timeout)
        transport.expected_server_pid = endpoint.pid
        return transport

    def send_follow_up(self, thread_id: str, text: str, cwd: str, *, start_if_inactive: bool = True,
                       expected_turn_id: str = '') -> dict[str, Any] | None:
        if not text.strip():
            raise ValueError('回复内容不能为空。')
        owner = self._legacy_for_current_desktop(thread_id)

        def guard() -> None:
            # Run after owner discovery and immediately before the one input
            # request. The native read is profile-specific and never resumes.
            if self.native.discover().pid != owner.expected_server_pid:
                raise DesktopBridgeSafetyError('Desktop 在提交前已重启，未发送消息；请重新检查任务。')
            state = self.native.read_thread(thread_id)
            thread = state.get('thread')
            if not isinstance(thread, dict) or thread.get('id') != thread_id:
                raise DesktopBridgeSafetyError('Desktop 返回的任务与目标不一致，未提交消息。')
            if not start_if_inactive or expected_turn_id:
                status = thread.get('status')
                if not isinstance(status, dict) or status.get('type') != 'active':
                    raise DesktopIpcInactive('该 Desktop 任务当前没有运行中的 Turn。')
            if expected_turn_id:
                turns = state.get('turns')
                if (not isinstance(turns, list) or not turns or not isinstance(turns[0], dict)
                        or turns[0].get('id') != expected_turn_id):
                    raise DesktopIpcInactive('Desktop 当前 Turn 与原问题不一致，未提交回答。')

        result = owner.send_follow_up(thread_id, text, cwd,
                                      start_if_inactive=start_if_inactive and not expected_turn_id,
                                      before_submit=guard)
        if result is not None and expected_turn_id and result.get('turn_id') != expected_turn_id:
            raise DesktopSubmissionUnknown('Desktop 接收消息的 Turn 与原问题不一致，请先核对；不会重发回答。')
        return result

    def interrupt_turn(self, thread_id: str, turn_id: str = "") -> bool:
        return self._legacy_for_current_desktop(thread_id).interrupt_turn(thread_id, turn_id)
