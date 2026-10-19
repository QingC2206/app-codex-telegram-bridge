"""Cycle-bound, single-use quota reset buttons. No network calls on the RPC reader."""
from codex_telegram_bridge.i18n import tr as _t
import hashlib
import json
import threading
import time
import uuid


class QuotaResetController:
    def __init__(self, bot, weekly_snapshot, credit_snapshot, gate):
        self.bot = bot
        self.weekly_snapshot = weekly_snapshot
        self.credit_snapshot = credit_snapshot
        self.gate = gate
        self.lock = threading.RLock()

    def state(self):
        return self.bot.state.snapshot()

    def render_reservation(self, reservation=None):
        reservation = reservation or self.state().get("quota_reset_reservation") or {}
        armed = reservation.get("status") == "armed"
        expiry = time.strftime('%m-%d %H:%M', time.localtime(reservation.get('expires_at', time.time())))
        text = (_t('**预约重置 · {p0}**\n\n到期：{p1}（本机时区）\n\n周剩余低于 3% 时仅使用指定的 1 张卡。\n\n自然重置、额度恢复或机器人重启后提前失效。', p0=format(_t('已生效') if armed else _t('已结束'), ''), p1=format(expiry, '')))
        if reservation.get("reason"):
            text += "\n\n" + reservation["reason"]
        rows = [[{"text": _t('取消预约'), "callback_data": "cancelreserve|" + reservation["id"]},
                 {"text": _t('刷新预约'), "callback_data": "refreshreserve|" + reservation["id"]}]] if armed else []
        rows.append([{"text": _t('返回额度'), "callback_data": "quotaview|refresh"}])
        return text, {"inline_keyboard": rows}

    def update_reservation_card(self, reservation):
        if reservation.get("message_id"):
            if getattr(getattr(self.bot, "_ui_local", None), "message_id", None) == reservation["message_id"]:
                return
            text, keyboard = self.render_reservation(reservation)
            self.bot.enqueue_edit(reservation["message_id"], text, keyboard)

    def cancel_reservation(self, reason):
        reservation = self.state().get("quota_reset_reservation") or {}
        if reservation.get("status") == "armed":
            reservation.update(status="cancelled", reason=reason)
            self.bot.state.update(quota_reset_reservation=reservation)
            self.update_reservation_card(reservation)
            if not reservation.get("message_id"):
                self.bot.enqueue(_t('**预约重置已取消**\n\n') + reason)

    def startup(self):
        with self.lock:
            self.cancel_reservation(_t('机器人已重启，无法确认离线期间额度变化；原预约不再执行。'))
            # Confirmations from an older process/version cannot authorize redemption.
            self.bot.state.update(quota_reset_confirmation={}, quota_reset_selection={})

    def observe(self, result):
        with self.lock:
            now = time.time()
            state = self.state()
            reservation = state.get("quota_reset_reservation") or {}
            if reservation.get("status") == "armed" and now >= reservation["expires_at"]:
                self.cancel_reservation(_t('预约有效期已结束或已到周自然重置时间。'))
            current = self.weekly_snapshot(result)
            previous = state.get("quota_reset_cycle") or {}
            if current is None or current["resets_at"] <= now:
                self.cancel_reservation(_t('无法确认当前周额度周期，原预约已失效。'))
                self.bot.state.update(quota_reset_cycle={}, quota_reset_buttons={}, quota_reset_confirmation={}, quota_reset_selection={})
                return None
            attempt = state.get("quota_reset_attempt") or {}
            if attempt.get("request_sent") and not attempt.get("recovery_observed"):
                # A missing snapshot/restart alone must never unlock another redemption.
                if (attempt.get("resets_at") is not None and
                    (current["resets_at"] != attempt["resets_at"] or
                     (current["used_percent"] < 90 and
                      current["used_percent"] < attempt.get("used_percent", 0)))):
                    attempt["recovery_observed"] = True
                    self.bot.state.update(quota_reset_attempt=attempt)
            changed = (not previous or previous["resets_at"] != current["resets_at"]
                       or current["used_percent"] < previous["used_percent"])
            if changed:
                self.cancel_reservation(_t('检测到周额度恢复或重置时间变化，原预约不能用于新的额度周期。'))
                current = {**current, "id": uuid.uuid4().hex}
                self.bot.state.update(quota_reset_buttons={}, quota_reset_confirmation={}, quota_reset_selection={})
            else:
                current = {**current, "id": previous["id"]}
            self.bot.state.update(quota_reset_cycle=current)
            return current

    def buttons(self, current, tier):
        with self.lock:
            cycle = self.state().get("quota_reset_cycle") or {}
            if not cycle or current["resets_at"] != cycle["resets_at"]:
                return None
            token = uuid.uuid4().hex
            records = self.state().get("quota_reset_buttons") or {}
            records[token] = {"cycle": cycle["id"], "immediate": tier <= 5}
            self.bot.state.update(quota_reset_buttons=records)
            row = [{"text": _t('预约重置'), "callback_data": "reservequota|" + token}]
            if tier <= 5:
                row.append({"text": _t('立即重置'), "callback_data": "resetnow|" + token})
            return {"inline_keyboard": [row]}

    def _identity(self):
        account = self.bot.app.request("account/read", {"refreshToken": False}, timeout=30).get("account")
        if not isinstance(account, dict) or account.get("type") != "chatgpt" or not account.get("email"):
            raise ValueError(_t('无法确认当前账户，未执行重置。'))
        return hashlib.sha256(json.dumps(account, sort_keys=True).encode()).hexdigest()

    def _fresh(self):
        self._drain_events()
        result = self.bot.app.request("account/rateLimits/read", {}, timeout=45)
        self._drain_events()
        cycle = self.observe(result)
        if cycle is None:
            raise ValueError(_t('无法取得有效的周额度，未执行重置。'))
        return result, cycle

    def _drain_events(self):
        import queue
        while True:
            try:
                event = self.bot.quota_events.get_nowait()
            except queue.Empty:
                return
            self.observe(event)

    def _check_unused(self, cycle):
        attempt = self.state().get("quota_reset_attempt") or {}
        if attempt.get("request_sent"):
            if attempt.get("status") in {"uncertain", "consuming"}:
                raise ValueError(_t('上一次兑换结果尚未确认，已锁定新的兑换，避免重复扣卡。请先查看 /quota。'))
            if attempt.get("cycle") == cycle or (attempt.get("cycle") and not attempt.get("recovery_observed")):
                raise ValueError(_t('本周期已经发出过兑换请求，不能重复使用重置卡。'))

    def _select_credit(self, result):
        credits = self.credit_snapshot(result)
        if not credits or credits["available_count"] <= 0:
            raise ValueError(_t('当前没有可用重置卡，未执行重置。'))
        eligible = [c for c in credits["credits"] if c.get("id")
                    and (c.get("expires_at") is None or c["expires_at"] > time.time())]
        if not eligible:
            raise ValueError(_t('未返回有效的重置卡明细，暂不允许预约或兑换。'))
        return min(eligible, key=lambda c: c.get("expires_at") or float("inf"))["id"]

    def handle(self, callback):
        with self.lock:
            message = callback.get("message") or {}
            actor = str((callback.get("from") or {}).get("id") or "")
            if str((message.get("chat") or {}).get("id")) != self.bot.chat_id or not actor:
                raise ValueError(_t('没有权限。'))
            action, _, token = str(callback.get("data") or "").partition("|")
            if action in {"cancelhours", "backhours"}:
                selection = self.state().get("quota_reset_selection") or {}
                if (selection.get("token") != token or selection.get("actor") != actor
                        or not selection.get("message_id")
                        or selection.get("message_id") != message.get("message_id")):
                    raise ValueError(_t('时长选择已关闭或不属于你。'))
                self.bot.state.update(quota_reset_selection={})
                self.bot.send(_t('已退出时长选择，没有建立预约，也没有使用重置卡。'),
                              {"inline_keyboard": [[{"text": _t('返回额度'), "callback_data": "quotaview|refresh"}]]})
                return
            if action == "refreshreserve":
                reservation = self.state().get("quota_reset_reservation") or {}
                if token != reservation.get("id") or actor != reservation.get("actor"):
                    raise ValueError(_t('预约刷新按钮无效。'))
                self._fresh()
                self.bot.send(*self.render_reservation())
                return
            if action == "cancelreserve":
                reservation = self.state().get("quota_reset_reservation") or {}
                if token != reservation.get("id") or actor != reservation.get("actor"):
                    raise ValueError(_t('预约取消按钮无效。'))
                self.cancel_reservation(_t('已由预约人取消。'))
                self.bot.send(*self.render_reservation())
                return
            if action in {"confirmquota", "cancelquota"}:
                return self._confirm(callback, action, token, actor)
            hours = None
            if action == "reservehours":
                token, separator, choice = token.partition("|")
                if not separator or choice not in {"6", "12", "24"}:
                    raise ValueError(_t('请选择 6、12 或 24 小时。'))
                hours = int(choice)
                selection = self.state().get("quota_reset_selection") or {}
                if (selection.get("token") != token or selection.get("actor") != actor
                        or selection.get("message_id") != message.get("message_id")
                        or not selection.get("message_id")
                        or time.time() >= selection.get("expires_at", 0)):
                    raise ValueError(_t('时长选择已失效或不属于你，请重新点击预约重置。'))
            record = (self.state().get("quota_reset_buttons") or {}).get(token)
            if not record:
                raise ValueError(_t('旧额度提醒的按钮已失效，请使用当前周期的新提醒。'))
            result, cycle = self._fresh()
            if record["cycle"] != cycle["id"]:
                raise ValueError(_t('额度周期已变化，旧提醒按钮不能用于本周期。'))
            self._check_unused(cycle["id"])
            remaining = 100 - cycle["used_percent"]
            if action in {"reservequota", "reservehours"}:
                confirmation = self.state().get("quota_reset_confirmation") or {}
                if confirmation.get("expires_at", 0) > time.time():
                    raise ValueError(_t('已有立即重置请求等待确认，请先确认或取消该请求。'))
                old = self.state().get("quota_reset_reservation") or {}
                if old.get("cycle") == cycle["id"]:
                    if old.get("status") == "armed":
                        self.bot.send(*self.render_reservation(old))
                    else:
                        self.bot.send(_t('本周期的预约已结束，旧按钮不能重新预约；可在符合条件时选择立即重置。'))
                    return
                if hours is None:
                    existing = self.state().get("quota_reset_selection") or {}
                    if existing.get("expires_at", 0) > time.time() and existing.get("cycle") == cycle["id"]:
                        self.bot.send(_t('已有时长选择，请使用原消息的按钮；重复点击不会延长有效期。'))
                        return
                    selection = {"token": token, "actor": actor, "cycle": cycle["id"],
                                 "expires_at": time.time() + 120}
                    sent = self.bot.send(_t('**预约重置：选择有效时长**\n\n选择即授权：所选时间内，周剩余额度低于 3% 时自动使用指定的 1 张卡。\n\n到期、自然重置、活动恢复额度或机器人重启后失效；仅执行一次。\n\n请选择（两分钟内有效）：'),
                                         {"inline_keyboard": [[{"text": _t('{p0} 小时', p0=format(h, '')), "callback_data": f"reservehours|{token}|{h}"}
                                                               for h in (6, 12, 24)],
                                                              [{"text": _t('返回'), "callback_data": "backhours|" + token},
                                                               {"text": _t('取消'), "callback_data": "cancelhours|" + token}]]})
                    selection["message_id"] = sent.get("message_id") if isinstance(sent, dict) else None
                    self.bot.state.update(quota_reset_selection=selection)
                    return
                credit = self._select_credit(result)
                identity = self._identity()
                # Account lookup may take time and receive recovery events.
                _, checked_cycle = self._fresh()
                if checked_cycle["id"] != cycle["id"] or time.time() >= selection["expires_at"]:
                    raise ValueError(_t('额度周期已变化或时长选择已过期，请重新预约。'))
                reservation = {"id": uuid.uuid4().hex, "cycle": cycle["id"], "actor": actor,
                               "status": "armed", "hours": hours, "created_at": time.time(),
                               "expires_at": min(time.time() + hours * 3600, cycle["resets_at"]),
                               "identity": identity, "credit_id": credit, "message_id": message.get("message_id")}
                self.bot.state.update(quota_reset_reservation=reservation, quota_reset_selection={})
                self.bot.send(*self.render_reservation(reservation))
                self.bot._schedule_quota_monitor_poll(1)
                return
            if action != "resetnow" or not record["immediate"]:
                raise ValueError(_t('此提醒不允许立即重置。'))
            self.gate(result)
            existing = self.state().get("quota_reset_confirmation") or {}
            if existing.get("cycle") == cycle["id"] and existing.get("expires_at", 0) > time.time():
                self.bot.send(_t('已有待确认的重置请求，请使用原确认按钮，不会创建重复请求。'))
                return
            credit = self._select_credit(result)
            identity = self._identity()
            confirmation = {"nonce": uuid.uuid4().hex, "cycle": cycle["id"], "actor": actor,
                            "expires_at": time.time() + 120, "credit_id": credit, "identity": identity}
            self.cancel_reservation(_t('已选择立即重置，自动预约已取消，避免两种方式同时执行。'))
            self.bot.state.update(quota_reset_confirmation=confirmation)
            sent = self.bot.send(_t('**确认立即重置**\n\n周剩余 {p0}%，确认后使用 1 张卡。确认两分钟内有效，执行前再次检查周剩余 ≤5%。', p0=format(remaining, f'g')),
                                 {"inline_keyboard": [[
                                     {"text": _t('确认使用 1 张重置卡'), "callback_data": "confirmquota|" + confirmation["nonce"]},
                                     {"text": _t('取消'), "callback_data": "cancelquota|" + confirmation["nonce"]}]]})
            confirmation["message_id"] = sent.get("message_id") if isinstance(sent, dict) else None
            self.bot.state.update(quota_reset_confirmation=confirmation)

    def _confirm(self, callback, action, nonce, actor):
        confirmation = self.state().get("quota_reset_confirmation") or {}
        if (not nonce or nonce != confirmation.get("nonce") or actor != confirmation.get("actor")
                or not confirmation.get("message_id")
                or (callback.get("message") or {}).get("message_id") != confirmation["message_id"]):
            raise ValueError(_t('确认已失效、已处理或不属于你。'))
        self.bot.state.update(quota_reset_confirmation={}, quota_reset_selection={})
        if action == "cancelquota":
            self.bot.send(_t('已取消立即重置。'))
            return
        if time.time() >= confirmation["expires_at"]:
            raise ValueError(_t('确认已超过两分钟，未执行重置。'))
        self._execute(confirmation, automatic=False)

    def maybe_execute(self):
        with self.lock:
            reservation = self.state().get("quota_reset_reservation") or {}
            if reservation.get("status") != "armed":
                return
            if time.time() >= reservation["expires_at"]:
                self.cancel_reservation(_t('预约有效期已结束。'))
                return
            try:
                result, cycle = self._fresh()
                current = self.state().get("quota_reset_reservation") or {}
                if current.get("status") != "armed" or current.get("cycle") != cycle["id"]:
                    return
                if 100 - cycle["used_percent"] >= 3:
                    return
                self._execute(current, automatic=True)
            except Exception as exc:
                self.cancel_reservation(_t('无法安全确认执行条件，预约已取消：') + str(exc))

    def _execute(self, authorization, automatic):
        # Caller holds the one lock shared by immediate and automatic execution.
        identity = self._identity()
        result, cycle = self._fresh()
        if authorization["identity"] != identity:
            raise ValueError(_t('账户已变化，原授权失效。'))
        if cycle["id"] != authorization["cycle"] or time.time() >= authorization["expires_at"]:
            raise ValueError(_t('额度周期已变化或授权已过期，未执行重置。'))
        if automatic:
            reservation = self.state().get("quota_reset_reservation") or {}
            if reservation.get("status") != "armed" or reservation.get("id") != authorization["id"]:
                raise ValueError(_t('预约已取消或已执行。'))
            if 100 - cycle["used_percent"] >= 3:
                return
        self.gate(result)
        self._check_unused(cycle["id"])
        credits = self.credit_snapshot(result)
        if not any(c["id"] == authorization["credit_id"] and
                   (c.get("expires_at") is None or c["expires_at"] > time.time()) for c in credits["credits"]):
            raise ValueError(_t('预约或确认所指定的卡已不可用，不会自动改用另一张卡。'))
        self._drain_events()
        live_cycle = self.state().get("quota_reset_cycle") or {}
        if live_cycle.get("id") != authorization["cycle"] or time.time() >= authorization["expires_at"]:
            raise ValueError(_t('执行前额度发生变化或授权过期，未执行重置。'))
        attempt = {"cycle": cycle["id"], "idempotency_key": str(uuid.uuid4()),
                   "credit_id": authorization["credit_id"], "status": "consuming", "request_sent": True,
                   "resets_at": cycle["resets_at"], "used_percent": cycle["used_percent"],
                   "recovery_observed": False}
        reservation = self.state().get("quota_reset_reservation") or {}
        if reservation.get("status") == "armed":
            reservation["status"] = "triggered"
        # One atomic save before the API call: a crash/restart cannot re-arm this request.
        self.bot.state.update(quota_reset_attempt=attempt, quota_reset_reservation=reservation,
                               quota_reset_confirmation={}, quota_reset_buttons={})
        self.update_reservation_card(reservation)
        try:
            response = self.bot.app.request("account/rateLimitResetCredit/consume",
                                            {"idempotencyKey": attempt["idempotency_key"],
                                             "creditId": attempt["credit_id"]}, timeout=45)
            outcome = response.get("outcome")
            if outcome not in {"reset", "alreadyRedeemed", "noCredit", "nothingToReset"}:
                raise ValueError(_t('未知兑换结果'))
        except Exception:
            attempt["status"] = "uncertain"
            self.bot.state.update(quota_reset_attempt=attempt)
            if not automatic:
                self.bot.send(_t('**重置结果暂时无法确认**\n\n已锁定新的兑换，请先核对额度。'),
                              {"inline_keyboard": [[{"text": _t('查看额度'), "callback_data": "quotaview|refresh"}]]})
            self.bot.enqueue(_t('**重置结果暂时无法确认**\n\n已停止自动重试并锁定新的兑换，避免重复扣卡。请先通过 `/quota` 核对。'))
            return
        attempt["status"] = outcome
        self.bot.state.update(quota_reset_attempt=attempt)
        labels = {"reset": _t('已使用 1 张重置卡'), "alreadyRedeemed": _t('本次兑换此前已完成，未再次扣卡'),
                  "noCredit": _t('没有可用重置卡，未执行重置'), "nothingToReset": _t('没有可重置窗口，未执行重置')}
        if automatic:
            reservation["reason"] = labels[outcome]
            self.bot.state.update(quota_reset_reservation=reservation)
            self.update_reservation_card(reservation)
        else:
            self.bot.send("**" + labels[outcome] + _t('**\n\n本次授权已结束。'),
                          {"inline_keyboard": [[{"text": _t('返回额度'), "callback_data": "quotaview|refresh"}]]})
        self.bot.enqueue("**" + (_t('预约重置：') if automatic else _t('立即重置：')) + labels[outcome] + _t('**\n\n本次授权已结束，不会继续使用其他卡。'))
        try:
            refreshed = self.bot.app.request("account/rateLimits/read", {}, timeout=45)
            self.bot._observe_quota(refreshed)
        except Exception:
            self.bot.enqueue(_t('重置后额度查询失败，请稍后用 `/quota` 查看。'))
