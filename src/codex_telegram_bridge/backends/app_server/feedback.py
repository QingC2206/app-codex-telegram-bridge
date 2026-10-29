"""Compatibility alias; one implementation lives in telegram.feedback."""
import importlib
import sys
sys.modules[__name__] = importlib.import_module("codex_telegram_bridge.telegram.feedback")
