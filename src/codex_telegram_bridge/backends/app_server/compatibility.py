"""Decode old Telegram card identifiers into the common interaction protocol.

This module does not implement task operations or rendering. It retains old
links while there is only one live command/callback state machine.
"""
import json
from codex_telegram_bridge.i18n import tr as _t
from pathlib import Path
from ...telegram import notifications as notify


def translate_callback(bot, callback):
    parts = str(callback.get('data') or '').split('|', 2)
    if len(parts) != 3 or parts[0] != 'ux':
        return callback
    _, action, value = parts
    simple = {'help': 'compathelp', 'quota': 'quotaview', 'settings': 'settings',
              'language': 'language', 'list': 'tasklist', 'notify': 'settings',
              'models': 'runtime', 'efforts': 'runtime', 'setmodel': 'runtime', 'seteffort': 'runtime'}
    tasks = {'task': 'task', 'progress': 'watch', 'watch': 'watch', 'unwatch': 'unwatch',
             'usage': 'usage', 'diff': 'diff', 'reply': 'direct', 'stopask': 'compatstop', 'stop': 'stop'}
    if action in tasks:
        row = bot.legacy.conversation(value) or bot.legacy.by_thread(value)
        target = row.get('thread_id') if row else None
        data = tasks[action]+'|'+target if target else 'expired|'
    elif action in simple:
        if action == 'notify':
            value = 'completion:'+value
        elif action in {'models', 'efforts', 'setmodel', 'seteffort'}:
            value = action+':'+value
        elif action == 'list' and value.startswith('desktop:'):
            value = 'all:'+value.partition(':')[2]
        data = simple[action]+'|'+value
    elif action in {'page', 'download'}:
        view, _, page = value.partition(':')
        cached = bot.state.snapshot().get('legacy_reply_cache', {}).get(view)
        data = (('reply|'+cached+'|'+(page or '0')) if action == 'page' else 'replyfile|'+cached) if cached else 'expired|'
    elif action in {'draft', 'continue', 'retrydesktop'}:
        # A former "new task" button is never turned into a send operation.
        data = ('legacyretry' if action == 'retrydesktop' else 'legacydraft')+'|'+value
    else:
        data = 'expired|'
    return {**callback, 'data': data}


def handle_callback(bot, action, value):
    if action == 'compathelp':
        bot.command_help()
    elif action == 'compatstop':
        bot.send(_t('⏹ 确认中止这个任务？'), {'inline_keyboard': [[
            {'text': _t('确认中止'), 'callback_data': 'stop|'+value},
            {'text': _t('返回任务'), 'callback_data': 'task|'+value}]]})
    elif action == 'legacydraft':
        job = bot.legacy.job(int(value))
        if not job:
            raise ValueError(_t('原消息记录不可用。'))
        notify.send_document(bot.token, chat_id=bot.chat_id,
            filename='codex-message-'+str(job['update_id'])+'.md',
            content=job['prompt'].encode(), caption=_t('原消息；下载不会执行任务。'), silent=True)
    elif action == 'legacyretry':
        bot._require_task_actions()
        job = bot.legacy.job(int(value))
        row = bot.legacy.conversation(job['conversation']) if job else None
        if not row or row['origin'] != 'desktop' or not row.get('thread_id'):
            raise ValueError(_t('原任务记录不可用。'))
        prompt = job['prompt']
        for attachment in json.loads(job.get('input') or '[]')[1:]:
            if attachment.get('type') == 'localImage':
                path = Path(attachment.get('path', ''))
                if not path.is_absolute() or not path.is_file():
                    raise ValueError(_t('原附件已不可用，请重新回复原任务并上传附件。'))
                prompt += '\n\n'+_t('用户附件（本机绝对路径）：')+str(path)
            elif attachment.get('type') == 'text':
                prompt += '\n\n'+attachment.get('text', '')
            else:
                raise ValueError(_t('原附件格式无法确认，请重新上传。'))
        if not bot.legacy.claim_desktop_input(int(value), retry=True):
            raise ValueError(_t('这条消息已处理或接收状态未确认，未重复提交。'))
        # Preserve the original prompt and use the same running-turn chooser.
        # Never retry on an uncertain result, even after process restart.
        source = bot.legacy.get('input_message:'+str(job['update_id']))
        if not isinstance(source, int) or source <= 0:
            # Old records without a trustworthy Telegram source can still be
            # retried, but never attach feedback to an unrelated clicked card.
            bot.submit_direct_reply(row['thread_id'], prompt)
            return True
        previous = getattr(bot._ui_local, 'source_message_id', None)
        previous_feedback = getattr(bot._ui_local, 'feedback', None)
        bot._ui_local.source_message_id = source
        bot._ui_local.feedback = {'source': source, 'deferred': False, 'failed': False}
        operation = bot.delivery.feedback.operation(job['update_id'], source) if bot.delivery else None
        if operation:
            operation.__enter__()
        try:
            bot.submit_direct_reply(row['thread_id'], prompt)
            bot._finish_feedback()
        except Exception as exc:
            bot._feedback_exception(exc)
            bot._finish_feedback()
            raise
        finally:
            if operation:
                operation.__exit__(None, None, None)
            bot._ui_local.source_message_id = previous
            bot._ui_local.feedback = previous_feedback
    else:
        return False
    return True
