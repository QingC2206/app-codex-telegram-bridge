"""Credential storage with legacy Windows DPAPI compatibility."""
import hashlib
import os
import sys
from pathlib import Path


def _keyring():
    # Explicit OS backends prevent a third-party plaintext/null fallback.
    import sys
    if sys.platform == 'darwin':
        from keyring.backends.macOS import Keyring
    elif sys.platform.startswith('linux'):
        from keyring.backends.SecretService import Keyring
    else:
        raise RuntimeError('Unsupported secure credential backend')
    backend = Keyring()
    if backend.priority <= 0:
        raise RuntimeError('System keyring is unavailable; unlock the desktop keyring and retry.')
    return backend


def store_token(token: str, path: Path) -> dict:
    if sys.platform == 'win32':
        from .platforms.windows.credentials import protect_secret
        return {'token_dpapi': protect_secret(token)}
    account = hashlib.sha256(str(path.resolve()).encode()).hexdigest()
    _keyring().set_password('app-codex-telegram-bridge', account, token)
    return {'token_keyring': account}


def load_token(config: dict) -> str:
    if 'token_dpapi' in config:
        if sys.platform != 'win32':
            raise RuntimeError('Windows DPAPI credentials cannot be moved to this OS. Run setup here.')
        from .platforms.windows.credentials import unprotect_secret
        return unprotect_secret(str(config['token_dpapi']))
    account = config.get('token_keyring')
    if not isinstance(account, str) or len(account) != 64:
        raise ValueError('Missing system-keyring credential reference. Run setup.')
    token = _keyring().get_password('app-codex-telegram-bridge', account)
    if not token:
        raise RuntimeError('Bot token is missing from the system keyring. Run setup.')
    return token
