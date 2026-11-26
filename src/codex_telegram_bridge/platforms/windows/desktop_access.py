"""Windows Desktop access and its registered package's background queue."""
from pathlib import Path

from ..desktop_access import NativeDesktopAccess
from ...codex.errors import DesktopBridgeSafetyError
from ...i18n import tr as _t


class WindowsDesktopAccess(NativeDesktopAccess):
    """No macOS local-record fallback is enabled for the Windows backend."""

    supports_background_queue = True

    def queue_runtime(self, endpoint):
        import psutil
        from .discovery import _process_environment
        from .runtime import _packaged_runtime_root, _registered_package_path

        try:
            root = Path(psutil.Process(endpoint.pid).exe()).resolve()
            executable = root.parent / 'resources' / 'codex.exe'
            package = _packaged_runtime_root(executable)
            if (root.name.lower() != 'chatgpt.exe' or not executable.is_file()
                    or not package or _registered_package_path(package.name) != package):
                raise ValueError('unverified Desktop package')
            environment = _process_environment(endpoint.pid)
            sqlite_home = environment.get('CODEX_SQLITE_HOME')
            profile = endpoint.profile.resolve()
            if sqlite_home and (not Path(sqlite_home).is_absolute() or Path(sqlite_home).resolve() != profile):
                raise ValueError('separate queue storage')
            return executable, {'CODEX_HOME': str(profile), 'CODEX_SQLITE_HOME': str(profile)}
        except (psutil.Error, OSError, ValueError, AttributeError) as exc:
            raise DesktopBridgeSafetyError(_t('无法核实 Desktop 实例的后台队列位置，未提交消息。')) from exc
