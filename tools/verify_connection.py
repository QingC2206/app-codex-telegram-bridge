"""Explicit real communication acceptance. See --help before submitting."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from codex_telegram_bridge.management.connection_check import main

if __name__ == '__main__':
    raise SystemExit(main())
