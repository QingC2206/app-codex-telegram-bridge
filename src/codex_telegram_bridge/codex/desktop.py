"""Desktop-owned user input via the app's local follower IPC.

Discovery reads only the Desktop process environment. Owner recovery opens the
existing task in the verified Desktop; it never creates another backend writer.
A possibly submitted input is never retried on another route.
"""
from __future__ import annotations
from codex_telegram_bridge.i18n import tr as _t

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
from .errors import *
from ..paths import codex_home
from ..platforms.discovery import desktop_roots as _desktop_roots, process_environment as _process_environment, validate_pipe_server, valid_native_endpoint



@dataclass(frozen=True)
class DesktopEndpoint:
    pid: int
    pipe: str | None
    profile: Path | None = field(default=None, compare=False)
    other_profiles: tuple[Path, ...] = field(default=(), compare=False)


_APP_TOOLS_PIPE = re.compile(
    r"\\\\\.\\pipe\\codex-(?:app-tools|browser-use)-[A-Za-z0-9][A-Za-z0-9-]{0,127}"
)



def discover_desktop(expected_home: Path | None = None) -> DesktopEndpoint:
    expected = (expected_home or codex_home()).resolve()
    roots = _desktop_roots()
    candidates = []
    other_profiles = set()
    for record in roots:
        environment = _process_environment(int(record["ProcessId"]))
        profile_hint = environment.get("CODEX_HOME")
        if not profile_hint:
            user_profile, home = environment.get("USERPROFILE"), environment.get("HOME")
            if user_profile and home and Path(user_profile).resolve() != Path(home).resolve():
                raise DesktopBridgeSafetyError(_t('Desktop 的用户目录存在歧义，未提交消息。'))
            # A launcher can isolate USERPROFILE without setting CODEX_HOME.
            # Never substitute the bot's own home for another process's home.
            desktop_home = Path(user_profile or home or Path.home())
            profile_hint = str(desktop_home / ".codex")
        profile = Path(profile_hint).expanduser()
        if not profile.is_absolute():
            raise DesktopBridgeSafetyError(_t('Desktop 的环境目录不是绝对路径，未提交消息。'))
        if profile.resolve() != expected:
            other_profiles.add(profile.resolve())
            continue
        pipe = environment.get("CODEX_APP_TOOLS_PIPE_PATH") or None
        if pipe and not valid_native_endpoint(pipe):
            raise DesktopBridgeSafetyError(_t('Desktop 原生连接地址无法验证，未提交消息。'))
        candidates.append(DesktopEndpoint(int(record["ProcessId"]), pipe))
    if len(candidates) > 1:
        raise DesktopBridgeSafetyError(_t('同一环境有多个 Codex Desktop 实例，请关闭重复实例后再发送。'))
    if not candidates:
        if roots:
            raise DesktopBridgeSafetyError(_t('运行中的 Desktop 使用另一个环境，未向其他订阅实例提交消息。'))
        raise DesktopIpcUnavailable(_t('Codex Desktop 未运行，请启动 Desktop 后再发送。'))
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
            raise DesktopBridgeSafetyError(_t('无法排除另一 Desktop 环境中的同名任务，未提交消息。')) from exc
        if duplicate:
            raise DesktopBridgeSafetyError(_t('同一任务编号存在于多个运行中的 Desktop 环境，未提交消息。'))



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
            raise DesktopIpcUnavailable(_t('当前 Desktop 未提供原生任务接口。'))
        remaining = min(self.timeout, getattr(self, '_deadline', float('inf')) - time.monotonic())
        if remaining <= 0:
            raise TimeoutError('Desktop read deadline exceeded')
        transport = self.transport_factory(timeout=remaining)
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
            if hasattr(self, '_deadline'):
                remaining = self._deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Desktop read deadline exceeded')
                transport.timeout = min(self.timeout, remaining)
            # Once a mutating frame is written (even partially), its outcome may
            # be unknown. Never make a second writer or retry through legacy IPC.
            transport._write(handle, request)
            deadline = min(time.monotonic() + self.timeout, getattr(self, '_deadline', float('inf')))
            while True:
                response = transport._next_message(handle, deadline)
                if response.get("id") == request_id:
                    break
        except Exception as exc:
            if mutating:
                raise DesktopSubmissionUnknown(_t('Desktop 提交结果未知：连接在发送期间中断。请先查看任务最新消息，避免重复发送。')) from exc
            raise DesktopIpcUnavailable(_t('Desktop 原生接口查询失败，消息尚未发送。')) from exc
        error = response.get("error")
        if error:
            # Invalid parameters/method are rejected before dispatch. A generic
            # application error could occur after queuing, so keep it uncertain.
            rejected = isinstance(error, dict) and error.get("code") in (-32600, -32601, -32602)
            if mutating and not rejected:
                raise DesktopSubmissionUnknown(_t('Desktop 未能确认提交结果。请先查看任务最新消息，避免重复发送。'))
            raise DesktopIpcError(_t('Desktop 拒绝了任务请求，消息未提交。'))
        result = response.get("result")
        if not isinstance(result, dict):
            if mutating:
                raise DesktopSubmissionUnknown(_t('Desktop 返回的提交结果不完整。请先查看任务最新消息，避免重复发送。'))
            raise DesktopIpcUnavailable(_t('Desktop 原生接口返回无效查询结果，消息尚未发送。'))
        return result

    def _catalog(self, transport: Any, handle: int, required: set[str]) -> None:
        result = self._rpc(transport, handle, "tools/list", {"threadStartKind": "all"})
        names = {tool.get("name") for tool in result.get("tools", [])
                 if isinstance(tool, dict) and tool.get("namespace") == "codex_app"}
        if not required.issubset(names):
            raise DesktopIpcUnavailable(_t('Desktop 原生接口缺少所需任务功能，消息尚未发送。'))

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
            raise DesktopIpcError(_t('Desktop 未接受任务请求：') + (text[:500] or _t('未返回成功状态。')))
        return result

    def read_thread(self, thread_id: str, *, timeout: float | None = None) -> dict[str, Any]:
        if timeout is not None:
            if timeout <= 0:
                raise ValueError(_t('查询超时必须为正数。'))
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

    def read_submission_history(self, thread_id: str, *, timeout: float) -> dict[str, Any]:
        """Read bounded user items from Desktop when local rollouts lag behind.

        Discovery, catalog lookup and history share one budget. This connection
        is independent of submissions and never starts or resumes a thread.
        """
        if timeout <= 0:
            raise TimeoutError('Desktop read deadline exceeded')
        reader = DesktopAppTools(self.transport_factory, min(timeout, self.timeout), self.discover)
        reader._deadline = time.monotonic() + min(timeout, self.timeout)
        transport, handle = reader._connect()
        try:
            reader._catalog(transport, handle, {'read_thread'})
            if time.monotonic() >= reader._deadline:
                raise TimeoutError('Desktop read deadline exceeded')
            result = reader._call(transport, handle, 'read_thread', thread_id,
                {'threadId': thread_id, 'hostId': 'local', 'turnLimit': 3,
                 'includeOutputs': False, 'maxOutputCharsPerItem': 20000})
            if time.monotonic() >= reader._deadline:
                raise TimeoutError('Desktop read deadline exceeded')
            return _content_json(result)
        finally:
            transport._close(handle)

    def open_thread(self, thread_id: str, *, deadline: float, before_open: Callable[[], None]) -> None:
        """Open a verified local task without sending input or resuming via CLI.

        Navigation changes the app's visible task. Its acknowledgement does not
        prove that a follower owner is ready; the caller must discover it again.
        """
        reader = DesktopAppTools(self.transport_factory, self.timeout, self.discover)
        reader._deadline = deadline
        transport, handle = reader._connect()
        try:
            reader._catalog(transport, handle, {'read_thread', 'navigate_to_codex_page'})
            state = _content_json(reader._call(transport, handle, 'read_thread', thread_id,
                {'threadId': thread_id, 'hostId': 'local', 'turnLimit': 1,
                 'includeOutputs': False, 'maxOutputCharsPerItem': 0}))
            thread = state.get('thread')
            if not isinstance(thread, dict) or thread.get('id') != thread_id:
                raise DesktopBridgeSafetyError(_t('Desktop 返回的任务与目标不一致，未提交消息。'))
            before_open()
            # This UI operation carries no user input. A lost navigation reply
            # must not be treated as a possibly submitted user message.
            result = _content_json(reader._call(transport, handle, 'navigate_to_codex_page',
                                                thread_id, {'threadId': thread_id}))
            if result.get('navigated') is not True:
                raise DesktopIpcUnavailable(_t('Desktop 未确认已打开目标任务，消息尚未发送。'))
        finally:
            transport._close(handle)

    def list_threads(self, context_thread_id: str, *, timeout: float | None = None) -> dict[str, Any]:
        """Read live sidebar state, including every pinned task.

        This endpoint caps recent non-pinned items at 50. Callers must search
        the local metadata pages too when they need older running tasks.
        """
        if timeout is not None:
            if timeout <= 0:
                raise ValueError(_t('查询超时必须为正数。'))
            return DesktopAppTools(self.transport_factory, min(timeout, self.timeout), self.discover).list_threads(context_thread_id)
        transport, handle = self._connect()
        try:
            self._catalog(transport, handle, {'list_threads'})
            result = self._call(transport, handle, 'list_threads', context_thread_id, {'limit': 50})
            content = _content_json(result)
            if not isinstance(content.get('threads'), list) or not isinstance(content.get('pinnedThreads'), list):
                raise DesktopIpcUnavailable(_t('Desktop 未返回有效任务列表，无法确认运行状态。'))
            return content
        finally:
            transport._close(handle)

def thread_activity(state: dict[str, Any], thread_id: str) -> tuple[bool, str]:
    """Parse live Desktop state, independent of a renderer's loaded owner."""
    thread = state.get('thread')
    if not isinstance(thread, dict) or thread.get('id') != thread_id:
        raise DesktopBridgeSafetyError(_t('Desktop 返回的任务与目标不一致，未提交消息。'))
    status = thread.get('status')
    kind = status.get('type') if isinstance(status, dict) else None
    if kind not in {'active', 'idle'}:
        raise DesktopIpcUnavailable(_t('Desktop 当前任务状态未能核实，请稍后重试。'))
    turns = state.get('turns')
    latest = turns[0] if isinstance(turns, list) and turns and isinstance(turns[0], dict) else {}
    turn_id = latest.get('id')
    return kind == 'active', turn_id if kind == 'active' and isinstance(turn_id, str) else ''


class DesktopControl:
    """Read through App Tools; submit human input only through its owner.

    send_message_to_thread deliberately represents an agent delegation. It is
    never a fallback for human Telegram input, even when no owner is found.
    """
    def __init__(self, transport_factory: Callable[..., Any], timeout: float = 10):
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
            endpoint = self.native.discover()
            available = self._legacy_for_current_desktop(thread_id, endpoint=endpoint).find_thread_owner(thread_id)
            if not available:
                # A remote iOS turn can be active before any renderer owns it.
                # Check the recovery capability without navigating or resuming.
                native = DesktopAppTools(self.native.transport_factory, self.timeout, lambda: endpoint)
                native._deadline = time.monotonic() + min(self.timeout, 10.0)
                transport, handle = native._connect()
                try:
                    native._catalog(transport, handle, {'read_thread', 'navigate_to_codex_page'})
                    state = _content_json(native._call(transport, handle, 'read_thread', thread_id,
                        {'threadId': thread_id, 'hostId': 'local', 'turnLimit': 1,
                         'includeOutputs': False, 'maxOutputCharsPerItem': 0}))
                    active, turn_id = thread_activity(state, thread_id)
                    self._check_instance(endpoint, thread_id)
                    available = active and bool(turn_id)
                finally:
                    transport._close(handle)
        except (DesktopIpcError, OSError, TimeoutError, ValueError):
            pass
        self._interrupt_capabilities[thread_id] = (time.monotonic(), bool(available))
        return bool(available)

    def _legacy_for_current_desktop(self, thread_id: str = '', *,
                                    endpoint: DesktopEndpoint | None = None,
                                    timeout: float | None = None) -> Any:
        # The old broker is global. Verify its server is the same-profile app,
        # so a Cockpit/second-subscription instance cannot receive the message.
        endpoint = endpoint or self.native.discover()
        if thread_id:
            validate_thread_profile(endpoint, thread_id)
        # Each operation has its own pipe handle and receive buffer. Observer
        # capability probes must not clear a concurrent submission's buffer.
        transport = self.transport_factory(timeout=self.timeout if timeout is None else timeout)
        transport.expected_server_pid = endpoint.pid
        if os.name != "nt":
            from ..platforms.transport import default_broker_path
            transport.PIPE_PATH = default_broker_path(endpoint.profile)
        return transport

    def _check_instance(self, endpoint: DesktopEndpoint, thread_id: str) -> None:
        current = self.native.discover()
        if current != endpoint or current.profile != endpoint.profile:
            raise DesktopBridgeSafetyError(_t('Desktop 在提交前已重启，未发送消息；请重新检查任务。'))
        validate_thread_profile(current, thread_id)

    def _recover_thread_owner(self, endpoint: DesktopEndpoint, thread_id: str) -> None:
        # Called only after discovery proved that no input frame was sent.
        # The native pipe and every broker probe stay pinned to this instance.
        deadline = time.monotonic() + min(self.timeout, 10.0)
        check = lambda: self._check_instance(endpoint, thread_id)
        check()
        native = DesktopAppTools(self.native.transport_factory, self.timeout, lambda: endpoint)
        native.open_thread(thread_id, deadline=deadline, before_open=check)
        while time.monotonic() < deadline:
            check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            probe = self._legacy_for_current_desktop(thread_id, endpoint=endpoint, timeout=remaining)
            probe._deadline = deadline
            if probe.find_thread_owner(thread_id) and time.monotonic() < deadline:
                self._interrupt_capabilities.pop(thread_id, None)
                return
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(0.25, remaining))
        raise DesktopIpcUnavailable(_t('Desktop 已尝试打开目标任务，但回复通道尚未就绪。消息未发送，请在 Desktop 检查任务后再试。'))

    def send_follow_up(self, thread_id: str, text: str, cwd: str, *, start_if_inactive: bool = True,
                       expected_turn_id: str = '', recover_owner: bool = False) -> dict[str, Any] | None:
        if not text.strip():
            raise ValueError(_t('回复内容不能为空。'))
        endpoint = self.native.discover()
        owner = self._legacy_for_current_desktop(thread_id, endpoint=endpoint)
        reader = DesktopAppTools(self.native.transport_factory, self.native.timeout, lambda: endpoint)

        def guard() -> None:
            # Run after owner discovery and immediately before the one input
            # request. The native read is profile-specific and never resumes.
            self._check_instance(endpoint, thread_id)
            state = reader.read_thread(thread_id)
            thread = state.get('thread')
            if not isinstance(thread, dict) or thread.get('id') != thread_id:
                raise DesktopBridgeSafetyError(_t('Desktop 返回的任务与目标不一致，未提交消息。'))
            if not start_if_inactive or expected_turn_id:
                status = thread.get('status')
                if not isinstance(status, dict) or status.get('type') != 'active':
                    raise DesktopIpcInactive(_t('该 Desktop 任务当前没有运行中的 Turn。'))
            if expected_turn_id:
                turns = state.get('turns')
                if (not isinstance(turns, list) or not turns or not isinstance(turns[0], dict)
                        or turns[0].get('id') != expected_turn_id):
                    raise DesktopIpcInactive(_t('Desktop 当前 Turn 与原问题不一致，未提交回答。'))

        result = owner.send_follow_up(thread_id, text, cwd,
                                      start_if_inactive=start_if_inactive and not expected_turn_id,
                                      before_submit=guard)
        if result is None and recover_owner:
            # None is returned exclusively by pre-input owner discovery. Do
            # not catch dispatch errors here, especially unknown outcomes.
            self._recover_thread_owner(endpoint, thread_id)
            self._check_instance(endpoint, thread_id)
            owner = self._legacy_for_current_desktop(thread_id, endpoint=endpoint)
            result = owner.send_follow_up(thread_id, text, cwd,
                                          start_if_inactive=start_if_inactive and not expected_turn_id,
                                          before_submit=guard)
            if result is not None:
                result['owner_recovered'] = True
        if result is not None and expected_turn_id and result.get('turn_id') != expected_turn_id:
            raise DesktopSubmissionUnknown(_t('Desktop 接收消息的 Turn 与原问题不一致，请先核对；不会重发回答。'))
        return result

    def interrupt_turn(self, thread_id: str, turn_id: str = "", *, recover_owner: bool = False) -> bool:
        endpoint = self.native.discover()
        reader = DesktopAppTools(self.native.transport_factory, self.timeout, lambda: endpoint)
        self._check_instance(endpoint, thread_id)
        active, current_turn = thread_activity(reader.read_thread(thread_id), thread_id)
        if not active or not current_turn or turn_id and turn_id != current_turn:
            raise DesktopIpcInactive(_t('原 Turn 已结束或发生变化，未中止其他 Turn，也未发送后续消息。'))
        target_turn = turn_id or current_turn

        def guard() -> None:
            self._check_instance(endpoint, thread_id)
            active, current = thread_activity(reader.read_thread(thread_id), thread_id)
            if not active or current != target_turn:
                raise DesktopIpcInactive(_t('原 Turn 已结束或发生变化，未中止其他 Turn，也未发送后续消息。'))

        owner = self._legacy_for_current_desktop(thread_id, endpoint=endpoint)
        result = owner.interrupt_turn(thread_id, target_turn, before_submit=guard)
        if result is False and recover_owner:
            # False means no owner was found BEFORE any interrupt frame.
            # Unknown acknowledgements never enter this recovery path.
            self._recover_thread_owner(endpoint, thread_id)
            owner = self._legacy_for_current_desktop(thread_id, endpoint=endpoint)
            result = owner.interrupt_turn(thread_id, target_turn, before_submit=guard)
        return result
