from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from .constants import CACHE_SCHEMA_VERSION, STATUS_SCHEMA_VERSION
from .model import DeviceRole, DeviceRuntime, PairHealth

log = logging.getLogger(__name__)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class DeviceCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._entries: dict[str, dict[str, Any]] = {}
        self.load()

    def load(self) -> None:
        with self._lock:
            if not self.path.exists():
                return
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if data.get("version") != CACHE_SCHEMA_VERSION or not isinstance(data.get("devices"), dict):
                    raise ValueError("unsupported or malformed cache schema")
                self._entries = data["devices"]
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                log.warning("Cache invalidated: %s", error)
                self._entries = {}

    def get(self, identity: str, wpid: int) -> dict[str, Any] | None:
        with self._lock:
            value = self._entries.get(identity)
            if not isinstance(value, dict) or value.get("wpid") != wpid:
                return None
            return dict(value)

    def save(self, device: DeviceRuntime) -> None:
        snapshot = device.snapshot()
        entry = {
            "wpid": device.wpid,
            "role": device.role.value if device.role else None,
            "name": device.name,
            "features": {str(k): v for k, v in device.feature_indexes.items()},
            "easy_switch_cids": list(device.easy_switch_cids),
            "supported_flags": device.supported_flags,
        }
        with self._lock:
            self._entries[device.identity] = entry
            atomic_write_json(self.path, {"version": CACHE_SCHEMA_VERSION, "devices": self._entries})
        log.debug("Cached READY device %s (%s)", snapshot.get("name"), device.identity)

    def invalidate(self, identity: str, reason: str) -> None:
        with self._lock:
            if self._entries.pop(identity, None) is not None:
                atomic_write_json(self.path, {"version": CACHE_SCHEMA_VERSION, "devices": self._entries})
        log.warning("Cache invalidated for %s: %s", identity, reason)


class StatusStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    def write(self, health: PairHealth, devices: list[DeviceRuntime], *, hidapi_version: str) -> None:
        roles: dict[str, dict[str, Any] | None] = {"keyboard": None, "mouse": None}
        for device in devices:
            if device.role is not None:
                roles[device.role.value] = device.snapshot()
        payload = {
            "version": STATUS_SCHEMA_VERSION,
            "pair": health.value,
            "hidapi": hidapi_version,
            "keyboard": roles[DeviceRole.KEYBOARD.value],
            "mouse": roles[DeviceRole.MOUSE.value],
        }
        with self._lock:
            atomic_write_json(self.path, payload)

    def read(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if value.get("version") == STATUS_SCHEMA_VERSION else None
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None
