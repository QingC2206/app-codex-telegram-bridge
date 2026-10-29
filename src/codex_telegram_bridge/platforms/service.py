"""Shared service contract; adapters own operating-system commands only."""
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Callable


@dataclass(frozen=True)
class ServiceContext:
    profile: Path
    data: Path
    argv: list[str]
    label: str
    run: Callable
    stop_owned_processes: Callable
    validate_definition: Callable


def restore_definition(path, content):
    if content is None:
        path.unlink(missing_ok=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(0o600)


def service_adapter(platform=None):
    platform = sys.platform if platform is None else platform
    if platform == 'win32':
        from .windows import services
    elif platform == 'darwin':
        from .macos import services
    elif platform.startswith('linux'):
        from .linux import services
    else:
        raise RuntimeError('Unsupported operating system.')
    return services
