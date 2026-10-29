"""Validation for bot-owned requests before they enter shared interaction UI."""
from urllib.parse import urlsplit
import json
from codex_telegram_bridge.i18n import tr as _t


def refusal(method):
    if method == 'item/tool/requestUserInput': return {'answers': {}}
    if method == 'item/permissions/requestApproval': return {'permissions': {}, 'scope': 'turn'}
    if method == 'mcpServer/elicitation/request': return {'action': 'decline', 'content': None}
    return {'decision': 'decline'}


def validate_request(bot, message):
    """Return reviewable detail, or fail closed before showing buttons."""
    params, method = message.get('params') or {}, message['method']
    tid, turn = params.get('threadId'), params.get('turnId')
    current = bot.active_turns.get(tid)
    if not current and tid in bot.starting_threads and turn:
        current = turn
    if (not bot._owns_thread(tid) or not current
            or turn != current and not (method == 'mcpServer/elicitation/request' and turn is None)):
        raise ValueError(_t('请求不属于当前机器人回合，已拒绝。'))
    if method == 'item/tool/requestUserInput':
        questions = params.get('questions')
        if not isinstance(questions, list) or not questions or any(not isinstance(q, dict) or q.get('isSecret') for q in questions):
            raise ValueError(_t('敏感信息或无效问题需要在本机处理。'))
    if method == 'item/commandExecution/requestApproval':
        if not params.get('command') and not params.get('networkApprovalContext'):
            raise ValueError(_t('命令审批未提供操作或网络目标，已拒绝。'))
    if method == 'item/fileChange/requestApproval':
        item = bot.request_items.get((tid, turn, params.get('itemId')), {})
        if not item.get('changes'):
            raise ValueError(_t('文件变更未提供可审阅的差异，已拒绝。'))
        return json.dumps(item['changes'], ensure_ascii=False, indent=2)
    return json.dumps({k: params[k] for k in ('command', 'cwd', 'reason', 'permissions',
                       'networkApprovalContext', 'grantRoot', 'additionalPermissions') if params.get(k) is not None},
                     ensure_ascii=False, indent=2)


def safe_url(value):
    parsed = urlsplit(value)
    return (parsed.scheme == 'https' and bool(parsed.hostname) and not parsed.username and not parsed.password)
