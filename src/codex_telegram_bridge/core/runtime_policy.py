"""Runtime settings change execution policy, never Telegram interaction rules."""
from copy import deepcopy


def request_options(method, params, settings, *, side_chat=False):
    result = deepcopy(params)
    if method in {'thread/start', 'thread/resume'} and not side_chat:
        result.update(approvalPolicy='on-request', sandbox='workspace-write')
        result['config'] = {**result.get('config', {}), 'notify': [],
                            'sandbox_workspace_write.network_access': False}
        if settings.get('model'):
            result['model'] = settings['model']
    if method == 'turn/start' and not side_chat:
        result['approvalPolicy'] = 'on-request'
        if settings.get('model'):
            result['model'] = settings['model']
        if settings.get('effort'):
            result['effort'] = settings['effort']
    return result
