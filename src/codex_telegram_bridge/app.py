"""Compatibility import; implementation lives in codex_telegram_bridge.backends.desktop.bot."""
import importlib as _importlib
import sys as _sys

_implementation = _importlib.import_module('codex_telegram_bridge.backends.desktop.bot')
if __name__ == "__main__":
    raise SystemExit(_implementation.main())
_sys.modules[__name__] = _implementation
