"""Compatibility import; implementation lives in codex_telegram_bridge.backends.app_server.approvals."""
import importlib as _importlib
import sys as _sys

_implementation = _importlib.import_module('codex_telegram_bridge.backends.app_server.approvals')
_sys.modules[__name__] = _implementation
