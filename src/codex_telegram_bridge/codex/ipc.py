from __future__ import annotations
from codex_telegram_bridge.i18n import tr as _t
import json,time,uuid
from typing import Any, Callable
from .errors import *
from ..platforms.transport import Transport, default_broker_path

class CodexDesktopIpc(Transport):
    """Send follow-up turns through the Codex Desktop window that owns a task."""

    PIPE_PATH = default_broker_path()
    _INITIAL_CLIENT_ID = "initializing-client"
    _METHOD_VERSIONS = {
        "initialize": 0,
        "thread-owner-discovery": 1,
        "thread-follower-start-turn": 2,
        "thread-follower-steer-turn": 1,
        "thread-follower-interrupt-turn": 4,
    }



    def _request(
        self,
        handle: int,
        client_id: str,
        method: str,
        params: dict[str, Any],
        *,
        target_client_id: str | None = None,
    ) -> dict[str, Any]:
        remaining = getattr(self, '_deadline', float('inf')) - time.monotonic()
        if remaining <= 0:
            raise DesktopIpcUnavailable(_t('Desktop 持有者查询失败，消息尚未发送。'))
        timeout = min(self.timeout, remaining)
        request_id = str(uuid.uuid4())
        version = self._METHOD_VERSIONS[method]
        if method == "thread-follower-interrupt-turn" and not params.get("expectedTurnId"):
            # Desktop still accepts v3 when the active Turn ID is unavailable.
            version = 3
        message: dict[str, Any] = {
            "type": "request",
            "requestId": request_id,
            "sourceClientId": client_id,
            "version": version,
            "method": method,
            "params": params,
            "timeoutMs": max(1, int(timeout * 1000)),
        }
        if target_client_id:
            message["targetClientId"] = target_client_id
        mutating = method in {'thread-follower-start-turn', 'thread-follower-steer-turn',
                              'thread-follower-interrupt-turn'}
        try:
            self.timeout = timeout
            self._write(handle, message)
            deadline = min(time.monotonic() + timeout, getattr(self, '_deadline', float('inf')))
            while True:
                response = self._next_message(handle, deadline)
                if response.get("type") == "client-discovery-request":
                    self._write(handle, {
                        "type": "client-discovery-response",
                        "requestId": response.get("requestId"),
                        "response": {"canHandle": False},
                    })
                    continue
                if response.get("type") == "response" and response.get("requestId") == request_id:
                    break
        except Exception as exc:
            if mutating:
                raise DesktopSubmissionUnknown(_t('Desktop 提交结果未知：发送期间连接中断。请先查看任务，避免重复发送。')) from exc
            raise DesktopIpcUnavailable(_t('Desktop 持有者查询失败，消息尚未发送。')) from exc
        if response.get("resultType") != "success":
            error = DesktopIpcError(str(response.get("error") or _t('Codex Desktop 请求失败')))
            # Only explicit pre-dispatch/inactive errors prove non-delivery.
            rejected = ('no-client-found' in str(error).lower()
                        or 'client-not-found' in str(error).lower()
                        or method == 'thread-follower-steer-turn' and self._is_inactive_steer_error(error))
            if mutating and not rejected:
                raise DesktopSubmissionUnknown(_t('Desktop 未确认提交结果：') + str(error)[:300]
                                               + _t('。请先查看任务，避免重复发送。'))
            raise error
        return response

    @staticmethod
    def _turn_id(value: Any) -> str | None:
        if not isinstance(value, dict):
            return None
        direct = value.get("turnId")
        if isinstance(direct, str) and direct:
            return direct
        turn = value.get("turn")
        if isinstance(turn, dict) and isinstance(turn.get("id"), str):
            return str(turn["id"])
        nested = value.get("result")
        return CodexDesktopIpc._turn_id(nested)

    @staticmethod
    def _is_inactive_steer_error(error: DesktopIpcError) -> bool:
        message = str(error).lower()
        return any(
            marker in message
            for marker in (
                "steerturninactiveerror",
                "active turn already ended",
                "without an active turn id",
                "no active turn to steer",
                "noactiveturn",
            )
        )

    def send_follow_up(
        self,
        thread_id: str,
        prompt: str,
        cwd: str = "",
        *,
        start_if_inactive: bool = True,
        before_submit: Callable[[], None] | None = None,
    ) -> dict[str, Any] | None:
        """Return None when no Desktop window currently owns the task."""
        handle = self._open()
        try:
            initialized = self._request(
                handle,
                self._INITIAL_CLIENT_ID,
                "initialize",
                {"clientType": "telegram-bot"},
            )
            result = initialized.get("result")
            client_id = result.get("clientId") if isinstance(result, dict) else None
            if not isinstance(client_id, str) or not client_id:
                raise DesktopIpcError(_t('Codex Desktop IPC 初始化未返回 Client ID。'))
            try:
                owner = self._request(
                    handle,
                    client_id,
                    "thread-owner-discovery",
                    {"hostId": "local", "conversationId": thread_id},
                )
            except DesktopIpcError as exc:
                if "no-client-found" in str(exc).lower():
                    return None
                raise
            owner_id = owner.get("handledByClientId")
            if not isinstance(owner_id, str) or not owner_id:
                return None

            if before_submit is not None:
                before_submit()

            input_items = [{"type": "text", "text": prompt, "text_elements": []}]
            client_message_id = str(uuid.uuid4())
            workspace_root = cwd or "/"
            restore_message = {
                "id": client_message_id,
                "text": prompt,
                "context": {
                    "prompt": prompt,
                    "addedFiles": [],
                    "fileAttachments": [],
                    "ideContext": None,
                    "imageAttachments": [],
                    "workspaceRoots": [workspace_root],
                },
                "cwd": workspace_root,
                "createdAt": int(time.time() * 1000),
            }
            try:
                steered = self._request(
                    handle,
                    client_id,
                    "thread-follower-steer-turn",
                    {
                        "conversationId": thread_id,
                        "clientUserMessageId": client_message_id,
                        "input": input_items,
                        "serviceTier": None,
                        "attachments": [],
                        "additionalContext": None,
                        "restoreMessage": restore_message,
                    },
                    target_client_id=owner_id,
                )
                turn_id = self._turn_id(steered)
                if not turn_id:
                    raise DesktopSubmissionUnknown(_t('Desktop 未返回接收消息的 Turn ID，请先核对；不会重发。'))
                return {"mode": "steer", "turn_id": turn_id, "route": "desktop-user-input",
                        "client_message_id": client_message_id}
            except DesktopIpcError as exc:
                if not self._is_inactive_steer_error(exc):
                    raise
                if not start_if_inactive:
                    raise DesktopIpcInactive(_t('该 Desktop 任务当前没有运行中的 Turn。')) from exc

            started = self._request(
                handle,
                client_id,
                "thread-follower-start-turn",
                {
                    "conversationId": thread_id,
                    "turnStart": {
                        "request": {
                            "threadId": thread_id,
                            "clientUserMessageId": client_message_id,
                            "input": input_items,
                        },
                        "context": {"inheritThreadSettings": True},
                    },
                },
                target_client_id=owner_id,
            )
            turn_id = self._turn_id(started)
            if not turn_id:
                raise DesktopSubmissionUnknown(_t('Desktop 未返回接收消息的 Turn ID，请先核对；不会重发。'))
            return {"mode": "start", "turn_id": turn_id, "route": "desktop-user-input",
                    "client_message_id": client_message_id}
        finally:
            self._close(handle)

    def interrupt_turn(self, thread_id: str, turn_id: str = "", *, before_submit=None) -> bool:
        """Interrupt a Desktop-owned turn; return False when no window owns it."""
        handle = self._open()
        try:
            initialized = self._request(
                handle,
                self._INITIAL_CLIENT_ID,
                "initialize",
                {"clientType": "telegram-bot"},
            )
            result = initialized.get("result")
            client_id = result.get("clientId") if isinstance(result, dict) else None
            if not isinstance(client_id, str) or not client_id:
                raise DesktopIpcError(_t('Codex Desktop IPC 初始化未返回 Client ID。'))
            try:
                owner = self._request(
                    handle,
                    client_id,
                    "thread-owner-discovery",
                    {"hostId": "local", "conversationId": thread_id},
                )
            except DesktopIpcError as exc:
                if "no-client-found" in str(exc).lower():
                    return False
                raise
            owner_id = owner.get("handledByClientId")
            if not isinstance(owner_id, str) or not owner_id:
                return False
            params: dict[str, Any] = {"conversationId": thread_id, "mode": "user-stop"}
            if turn_id:
                params["expectedTurnId"] = turn_id
            if before_submit is not None:
                before_submit()
            response = self._request(
                handle,
                client_id,
                "thread-follower-interrupt-turn",
                params,
                target_client_id=owner_id,
            )
            result = response.get('result')
            if not isinstance(result, dict) or 'interruptedTurnId' not in result:
                raise DesktopSubmissionUnknown(_t('Desktop 未确认实际中止的 Turn ID，请先在 Desktop 核对；不会自动重试。'))
            interrupted = result['interruptedTurnId']
            if interrupted is None:
                raise DesktopIpcInactive(_t('原 Turn 已结束或发生变化，未中止其他 Turn，也未发送后续消息。'))
            if not isinstance(interrupted, str) or not interrupted or turn_id and interrupted != turn_id:
                raise DesktopSubmissionUnknown(_t('Desktop 返回的中止 Turn ID 与预期不一致，请先核对；不会继续发送消息。'))
            if result.get('goalPauseError'):
                raise DesktopIpcError(_t('Turn 已中止，但 Desktop 未确认暂停长期目标，请在 Desktop 核对后再继续。'))
            return True
        finally:
            self._close(handle)

    def find_thread_owner(self, thread_id: str) -> bool:
        """Read-only capability probe; never resumes or mutates a task."""
        handle = self._open()
        try:
            initialized = self._request(handle, self._INITIAL_CLIENT_ID, 'initialize', {'clientType': 'telegram-bot'})
            client_id = (initialized.get('result') or {}).get('clientId')
            if not isinstance(client_id, str) or not client_id:
                raise DesktopIpcError(_t('Desktop IPC 未返回 Client ID。'))
            try:
                response = self._request(handle, client_id, 'thread-owner-discovery',
                                         {'hostId': 'local', 'conversationId': thread_id})
            except DesktopIpcError as exc:
                if 'no-client-found' in str(exc).lower():
                    return False
                raise
            return bool(response.get('handledByClientId'))
        finally:
            self._close(handle)
