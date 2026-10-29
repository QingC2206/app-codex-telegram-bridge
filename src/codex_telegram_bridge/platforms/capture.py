"""Screenshot dispatch; platform permissions remain enforced by the OS."""
import sys

if sys.platform == 'win32':
    from .windows.capture import codex_windows, monitors, capture, failure_hint
elif sys.platform == 'darwin':
    from .macos.capture import codex_windows, monitors, capture, failure_hint
else:
    from .linux.capture import codex_windows, monitors, capture, failure_hint
