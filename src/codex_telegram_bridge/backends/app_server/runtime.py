"""Configured app-server profile of the shared Telegram controller.

This adapter owns startup, migration and old wire identifiers. Commands,
callbacks, notifications, approvals and task transitions live in core.bot.
"""
from functools import partial
import json
from pathlib import Path
import sys
import threading

from ... import __version__
from ...core.bot import TelegramCodexBot, CodexAppServer, log
from ...paths import ensure_data_dir
from ...platforms.locking import SingleInstance
from ...storage.bridge import BridgeState
from ...storage.migration import migrate_controller
from ...telegram import notifications as notify
from .client import AppServerClient
from ...telegram.notifications import telegram_call


def credentials():
    loaded = notify.load_config()
    if loaded is None:
        raise RuntimeError('Telegram credentials are not configured.')
    config, token = loaded
    return token, int(config['chat_id'])


def thread_options(cwd, settings):
    from ...core.runtime_policy import request_options
    return request_options('thread/start', {'cwd': cwd}, settings)


class AppServerBot(TelegramCodexBot):
    def __init__(self, settings, *, token=None, credential_config=None, app_factory=None, legacy=None):
        if token is None:
            loaded = notify.load_config()
            if loaded is None:
                raise RuntimeError('Telegram credentials are not configured.')
            credential_config, token = loaded
        self.settings = dict(settings)
        super().__init__(token, credential_config,
                         app_factory=app_factory or partial(CodexAppServer, runtime=self.settings))
        from ...features.runtime_options import RuntimeOptions
        self.runtime_options = RuntimeOptions(self)
        self.strict_requests = True
        self.stop = self.stop_event
        self.legacy = legacy
        self.hook_worker = None

    def run(self, *, quiet_start=False):
        with SingleInstance():
            try:
                self.legacy = self.legacy or BridgeState(ensure_data_dir()/'app-server-state.sqlite3')
                migrate_controller(self, self.legacy, self.settings)
                for name in ('model', 'effort'):
                    self.settings[name] = self.state.snapshot().get('runtime_'+name, self.settings.get(name))
                from ...telegram.controller_delivery import ControllerDelivery
                self.delivery = ControllerDelivery(self, self.legacy)
                notify.telegram_call(self.token, 'getMe')
                if notify.telegram_call(self.token, 'getWebhookInfo').get('url'):
                    raise RuntimeError('Telegram webhook is configured; polling was not started.')
                self.hook_worker = threading.Thread(target=self._hook_loop, daemon=True,
                                                   name='desktop-hook-inbox')
                self.hook_worker.start()
                return super()._run_service(quiet_start=quiet_start)
            finally:
                try:
                    self.shutdown()
                finally:
                    try:
                        self._wait_service_workers()
                    finally:
                        # A shared shutdown error must not skip the writer's
                        # join and release the reservation during an upload.
                        if self.hook_worker and self.hook_worker.ident is not None:
                            self.hook_worker.join()
                        if self.legacy:
                            self.legacy.close()

    def _hook_loop(self):
        sender = threading.Thread(target=self.delivery.run, args=(self.stop_event,),
                                  daemon=True, name='telegram-delivery')
        sender.start()
        try:
            while not self.stop_event.is_set():
                try:
                    self.process_hooks()
                except Exception as exc:
                    log('Desktop hook inbox postponed: '+type(exc).__name__, self.token)
                self.stop_event.wait(2)
        finally:
            self.delivery.state.outbox_event.set()
            # Keep the hook worker alive until the writer exits. run() must
            # not close its database underneath an in-flight upload.
            sender.join()

    def process_hooks(self):
        for key, event in self.legacy.pending_hooks():
            if self.stop_event.is_set():
                break
            payload = event.get('payload') or {
                'hook_event_name': 'Stop', 'session_id': event['thread_id'],
                'turn_id': event['turn_id'], 'cwd': event['cwd'],
                'last_assistant_message': event['text']}
            if not event.get('suppressed') and event.get('quiet_generation', 0) == self.state.snapshot().get('quiet_generation', 0):
                notify.send_hook_notification(payload)
            self.legacy.hook_handled(key)

    def handle_update(self, update):
        # Translation has no side effects and is reached only after sender
        # authentication. Authorization also runs again in the shared core.
        callback = update.get('callback_query')
        if not callback and self.legacy:
            if (update.get('message') or {}).get('date', 0) < self.legacy.get('accept_after', 0):
                return
        if callback and self.legacy and self._authorized_sender(
                (callback.get('message') or {}).get('chat'), callback.get('from')):
            from .compatibility import translate_callback
            callback = translate_callback(self, callback)
            update = {**update, 'callback_query': callback}
        return super().handle_update(update)

    def compatibility_callback(self, action, value):
        from .compatibility import handle_callback
        return handle_callback(self, action, value)


def doctor(settings, offline=False):
    python_version = sys.version.split()[0]
    report = {'backend': 'app-server', 'python': sys.executable, 'version': python_version,
              'bridge_version': __version__, 'python_version': python_version,
              'codex_executable': settings['codex_executable'], 'cwd': settings['cwd'], 'ssl_verification': True}
    ok = Path(settings['codex_executable']).is_file() and Path(settings['cwd']).is_dir()
    if not offline:
        client = AppServerClient(settings['codex_executable'], ensure_data_dir()/'app-server-doctor.stderr.log')
        try:
            client.start()
            report['app_server'] = 'connected'
            account = client.request('account/read', {'refreshToken': False})
            report['account_type'] = (account.get('account') or {}).get('type')
            token, chat = credentials()
            me = telegram_call(token, 'getMe')
            webhook = telegram_call(token, 'getWebhookInfo')
            report['telegram'] = 'connected'
            report['bot_username'] = me.get('username')
            report['private_chat_configured'] = chat > 0
            report['webhook_configured'] = bool(webhook.get('url'))
            ok = ok and bool(account.get('account')) and not webhook.get('url')
            from ...codex.desktop import DesktopControl
            from ...codex.ipc import CodexDesktopIpc
            DesktopControl(CodexDesktopIpc).native.probe()
            report['desktop_connection'] = 'connected'
            report['live_task_submission_tested'] = False
        except Exception as exc:
            report['error_type'] = type(exc).__name__
            ok = False
        finally:
            client.close()
    report['ok'] = bool(ok)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if ok else 1
