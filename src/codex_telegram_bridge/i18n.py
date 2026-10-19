"""Translate application-owned templates, never messages supplied by users/models."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
import json
import os
from pathlib import Path
import tempfile

from .paths import data_dir, resources_dir

_override = ContextVar('bridge_language', default=None)
LANGUAGES = ('zh-CN', 'en')


def normalize_language(value):
    return {'zh': 'zh-CN', 'zh-cn': 'zh-CN', '中文': 'zh-CN',
            'en': 'en', 'english': 'en'}.get(str(value).strip().lower())


@lru_cache(maxsize=16)
def _read_preference(path, modified, size):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
        return normalize_language(value.get('language')) or 'zh-CN'
    except (OSError, ValueError, AttributeError):
        return 'zh-CN'


def get_language():
    override = _override.get()
    if override is not None:
        return override
    try:
        path = data_dir() / 'telegram_language.json'
        stat = path.stat()
        return _read_preference(str(path), stat.st_mtime_ns, stat.st_size)
    except (OSError, RuntimeError):
        return 'zh-CN'


def set_language(value):
    language = normalize_language(value)
    if language is None:
        raise ValueError('Supported languages: zh-CN, en')
    path = data_dir() / 'telegram_language.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.language-', delete=False) as handle:
            temporary = Path(handle.name)
            json.dump({'language': language}, handle)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    _read_preference.cache_clear()
    return language


@contextmanager
def using_language(value):
    language = normalize_language(value)
    if language is None:
        raise ValueError('Supported languages: zh-CN, en')
    token = _override.set(language)
    try:
        yield
    finally:
        _override.reset(token)


@lru_cache(maxsize=1)
def english_catalog():
    return json.loads((resources_dir() / 'messages.en.json').read_text(encoding='utf-8'))


def tr(template, **values):
    """Only call with a literal UI template; interpolated values stay verbatim."""
    translated = english_catalog().get(template, template) if get_language() == 'en' else template
    return translated.format(**values) if values else translated
