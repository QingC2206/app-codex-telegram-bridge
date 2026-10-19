"""Live checks of the real Desktop pipe and Telegram API; no test transports.

Read-only by default. A probe requires an explicit, active task, is submitted
once, and succeeds only after its exact input appears in that task's rollout.
Never polls getUpdates or starts/resumes a task in a separate app-server.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time
from typing import Any
import uuid
from ..paths import data_dir, codex_home as selected_profile
from ..config import load_token

import codex_telegram_bridge.codex.desktop as desktop
import codex_telegram_bridge.codex.receipts as receipts
import codex_telegram_bridge.telegram.notifications as notify
from codex_telegram_bridge.app import CodexDesktopIpc


def default_credentials(profile: Path) -> Path:
    hooks = profile / "hooks"
    local = data_dir() / "telegram_notify.credentials.json"
    if profile.resolve() == selected_profile() and local.is_file():
        return local
    candidates = [p for p in (profile / 'telegram-bridge' / local.name, hooks / local.name, hooks / "codex-telegram-bot" / local.name)
                  if p.is_file()]
    if len(candidates) != 1:
        raise ValueError("未找到唯一的机器人凭据文件，请显式指定 --credentials。")
    return candidates[0]


def check_connection(*, thread_id: str = "", send_probe: bool = False,
                     send_telegram: bool = False, codex_home: Path | None = None,
                     credentials: Path | None = None, timeout: float = 15,
                     receipt_timeout: float = 20) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema_version": 2,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "mode": "submission-and-receipt" if send_probe else "read-only",
        "success": False,
        "checks": {name: "not_run" for name in (
            "telegram_api", "desktop_connection", "target_task",
            "desktop_delivery", "telegram_delivery")},
        "telegram_inbound": "not_exercised",
        "desktop_attempts": 0, "telegram_attempts": 0,
    }
    token = ""
    stage = "arguments"
    try:
        if not 1 <= timeout <= 60 or not 0 < receipt_timeout <= 60:
            raise ValueError("检查超时必须在 1–60 秒内。")
        if thread_id and not re.fullmatch(r"[A-Za-z0-9-]{6,64}", thread_id):
            raise ValueError("任务编号格式无效。")
        if send_probe and not thread_id:
            raise ValueError("提交验收消息必须显式指定 --thread-id。")
        profile = (codex_home or selected_profile()).resolve()
        stage = "credentials"
        path = credentials or default_credentials(profile)
        config = json.loads(path.read_text(encoding="utf-8-sig"))
        chat = str(config["chat_id"])
        if not re.fullmatch(r"-?[1-9][0-9]*", chat):
            raise ValueError("机器人聊天编号无效。")
        token = load_token(config)
        stage = "telegram_api"
        identity = notify.telegram_call(token, "getMe", timeout=timeout)
        if not isinstance(identity, dict) or identity.get("is_bot") is not True or not identity.get("id"):
            raise RuntimeError("Telegram 未返回有效的机器人身份。")
        report["checks"][stage] = "passed"

        stage = "desktop_connection"
        endpoint = desktop.discover_desktop(profile)
        # Pin this discovered instance. _connect still verifies the real pipe
        # server PID on every connection; no legacy fallback can hide a failure.
        client = desktop.DesktopControl(CodexDesktopIpc, timeout=timeout)
        client.native.discover = lambda: endpoint
        transport, handle = client.native._connect()
        try:
            client.native._catalog(transport, handle, {"read_thread"})
        finally:
            transport._close(handle)
        report["desktop_pid"] = endpoint.pid
        report["checks"][stage] = "passed"

        if thread_id:
            stage = "target_task"
            result = client.native.read_thread(thread_id)
            thread = result.get("thread")
            if not isinstance(thread, dict) or thread.get("id") != thread_id:
                raise RuntimeError("Desktop 返回的任务与指定任务不一致。")
            status = thread.get("status")
            if send_probe and (not isinstance(status, dict) or status.get("type") != "active"):
                raise RuntimeError("验收消息只能追加到正在运行的指定任务；不会自动启动任务。")
            if send_probe and not client._legacy_for_current_desktop(thread_id).find_thread_owner(thread_id):
                raise RuntimeError('Desktop 未持有验收任务，不能验证用户输入通道；未发送消息。')
            report["thread_id"] = thread_id
            report["checks"][stage] = "passed"

        if send_probe:
            stage = "desktop_delivery"
            sessions = profile / "sessions"
            rollout, offset = receipts.checkpoint(thread_id, sessions)
            if not rollout:
                raise RuntimeError("找不到该任务的接收记录，未提交验收消息。")
            probe_id = uuid.uuid4().hex
            prompt = (f"【Robot 通信验收 {probe_id}】这是一条自动生成的连接自检消息。"
                      "仅用于确认消息到达当前指定任务；请继续原任务，无需为此开展其他工作。")
            digest = receipts.prompt_digest(thread_id, prompt)
            report["probe_id"] = probe_id
            report["desktop_attempts"] = 1
            result = client.send_follow_up(thread_id, prompt, "", start_if_inactive=False)
            if not isinstance(result, dict) or result.get("route") != "desktop-user-input" or not result.get('turn_id'):
                raise RuntimeError("消息提交未确认使用 Desktop 用户输入通道；不会重发。")
            report['desktop_route'] = result['route']
            deadline = time.monotonic() + receipt_timeout
            while True:
                if Path(rollout).stat().st_size < offset:
                    raise RuntimeError("任务记录在验收期间发生截断，无法确认接收；不会重发。")
                entries, offset = receipts.read_receipts(Path(rollout), offset)
                matches = [entry for entry in entries if entry.get("digest") == digest and entry.get('origin') == 'user']
                if len(matches) > 1:
                    raise RuntimeError("发现重复的验收消息记录，验收失败。")
                if matches:
                    if matches[0]['turn_id'] != result['turn_id']:
                        raise RuntimeError('实际接收 Turn 与 Desktop 确认值不一致，验收失败；不会重发。')
                    report["receipt_item_id"] = matches[0]["item_id"]
                    report["receipt_turn_id"] = matches[0]["turn_id"]
                    report['receipt_origin'] = matches[0]['origin']
                    report["checks"][stage] = "passed"
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("提交后未观察到指定任务的匹配接收记录；结果未确认，不会重发。")
                time.sleep(min(0.25, remaining))

        if send_probe or send_telegram:
            stage = "telegram_delivery"
            text = ("✅ Robot 通信验收：指定 Desktop 任务的接收记录已确认。"
                    if send_probe else "✅ Robot 只读连接检查：Telegram API 与 Desktop 原生接口可用。")
            text += "\n本检查未模拟手机入站消息，也未消费 Telegram 更新。"
            if thread_id:
                text += "\n任务：" + thread_id[:8]
            report["telegram_attempts"] = 1
            # telegram_call sends once, unlike the retrying send_message helper.
            message = notify.telegram_call(token, "sendMessage", {
                "chat_id": chat, "text": text, "disable_notification": True,
            }, timeout=timeout)
            if (not isinstance(message, dict) or not message.get("message_id")
                    or str((message.get("chat") or {}).get("id")) != chat):
                raise RuntimeError("Telegram 未确认消息送达配置的聊天；不会重发。")
            report["telegram_message_id"] = message["message_id"]
            report["checks"][stage] = "passed"
        report["success"] = True
    except Exception as exc:
        if stage in report["checks"]:
            report["checks"][stage] = "unconfirmed" if (
                stage == "desktop_delivery" and report["desktop_attempts"]
                or stage == "telegram_delivery" and report["telegram_attempts"]
            ) else "failed"
        safe = str(exc).replace(token, "<redacted>") if token else str(exc)
        report["failure_stage"] = stage
        report["error_type"] = type(exc).__name__
        report["error"] = re.sub(r"\d{6,}:[A-Za-z0-9_-]{20,}", "<redacted>", safe)[:600]
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    return report


def print_result(report: dict[str, Any]) -> None:
    label = "真实提交及接收验收" if report["mode"] == "submission-and-receipt" else "只读连接检查（未验证任务投递）"
    print(label + ("通过。" if report["success"] else "失败：" + report.get("error", "未知错误")))
    for name, status in report["checks"].items():
        print(f"  {name}: {status}")
    print("  telegram_inbound: not_exercised（手机入站需另行实测）")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thread-id", default="")
    parser.add_argument("--send-probe", action="store_true",
                        help="send one labelled probe to an active task and verify its receipt")
    parser.add_argument("--send-telegram", action="store_true")
    parser.add_argument("--codex-home", type=Path)
    parser.add_argument("--credentials", type=Path)
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--receipt-timeout", type=float, default=20)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = check_connection(thread_id=args.thread_id, send_probe=args.send_probe,
                              send_telegram=args.send_telegram, codex_home=args.codex_home,
                              credentials=args.credentials, timeout=args.timeout,
                              receipt_timeout=args.receipt_timeout)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.report.with_name(args.report.name + ".tmp-" + uuid.uuid4().hex)
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(args.report)
    print_result(report)
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
