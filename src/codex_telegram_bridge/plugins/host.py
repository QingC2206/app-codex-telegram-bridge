"""Discover trusted local plugins without importing them into the bot process."""
from codex_telegram_bridge.i18n import tr as _t
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

import codex_telegram_bridge.telegram.notifications as notify
from ..paths import resources_dir
from ..platforms.processes import ChildProcessJob, spawn_options
from codex_telegram_bridge.plugins.bundled import install_bundled
from ..i18n import get_language

CORE_COMMANDS = set('start help settings language plugins side tasks running task watch diff usage quota new send steer stop cwd logs mute unmute subagents'.split())
ROOT = Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex')
PLUGIN_ROOT = ROOT/'telegram-plugins'
STATE_ROOT = ROOT/'telegram-plugin-state'
MAX_FILE = 49 * 1024 * 1024


@dataclass(frozen=True)
class Plugin:
    id: str
    manifest: dict
    files: dict
    config: dict
    revision: str


class PluginHost:
    def __init__(self, bot, root=PLUGIN_ROOT, state_root=STATE_ROOT):
        self.bot, self.root, self.state_root = bot, Path(root), Path(state_root)
        self.lock = threading.RLock()
        self.plugins, self.commands, self.errors = {}, {}, {}
        self.running, self.next_run = set(), {}
        self.jobs = set()
        self._menu_signature = None

    @contextmanager
    def db(self):
        self.state_root.mkdir(parents=True, exist_ok=True)
        bot_id = hashlib.sha256(self.bot.token.split(':')[0].encode()).hexdigest()[:16]
        db = sqlite3.connect(self.state_root/f'dispatch-{bot_id}.sqlite3', timeout=5)
        try:
            db.execute('CREATE TABLE IF NOT EXISTS callbacks (token TEXT PRIMARY KEY, plugin TEXT, revision TEXT, chat TEXT, source INTEGER, panel INTEGER, action TEXT, expires REAL, used INTEGER DEFAULT 0)')
            db.execute('CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, created REAL)')
            with db:
                yield db
        finally:
            db.close()

    def seed_bundled(self):
        self.bundle_notes = install_bundled(resources_dir(), self.root, self.state_root)

    def discover(self):
        found, errors = {}, {}
        if self.root.exists():
            for folder in sorted(self.root.iterdir()):
                if folder.name.startswith('.') or not folder.is_dir(): continue
                try:
                    if folder.is_symlink() or folder.resolve().parent != self.root.resolve():
                        raise ValueError(_t('插件目录不可链接到外部路径'))
                    manifest = json.loads((folder/'plugin.json').read_text(encoding='utf-8-sig'))
                    raw_manifest = dict(manifest)
                    if manifest.get('enabled', True) is False: continue
                    ident = manifest.get('id', '')
                    if not isinstance(ident, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,31}', ident) or folder.name != ident:
                        raise ValueError(_t('id 必须与目录名一致，仅使用小写字母、数字、下划线'))
                    if manifest.get('api_version') != 1: raise ValueError(_t('不支持的 api_version'))
                    if not isinstance(manifest.get('version'), str) or not manifest['version']: raise ValueError(_t('缺少 version'))
                    entry = manifest.get('entrypoint', 'main.py')
                    if not re.fullmatch(r'[A-Za-z0-9_]+\.py', str(entry)): raise ValueError(_t('entrypoint 必须为目录内 Python 文件'))
                    manifest['entrypoint'] = entry
                    commands = manifest.get('commands')
                    if not isinstance(commands, dict) or not commands: raise ValueError(_t('commands 不能为空'))
                    for name, description in commands.items():
                        if not re.fullmatch(r'[a-z][a-z0-9_]{0,31}', name) or name in CORE_COMMANDS:
                            raise ValueError(_t('命令无效或与主程序冲突：')+name)
                        if not isinstance(description, str) or not 1 <= len(description) <= 100: raise ValueError(_t('命令说明需要 1–100 字符'))
                    translations = manifest.get('command_descriptions', {})
                    if not isinstance(translations, dict): raise ValueError('command_descriptions must be an object')
                    for language, descriptions in translations.items():
                        if language not in {'zh-CN', 'en'} or not isinstance(descriptions, dict):
                            raise ValueError('command_descriptions supports zh-CN and en objects')
                        if any(name not in commands or not isinstance(text, str) or not 1 <= len(text) <= 100
                               for name, text in descriptions.items()):
                            raise ValueError('Translated descriptions must reference known commands and contain 1–100 characters')
                    if not 1 <= manifest.get('timeout_seconds', 60) <= 180: raise ValueError(_t('超时范围 1–180 秒'))
                    if not 0 <= manifest.get('cooldown_seconds', 0) <= 3600: raise ValueError(_t('冷却范围 0–3600 秒'))
                    files, total = {}, 0
                    for path in sorted(folder.rglob('*')):
                        if '__pycache__' in path.parts or path.name.startswith('.'): continue
                        if not path.is_file(): continue
                        if not path.resolve().is_relative_to(folder.resolve()): raise ValueError(_t('插件文件不能链接到目录外'))
                        if path.stat().st_size > 5*1024*1024: raise ValueError(_t('插件资源过大'))
                        data = path.read_bytes(); total += len(data)
                        if total > 5*1024*1024 or len(files) >= 200: raise ValueError(_t('插件最多 200 文件、5 MB'))
                        files[path.relative_to(folder).as_posix()] = data
                    if entry not in files: raise ValueError(_t('入口文件不存在'))
                    # Validate syntax but never execute third-party code in the host.
                    for name, data in files.items():
                        if name.endswith('.py'): compile(data, name, 'exec')
                    if json.loads(files['plugin.json'].decode('utf-8-sig')) != raw_manifest:
                        raise ValueError(_t('插件清单正在更新，请稍后重试'))
                    config_path = self.state_root/ident/'config.json'
                    if config_path.exists() and config_path.stat().st_size > 32768: raise ValueError(_t('config 最多 32 KB'))
                    config = json.loads(config_path.read_text(encoding='utf-8-sig')) if config_path.exists() else manifest.get('config', {})
                    if not isinstance(config, dict): raise ValueError(_t('config 必须是对象'))
                    if len(json.dumps(config).encode()) > 32768: raise ValueError(_t('config 最多 32 KB'))
                    digest = hashlib.sha256()
                    for name, data in files.items(): digest.update(name.encode()+b'\0'+data)
                    digest.update(json.dumps(config, sort_keys=True).encode())
                    found[ident] = Plugin(ident, manifest, files, config, digest.hexdigest())
                except Exception as exc:
                    errors[folder.name] = str(exc)[:250]
        owners = {}
        for ident, plugin in found.items():
            for cmd in plugin.manifest['commands']: owners.setdefault(cmd, []).append(ident)
        conflicts = {ident for ids in owners.values() if len(ids)>1 for ident in ids}
        for ident in conflicts:
            errors[ident] = _t('命令与其他插件冲突，冲突双方均已停用'); found.pop(ident)
        with self.lock:
            self.plugins, self.errors = found, errors
            self.commands = {cmd:p for p in found.values() for cmd in p.manifest['commands']}
        return found

    def menu(self):
        self.discover()
        with self.lock:
            return [{'command': cmd, 'description': p.manifest.get('command_descriptions', {}).get(get_language(), {}).get(cmd, p.manifest['commands'][cmd])}
                    for cmd,p in sorted(self.commands.items())]

    def help(self):
        rows = self.menu()
        return '\n\n'.join(f"`/{r['command']}` — {r['description']}" for r in rows)

    def status(self):
        self.discover()
        lines = [_t('**🧩 本地插件**'), _t('插件目录：`{p0}`', p0=format(self.root, ''))]
        for p in self.plugins.values():
            lines.append(f"**{p.id} · {p.manifest['version']}**\n\n"+' / '.join('`/'+x+'`' for x in p.manifest['commands']))
        for name, error in self.errors.items(): lines.append(f'❌ {name}：{error}')
        if not self.plugins: lines.append(_t('没有已启用的插件。'))
        lines.extend(getattr(self,'bundle_notes',[]))
        lines.append(_t('文件变更自动识别；删除目录或设置 enabled=false 可停用。'))
        self.bot.send('\n\n'.join(lines))

    def start(self):
        self.seed_bundled()
        self.discover()
        threading.Thread(target=self._watch, daemon=True, name='telegram-plugin-watch').start()

    def _watch(self):
        while not self.bot.stop_event.wait(10):
            try:
                signature = json.dumps(self.menu(), sort_keys=True)
                if signature != self._menu_signature:
                    if self.bot._set_commands(): self._menu_signature = signature
            except Exception:
                pass  # Local diagnostics remain visible through /plugins.

    def command(self, command, argument):
        self.discover()
        with self.lock: plugin = self.commands.get(command.lstrip('/'))
        if not plugin: return False
        source = getattr(self.bot._ui_local, 'source_message_id', None)
        key = f'cmd:{self.bot.chat_id}:{source}' if source else None
        self.launch(plugin, {'kind':'command', 'command':command.lstrip('/'), 'argument':argument}, source, None, key)
        return True

    def callback(self, data, panel):
        token = data.removeprefix('plug|')
        self.discover()
        with self.db() as db:
            row = db.execute('SELECT plugin,revision,chat,source,panel,action,expires,used FROM callbacks WHERE token=?', (token,)).fetchone()
        if not row or row[2] != self.bot.chat_id or row[4] != panel or row[6] < time.time() or row[7]:
            raise ValueError(_t('此插件按钮已失效或已处理，请重新发送插件命令。'))
        plugin = self.plugins.get(row[0])
        if not plugin or plugin.revision != row[1]: raise ValueError(_t('插件或配置已更新，请重新发送命令。'))
        self.launch(plugin, {'kind':'callback', 'action':json.loads(row[5])}, row[3], panel, 'callback:'+token, token)

    def launch(self, plugin, request, source, panel, key, token=None):
        with self.lock:
            if plugin.id in self.running: raise ValueError(_t('该插件正在处理，请稍候。'))
            if len(self.running) >= 3: raise ValueError(_t('插件繁忙，请稍后重试。'))
            if time.monotonic() < self.next_run.get(plugin.id, 0): raise ValueError(_t('刚刚已执行，请稍等几秒再试。'))
            with self.db() as db:
                db.execute('DELETE FROM callbacks WHERE expires<?', (time.time()-3600,))
                db.execute('DELETE FROM requests WHERE created<?', (time.time()-7*86400,))
                if key and not db.execute('INSERT OR IGNORE INTO requests VALUES(?,?)', (key,time.time())).rowcount:
                    raise ValueError(_t('这次请求已处理，不会重复发送。'))
                if token:
                    if not db.execute('UPDATE callbacks SET used=1 WHERE token=? AND used=0', (token,)).rowcount:
                        raise ValueError(_t('这个按钮已处理。'))
                    # A panel is one interaction generation. Other old buttons
                    # must not remain live after navigating or sending a file.
                    db.execute('UPDATE callbacks SET used=1 WHERE chat=? AND panel=?', (self.bot.chat_id,panel))
            self.running.add(plugin.id)
        worker = threading.Thread(target=self._run, args=(plugin,request,source,panel), daemon=True, name='plugin-'+plugin.id)
        with self.lock: self.jobs.add(worker)
        getattr(self.bot, '_defer_feedback', lambda: None)()
        try: worker.start()
        except Exception:
            with self.lock: self.running.discard(plugin.id); self.jobs.discard(worker)
            raise

    def execute(self, plugin, request, folder):
        code, output = folder/'code', folder/'output'
        code.mkdir(); output.mkdir()
        for name, data in plugin.files.items():
            path = code/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
        state = self.state_root/plugin.id; state.mkdir(parents=True, exist_ok=True)
        wire = {'plugin_dir':str(code), 'entrypoint':plugin.manifest['entrypoint'], 'output_dir':str(output),
                'state_dir':str(state), 'result_path':str(folder/'result.json'), 'config':plugin.config, 'request':request,
                'language':get_language()}
        # Do not forward arbitrary process environment (API keys, tokens, etc.).
        env = {key:value for key,value in os.environ.items() if key.upper() in
               {'SYSTEMROOT','WINDIR','TEMP','TMP','PATH','USERPROFILE','LOCALAPPDATA','APPDATA','PROGRAMFILES','PROGRAMFILES(X86)','HOME','DISPLAY','WAYLAND_DISPLAY','XDG_RUNTIME_DIR','DBUS_SESSION_BUS_ADDRESS','XAUTHORITY'}}
        env.update(PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1', CODEX_HOME=str(ROOT.resolve()))
        flags = subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
        proc = subprocess.Popen([sys.executable, '-B', '-m', 'codex_telegram_bridge.plugins.worker'],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=code, env=env, creationflags=flags, **spawn_options())
        job = ChildProcessJob()
        job.attach(proc)
        try:
            proc.communicate(json.dumps(wire, ensure_ascii=False).encode('utf-8'), timeout=plugin.manifest.get('timeout_seconds',60))
        except subprocess.TimeoutExpired:
            job.close()
            proc.communicate(timeout=5)
            raise TimeoutError(_t('插件执行超时，已终止；不会自动重试。'))
        finally:
            job.close()
        path = folder/'result.json'
        if not path.exists() or path.stat().st_size>262144: raise ValueError(_t('插件未返回有效结果'))
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value,dict) or 'error' in value:
            if isinstance(value,dict): (state/'last-error.txt').write_text(str(value.get('error')),encoding='utf-8')
            raise ValueError(_t('插件执行失败，详见本机插件状态目录中的 last-error.txt'))
        self.validate(value, output)
        return value

    @staticmethod
    def validate(value, output):
        actions = value.get('actions')
        if not isinstance(actions,list) or not 1<=len(actions)<=12: raise ValueError(_t('插件必须返回 1–12 个动作'))
        total = 0
        def file(path):
            nonlocal total
            p = Path(path).resolve()
            if not p.is_relative_to(output.resolve()) or not p.is_file(): raise ValueError(_t('附件必须位于本次 output_dir 内'))
            size = p.stat().st_size; total += size
            if size>MAX_FILE or total>150*1024*1024: raise ValueError(_t('插件附件超过大小限制'))
        for action in actions:
            kind = action.get('type')
            if kind=='message':
                if not isinstance(action.get('text'),str) or not 1<=len(action['text'])<=3500: raise ValueError(_t('每条消息限 1–3500 字符，请分页'))
                rows = action.get('buttons',[])
                if not isinstance(rows,list) or len(rows)>12: raise ValueError(_t('按钮最多 12 行'))
                for row in rows:
                    if not isinstance(row,list) or not 1<=len(row)<=4: raise ValueError(_t('每行按钮限 1–4 个'))
                    for item in row:
                        if not isinstance(item.get('text'),str) or not 1<=len(item['text'])<=64 or not isinstance(item.get('action'),dict): raise ValueError(_t('按钮格式无效'))
                        if len(json.dumps(item['action']).encode())>8192: raise ValueError(_t('按钮参数过大'))
            elif kind=='document':
                file(action['path'])
                if not isinstance(action.get('filename'),str) or Path(action['filename']).name!=action['filename']: raise ValueError(_t('附件名称无效'))
                if len(action.get('caption',''))>900: raise ValueError(_t('附件说明过长'))
            elif kind=='photos':
                items=action.get('items')
                if not isinstance(items,list) or not 1<=len(items)<=20: raise ValueError(_t('照片限 1–20 张'))
                for item in items:
                    file(item['path'])
                    if not isinstance(item.get('width'),int) or not isinstance(item.get('height'),int) or min(item['width'],item['height'])<=0: raise ValueError(_t('照片尺寸无效'))
                    if item['width']+item['height']>10000 or max(item['width'],item['height'])/min(item['width'],item['height'])>20 or Path(item['path']).stat().st_size>10*1024*1024:
                        raise ValueError(_t('照片超出 Telegram 照片限制，未改为文件发送'))
                    if len(item.get('caption',''))>900: raise ValueError(_t('照片说明过长'))
            else: raise ValueError(_t('不支持的插件动作：')+str(kind))

    def _buttons(self, plugin, rows, source, panel):
        buttons, tokens = [], []
        with self.db() as db:
            for row in rows:
                current=[]
                for item in row:
                    token=secrets.token_hex(16); tokens.append(token)
                    db.execute('INSERT INTO callbacks VALUES(?,?,?,?,?,?,?,?,0)',
                        (token,plugin.id,plugin.revision,self.bot.chat_id,source,panel,json.dumps(item['action']),time.time()+1800))
                    current.append({'text':item['text'],'callback_data':'plug|'+token})
                buttons.append(current)
        return {'inline_keyboard':buttons}, tokens

    def deliver(self, plugin, value, source, panel):
        for action in value['actions']:
            kind=action['type']
            if kind=='message':
                markup,tokens=self._buttons(plugin,action.get('buttons',[]),source,panel)
                if panel:
                    self.bot.edit_message(panel,action['text'],markup); sent={'message_id':panel}; panel=None
                else: sent=self.bot.send(action['text'],markup,reply_to=source)
                if tokens:
                    if not isinstance(sent,dict) or not sent.get('message_id'): raise ValueError(_t('Telegram 未返回消息 ID'))
                    with self.db() as db:
                        db.executemany('UPDATE callbacks SET panel=? WHERE token=?',[(sent['message_id'],t) for t in tokens])
            elif kind=='document':
                fields={'chat_id':self.bot.chat_id,'caption':action.get('caption',''),'disable_notification':self.bot.silent}
                if source: fields['reply_parameters']={'message_id':source,'allow_sending_without_reply':True}
                notify.telegram_multipart_call(self.bot.token,'sendDocument',fields,file_field='document',
                    filename=action['filename'],content=Path(action['path']).read_bytes(),content_type='application/octet-stream')
            else:
                pictures=[dict(item,content=Path(item['path']).read_bytes()) for item in action['items']]
                notify.send_screenshot_album(self.bot.token,self.bot.chat_id,pictures,self.bot.silent,source)

    def _run(self, plugin, request, source, panel):
        self.bot._ui_local.source_message_id=source
        self.bot._ui_local.message_id=None
        try:
            if not self.bot._reaction('🤔'): self.bot.send(_t('⏳ 正在执行插件：')+plugin.id,reply_to=source)
            self.state_root.mkdir(parents=True,exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='job-',dir=self.state_root) as directory:
                value=self.execute(plugin,request,Path(directory))
                self.deliver(plugin,value,source,panel)
                self.bot._reaction('👌' if value.get('ok',True) else '👎')
        except Exception as exc:
            self.bot._reaction('👎')
            try:
                self.bot.send(_t('❌ 插件 ')+plugin.id+'：'+str(exc).replace(self.bot.token,'[redacted]')[:500]+
                    _t('\n\n未自动重试；若发送结果不明确，请先检查聊天，避免重复发送。'),reply_to=source)
            except Exception: pass
        finally:
            self.bot._ui_local.source_message_id=None
            with self.lock:
                self.running.discard(plugin.id)
                self.next_run[plugin.id]=time.monotonic()+plugin.manifest.get('cooldown_seconds',0)
                self.jobs.discard(threading.current_thread())
