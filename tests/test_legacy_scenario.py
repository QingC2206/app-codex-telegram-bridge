"""Original full regression scenario, isolated from the installed service."""
import unittest
from codex_telegram_bridge.app import (
    argparse,
    ctypes,
    wintypes,
    datetime,
    json,
    math,
    os,
    Path,
    queue,
    re,
    shutil,
    struct,
    subprocess,
    sys,
    tempfile,
    threading,
    time,
    traceback,
    Any,
    Callable,
    uuid,
    QuotaResetController,
    PluginHost,
    SideChats,
    AsyncInputs,
    read_thread_history,
    collect_running_tasks,
    form_questions,
    form_content,
    ChildProcessJob,
    DesktopControl,
    DesktopIpcError,
    DesktopIpcUnavailable,
    DesktopIpcInactive,
    DesktopSubmissionUnknown,
    validate_pipe_server,
    telegram_notify_module,
    cache_full_reply,
    load_config,
    load_cached_reply,
    markdown_to_telegram_html,
    markdown_preview,
    edit_markdown_message,
    lookup_task_message,
    remember_task_message,
    download_telegram_attachment,
    send_document,
    send_markdown_message,
    send_message,
    set_subagent_notifications,
    split_markdown_pages,
    subagent_notifications_enabled,
    telegram_call,
    HOOK_DIR,
    STATE_PATH,
    LOG_PATH,
    SESSION_ROOT,
    MAX_TEXT,
    TASK_LIMIT,
    WATCH_POLL_SECONDS,
    BOT_OPERATION_THREAD,
    OPERATION_CALLBACKS,
    QUOTA_MONITOR_SECONDS,
    QUOTA_NATURAL_RESET_GRACE_SECONDS,
    QUOTA_MONITOR_FAILURE_ALERT_THRESHOLD,
    QUOTA_LOW_THRESHOLDS,
    RESET_CONFIRM_SECONDS,
    SOURCE_KINDS,
    RpcError,
    ActiveWriterConflict,
    CodexDesktopIpc,
    redact,
    log,
    truncate,
    one_line,
    default_state,
    StateStore,
    _desktop_codex_path,
    _running_desktop_codex,
    locate_codex,
    CodexAppServer,
    status_label,
    extract_messages,
    parse_command,
    format_duration,
    format_token_count,
    format_quota_remaining,
    format_quota_reset,
    format_quota_reset_countdown,
    format_account_credits,
    format_reset_credit_detail,
    extract_quota_windows,
    weekly_quota_snapshot,
    quota_window_snapshot,
    reset_credit_snapshot,
    reset_credit_grant_delta,
    weekly_quota_reset_is_unexpected,
    weekly_quota_reset_is_natural,
    reset_credit_gate,
    diff_file_names,
    extract_latest_diff,
    describe_item,
    _LIFECYCLE_MARKERS,
    latest_lifecycle_event,
    persisted_thread_path,
    persisted_thread_activity,
    persisted_thread_is_active,
    persisted_thread_token_usage,
    TelegramCodexBot,
    SingleInstance,
    check_install,
    main
)

def self_test() -> int:
    assert parse_command("/tasks") == ("/tasks", "")
    assert parse_command("/send@mybot 1 hello") == ("/send", "1 hello")
    assert status_label({"type": "active"})[1] == "运行中"
    assert one_line("a\n b", 10) == "a b"
    assert format_duration(65) == "1分5秒"
    assert format_quota_remaining(32) == "68%"
    local_tz = datetime.now().astimezone().tzinfo
    quota_now = datetime(2026, 8, 29, 10, 0, tzinfo=local_tz)
    quota_grant = datetime(2026, 8, 28, 9, 15, tzinfo=local_tz).timestamp()
    quota_reset = datetime(2026, 8, 29, 18, 30, tzinfo=local_tz).timestamp()
    assert format_quota_reset(quota_reset, quota_now) == "今天 18:30"
    reset_credits = reset_credit_snapshot({
        "rateLimitResetCredits": {
            "availableCount": 2,
            "credits": [
                {
                    "id": "card-1",
                    "status": "available",
                    "grantedAt": quota_grant,
                    "expiresAt": quota_reset,
                },
                {"id": "card-2", "status": "available", "expiresAt": None},
            ],
        },
    })
    assert reset_credits is not None
    assert reset_credits["available_count"] == 2
    assert reset_credits["details_complete"]
    assert reset_credits["credits"][0]["granted_at"] == int(quota_grant)
    assert reset_credits["credits"][0]["expires_at"] == int(quota_reset)
    assert reset_credit_grant_delta({}, reset_credits)["count"] == 0
    assert reset_credit_grant_delta(
        {"available_count": 1, "credit_ids": ["card-1"], "details_complete": True},
        reset_credits,
    ) == {"count": 1, "credit_ids": ["card-2"]}
    quota_windows = extract_quota_windows({
        "rateLimits": {
            "primary": {"usedPercent": 32, "windowDurationMins": 300, "resetsAt": 1},
            "secondary": {"usedPercent": 58, "windowDurationMins": 10080, "resetsAt": 2},
        },
    })
    assert quota_windows[300]["window"]["usedPercent"] == 32
    assert quota_windows[10080]["window"]["usedPercent"] == 58
    main_only_windows = extract_quota_windows({
        "rateLimits": {
            "limitId": "codex",
            "primary": {"usedPercent": 74, "windowDurationMins": 10080, "resetsAt": 2},
        },
        "rateLimitsByLimitId": {
            "codex": {
                "limitId": "codex",
                "primary": {"usedPercent": 74, "windowDurationMins": 10080, "resetsAt": 2},
            },
            "codex_bengalfox": {
                "limitId": "codex_bengalfox",
                "limitName": "GPT-5.3-Codex-Spark",
                "primary": {"usedPercent": 0, "windowDurationMins": 300, "resetsAt": 3},
                "secondary": {"usedPercent": 0, "windowDurationMins": 10080, "resetsAt": 4},
            },
        },
    })
    assert 300 not in main_only_windows
    assert main_only_windows[10080]["window"]["usedPercent"] == 74
    quota_check_time = time.time()
    weekly_before = {"used_percent": 74, "resets_at": int(quota_check_time) + 86400}
    weekly_early_reset = {"used_percent": 0, "resets_at": int(quota_check_time) + 604800}
    assert weekly_quota_reset_is_unexpected(
        weekly_before,
        weekly_early_reset,
        quota_check_time,
    )
    assert not weekly_quota_reset_is_unexpected(
        weekly_before,
        weekly_early_reset,
        int(weekly_before["resets_at"]),
    )
    assert not weekly_quota_reset_is_unexpected(
        weekly_before,
        {"used_percent": 68, "resets_at": int(quota_check_time) + 604800},
        quota_check_time,
    )
    with tempfile.TemporaryDirectory(prefix="codex-quota-monitor-test-") as temporary_dir:
        monitor_bot = TelegramCodexBot("test-token", {"chat_id": "1"})
        monitor_bot.state = StateStore(Path(temporary_dir) / "state.json")
        monitor_alerts: list[str] = []
        monitor_bot.enqueue = lambda text, *_args, **_kwargs: monitor_alerts.append(text)  # type: ignore[method-assign]
        monitor_bot._observe_weekly_quota({
            "rateLimits": {
                "limitId": "codex",
                "primary": {
                    "usedPercent": 74,
                    "windowDurationMins": 10080,
                    "resetsAt": int(quota_check_time) + 86400,
                },
            },
        })
        assert monitor_alerts == []
        monitor_bot._observe_weekly_quota({
            "rateLimits": {
                "limitId": "codex_bengalfox",
                "primary": {
                    "usedPercent": 0,
                    "windowDurationMins": 300,
                    "resetsAt": int(quota_check_time) + 18000,
                },
            },
        })
        assert monitor_alerts == []
        reset_result = {
            "rateLimits": {
                "limitId": "codex",
                "primary": {
                    "usedPercent": 0,
                    "windowDurationMins": 10080,
                    "resetsAt": int(quota_check_time) + 604800,
                },
            },
        }
        monitor_bot._observe_weekly_quota(reset_result)
        monitor_bot._observe_weekly_quota(reset_result)
        assert len(monitor_alerts) == 1
        assert "周额度已提前恢复" in monitor_alerts[0]
        assert "重置前：** 已用 74%，剩余 26%" in monitor_alerts[0]
        assert "重置后：** 已用 0%，剩余 100%" in monitor_alerts[0]
        assert "额度归零" not in monitor_alerts[0]
        monitor_bot._observe_reset_credits({
            "rateLimitResetCredits": {"availableCount": 0, "credits": []},
        })
        assert len(monitor_alerts) == 1
        monitor_bot._observe_reset_credits({
            "rateLimitResetCredits": {
                "availableCount": 1,
                "credits": [{
                    "id": "card-new",
                    "status": "available",
                    "grantedAt": quota_grant,
                    "expiresAt": quota_reset,
                }],
            },
        })
        monitor_bot._observe_reset_credits({
            "rateLimitResetCredits": {
                "availableCount": 1,
                "credits": [{
                    "id": "card-new",
                    "status": "available",
                    "grantedAt": quota_grant,
                    "expiresAt": quota_reset,
                }],
            },
        })
        assert len(monitor_alerts) == 2
        assert "新增额度重置卡" in monitor_alerts[1]
        assert "本次新增：** 1 张" in monitor_alerts[1]
        quota_messages: list[str] = []
        quota_edits: list[str] = []
        monitor_bot.app.request = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
            "rateLimits": {
                "limitId": "codex",
                "primary": {"usedPercent": 32, "windowDurationMins": 300, "resetsAt": quota_reset},
                "secondary": {"usedPercent": 58, "windowDurationMins": 10080, "resetsAt": quota_reset},
            },
            "rateLimitResetCredits": {
                "availableCount": 1,
                "credits": [{
                    "id": "card-new",
                    "status": "available",
                    "grantedAt": quota_grant,
                    "expiresAt": quota_reset,
                }],
            },
        }
        monitor_bot.send = lambda text, *_args, **_kwargs: (  # type: ignore[method-assign]
            quota_messages.append(text) or {"message_id": 42}
        )
        monitor_bot.edit_message = lambda _message_id, text, *_args, **_kwargs: quota_edits.append(text)  # type: ignore[method-assign]
        monitor_bot.command_quota()
        assert len(quota_messages) == 1
        assert "正在查询 Codex 额度" in quota_messages[0]
        assert len(quota_edits) == 1
        assert "可用数量：** 1 张" in quota_edits[0]
        assert (
            f"第 1 张：发放 {format_quota_reset(quota_grant)}；"
            f"到期 {format_quota_reset(quota_reset)}"
        ) in quota_edits[0]

        health_alerts: list[str] = []
        monitor_bot.enqueue = lambda text, *_args, **_kwargs: health_alerts.append(text)  # type: ignore[method-assign]
        monitor_bot._record_quota_monitor_issue("temporary failure")
        monitor_bot._record_quota_monitor_issue("temporary failure")
        assert health_alerts == []
        monitor_bot._record_quota_monitor_issue("missing rateLimitResetCredits")
        monitor_bot._record_quota_monitor_issue("still missing")
        assert len(health_alerts) == 1
        assert "额度卡监控异常" in health_alerts[0]
        monitor_bot._record_quota_monitor_success()
        assert len(health_alerts) == 2
        assert "额度卡监控已恢复" in health_alerts[1]

        refresh_calls: list[bool] = []
        monitor_bot.app.restart_if_outdated = (  # type: ignore[method-assign]
            lambda: refresh_calls.append(True) or True
        )
        assert monitor_bot._refresh_quota_app_server_if_outdated()
        monitor_bot.active_turns["busy-thread"] = "busy-turn"
        assert not monitor_bot._refresh_quota_app_server_if_outdated()
        assert refresh_calls == [True]

        catch_up_bot = TelegramCodexBot("test-token", {"chat_id": "1"})
        catch_up_bot.state = StateStore(Path(temporary_dir) / "catch-up-state.json")
        catch_up_bot.state.update(quota_reset_credits_observation={
            "available_count": 1,
            "credits": [{
                "id": "existing-card",
                "granted_at": int(quota_grant),
                "expires_at": int(quota_reset),
            }],
            "credit_ids": ["existing-card"],
            "details_complete": True,
            "observed_at": int(quota_grant),
        })
        catch_up_messages: list[str] = []
        catch_up_bot.send = (  # type: ignore[method-assign]
            lambda text, *_args, **_kwargs: catch_up_messages.append(text) or {"message_id": 43}
        )
        catch_up_bot._send_unreported_credit_baseline()
        catch_up_bot._send_unreported_credit_baseline()
        assert len(catch_up_messages) == 1
        assert "补发提醒" in catch_up_messages[0]
        assert catch_up_bot.state.snapshot()["quota_reset_credit_baseline_alerted"] is True

        baseline_bot = TelegramCodexBot("test-token", {"chat_id": "1"})
        baseline_bot.state = StateStore(Path(temporary_dir) / "baseline-state.json")
        baseline_alerts: list[str] = []
        baseline_bot.enqueue = lambda text, *_args, **_kwargs: baseline_alerts.append(text)  # type: ignore[method-assign]
        baseline_bot._observe_reset_credits({
            "rateLimitResetCredits": {
                "availableCount": 1,
                "credits": [{
                    "id": "first-card",
                    "status": "available",
                    "grantedAt": quota_grant,
                    "expiresAt": quota_reset,
                }],
            },
        })
        assert len(baseline_alerts) == 1
        assert "首次建立监控基线" in baseline_alerts[0]
    assert latest_lifecycle_event(
        b'{"payload":{"type":"task_started"}}\n'
        b'{"payload":{"type":"task_complete"}}'
    ) == "task_complete"
    assert latest_lifecycle_event(b'{"payload":{"type":"task_started"}}') == "task_started"
    framed = CodexDesktopIpc.encode_frame({"type": "test", "text": "中文"})
    assert struct.unpack("<I", framed[:4])[0] == len(framed) - 4
    assert json.loads(framed[4:].decode("utf-8"))["text"] == "中文"
    sample_diff = "diff --git a/a.py b/a.py\n+++ b/a.py\n+hello"
    assert diff_file_names(sample_diff) == ["a.py"]
    stored_diff, stored_files = extract_latest_diff({
        "turns": [{
            "items": [{
                "type": "fileChange",
                "changes": [{"path": "b.py", "kind": "update", "diff": "+world"}],
            }],
        }],
    })
    assert stored_diff == "+world"
    assert stored_files == ["b.py"]

    test_bot = TelegramCodexBot("test-token", {"chat_id": "1"})
    captured_edits: list[tuple[int, str]] = []
    test_bot.enqueue_edit = lambda message_id, text, *_args: captured_edits.append((message_id, text))  # type: ignore[method-assign]
    test_bot.watches["thread-test"] = {
        "message_id": 9,
        "chat_id": "1",
        "title": "测试任务",
        "last_edit": 0,
        "last_text": "",
    }
    test_bot.on_app_event({
        "method": "turn/started",
        "params": {"threadId": "thread-test", "turn": {"id": "turn-test"}},
    })
    test_bot.watches["thread-test"]["last_edit"] = 0
    test_bot.on_app_event({
        "method": "thread/tokenUsage/updated",
        "params": {
            "threadId": "thread-test",
            "turnId": "turn-test",
            "tokenUsage": {
                "total": {"totalTokens": 1234},
                "last": {"totalTokens": 234},
                "modelContextWindow": 1000,
            },
        },
    })
    assert test_bot.active_turns["thread-test"] == "turn-test"
    assert test_bot.token_usage["thread-test"]["total"]["totalTokens"] == 1234
    assert any("1,234" in text for _, text in captured_edits)

    class FakeTimer:
        def __init__(self) -> None:
            self.cancelled = False

        def cancel(self) -> None:
            self.cancelled = True

    refresh_timer = FakeTimer()
    poll_timer = FakeTimer()
    test_bot.watch_timers["thread-test"] = refresh_timer  # type: ignore[assignment]
    test_bot.watch_poll_timers["thread-test"] = poll_timer  # type: ignore[assignment]
    test_bot.stop_watch_tracking("thread-test")
    assert refresh_timer.cancelled
    assert poll_timer.cancelled
    assert "thread-test" not in test_bot.watches
    assert "已停止跟踪" in captured_edits[-1][1]

    selection_messages: list[tuple[str, dict[str, Any] | None]] = []
    test_bot._list_threads = lambda limit=TASK_LIMIT: [  # type: ignore[method-assign]
        {
            "id": "thread-test",
            "name": "测试任务",
            "cwd": "C:\\repo",
            "status": {"type": "active"},
        },
        {
            "id": "desktop-test",
            "name": "桌面任务",
            "cwd": "D:\\repo",
            "status": {"type": "active"},
        },
    ]
    test_bot.state.snapshot = lambda: {  # type: ignore[method-assign]
        "managed_threads": {"thread-test": {}},
        "task_aliases": [],
    }
    test_bot.send = lambda text, reply_markup=None, **_kwargs: selection_messages.append((text, reply_markup)) or {"message_id": 1}  # type: ignore[method-assign]
    test_bot.command_watch("")
    watch_callbacks = [
        row[0]["callback_data"]
        for row in selection_messages[-1][1]["inline_keyboard"]
    ]
    assert watch_callbacks[:2] == ["watch|thread-test", "watch|desktop-test"]
    from unittest.mock import patch
    from codex_telegram_bridge.features.running_tasks import RunningTasks
    with patch('codex_telegram_bridge.app.collect_running_tasks', return_value=RunningTasks(test_bot._list_threads(), [])):
        test_bot.command_steer("")
    steer_callbacks = [
        row[0]["callback_data"]
        for row in selection_messages[-1][1]["inline_keyboard"]
    ]
    assert steer_callbacks[:2] == ["steerpick|thread-test", "steerpick|desktop-test"]
    test_bot.command_diff("")
    callbacks = [row[0]["callback_data"] for row in selection_messages[-1][1]["inline_keyboard"]]
    assert callbacks[:2] == ["diff|thread-test", "diff|desktop-test"]
    test_bot.command_send("")
    send_callbacks = [
        row[0]["callback_data"]
        for row in selection_messages[-1][1]["inline_keyboard"]
    ]
    assert send_callbacks[:2] == ["continue|thread-test", "continue|desktop-test"]
    test_bot.command_send("thread-test")
    assert test_bot.awaiting_continue["1"] == "thread-test"

    original_bot_session_root = globals()["SESSION_ROOT"]
    with tempfile.TemporaryDirectory() as temporary_watch_sessions:
        globals()["SESSION_ROOT"] = Path(temporary_watch_sessions)
        try:
            watch_day = Path(temporary_watch_sessions) / "2026" / "08" / "25"
            watch_day.mkdir(parents=True)
            external_id = "01a00000-0000-7000-8000-000000000099"
            watch_path = watch_day / f"rollout-watch-{external_id}.jsonl"
            watch_entries = [
                {
                    "type": "session_meta",
                    "payload": {"id": external_id, "source": "appServer"},
                },
                {
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "turn-external"},
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {"total_tokens": 4567},
                            "last_token_usage": {"total_tokens": 321},
                            "model_context_window": 10000,
                        },
                    },
                },
            ]
            watch_path.write_text(
                "\n".join(
                    json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
                    for entry in watch_entries
                ) + "\n",
                encoding="utf-8",
            )
            external_bot = TelegramCodexBot("test-token", {"chat_id": "1"})
            external_bot.desktop.can_interrupt = lambda *_args, **_kwargs: True  # type: ignore[method-assign]
            external_bot.state.snapshot = lambda: {  # type: ignore[method-assign]
                "managed_threads": {},
                "task_aliases": [],
            }
            external_bot._list_threads = lambda limit=TASK_LIMIT: [{  # type: ignore[method-assign]
                "id": external_id,
                "name": "Desktop 实时任务",
                "cwd": "D:\\repo",
                "status": {"type": "notLoaded"},
            }]
            external_thread = {
                "id": external_id,
                "name": "Desktop 实时任务",
                "cwd": "D:\\repo",
                "turns": [{
                    "id": "turn-external",
                    "status": "interrupted",
                    "startedAt": int(time.time()) - 12,
                    "durationMs": None,
                    "items": [{
                        "type": "commandExecution",
                        "command": "python -m pytest",
                        "status": "completed",
                    }],
                }],
            }
            external_bot.app.request = (  # type: ignore[method-assign]
                lambda method, _params, timeout=90: {"thread": external_thread}
                if method == "thread/read"
                else (_ for _ in ()).throw(AssertionError(method))
            )
            external_messages: list[tuple[str, dict[str, Any] | None]] = []
            external_bot.send = (  # type: ignore[method-assign]
                lambda text, reply_markup=None, **_kwargs:
                external_messages.append((text, reply_markup)) or {"message_id": 11}
            )
            scheduled_external: list[str] = []
            external_bot._schedule_external_watch_poll = (  # type: ignore[method-assign]
                lambda thread_id, delay=WATCH_POLL_SECONDS:
                scheduled_external.append(thread_id)
            )
            external_bot.desktop.native.read_thread = lambda tid, **kw: {
                'thread': {'id': tid, 'status': {'type': 'active'}},
                'turns': [{'id': 'turn-external', 'status': 'inProgress'}]}
            external_bot.command_watch(external_id)
            assert external_bot.watches[external_id]["managed"] is False
            assert external_bot.runtime_status[external_id]["type"] == "active"
            assert external_bot.token_usage[external_id]["total"]["totalTokens"] == 4567
            assert scheduled_external == [external_id]
            assert "运行中" in external_messages[-1][0]
            assert "4,567" in external_messages[-1][0]
            watch_actions = external_messages[-1][1]["inline_keyboard"][0]
            assert watch_actions[-1]["callback_data"] == f"direct|{external_id}"
            assert watch_actions[-1]["text"] == "💬 回复"
            interrupt_action = external_messages[-1][1]["inline_keyboard"][1][0]
            assert interrupt_action["callback_data"] == f"stop|{external_id}"
            stop_tracking = external_messages[-1][1]["inline_keyboard"][2][0]
            assert stop_tracking["callback_data"] == f"unwatch|{external_id}"
            assert stop_tracking["text"] == "⏹ 停止跟踪"
            assert persisted_thread_activity(external_id) == (True, "turn-external")
        finally:
            globals()["SESSION_ROOT"] = original_bot_session_root

    ipc = CodexDesktopIpc()
    ipc_calls: list[str] = []
    ipc._open = lambda: 1  # type: ignore[method-assign]
    ipc._close = lambda _handle: None  # type: ignore[method-assign]

    def fake_ipc_request(
        _handle: int,
        _client_id: str,
        method: str,
        _params: dict[str, Any],
        *,
        target_client_id: str | None = None,
    ) -> dict[str, Any]:
        del target_client_id
        ipc_calls.append(method)
        if method == "initialize":
            return {"result": {"clientId": "telegram-client"}}
        if method == "thread-owner-discovery":
            return {"handledByClientId": "desktop-owner"}
        if method == "thread-follower-steer-turn":
            raise DesktopIpcError("Cannot steer conversation desktop-test because its active turn already ended")
        if method == "thread-follower-start-turn":
            return {"result": {"result": {"result": {"turn": {"id": "desktop-turn"}}}}}
        if method == "thread-follower-interrupt-turn":
            return {"result": {"interruptedTurnId": "desktop-turn", "ok": True}}
        raise AssertionError(method)

    ipc._request = fake_ipc_request  # type: ignore[method-assign]
    ipc_result = ipc.send_follow_up("desktop-test", "继续处理", "D:\\repo")
    assert {key: ipc_result[key] for key in ('mode', 'turn_id', 'route')} == {
        "mode": "start", "turn_id": "desktop-turn", "route": "desktop-user-input"}
    assert str(uuid.UUID(ipc_result['client_message_id'])) == ipc_result['client_message_id']
    assert ipc_calls == [
        "initialize",
        "thread-owner-discovery",
        "thread-follower-steer-turn",
        "thread-follower-start-turn",
    ]
    ipc_calls.clear()
    try:
        ipc.send_follow_up(
            "desktop-test",
            "只追加",
            "D:\\repo",
            start_if_inactive=False,
        )
    except DesktopIpcInactive:
        pass
    else:
        raise AssertionError("strict Desktop steer should reject inactive tasks")
    assert ipc_calls == [
        "initialize",
        "thread-owner-discovery",
        "thread-follower-steer-turn",
    ]
    ipc_calls.clear()
    assert ipc.interrupt_turn("desktop-test", "desktop-turn")
    assert ipc_calls == [
        "initialize",
        "thread-owner-discovery",
        "thread-follower-interrupt-turn",
    ]

    desktop_bot = TelegramCodexBot("test-token", {"chat_id": "1"})
    desktop_state: dict[str, Any] = {"managed_threads": {}}
    desktop_bot.state.snapshot = lambda: json.loads(json.dumps(desktop_state))  # type: ignore[method-assign]
    desktop_app_calls: list[str] = []
    desktop_ipc_calls: list[tuple[str, str, str]] = []

    def fake_desktop_read(method: str, _params: dict[str, Any], timeout: float = 60) -> dict[str, Any]:
        del timeout
        desktop_app_calls.append(method)
        if method == "thread/read":
            return {"thread": {"id": "desktop-test", "cwd": "D:\\repo", "status": {"type": "notLoaded"}}}
        raise AssertionError(method)

    desktop_bot.app.request = fake_desktop_read  # type: ignore[method-assign]
    desktop_bot.desktop.send_follow_up = (  # type: ignore[method-assign]
        lambda thread_id, prompt, cwd, **kwargs: desktop_ipc_calls.append((thread_id, prompt, cwd))
        or {"mode": "start", "turn_id": "desktop-turn"}
    )
    desktop_bot.send = lambda *_args, **_kwargs: {"message_id": 2}  # type: ignore[method-assign]
    desktop_bot.start_turn("desktop-test", "继续处理")
    assert desktop_app_calls == ["thread/read"]
    assert desktop_ipc_calls == [("desktop-test", "继续处理", "D:\\repo")]
    assert desktop_state["managed_threads"] == {}

    desktop_steer_calls: list[tuple[str, str, str, bool]] = []

    def fake_desktop_steer(
        thread_id: str,
        prompt: str,
        cwd: str,
        *,
        start_if_inactive: bool = True,
        recover_owner: bool = False,
    ) -> dict[str, Any]:
        assert recover_owner
        desktop_steer_calls.append((thread_id, prompt, cwd, start_if_inactive))
        return {"mode": "steer", "turn_id": "desktop-turn"}

    desktop_bot.desktop.send_follow_up = fake_desktop_steer  # type: ignore[method-assign]
    desktop_bot.steer_turn("desktop-test", "追加处理")
    assert desktop_app_calls == ["thread/read", "thread/read"]
    assert desktop_steer_calls == [("desktop-test", "追加处理", "D:\\repo", False)]
    assert desktop_state["managed_threads"] == {}

    resume_bot = TelegramCodexBot("test-token", {"chat_id": "1"})
    resume_state: dict[str, Any] = {"managed_threads": {}}
    resume_bot.state.snapshot = lambda: json.loads(json.dumps(resume_state))  # type: ignore[method-assign]
    resume_bot.state.mutate = lambda callback: callback(resume_state)  # type: ignore[method-assign]
    resume_calls: list[str] = []

    def fake_resume_request(method: str, _params: dict[str, Any], timeout: float = 60) -> dict[str, Any]:
        del timeout
        resume_calls.append(method)
        if method == "thread/read":
            return {"thread": {"id": "desktop-test", "cwd": "D:\\repo", "status": {"type": "notLoaded"}}}
        if method == "thread/resume":
            return {"thread": {"id": "desktop-test"}}
        if method == "turn/start":
            return {"turn": {"id": "adopted-turn"}}
        raise AssertionError(method)

    resume_bot.app.request = fake_resume_request  # type: ignore[method-assign]
    resume_bot.desktop.send_follow_up = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    resume_bot.send = lambda *_args, **_kwargs: {"message_id": 2}  # type: ignore[method-assign]
    try:
        resume_bot.start_turn("desktop-test", "继续处理")
        raise AssertionError("An external task must not be adopted without Desktop")
    except DesktopIpcUnavailable:
        pass
    assert resume_calls == ["thread/read"]
    assert resume_state["managed_threads"] == {}

    direct_bot = TelegramCodexBot("test-token", {"chat_id": "1"})
    direct_bot.desktop.can_interrupt = lambda *_args, **_kwargs: True  # type: ignore[method-assign]
    direct_messages: list[tuple[str, dict[str, Any] | None]] = []
    direct_message_ids = iter(range(3, 100))
    direct_bot.send = (  # type: ignore[method-assign]
        lambda text, reply_markup=None, **_kwargs:
        direct_messages.append((text, reply_markup)) or {"message_id": next(direct_message_ids)}
    )
    direct_bot.request_direct_reply("desktop-test")
    direct_bot.request_direct_reply("other-test")
    assert direct_bot.awaiting_direct_reply == {3: "desktop-test", 4: "other-test"}
    assert "force_reply" not in direct_messages[-1][1]
    assert direct_messages[-1][1]["inline_keyboard"][0][0]["callback_data"] == "directcancel|other-test"

    started_direct: list[tuple[str, str]] = []
    direct_bot.start_turn = (  # type: ignore[method-assign]
        lambda thread_id, prompt: started_direct.append((thread_id, prompt))
    )
    direct_bot._thread_activity = lambda _thread_id, timeout=30: (False, "")  # type: ignore[method-assign]
    direct_bot.handle_text("发给第一条提示", reply_to_message_id=3)
    assert started_direct == [("desktop-test", "发给第一条提示")]
    assert direct_bot.awaiting_direct_reply == {4: "other-test"}
    direct_bot.handle_text("普通文字不应被转发")
    assert started_direct == [("desktop-test", "发给第一条提示")]
    assert direct_bot.awaiting_direct_reply == {4: "other-test"}
    direct_bot.handle_text("发给剩余的提示", reply_to_message_id=4)
    assert started_direct[-1] == ("other-test", "发给剩余的提示")
    assert direct_bot.awaiting_direct_reply == {}

    direct_bot.request_direct_reply("cancel-test")
    cancelled_id = next(iter(direct_bot.awaiting_direct_reply))
    direct_bot.cancel_direct_reply("cancel-test")
    before_cancelled_reply = list(started_direct)
    direct_bot.handle_text("取消后的旧提示不能发送", reply_to_message_id=cancelled_id)
    assert started_direct == before_cancelled_reply
    assert direct_bot.awaiting_direct_reply == {}

    direct_bot.submit_direct_reply("desktop-test", "空闲时继续")
    assert started_direct[-1] == ("desktop-test", "空闲时继续")

    direct_bot._thread_activity = lambda _thread_id, timeout=30: (True, "turn-running")  # type: ignore[method-assign]
    direct_bot.submit_direct_reply("desktop-test", "运行中追加")
    pending_sequence = next(iter(direct_bot.pending_direct_replies))
    direct_callbacks = [
        row[0]["callback_data"]
        for row in direct_messages[-1][1]["inline_keyboard"]
    ]
    assert direct_callbacks == [
        f"directchoice|{pending_sequence}:steer",
        f"directchoice|{pending_sequence}:interrupt",
        f"directchoice|{pending_sequence}:side",
        f"directchoice|{pending_sequence}:back",
    ]
    steered_direct: list[tuple[str, str]] = []
    direct_bot.steer_turn = (  # type: ignore[method-assign]
        lambda thread_id, prompt: steered_direct.append((thread_id, prompt))
    )
    direct_bot._handle_direct_reply_choice(f"{pending_sequence}:steer")
    assert steered_direct == [("desktop-test", "运行中追加")]
    assert pending_sequence not in direct_bot.pending_direct_replies

    direct_bot.submit_direct_reply("desktop-test", "改为中断后发送")
    interrupt_sequence = next(iter(direct_bot.pending_direct_replies))
    interrupted_direct: list[tuple[str, str, str]] = []
    direct_bot.interrupt_and_send = (  # type: ignore[method-assign]
        lambda thread_id, prompt, expected_turn_id="":
        interrupted_direct.append((thread_id, prompt, expected_turn_id))
    )
    direct_bot._handle_direct_reply_choice(f"{interrupt_sequence}:interrupt")
    assert interrupted_direct == [("desktop-test", "改为中断后发送", "turn-running")]
    assert interrupt_sequence not in direct_bot.pending_direct_replies

    writer_bot = TelegramCodexBot("test-token", {"chat_id": "1"})
    writer_bot.desktop.can_interrupt = lambda *_args, **_kwargs: True  # type: ignore[method-assign]
    writer_bot.state.snapshot = lambda: {"managed_threads": {"writer-test": {"origin": "bot_created"}}}  # type: ignore[method-assign]
    writer_calls: list[str] = []
    writer_desktop_attempts: list[str] = []

    def fake_writer_request(
        method: str,
        _params: dict[str, Any],
        timeout: float = 60,
    ) -> dict[str, Any]:
        del timeout
        writer_calls.append(method)
        if method == "thread/read":
            return {
                "thread": {
                    "id": "writer-test",
                    "cwd": "D:\\repo",
                    "status": {"type": "notLoaded"},
                }
            }
        if method == "thread/resume":
            raise RpcError("thread/resume: thread writer-test already has an active writer")
        raise AssertionError(method)

    writer_bot.app.request = fake_writer_request  # type: ignore[method-assign]
    writer_bot.desktop.send_follow_up = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: writer_desktop_attempts.append("attempt") or None
    )
    writer_bot.send = lambda *_args, **_kwargs: {"message_id": 11}  # type: ignore[method-assign]
    writer_bot._thread_activity = lambda _thread_id, timeout=30: (False, "")  # type: ignore[method-assign]
    writer_bot.submit_direct_reply("writer-test", "竞态回复")
    assert writer_calls == ["thread/read", "thread/resume"]
    assert writer_desktop_attempts == ["attempt", "attempt"]
    writer_pending = next(iter(writer_bot.pending_direct_replies.values()))
    assert writer_pending["thread_id"] == "writer-test"
    assert writer_pending["text"] == "竞态回复"
    writer_interrupts: list[tuple[str, str]] = []
    writer_restarts: list[tuple[str, str]] = []
    writer_bot.desktop.interrupt_turn = (  # type: ignore[method-assign]
        lambda thread_id, turn_id="", **kwargs: writer_interrupts.append((thread_id, turn_id)) or True
    )
    writer_bot.start_turn = (  # type: ignore[method-assign]
        lambda thread_id, prompt: writer_restarts.append((thread_id, prompt))
    )
    writer_bot.interrupt_and_send("writer-test", "中断后发送")
    assert writer_interrupts == [("writer-test", "")]
    assert writer_restarts == [("writer-test", "中断后发送")]

    stop_bot = TelegramCodexBot("test-token", {"chat_id": "1"})
    stop_bot._thread_activity = lambda _thread_id, timeout=30: (True, "desktop-turn")  # type: ignore[method-assign]
    desktop_interrupts: list[tuple[str, str]] = []
    stop_bot.desktop.interrupt_turn = (  # type: ignore[method-assign]
        lambda thread_id, turn_id="", **kwargs: desktop_interrupts.append((thread_id, turn_id)) or True
    )
    stop_bot.stop_turn("desktop-test", announce=False)
    assert desktop_interrupts == [("desktop-test", "desktop-turn")]

    rich = markdown_to_telegram_html(
        "## Heading\n- `code_value`\nD:\\repo\\a_b\n```\n<x>&y\n```"
    )
    assert "<b>Heading</b>" in rich
    assert "• <code>code_value</code>" in rich
    assert "D:\\repo\\a_b" in rich
    assert "<pre>&lt;x&gt;&amp;y</pre>" in rich

    long_reply = (
        "# 分页测试\n\n"
        + ("这是一个用于验证 Telegram 长回复分页的段落。" * 120)
        + "\n\n```python\n"
        + ("print('跨页代码块')\n" * 180)
        + "```\n\n结束。"
    )
    pages = split_markdown_pages(long_reply, limit=600)
    assert len(pages) > 3
    assert all(len(page) <= 600 for page in pages)
    assert all(
        sum(1 for line in page.splitlines() if line.lstrip().startswith("```")) % 2 == 0
        for page in pages
    )
    cache_key = "0123456789abcdef"
    assert test_bot._parse_reply_page(f"{cache_key}|2") == (cache_key, 2)
    page_keyboard = test_bot._reply_page_keyboard(cache_key, 1, 3)
    callbacks = [
        button["callback_data"]
        for row in page_keyboard["inline_keyboard"]
        for button in row
    ]
    assert callbacks == [
        f"replypage|{cache_key}|0",
        f"replypage|{cache_key}|1",
        f"replypage|{cache_key}|2",
        f"replyfile|{cache_key}",
    ]
    assert all(len(value.encode("utf-8")) <= 64 for value in callbacks)

    original_cache_dir = telegram_notify_module.REPLY_CACHE_DIR
    with tempfile.TemporaryDirectory() as temporary_cache:
        telegram_notify_module.REPLY_CACHE_DIR = Path(temporary_cache)
        try:
            cached_text = "完整回复\n" + ("内容" * 900)
            cached_id = cache_full_reply(
                {"last_assistant_message": cached_text, "cwd": "C:\\repo\\sample"},
                "thread-cache-test",
            )
            assert cached_id is not None
            cached_record = load_cached_reply(cached_id)
            assert cached_record["text"] == cached_text
            assert cached_record["thread_id"] == "thread-cache-test"
            assert cached_record["workspace"] == "sample"
            cached_path = Path(temporary_cache) / f"{cached_id}.json"
            expired_at = time.time() - telegram_notify_module.REPLY_RETENTION_SECONDS - 1
            os.utime(cached_path, (expired_at, expired_at))
            telegram_notify_module.cleanup_reply_cache()
            assert not cached_path.exists()
        finally:
            telegram_notify_module.REPLY_CACHE_DIR = original_cache_dir

    assert not telegram_notify_module.subagent_notifications_enabled({})
    assert not telegram_notify_module.subagent_notifications_enabled(
        {"include_subagent_notifications": "true"}
    )
    assert telegram_notify_module.subagent_notifications_enabled(
        {"include_subagent_notifications": True}
    )
    assert telegram_notify_module.subagent_completion_context({
        "hook_event_name": "SubagentStop",
        "agent_id": "agent-test",
        "agent_type": "reviewer",
    }) == {"agent_id": "agent-test", "agent_type": "reviewer"}
    assert telegram_notify_module.is_empty_internal_exclusion_completion({
        "last-assistant-message": '{"exclude":[]}',
        "threadSource": "ambient_suggestion_safety",
    })
    assert not telegram_notify_module.is_empty_internal_exclusion_completion({
        "last-assistant-message": '{"exclude":["suggestion-1"]}',
    })
    assert not telegram_notify_module.is_empty_internal_exclusion_completion({
        "last-assistant-message": '{"result":[]}',
    })

    notification_calls: list[dict[str, Any]] = []
    notification_config: dict[str, Any] = {
        "chat_id": "1",
        "include_message_preview": False,
        "include_subagent_notifications": False,
        "silent": False,
    }
    original_forward = telegram_notify_module.forward_computer_use_notification
    original_load_config = telegram_notify_module.load_config
    original_muted = telegram_notify_module.notifications_muted
    original_send_markdown = telegram_notify_module.send_markdown_message
    original_managed_env = os.environ.pop("CODEX_TELEGRAM_MANAGED_APP_SERVER", None)
    telegram_notify_module.forward_computer_use_notification = lambda _raw: None
    telegram_notify_module.load_config = lambda: (notification_config, "test-token")
    telegram_notify_module.notifications_muted = lambda: False
    telegram_notify_module.send_markdown_message = (
        lambda _token, request: notification_calls.append(request)
    )
    try:
        subagent_payload = {
            "type": "agent-turn-complete",
            "sourceKind": "subAgent",
            "thread-id": "subagent-test",
            "cwd": "C:\\repo",
            "last-assistant-message": "子代理完成",
        }
        telegram_notify_module.send_desktop_notification(subagent_payload, "{}")
        assert notification_calls == []
        notification_config["include_subagent_notifications"] = True
        telegram_notify_module.send_desktop_notification(subagent_payload, "{}")
        assert len(notification_calls) == 1
        assert "**类型：** 子代理" in notification_calls[-1]["text"]
        notification_config["include_subagent_notifications"] = False
        root_payload = dict(subagent_payload)
        root_payload.pop("sourceKind")
        root_payload["thread-id"] = "root-test"
        telegram_notify_module.send_desktop_notification(root_payload, "{}")
        assert len(notification_calls) == 2
        assert "**类型：** 子代理" not in notification_calls[-1]["text"]
        root_callbacks = [
            button["callback_data"]
            for button in notification_calls[-1]["reply_markup"]["inline_keyboard"][0]
        ]
        assert root_callbacks == ["task|root-test", "direct|root-test"]
        empty_exclusion_payload = dict(root_payload)
        empty_exclusion_payload["last-assistant-message"] = '{"exclude":[]}'
        empty_exclusion_payload['threadSource'] = 'ambient_suggestion_safety'
        telegram_notify_module.send_desktop_notification(empty_exclusion_payload, "{}")
        assert len(notification_calls) == 2

        plan_question_payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "request_user_input",
            "session_id": "01a00000-0000-7000-8000-000000000003",
            "turn_id": "turn-plan-test",
            "cwd": "C:\\repo\\plan-sample",
            "permission_mode": "plan",
            "tool_input": {
                "questions": [{
                    "header": "实现范围",
                    "id": "scope",
                    "question": "请选择第一版范围。",
                    "options": [{
                        "label": "最小范围 (Recommended)",
                        "description": "先完成主路径。",
                    }],
                }],
            },
        }
        telegram_notify_module.send_hook_notification(plan_question_payload)
        assert len(notification_calls) == 3
        assert "正在等待你的回答" in notification_calls[-1]["text"]
        assert "Plan" in notification_calls[-1]["text"]
        plan_callbacks = [
            button["callback_data"]
            for button in notification_calls[-1]["reply_markup"]["inline_keyboard"][0]
        ]
        assert plan_callbacks == [
            "task|01a00000-0000-7000-8000-000000000003",
            "direct|01a00000-0000-7000-8000-000000000003",
        ]
    finally:
        telegram_notify_module.forward_computer_use_notification = original_forward
        if original_managed_env is not None:
            os.environ["CODEX_TELEGRAM_MANAGED_APP_SERVER"] = original_managed_env
        telegram_notify_module.load_config = original_load_config
        telegram_notify_module.notifications_muted = original_muted
        telegram_notify_module.send_markdown_message = original_send_markdown

    original_session_root = telegram_notify_module.SESSION_ROOT
    with tempfile.TemporaryDirectory() as temporary_sessions:
        telegram_notify_module.SESSION_ROOT = Path(temporary_sessions)
        try:
            session_day = Path(temporary_sessions) / "2026" / "08" / "25"
            session_day.mkdir(parents=True)
            root_id = "01a00000-0000-7000-8000-000000000001"
            subagent_id = "01a00000-0000-7000-8000-000000000002"
            (session_day / f"rollout-root-{root_id}.jsonl").write_text(
                json.dumps({
                    "type": "session_meta",
                    "payload": {"id": root_id, "source": "appServer"},
                }) + "\n",
                encoding="utf-8",
            )
            (session_day / f"rollout-subagent-{subagent_id}.jsonl").write_text(
                json.dumps({
                    "type": "session_meta",
                    "payload": {
                        "id": subagent_id,
                        "source": {
                            "subagent": {
                                "thread_spawn": {
                                    "parent_thread_id": root_id,
                                    "agent_path": "/root/reviewer",
                                }
                            }
                        },
                    },
                }) + "\n",
                encoding="utf-8",
            )
            assert telegram_notify_module.subagent_completion_context(
                {"type": "agent-turn-complete", "thread-id": root_id}
            ) is None
            detected_subagent = telegram_notify_module.subagent_completion_context(
                {"type": "agent-turn-complete", "thread-id": subagent_id}
            )
            assert detected_subagent is not None
            assert detected_subagent["thread_spawn"]["parent_thread_id"] == root_id
        finally:
            telegram_notify_module.SESSION_ROOT = original_session_root

    original_config_path = telegram_notify_module.CONFIG_PATH
    with tempfile.TemporaryDirectory() as temporary_config:
        telegram_notify_module.CONFIG_PATH = Path(temporary_config) / "credentials.json"
        try:
            telegram_notify_module.CONFIG_PATH.write_text(
                json.dumps({"chat_id": "1", "include_message_preview": True}),
                encoding="utf-8",
            )
            settings_bot = TelegramCodexBot("test-token", {"chat_id": "1"})
            settings_messages: list[str] = []
            settings_bot.send = (  # type: ignore[method-assign]
                lambda message, *_args, **_kwargs: settings_messages.append(message)
                or {"message_id": 3}
            )
            settings_bot.command_subagents("")
            assert "关闭" in settings_messages[-1]
            settings_bot.command_subagents("on")
            saved_config = json.loads(
                telegram_notify_module.CONFIG_PATH.read_text(encoding="utf-8")
            )
            assert saved_config["include_subagent_notifications"] is True
            settings_bot.command_subagents("off")
            saved_config = json.loads(
                telegram_notify_module.CONFIG_PATH.read_text(encoding="utf-8")
            )
            assert saved_config["include_subagent_notifications"] is False
        finally:
            telegram_notify_module.CONFIG_PATH = original_config_path

    captured_upload: dict[str, Any] = {}

    class FakeTelegramResponse:
        def __enter__(self) -> "FakeTelegramResponse":
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        @staticmethod
        def read() -> bytes:
            return b'{"ok":true,"result":{"document":{"file_id":"test"}}}'

    def fake_urlopen(request: Any, timeout: float = 0) -> FakeTelegramResponse:
        captured_upload["url"] = request.full_url
        captured_upload["body"] = request.data
        captured_upload["content_type"] = request.headers.get("Content-type")
        captured_upload["timeout"] = timeout
        return FakeTelegramResponse()

    original_urlopen = telegram_notify_module.urlopen
    telegram_notify_module.urlopen = fake_urlopen
    try:
        upload_result = telegram_notify_module.telegram_multipart_call(
            "test-token",
            "sendDocument",
            {"chat_id": "1", "disable_notification": True},
            file_field="document",
            filename="完整回复.md",
            content="正文内容".encode("utf-8"),
            content_type="text/markdown; charset=utf-8",
        )
    finally:
        telegram_notify_module.urlopen = original_urlopen
    assert upload_result["document"]["file_id"] == "test"
    assert captured_upload["url"].endswith("/bottest-token/sendDocument")
    assert b'name="chat_id"\r\n\r\n1' in captured_upload["body"]
    assert b'filename="md"' in captured_upload["body"]
    assert "multipart/form-data; boundary=" in captured_upload["content_type"]
    assert "正文内容".encode("utf-8") in captured_upload["body"]

    assert locate_codex().is_file()
    print("self-test passed")
    return 0


class LegacyScenario(unittest.TestCase):
    def test_original_scenario(self):
        import types
        from codex_telegram_bridge import app
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder:
            runtime=Path(folder)/'desktop-runtime';runtime.write_bytes(b'fixture')
            with patch.object(app,'locate_codex',return_value=runtime):
                self.assertEqual(types.FunctionType(self_test.__code__, vars(app))(), 0)
