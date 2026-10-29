"""Compatibility alias; one implementation lives in features.runtime_options."""
import importlib
import sys
sys.modules[__name__] = importlib.import_module("codex_telegram_bridge.features.runtime_options")
