"""Profile-scoped runtime data; never write beside installed Python modules."""
import os
from pathlib import Path
import re


def resolved_rollout(value) -> Path:
    path = Path(value).resolve()
    # Windows Desktop records extended drive paths in its SQLite metadata.
    # Compare them in the profile's spelling only if they identify the same file.
    if os.name == 'nt' and re.match(r'^\\\\\?\\[A-Za-z]:\\', str(path)):
        ordinary = Path(str(path)[4:]).resolve()
        if ordinary.is_file() and ordinary.samefile(path):
            return ordinary
    return path


def codex_home() -> Path:
    return Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').expanduser().resolve()


def auth_stamp(profile):
    """Identify a change of login file without opening it."""
    try:
        stat = (profile / 'auth.json').stat()
        return stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns
    except OSError:
        return None


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
