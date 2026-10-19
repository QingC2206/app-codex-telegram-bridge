"""Local Telegram plugin API v1; standard library only. Not a security sandbox."""
from dataclasses import dataclass
from pathlib import Path
import json
import os


@dataclass(frozen=True)
class Context:
    plugin_dir: Path
    output_dir: Path
    state_dir: Path
    config: dict
    language: str = 'zh-CN'

    def tr(self, zh: str, en: str) -> str:
        """Choose plugin-owned UI text; do not translate user content."""
        return en if self.language == 'en' else zh

    def save_state(self, value):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        target = self.state_dir / 'state.json'
        temp = target.with_suffix('.tmp')
        temp.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        os.replace(temp, target)

    def load_state(self):
        path = self.state_dir / 'state.json'
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}


def button(text, action):
    return {'text': text, 'action': action}


def message(text, buttons=None):
    return {'type': 'message', 'text': text, 'buttons': buttons or []}


def document(path, filename=None, caption=''):
    return {'type': 'document', 'path': str(path), 'filename': filename or Path(path).name, 'caption': caption}


def photos(items):
    return {'type': 'photos', 'items': items}


def result(*actions, ok=True):
    return {'actions': list(actions), 'ok': ok}
