from __future__ import annotations
import json,os,threading,logging
from pathlib import Path
from typing import Any,Callable
from ..paths import data_dir
from .quota_schema import ROOT, migrated, legacy_quota_key, validate

def default_state() -> dict[str, Any]:
    return {
        "version": 1,
        "update_offset": 0,
        "muted": False,
        "quiet_mode": False,
        "quiet_generation": 0,
        "default_cwd": str(Path.home()),
        "managed_threads": {},
        "task_aliases": [],
        "quota_weekly_observation": {},
        "quota_reset_credits_observation": {},
    }


class StateStore:
    def __init__(self, path: Path | None = None):
        self.path = path or data_dir()/"telegram_bot.state.json"
        self.lock = threading.RLock()
        self.data = default_state()
        self.reload()

    def reload(self) -> dict[str, Any]:
        with self.lock:
            if self.path.exists():
                try:
                    loaded = json.loads(self.path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        self.data.update(loaded)
                except Exception as exc:
                    logging.getLogger(__name__).error("State load failed: %s",type(exc).__name__)
            if not isinstance(self.data.get("managed_threads"), dict):
                self.data["managed_threads"] = {}
            if not isinstance(self.data.get("task_aliases"), list):
                self.data["task_aliases"] = []
            if migrated(self.data):
                for key in list(self.data):
                    if legacy_quota_key(key):
                        if self.data[key]:
                            raise ValueError('Unassigned legacy quota data conflicts with migrated account state; preserve and review it.')
                        del self.data[key]
                validate(self.data)
            else:
                if not isinstance(self.data.get("quota_weekly_observation"), dict):
                    self.data["quota_weekly_observation"] = {}
                if not isinstance(self.data.get("quota_reset_credits_observation"), dict):
                    self.data["quota_reset_credits_observation"] = {}
            if not isinstance(self.data.get("quota_monitor_health"), dict):
                self.data["quota_monitor_health"] = {}
            return dict(self.data)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return json.loads(json.dumps(self.data, ensure_ascii=False))

    def update(self, **values: Any) -> None:
        with self.lock:
            if migrated(self.data) and any(legacy_quota_key(key) or key == ROOT for key in values):
                raise ValueError('Use the account-scoped quota accessor to update quota state.')
            self.mutate(lambda state: state.update(values))

    def mutate(self, callback: Callable[[dict[str, Any]], None]) -> None:
        with self.lock:
            candidate = json.loads(json.dumps(self.data, ensure_ascii=False))
            callback(candidate)
            if migrated(self.data) and not migrated(candidate):
                raise ValueError('Cannot remove account-scoped quota history.')
            validate(candidate)
            previous = self.data
            self.data = candidate
            try:
                self._save_locked()
            except BaseException:
                self.data = previous
                raise

    def _save_locked(self) -> None:
        temporary = self.path.with_name(f"{self.path.name}.tmp.{os.getpid()}")
        temporary.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)
