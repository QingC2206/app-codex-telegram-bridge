"""Windows retains its existing live task/turn and native navigation checks."""
from ..desktop_access import NativeDesktopAccess


class WindowsDesktopAccess(NativeDesktopAccess):
    """No macOS local-record fallback is enabled for the Windows backend."""
