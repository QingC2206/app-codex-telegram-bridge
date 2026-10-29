"""Compatibility alias; one implementation lives in backends.app_server.runtime."""
import importlib
import sys
sys.modules[__name__] = importlib.import_module("codex_telegram_bridge.backends.app_server.runtime")
