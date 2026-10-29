"""Optional configured-runtime preferences; shared by every OS using it."""
from pathlib import Path
import hashlib
from ..i18n import tr as _t
from ..telegram.notifications import send_document


class RuntimeOptions:
    commands = {'/model', '/effort', '/status', '/use', '/last', '/plan', '/progress', '/desktop', '/cancel', '/file'}
    def __init__(self, bot):
        self.bot = bot
        self.models = []

    def menu(self):
        return [{'command': 'model', 'description': _t('选择模型')},
                {'command': 'effort', 'description': _t('思考级别')},
                {'command': 'file', 'description': _t('下载任务目录中的文件')}]

    def show(self, kind='models'):
        result = self.bot.app.request('model/list', {'limit': 100})
        self.models = [m for m in result.get('data', []) if isinstance(m, dict) and m.get('model')]
        rows = []
        if kind == 'efforts':
            current = next((m for m in self.models if m['model'] == self.bot.settings.get('model')), None)
            values = (current or {}).get('supportedReasoningEfforts', [])
            rows = [[{'text': v['reasoningEffort'], 'callback_data': 'runtime|seteffort:'+v['reasoningEffort']}] for v in values]
            title = _t('思考级别')
        else:
            choices = {hashlib.sha256(m['model'].encode()).hexdigest()[:16]: m['model'] for m in self.models}
            self.bot.state.update(runtime_model_choices={**self.bot.state.snapshot().get('runtime_model_choices', {}), **choices})
            rows = [[{'text': m.get('displayName') or m['model'],
                      'callback_data': 'runtime|setmodel:'+hashlib.sha256(m['model'].encode()).hexdigest()[:16]}]
                    for m in self.models]
            title = _t('选择模型')
        rows.append([{'text': _t('返回设置'), 'callback_data': 'settings|home'}])
        self.bot.send('**'+title+'**\n\n'+_t('选择后用于之后的任务。'), {'inline_keyboard': rows})

    def configure(self, kind, value):
        result = self.bot.app.request('model/list', {'limit': 100})
        models = [m for m in result.get('data', []) if isinstance(m, dict)]
        if kind == 'model':
            if not any(m.get('model') == value for m in models):
                raise ValueError(_t('模型不在当前可用列表中。'))
        else:
            model = next((m for m in models if m.get('model') == self.bot.settings.get('model')), None)
            if value not in {v.get('reasoningEffort') for v in (model or {}).get('supportedReasoningEfforts', [])}:
                raise ValueError(_t('当前模型不支持这个思考级别。'))
        self.bot.settings[kind] = value
        self.bot.state.update(**{'runtime_'+kind: value})
        if kind == 'model':
            self.bot.settings['effort'] = None
            self.bot.state.update(runtime_effort=None)

    def callback(self, value):
        action, _, selected = value.partition(':')
        if action in {'models', 'efforts'}:
            self.show(action)
        elif action in {'setmodel', 'seteffort'}:
            if action == 'setmodel':
                selected = self.bot.state.snapshot().get('runtime_model_choices', {}).get(selected, selected)
            self.configure('model' if action == 'setmodel' else 'effort', selected)
            self.bot.command_settings()
        else:
            raise ValueError(_t('按钮已失效，请重新发送命令。'))

    def command(self, command, argument, reply_id):
        if command in {'/model', '/effort'}:
            if argument:
                self.configure(command[1:], argument)
                self.bot.command_settings()
            else:
                self.show('models' if command == '/model' else 'efforts')
        elif command == '/status':
            self.bot.send(_t('机器人在线。')+'\n\n'+_t('运行中的任务：')+str(len(self.bot.active_turns)),
                {'inline_keyboard': [[{'text': _t('查看运行任务'), 'callback_data': 'tasklist|active:0'}]]})
        elif command == '/use':
            row = self.bot.legacy.conversation(argument) if self.bot.legacy else None
            self.bot._request_task_message(row['thread_id'] if row and row.get('thread_id') else self.bot.resolve_thread(argument), 'send')
        elif command in {'/last', '/plan', '/progress', '/desktop'}:
            handler = {'/last': self.bot.command_task, '/plan': self.bot.command_watch,
                       '/progress': self.bot.command_watch, '/desktop': self.bot.command_tasks}[command]
            handler(argument)
        elif command == '/cancel':
            # Preserve the original-thread target. Never clear unrelated jobs.
            from ..telegram.notifications import lookup_task_message
            target = lookup_task_message(self.bot.token, self.bot.chat_id, reply_id) if reply_id else None
            self.bot.command_stop(target or argument)
        elif command == '/file':
            reference, _, path = argument.partition(' ')
            tid = self.bot.resolve_thread(reference)
            thread = self.bot.app.request('thread/read', {'threadId': tid, 'includeTurns': False})['thread']
            root = Path(thread['cwd']).resolve()
            file = (root/path).resolve()
            if not path or not file.is_relative_to(root) or not file.is_file() or file.stat().st_size > 20*1024*1024:
                raise ValueError(_t('文件必须位于该任务工作目录内，且不超过 20 MiB。'))
            send_document(self.bot.token, chat_id=self.bot.chat_id, filename=file.name,
                          content=file.read_bytes(), caption=file.name, silent=True)
        else:
            return False
        return True
