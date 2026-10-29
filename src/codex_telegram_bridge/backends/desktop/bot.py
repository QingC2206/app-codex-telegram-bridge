"""Compatibility alias for the shared Telegram controller."""
import importlib
import sys
implementation = importlib.import_module("codex_telegram_bridge.core.bot")
if __name__ == "__main__":
    raise SystemExit(implementation.main())
sys.modules[__name__] = implementation
