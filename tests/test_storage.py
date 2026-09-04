from __future__ import annotations

import json

from logipair.model import DeviceRole, DeviceRuntime, DeviceState, PairHealth
from logipair.storage import DeviceCache, StatusStore


def test_cache_round_trip_and_invalidation(tmp_path):
    path = tmp_path / "nested" / "cache.json"
    device = DeviceRuntime("keys", 1, 2, 0xFF, "Bluetooth", DeviceRole.KEYBOARD, "MX Keys")
    device.feature_indexes = {0x1814: 5}
    device.easy_switch_cids = (0xD1, 0xD2, 0xD3)
    device.supported_flags = 4
    cache = DeviceCache(path)
    cache.save(device)
    assert DeviceCache(path).get("keys", 2)["features"] == {"6164": 5}
    cache.invalidate("keys", "test")
    assert cache.get("keys", 2) is None


def test_malformed_cache_is_discarded(tmp_path):
    path = tmp_path / "cache.json"
    path.write_text("not-json", encoding="utf-8")
    assert DeviceCache(path).get("anything", 1) is None


def test_status_store_is_atomic_and_readable(tmp_path):
    path = tmp_path / "status.json"
    device = DeviceRuntime("mouse", 1, 2, 0xFF, "Bluetooth", DeviceRole.MOUSE, "MX Anywhere 3S")
    device.transition(DeviceState.READY)
    store = StatusStore(path)
    store.write(PairHealth.DEGRADED, [device], hidapi_version="0.15.0")
    value = store.read()
    assert value["mouse"]["name"] == "MX Anywhere 3S"
    assert value["pair"] == "DEGRADED"
    assert not path.with_suffix(".json.tmp").exists()


def test_unknown_status_schema_is_rejected(tmp_path):
    path = tmp_path / "status.json"
    path.write_text(json.dumps({"version": 999}), encoding="utf-8")
    assert StatusStore(path).read() is None
