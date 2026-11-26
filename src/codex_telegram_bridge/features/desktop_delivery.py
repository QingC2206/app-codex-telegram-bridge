"""Recover ordinary Telegram text without replaying a potentially written Desktop request."""
import hashlib
import time
import uuid

from ..codex.errors import DesktopBridgeSafetyError, DesktopIpcUnavailable, DesktopSubmissionUnknown
from ..i18n import tr as _t
from ..paths import auth_stamp, codex_home
from ..storage import desktop_inbox as inbox
from ..telegram import notifications as notify


def enabled(bot):
    return (not bot.runtime_options
            and getattr(getattr(bot.desktop, 'access', None), 'supports_background_queue', False) is True)


def identity(bot):
    sender = getattr(bot._ui_local, 'sender_id', '') or (bot.chat_id if bot.chat_id.isdecimal() else '')
    return bot.token.split(':', 1)[0], bot.chat_id, sender


def account_binding(bot):
    profile = codex_home().resolve()
    # A changed login file, even a token refresh, is deliberately conservative.
    # On keyring-only installations use the existing read-only account identity.
    stamp = auth_stamp(profile)
    try:
        principal = ('file', stamp) if stamp else ('account', bot.quota_state.identity())
    except (OSError, RuntimeError, ValueError, AttributeError) as exc:
        notify.log_error('Desktop intake account unavailable: ' + type(exc).__name__)
        return ''
    return hashlib.sha256(repr((str(profile), principal)).encode()).hexdigest()


def may_stage(bot, update):
    message = update.get('message')
    if not enabled(bot) or not isinstance(message, dict) or 'callback_query' in update:
        return False
    text = message.get('text')
    if (type(message.get('message_id')) is not int or message['message_id'] <= 0 or message.get('sender_chat')
            or message.get('photo') or message.get('document') or not isinstance(text, str)
            or not bot._authorized_sender(message.get('chat'), message.get('from'))):
        return False
    from ..core.bot import parse_command
    command, args = parse_command(text)
    return (command == '/send' and bool(args.partition(' ')[2].strip()) if text.lstrip().startswith('/')
            else bool(text.strip()))


class IntakeUnavailable(Exception):
    """Local persistence failed. Leave the Telegram update unacknowledged."""


def full_thread_id(value):
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def admit(bot, thread, text, kind):
    update = getattr(bot._ui_local, 'poll_update', None)
    if not update:
        return False
    if not full_thread_id(thread) or bot._owns_thread(thread):
        bot._consume_poll_update()
        return False
    from ..platforms.windows.credentials import protect_secret
    bid, chat, sender = identity(bot)
    message = update['message']
    key = bid, chat, message['message_id']
    try:
        now = time.time()
        sent = message.get('date')
        created = min(now, sent) if type(sent) in (int, float) and sent > 0 else now
        selected = getattr(bot._ui_local, 'selection_nonce', '') if kind == 'send' else ''
        scope = account_binding(bot)
        inbox.receive(key, update['update_id'], sender, thread, kind, protect_secret(text) if scope else '', scope,
                      bot.state.snapshot().get('quiet_generation', 0), created, selected)
    except Exception as exc:
        raise IntakeUnavailable('Desktop inbox could not commit the Telegram update') from exc
    bot._ui_local.poll_update = None
    bot._defer_feedback()
    return True


def validate(bot, row):
    if (not bot._authorized_sender({'id': row['chat']}, {'id': row['sender']})
            or not bot._background_allowed(row['generation']) or bot._owns_thread(row['thread'])):
        raise DesktopBridgeSafetyError(_t('原账号、权限或静默设置已变化，请重新选择任务发送。'))
    current = account_binding(bot)
    if not current:
        raise DesktopIpcUnavailable(_t('暂时无法核实 Desktop 账号。'))
    if current != row['scope']:
        raise DesktopBridgeSafetyError(_t('原账号、权限或静默设置已变化，请重新选择任务发送。'))


def validate_for_write(bot, row):
    validate(bot, row)
    if row['expires'] <= time.time():
        raise DesktopIpcUnavailable(_t('本次自动发送已到期。'))


def before_write(bot):
    row = getattr(bot._ui_local, 'inbox_row', None)
    if row:
        validate_for_write(bot, row)
        inbox.writing((row['bot'], row['chat'], row['message']))


def send_notice(bot, row, key):
    reason = row['notice']
    keyboard = None
    task = row['thread'][:8]
    if reason == 'confirm':
        text = _t('⏸ 任务 {task} 的指令尚未提交，已暂停自动发送。确认后可继续。', task=task)
        keyboard = {'inline_keyboard': [[{'text': _t('确认发送'), 'callback_data': 'inboxsend|' + row['nonce']},
                                         {'text': _t('取消'), 'callback_data': 'inboxcancel|' + row['nonce']}]]}
    elif reason == 'unknown':
        text = _t('⚠️ 任务 {task} 的提交结果待核实，请查看原任务，消息未重发。', task=task)
    elif reason == 'expired':
        text = _t('任务 {task} 的待发指令已过期，请重新发送。', task=task)
    elif reason == 'waiting':
        text = _t('📨 任务 {task} 的指令已保存在本机，正在等待 Desktop 恢复。', task=task)
    elif reason == 'unbound':
        text = _t('接收指令时无法核实任务 {task} 的 Desktop 账号。请核对登录状态后重新发送。', task=task)
    else:
        text = _t('任务 {task} 的待发指令已停止，请核对账号、设置和原任务后重新发送。', task=task)
    try:
        bot.send(text, keyboard, reply_to=row['message'])
    except notify.TelegramApiError as exc:
        if exc.code == 429:
            from ..telegram.delivery import delivery_error
            inbox.notice_result(key, reason, time.time()+delivery_error(exc)[1])
            return
        raise
    inbox.notice_result(key, reason)


def drain(bot):
    from ..core.bot import RpcError
    from ..platforms.windows.credentials import unprotect_secret
    bid, chat, _ = identity(bot)
    for row in inbox.due(bid, chat):
        key = bid, chat, row['message']
        if row['expires'] <= time.time():
            inbox.expire(key)
            continue
        previous = getattr(bot._ui_local, 'source_message_id', None), getattr(bot._ui_local, 'sender_id', '')
        bot._ui_local.source_message_id, bot._ui_local.sender_id = row['message'], row['sender']
        bot._ui_local.inbox_row = row
        try:
            validate(bot, row)
            text = unprotect_secret(row['body'])
            if row['kind'] == 'reply':
                bot.submit_direct_reply(row['thread'], text)
            else:
                bot._send_plain_text(row['thread'], text)
            inbox.finished(key)
        except DesktopSubmissionUnknown:
            inbox.finished(key, 'unknown')
        except (DesktopIpcUnavailable, RpcError, TimeoutError):
            # Only 'ready' is retryable. A write guard that already ran never reopens.
            inbox.retry(key, time.time())
        except Exception as exc:
            notify.log_error('Desktop intake dispatch: ' + type(exc).__name__)
            inbox.finish_error(key)
        finally:
            bot._ui_local.inbox_row = None
            bot._ui_local.source_message_id, bot._ui_local.sender_id = previous
    for row in inbox.notices(bid, chat):
        key = bid, chat, row['message']
        if not bot._authorized_sender({'id': chat}, {'id': row['sender']}) or bot.quiet_mode_enabled():
            inbox.notice_result(key, row['notice'], time.time()+30)
            continue
        try:
            send_notice(bot, row, key)
        except Exception as exc:
            notify.log_error('Desktop intake notice: ' + type(exc).__name__)
            inbox.notice_result(key, row['notice'])


def confirm(bot, nonce, send):
    actor = identity(bot)
    row = inbox.confirmable(actor, nonce)
    if row is None:
        raise ValueError(_t('这次输入操作已结束，未影响当前操作。'))
    if send:
        validate(bot, row)
    if not inbox.decide(actor, nonce, send):
        raise ValueError(_t('这次输入操作已结束，未影响当前操作。'))
    bot.send(_t('已确认待发指令。') if send else _t('已取消本次输入。'), {'inline_keyboard': []})
