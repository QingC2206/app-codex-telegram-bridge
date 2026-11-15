"""Offline composer capture, same-message delivery and Desktop follow-up checks."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
from threading import Lock, local
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import UUID

from codex_telegram_bridge.codex.desktop import DesktopControl
from codex_telegram_bridge.codex.errors import DesktopIpcInactive
from codex_telegram_bridge.features import composer_replies as c
from codex_telegram_bridge.platforms.windows import desktop_proxy, runtime


THREAD = str(UUID(int=101))
TURN = str(UUID(int=102))
LATER = str(UUID(int=103))
TOKEN = "111:offline"
CHAT = "123"
MESSAGE = 456
SUGGESTION = "Show me an example 🧪.\nKeep *this* exact."


class ComposerRepliesTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        for p in (
            patch.object(c, "DB", self.root / "composer.sqlite3"),
            patch.object(c.notify, "notifications_muted", return_value=False),
            patch.object(c.notify, "log_error"),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.request = {"chat_id": CHAT, "text": "Existing completion", "reply_markup": {
            "inline_keyboard": [[{"text": "Open", "callback_data": "open"}]],
        }}
        self.key = c._key(TOKEN, CHAT, THREAD, TURN)
        self.callback = {"id": "callback", "message": {"message_id": MESSAGE, "chat": {"id": CHAT}}}

    def save_prediction(self):
        c._save(TOKEN, CHAT, THREAD, TURN, text=SUGGESTION)

    def save_notice(self):
        c.record_notification(TOKEN, self.request, {"message_id": MESSAGE}, THREAD, TURN)

    def state(self):
        with closing(c._open()) as db:
            return db.execute("SELECT status, retry_at FROM composer_replies WHERE key=?", (self.key,)).fetchone()

    def test_prediction_first_edits_only_the_original_notice(self):
        with patch.object(c.notify, "edit_markdown_message") as edit:
            self.save_prediction()
            edit.assert_not_called()
            self.save_notice()
            self.save_notice()
        edit.assert_called_once()
        request = edit.call_args.args[1]
        self.assertEqual(request["message_id"], MESSAGE)
        self.assertEqual(request["reply_markup"]["inline_keyboard"][0],
                         self.request["reply_markup"]["inline_keyboard"][0])
        self.assertEqual(request["reply_markup"]["inline_keyboard"][1][0]["callback_data"], "composer|" + self.key)
        self.assertEqual(self.state()["status"], "available")

    def test_verified_desktop_completion_replay_preserves_the_opt_in(self):
        self.save_prediction()
        payload = {"type": "agent-turn-complete", "thread-id": THREAD, "turn-id": TURN}
        route = Mock(return_value=None)
        send = Mock(return_value={"message_id": MESSAGE})
        edit = Mock()
        decision = SimpleNamespace(thread_id=THREAD, kind="conversation", requires_turn_evidence=False)
        with patch.multiple(
            c.notify, route_completion=route, send_markdown_message=send, edit_markdown_message=edit,
            forward_computer_use_notification=Mock(), reserve_completion=Mock(return_value=True),
            load_config=Mock(return_value=({"chat_id": CHAT, "include_message_preview": True}, TOKEN)),
            subagent_completion_context=Mock(return_value=None), cache_full_reply=Mock(return_value=None),
            build_message=Mock(return_value="Existing completion"), attach_task_reply=Mock(),
            build_notification_keyboard=Mock(return_value=self.request["reply_markup"]),
            resolve_desktop_submissions=Mock(), complete_payload_reactions=Mock(),
        ):
            with patch.dict("os.environ", {c.ENABLED_ENV: "1"}):
                c.notify.send_desktop_notification(payload, "")
            send.assert_not_called()
            deferred = route.call_args.args[0]
            self.assertIs(deferred["_telegram_composer_reply"], True)
            route.return_value = decision
            c.notify.send_desktop_notification(deferred, "", _replay=True)
        send.assert_called_once()
        edit.assert_called_once()
        self.assertEqual(edit.call_args.args[1]["message_id"], MESSAGE)

    def test_notice_first_recovers_from_disconnect_without_another_event(self):
        with patch.object(c.notify, "edit_markdown_message", side_effect=ConnectionError("offline")) as edit:
            self.save_notice()
            self.save_prediction()
        edit.assert_called_once()
        self.assertEqual(self.state()["status"], "editing")
        retry = self.state()["retry_at"]
        with patch.object(c.time, "time", return_value=retry + 1), patch.object(c.notify, "edit_markdown_message") as edit:
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda _: c.retry_pending(TOKEN, CHAT), range(4)))
        edit.assert_called_once()
        self.assertEqual(edit.call_args.args[1]["message_id"], MESSAGE)
        self.assertEqual(self.state()["status"], "available")

    def test_interrupted_edit_lease_survives_connection_close(self):
        with patch.object(c.notify, "edit_markdown_message", side_effect=KeyboardInterrupt):
            self.save_notice()
            with self.assertRaises(KeyboardInterrupt):
                self.save_prediction()
        lease = self.state()["retry_at"]
        with patch.object(c.notify, "edit_markdown_message") as edit:
            c.retry_pending(TOKEN, CHAT)
            edit.assert_not_called()
            with patch.object(c.time, "time", return_value=lease + 1):
                c.retry_pending(TOKEN, CHAT)
            edit.assert_called_once()

    def test_six_clicks_submit_exact_text_once_to_original_completed_turn(self):
        with patch.object(c.notify, "edit_markdown_message"):
            self.save_notice()
            self.save_prediction()
        bot = Mock(token=TOKEN, chat_id=CHAT, _ui_local=local())
        bot.desktop.native.read_thread.return_value = {"thread": {"id": THREAD, "cwd": "/example"}}
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda _: c.handle_click(bot, self.callback, self.key), range(6)))
        bot._desktop_follow_up.assert_called_once_with(THREAD, SUGGESTION, "/example",
                                                      expected_completed_turn_id=TURN)
        self.assertEqual(self.state()["status"], "claimed")

    def test_native_send_rejects_a_stale_turn_at_submit(self):
        control = DesktopControl(Mock())
        owner = Mock()
        endpoint = SimpleNamespace(pid=123)
        latest = {"id": LATER, "status": "completed"}
        control.native = Mock()
        control.native.discover.return_value = endpoint
        control._legacy_for_current_desktop = Mock(return_value=owner)
        control._check_instance = Mock()
        control._reader = Mock(return_value=SimpleNamespace(read_thread=lambda _: {
            "thread": {"id": THREAD, "status": {"type": "idle"}}, "turns": [latest],
        }))
        submit = Mock(return_value={"turn_id": LATER})

        def guarded_send(thread, text, cwd, *, before_submit, **_):
            before_submit()
            return submit(thread, text, cwd)

        owner.send_follow_up.side_effect = guarded_send
        with self.assertRaises(DesktopIpcInactive):
            control.send_follow_up(THREAD, SUGGESTION, "/example", expected_completed_turn_id=TURN)
        submit.assert_not_called()
        latest["id"] = TURN
        control.send_follow_up(THREAD, SUGGESTION, "/example", expected_completed_turn_id=TURN)
        submit.assert_called_once_with(THREAD, SUGGESTION, "/example")

    def test_only_safety_approved_matching_predictions_are_stored(self):
        requested = time.time() - 1
        now = datetime.now(timezone.utc)
        log = self.root / "Codex" / "Logs" / now.strftime("%Y/%m/%d") / "codex-desktop-test-42-t0-i1-0.log"
        log.parent.mkdir(parents=True)
        record = {"threadId": THREAD, "turnId": TURN, "text": SUGGESTION, "requested": requested}
        prefix = f"{now.isoformat()} info [cumberbatch] cumberbatch_safety_decision sourceThreadId={THREAD} sourceTurnId="
        with patch.dict("os.environ", {"LOCALAPPDATA": str(self.root)}), \
                patch.object(c.notify, "load_config", return_value=({"chat_id": CHAT, "include_message_preview": True}, TOKEN)), \
                patch.object(c.time, "sleep"):
            log.write_text(prefix + f"{LATER} suppressed=false\n", encoding="utf-8")
            c.record_prediction(record, 42)
            self.assertFalse(c.DB.exists())
            log.write_text(prefix + f"{TURN} suppressed=true\n", encoding="utf-8")
            c.record_prediction(record, 42)
            self.assertFalse(c.DB.exists())
            log.write_text(prefix + f"{TURN} suppressed=false\n", encoding="utf-8")
            c.record_prediction(record, 42)
        with closing(sqlite3.connect(c.DB)) as db:
            self.assertEqual(db.execute("SELECT prediction FROM composer_replies").fetchone(), (SUGGESTION,))


class DesktopProxyTests(unittest.TestCase):
    def test_sample_request_and_response_require_the_same_turn(self):
        request_id = "thread/sample:1"
        params = {"threadId": THREAD, "expectedTurnId": TURN, "threadSource": "composer_predictions",
                  "responsesapiClientMetadata": {"prediction_source_thread_id": THREAD,
                                                 "prediction_source_turn_id": TURN}}
        request = {"id": request_id, "method": "thread/sample", "params": params}
        req = desktop_proxy.prediction_request(json.dumps(request).encode())
        self.assertEqual(req[0], request_id)
        lock = Lock()

        def response(turn):
            return json.dumps({"id": request_id, "result": {
                "sourceTurnId": turn, "text": json.dumps({"suggestion": SUGGESTION}),
            }}).encode()

        self.assertIsNone(desktop_proxy.prediction_response(response(LATER), dict([req]), lock))
        result = desktop_proxy.prediction_response(response(TURN), dict([req]), lock)
        self.assertEqual((result["threadId"], result["turnId"], result["text"]), (THREAD, TURN, SUGGESTION))
        params["responsesapiClientMetadata"]["prediction_source_turn_id"] = LATER
        self.assertIsNone(desktop_proxy.prediction_request(json.dumps(request).encode()))

    def test_proxy_runtime_requires_the_registered_package_layout(self):
        with tempfile.TemporaryDirectory() as folder:
            parent = Path(folder).resolve()
            app = parent / "WindowsApps" / "OpenAI.Codex_26.100.0.0_x64__2p2nqsd0c76g0" / "app"
            real = app / "resources" / "codex.exe"
            real.parent.mkdir(parents=True)
            real.touch()
            (app / "ChatGPT.exe").touch()
            with patch.dict("os.environ", {"ProgramW6432": str(parent)}):
                self.assertEqual(runtime.store_desktop_runtime(app / "ChatGPT.exe"), real)
                self.assertIsNone(runtime.store_desktop_runtime(parent / "app" / "ChatGPT.exe"))
                self.assertIsNone(runtime.store_desktop_runtime(app / "Different.exe"))


if __name__ == "__main__":
    unittest.main()
