"""Offline security regressions; native process tests launch only idle Python."""
import ctypes
from ctypes import wintypes
import io
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import codex_telegram_bridge.app as b
import codex_telegram_bridge.features.side_chat as side_chat
from codex_telegram_bridge.platforms.windows.processes import ChildProcessJob


@unittest.skipUnless(os.name == "nt", "Windows runtime selection; POSIX has separate adapter tests")
class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runtime = self.root / "OpenAI/Codex/bin"
        self.desktop = self.runtime / "desktop-build/codex.exe"
        self.desktop.parent.mkdir(parents=True)
        self.desktop.write_bytes(b"desktop fixture")
        for context in (patch.dict(os.environ, {"LOCALAPPDATA": str(self.root), "CODEX_CLI_PATH": "", "CODEX_HOME": str(self.root / ".codex")}),
                        patch.object(Path, "home", return_value=self.root),
                        patch.object(b, "_running_desktop_codex", return_value=[])):
            context.start()
            self.addCleanup(context.stop)

    def test_newer_vscode_and_path_cli_are_never_selected(self):
        vscode = self.root / ".vscode/extensions/openai.chatgpt-new/bin/windows/codex.exe"
        vscode.parent.mkdir(parents=True)
        vscode.write_bytes(b"newer vscode fixture")
        os.utime(vscode, (2000000000, 2000000000))
        with patch.object(b.shutil, "which", return_value=str(vscode)) as on_path:
            self.assertEqual(b.locate_codex(), self.desktop.resolve())
        on_path.assert_not_called()

    def test_explicit_desktop_setting_takes_precedence_over_newer_cache(self):
        other = self.runtime / "newer-cache/codex.exe"
        other.parent.mkdir()
        other.write_bytes(b"other")
        os.utime(other, (2000000000, 2000000000))
        with patch.dict(os.environ, {"CODEX_CLI_PATH": str(self.desktop)}):
            self.assertEqual(b.locate_codex(), self.desktop.resolve())

    def test_active_desktop_backend_overrides_stale_valid_hint(self):
        active = self.runtime / "active-build/codex.exe"
        active.parent.mkdir()
        active.write_bytes(b"active")
        with patch.dict(os.environ, {"CODEX_CLI_PATH": str(self.desktop)}), \
                patch.object(b, "_running_desktop_codex", return_value=[active]):
            self.assertEqual(b.locate_codex(), active)

    def test_ambiguous_active_versions_fail_instead_of_switching(self):
        with patch.object(b, "_running_desktop_codex", return_value=[self.desktop, self.desktop.parent / "other.exe"]):
            with self.assertRaisesRegex(b.RpcError, "多个不同版本"):
                b.locate_codex()

    def test_non_desktop_override_is_rejected_even_with_desktop_installed(self):
        with patch.dict(os.environ, {"CODEX_CLI_PATH": str(self.root / ".vscode/codex.exe")}):
            with self.assertRaisesRegex(ValueError, "禁止 VS Code"):
                b.locate_codex()

    def test_config_hint_is_validated(self):
        config = self.root / ".codex/config.toml"
        config.parent.mkdir()
        config.write_text('[mcp_servers.node_repl.env]\nCODEX_CLI_PATH = "other/codex.exe"\n')
        with self.assertRaises(ValueError):
            b.locate_codex()

    def test_desktop_missing_has_no_cli_fallback(self):
        self.desktop.unlink()
        with self.assertRaisesRegex(FileNotFoundError, "不会回退"):
            b.locate_codex()

    @unittest.skipUnless(os.name == "nt", "Windows runtime discovery")
    def test_process_discovery_excludes_bot_and_vscode_children(self):
        records = [
            {"Name": "ChatGPT.exe", "ProcessId": 10, "ExecutablePath": str(self.root / "WindowsApps/OpenAI.Codex_version/app/ChatGPT.exe")},
            {"Name": "codex.exe", "ParentProcessId": 10, "ExecutablePath": str(self.desktop)},
            {"Name": "codex.exe", "ParentProcessId": 999, "ExecutablePath": str(self.desktop)},
            {"Name": "codex.exe", "ParentProcessId": 10, "ExecutablePath": str(self.root / ".vscode/codex.exe")},
        ]
        import psutil
        processes = [Mock(info={'name':r.get('Name'),'pid':r.get('ProcessId'),'ppid':r.get('ParentProcessId'),'exe':r.get('ExecutablePath')}) for r in records]
        with patch.object(psutil, "process_iter", return_value=processes):
            # Use the real function while the normal selector's discovery is patched.
            result = self.discovery(self.runtime)
        self.assertEqual(result, [self.desktop.resolve()])

    from codex_telegram_bridge.platforms.windows.runtime import _running_desktop_codex as _real_discovery
    discovery = staticmethod(_real_discovery)


class OwnershipAndAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bot = b.TelegramCodexBot("111:secret", {"chat_id": "123"})
        self.bot.state = b.StateStore(Path(self.tmp.name) / "state.json")
        self.bot.app = Mock()
        self.bot.app.request.return_value = {"thread": {"id": "task", "cwd": "repo", "status": {"type": "notLoaded"}}}
        self.bot.desktop = Mock()
        self.bot.desktop.send_follow_up.return_value = None
        self.bot.send = Mock()
        self.bot._announce_desktop_follow_up = Mock()

    def test_external_idle_task_is_not_resumed_without_desktop_owner(self):
        with patch.object(b, "persisted_thread_is_active", return_value=False):
            with self.assertRaisesRegex(b.DesktopIpcUnavailable, "不会自动接管"):
                self.bot.start_turn("task", "continue")
        self.assertEqual([c.args[0] for c in self.bot.app.request.call_args_list], ["thread/read"])
        self.assertEqual(self.bot.state.snapshot()["managed_threads"], {})

    def test_old_imported_task_is_not_treated_as_bot_created(self):
        self.bot.state.update(managed_threads={"task": {"origin": "unloaded_resumed"}})
        with patch.object(b, "persisted_thread_is_active", return_value=False):
            with self.assertRaises(b.DesktopIpcUnavailable):
                self.bot.start_turn("task", "continue")
        self.assertEqual([c.args[0] for c in self.bot.app.request.call_args_list], ["thread/read"])

    def test_external_task_uses_desktop_even_with_stale_local_active_state(self):
        self.bot.active_turns["task"] = "old-turn"
        self.bot.desktop.send_follow_up.return_value = {"mode": "steer", "turn_id": "desktop-turn"}
        self.bot.start_turn("task", "continue")
        self.bot._announce_desktop_follow_up.assert_called_once()
        self.assertEqual([c.args[0] for c in self.bot.app.request.call_args_list], ["thread/read"])
        self.assertTrue(self.bot.desktop.send_follow_up.call_args.kwargs['recover_owner'])

    def test_external_recovery_failure_never_resumes_in_bot_backend(self):
        self.bot.desktop.send_follow_up.side_effect = b.DesktopIpcUnavailable('recovery not ready')
        with patch.object(b, 'persisted_thread_is_active', return_value=False):
            with self.assertRaises(b.DesktopIpcUnavailable):
                self.bot.start_turn('task', 'continue')
        self.assertEqual([c.args[0] for c in self.bot.app.request.call_args_list], ['thread/read'])
        self.bot.desktop.send_follow_up.assert_called_once()

    def test_quiet_mode_blocks_delivery_before_owner_recovery(self):
        self.bot.state.update(quiet_mode=True)
        with self.assertRaises(ValueError):
            self.bot._desktop_follow_up('task', 'continue', '')
        self.bot.desktop.send_follow_up.assert_not_called()

    def test_bot_created_task_can_resume_its_own_background_thread(self):
        self.bot.state.update(managed_threads={"task": {"origin": "bot_created"}})
        self.bot.app.request.side_effect = [
            {"thread": {"id": "task", "status": {"type": "notLoaded"}}},
            {"thread": {"id": "task"}}, {"turn": {"id": "bot-turn"}},
        ]
        with patch.object(b, "persisted_thread_is_active", return_value=False):
            self.bot.start_turn("task", "continue")
        self.assertEqual([c.args[0] for c in self.bot.app.request.call_args_list],
                         ["thread/read", "thread/resume", "turn/start"])
        self.assertEqual(self.bot.active_turns["task"], "bot-turn")
        self.assertNotIn('recover_owner', self.bot.desktop.send_follow_up.call_args.kwargs)

    def test_authorization_precedes_attachment_and_plugin_dispatch(self):
        self.bot.handle_text = Mock()
        with patch.object(b, "download_telegram_attachment") as download:
            for sender in ({"id": 999}, {"id": 123, "is_bot": True}, None):
                self.bot.handle_update({"message": {"chat": {"id": 123}, "from": sender,
                                                    "document": {"file_id": "file"}, "text": "/screenshot"}})
        download.assert_not_called()
        self.bot.handle_text.assert_not_called()

    def test_private_owner_can_send_commands(self):
        self.bot.handle_text = Mock()
        self.bot.handle_update({"message": {"chat": {"id": 123}, "from": {"id": 123}, "text": "/tasks"}})
        self.bot.handle_text.assert_called_once()

    def test_callbacks_from_other_users_cannot_modify_navigation_or_approvals(self):
        self.bot._handle_callback_scoped = Mock()
        with patch.object(b.telegram_notify_module, "navigation_view") as navigation:
            self.bot.handle_callback({"from": {"id": 999}, "message": {"chat": {"id": 123}}, "data": "approval|1:accept"})
        navigation.assert_not_called()
        self.bot._handle_callback_scoped.assert_not_called()

    def test_group_requires_explicit_user_allowlist_and_rejects_anonymous_sender(self):
        bot = b.TelegramCodexBot("111:secret", {"chat_id": "-123", "allowed_user_ids": [42]})
        bot.handle_text = Mock()
        message = {"chat": {"id": -123}, "from": {"id": 42}, "text": "/tasks"}
        bot.handle_update({"message": dict(message, sender_chat={"id": -123})})
        bot.handle_update({"message": dict(message, **{"from": {"id": 43}})})
        bot.handle_text.assert_not_called()
        bot.handle_update({"message": message})
        bot.handle_text.assert_called_once()
        self.assertFalse(b.TelegramCodexBot("111:secret", {"chat_id": "-123"})._authorized_sender(message["chat"], message["from"]))

    def test_raw_and_url_bot_tokens_are_redacted_without_explicit_token(self):
        token = "123456789:" + "x" * 30
        self.assertNotIn(token, b.redact("raw=" + token + " URL=https://api.telegram.org/bot" + token + "/getMe"))


class ProcessTests(unittest.TestCase):
    def test_old_reader_exit_does_not_fail_new_process_requests(self):
        app = b.CodexAppServer(Mock(), Mock())
        old = Mock(stdout=io.StringIO(""))
        app.process = Mock()
        target = queue.Queue()
        app.pending[1] = target
        app._read_stdout(old)
        self.assertTrue(target.empty())
        self.assertFalse(app.closed.is_set())

    def test_failed_initialize_closes_job_and_process(self):
        app = b.CodexAppServer(Mock(), Mock())
        process = Mock(stdin=io.StringIO(), stdout=io.StringIO(), stderr=io.StringIO())
        process.poll.return_value = None
        job = Mock()
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / "codex.exe"
            executable.write_bytes(b"fixture")
            with patch.object(b, "locate_codex", return_value=executable), \
                    patch.object(b, "ChildProcessJob", return_value=job), \
                    patch.object(b.subprocess, "Popen", return_value=process), \
                    patch.object(app, "_request", side_effect=b.RpcError("initialize failed")):
                with self.assertRaises(b.RpcError):
                    app.ensure_started()
        job.close.assert_called_once()
        process.terminate.assert_called_once()
        self.assertIsNone(app.process)

    def test_failed_job_assignment_terminates_unbound_backend(self):
        app = b.CodexAppServer(Mock(), Mock())
        process = Mock(stdin=io.StringIO(), stdout=io.StringIO(), stderr=io.StringIO())
        process.poll.return_value = None
        job = Mock()
        job.attach.side_effect = OSError("job assignment failed")
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / "codex.exe"
            executable.write_bytes(b"fixture")
            with patch.object(b, "locate_codex", return_value=executable), \
                    patch.object(b, "ChildProcessJob", return_value=job), \
                    patch.object(b.subprocess, "Popen", return_value=process):
                with self.assertRaises(OSError):
                    app.ensure_started()
        job.close.assert_called_once()
        process.terminate.assert_called_once()
        self.assertIsNone(app.process)

    def test_release_of_old_side_process_never_starts_new_backend(self):
        app = b.CodexAppServer(Mock(), Mock())
        app.process = Mock()
        app.process.poll.return_value = None
        with patch.object(app, "_request") as request:
            self.assertFalse(app.release_thread("side", Mock()))
        request.assert_not_called()

    def test_update_does_not_restart_backend_with_actual_active_thread(self):
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / "codex.exe"
            executable.write_bytes(b"fixture")
            app = b.CodexAppServer(Mock(), Mock())
            app.process = Mock()
            app.process.poll.return_value = None
            app.executable_signature = ("old", 0, 0)
            with patch.object(b, "locate_codex", return_value=executable), \
                    patch.object(app, "_request", side_effect=[{"data": ["active"]}, {"thread": {"status": {"type": "active"}}}]), \
                    patch.object(app, "stop") as stop:
                self.assertFalse(app.restart_if_outdated())
            stop.assert_not_called()

    def test_update_does_not_restart_when_backend_state_is_uncertain(self):
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / "codex.exe"
            executable.write_bytes(b"fixture")
            for replies in ([{}], [{"data": "not-a-list"}], [{"data": [None]}],
                            [{"data": ["task"]}, {"thread": {"status": {"type": "unknown"}}}]):
                with self.subTest(replies=replies):
                    app = b.CodexAppServer(Mock(), Mock())
                    app.process = Mock()
                    app.process.poll.return_value = None
                    app.executable_signature = ("old", 0, 0)
                    with patch.object(b, "locate_codex", return_value=executable), \
                            patch.object(app, "_request", side_effect=replies), \
                            patch.object(app, "stop") as stop:
                        self.assertFalse(app.restart_if_outdated())
                    stop.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Native Windows job lifetime")
    def test_abrupt_service_exit_terminates_bound_child(self):
        script = (
            "import os,subprocess,sys; from codex_telegram_bridge.platforms.windows.processes import ChildProcessJob; "
            "job=ChildProcessJob(); "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW); "
            "job.attach(child); print(child.pid,flush=True); os._exit(0)"
        )
        helper = subprocess.Popen([sys.executable, "-X", "utf8", "-c", script],
                                  cwd=Path(__file__).resolve().parent, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        output, error = helper.communicate(timeout=10)
        self.assertEqual(helper.returncode, 0, error)
        child_pid = int(output.strip())
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x100001, False, child_pid)
        if handle:
            try:
                result = kernel.WaitForSingleObject(handle, 5000)
                if result != 0:
                    kernel.TerminateProcess(handle, 1)
                self.assertEqual(result, 0, "bound child survived parent exit")
            finally:
                kernel.CloseHandle(handle)


if __name__ == "__main__":
    unittest.main(verbosity=2)
