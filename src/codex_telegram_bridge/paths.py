"""Profile-scoped runtime data; never write beside installed Python modules."""
import os
from pathlib import Path


def codex_home() -> Path:
    return Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').expanduser().resolve()


def data_dir() -> Path:
    explicit = os.environ.get('CODEX_TELEGRAM_DATA_DIR')
    if explicit:
        return Path(explicit).expanduser().resolve()
    profile = codex_home()
    # Reuse a legacy installation in place: credentials and all reply routes
    # remain valid. Ambiguity requires an explicit choice, never newest-wins.
    candidates = [p for p in (profile/'telegram-bridge', profile/'hooks',
                              profile/'hooks/codex-telegram-bot')
                  if (p/'telegram_notify.credentials.json').is_file()]
    if len(candidates) > 1:
        raise RuntimeError('Multiple Robot data directories; set CODEX_TELEGRAM_DATA_DIR explicitly.')
    return candidates[0] if candidates else profile/'telegram-bridge'


def ensure_data_dir() -> Path:
    path = data_dir()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def resources_dir() -> Path:
    return Path(__file__).parent/'resources'
