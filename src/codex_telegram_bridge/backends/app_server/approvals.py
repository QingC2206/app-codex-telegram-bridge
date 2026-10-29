"""Compatibility alias; one implementation lives in features.request_policy."""
import importlib
import sys
sys.modules[__name__] = importlib.import_module("codex_telegram_bridge.features.request_policy")
