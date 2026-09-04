from __future__ import annotations

import json
import time

import pytest
from conftest import wait_until

from logipair.actor import TransportActor
from logipair.constants import FEATURE_CHANGE_HOST, FEATURE_REPROG_CONTROLS_V4, KEY_FLAG_ANALYTICS
from logipair.controller import PairController
from logipair.errors import TransportError
from logipair.model import DeviceRole, DeviceRuntime, DeviceState, HidPathInfo, HostChange, PairHealth
from logipair.service import LogiPairService
from logipair.storage import DeviceCache


def notification(feature: int, payload: bytes, function: int = 0) -> bytes:
    return bytes((0x11, 0xFF, feature, function << 4)) + payload.ljust(16, b"\0")


def start_pair(fake_backend, tmp_path, *, debounce=0.4):
    kb_path = fake_backend.add(b"kb", name="MX Keys Wireless Keyboard", device_type=0)
    mouse_path = fake_backend.add(b"mouse", name="MX Anywhere 3S", device_type=3)
    controller = PairController(lambda *_: None, debounce_seconds=debounce)
    cache = DeviceCache(tmp_path / "cache.json")
    keyboard = TransportActor(
        kb_path.pid, [kb_path], fake_backend, controller, cache, read_timeout_ms=1, request_timeout_ms=80
    )
    mouse = TransportActor(
        mouse_path.pid, [mouse_path], fake_backend, controller, cache, read_timeout_ms=1, request_timeout_ms=80
    )
    controller.start()
    keyboard.start()
    mouse.start()
    wait_until(lambda: controller.health == PairHealth.PAIR_READY)
    return controller, keyboard, mouse


def stop_pair(controller, *actors):
    for actor in actors:
        actor.stop()
    for actor in actors:
        actor.join(timeout=3)
    controller.stop()
    controller.join(timeout=3)


def test_keyboard_switch_writes_only_to_mouse(fake_backend, tmp_path):
    controller, keyboard, mouse = start_pair(fake_backend, tmp_path)
    fake_backend.inject(b"kb", notification(2, b"\x00\x02"))
    wait_until(lambda: len(fake_backend.change_writes(b"mouse")) == 1)
    stop_pair(controller, keyboard, mouse)
    assert fake_backend.change_writes(b"kb") == []
    assert fake_backend.change_writes(b"mouse")[0][1][4] == 2
    assert fake_backend.violations == []


def test_mouse_notification_enables_reverse_path(fake_backend, tmp_path):
    controller, keyboard, mouse = start_pair(fake_backend, tmp_path)
    fake_backend.inject(b"mouse", notification(2, b"\x02\x01"))
    wait_until(lambda: len(fake_backend.change_writes(b"kb")) == 1)
    stop_pair(controller, keyboard, mouse)
    mouse_runtime = next(value for value in controller.devices() if value.role == DeviceRole.MOUSE)
    assert mouse_runtime.reverse_notifications_observed
    assert fake_backend.change_writes(b"mouse") == []


def test_source_disconnect_does_not_cancel_peer_write(fake_backend, tmp_path):
    controller, keyboard, mouse = start_pair(fake_backend, tmp_path)
    fake_backend.inject(b"kb", notification(2, b"\x00\x02"))
    fake_backend.inject(b"kb", TransportError("source departed"))
    wait_until(lambda: len(fake_backend.change_writes(b"mouse")) == 1)
    stop_pair(controller, keyboard, mouse)
    assert len(fake_backend.change_writes(b"mouse")) == 1


def test_peer_write_failure_recovers_without_crash(fake_backend, tmp_path):
    controller, keyboard, mouse = start_pair(fake_backend, tmp_path)
    fake_backend.fail_change_for.add(b"mouse")
    fake_backend.inject(b"kb", notification(2, b"\x00\x01"))
    wait_until(lambda: len(fake_backend.change_writes(b"mouse")) >= 1)
    wait_until(lambda: len([item for item in fake_backend.opens if item[0] == b"mouse"]) >= 2)
    wait_until(lambda: len(fake_backend.change_writes(b"mouse")) == 2)
    wait_until(lambda: controller.health == PairHealth.PAIR_READY)
    stop_pair(controller, keyboard, mouse)
    assert fake_backend.violations == []
    assert len([item for item in fake_backend.opens if item[0] == b"mouse"]) >= 2
    assert len(fake_backend.change_writes(b"mouse")) == 2


def test_change_host_protocol_error_invalidates_and_recovers(fake_backend, tmp_path):
    controller, keyboard, mouse = start_pair(fake_backend, tmp_path)
    error = bytes((0x11, 0xFF, 0xFF, 0x0F, 0, 2)) + bytes(14)
    fake_backend.inject(b"mouse", error)
    wait_until(lambda: len([item for item in fake_backend.opens if item[0] == b"mouse"]) >= 2)
    wait_until(lambda: controller.health == PairHealth.PAIR_READY)
    stop_pair(controller, keyboard, mouse)
    assert fake_backend.violations == []


def test_device_never_ready_before_all_arm_acks(fake_backend, tmp_path):
    path = fake_backend.add(b"kb", name="MX Keys", device_type=0)
    fake_backend.withhold_arm_ack.add(b"kb")
    controller = PairController(lambda *_: None)
    actor = TransportActor(
        path.pid,
        [path],
        fake_backend,
        controller,
        DeviceCache(tmp_path / "cache.json"),
        read_timeout_ms=1,
        request_timeout_ms=30,
    )
    controller.start()
    actor.start()
    wait_until(lambda: controller.devices() and controller.devices()[0].state == DeviceState.RECOVERING)
    time.sleep(0.1)
    assert controller.devices()[0].state != DeviceState.READY
    actor.stop()
    actor.join()
    controller.stop()
    controller.join()


def test_external_flag_removal_is_throttled_and_rearmed(fake_backend, tmp_path):
    controller, keyboard, mouse = start_pair(fake_backend, tmp_path)
    baseline = len(fake_backend.arm_writes(b"kb"))
    removed = bytes((0x00, 0xD1, 0x02, 0, 0, 0))
    fake_backend.inject(b"kb", notification(3, removed, function=3))
    wait_until(lambda: len(fake_backend.arm_writes(b"kb")) == baseline + 3)
    fake_backend.inject(b"kb", notification(3, removed, function=3))
    time.sleep(0.05)
    stop_pair(controller, keyboard, mouse)
    assert len(fake_backend.arm_writes(b"kb")) == baseline + 3


def test_invalid_cached_feature_index_is_rebuilt(fake_backend, tmp_path):
    path = fake_backend.add(b"kb", name="MX Keys", device_type=0)
    identity = f"bluetooth:{path.pid:04x}:kb"
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(
        json.dumps(
            {
                "version": 1,
                "devices": {
                    identity: {
                        "wpid": path.pid,
                        "role": "keyboard",
                        "name": "MX Keys",
                        "features": {str(FEATURE_CHANGE_HOST): 99, str(FEATURE_REPROG_CONTROLS_V4): 3},
                        "easy_switch_cids": [0xD1, 0xD2, 0xD3],
                        "supported_flags": KEY_FLAG_ANALYTICS,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    controller = PairController(lambda *_: None)
    actor = TransportActor(
        path.pid, [path], fake_backend, controller, DeviceCache(cache_path), read_timeout_ms=1, request_timeout_ms=80
    )
    controller.start()
    actor.start()
    wait_until(lambda: controller.devices() and controller.devices()[0].state == DeviceState.READY)
    actor.stop()
    actor.join()
    controller.stop()
    controller.join()
    rebuilt = json.loads(cache_path.read_text(encoding="utf-8"))
    assert rebuilt["devices"][identity]["features"][str(FEATURE_CHANGE_HOST)] == 2


def test_receiver_collections_have_one_owner_and_reach_ready(fake_backend, tmp_path):
    pid = 0xC52F
    short = HidPathInfo(b"receiver-short", 0x046D, pid, 0xFF00, 1, 1, "receiver")
    long = HidPathInfo(b"receiver-long", 0x046D, pid, 0xFF00, 2, 1, "receiver")
    fake_backend.specs[b"receiver-short"] = {"name": "receiver", "type": 0, "pid": pid}
    fake_backend.specs[b"receiver-long"] = {"name": "MX Keys", "type": 0, "pid": pid}
    controller = PairController(lambda *_: None)
    actor = TransportActor(
        pid,
        [short, long],
        fake_backend,
        controller,
        DeviceCache(tmp_path / "cache.json"),
        read_timeout_ms=1,
        request_timeout_ms=80,
    )
    controller.start()
    actor.start()
    wait_until(lambda: len(fake_backend.opens) == 2)
    wpid = 0x1234
    connection = bytes((0x10, 1, 0x41, 0, 1, wpid & 0xFF, wpid >> 8))
    fake_backend.inject(b"receiver-short", connection)
    wait_until(lambda: controller.devices() and controller.devices()[0].state == DeviceState.READY)
    actor.stop()
    actor.join()
    controller.stop()
    controller.join()
    owner_threads = {owner for _, owner in fake_backend.opens}
    assert len(owner_threads) == 1
    assert {owner for _, owner in fake_backend.closes} == owner_threads
    assert fake_backend.violations == []


def test_mx_keys_mini_is_not_selected_for_this_personal_pair():
    mini = DeviceRuntime("mini", 1, 1, 0xFF, "Bluetooth", DeviceRole.KEYBOARD, "MX Keys Mini")
    keys = DeviceRuntime("keys", 2, 2, 0xFF, "Bluetooth", DeviceRole.KEYBOARD, "MX Keys")
    assert not TransportActor._is_target_device(mini)
    assert TransportActor._is_target_device(keys)


def test_cached_peer_switches_during_noncritical_initialization(fake_backend, tmp_path):
    kb_path = fake_backend.add(b"kb", name="MX Keys Wireless Keyboard", device_type=0)
    mouse_path = fake_backend.add(b"mouse", name="MX Anywhere 3S", device_type=3)
    mouse_identity = f"bluetooth:{mouse_path.pid:04x}:mouse"
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(
        json.dumps(
            {
                "version": 1,
                "devices": {
                    mouse_identity: {
                        "wpid": mouse_path.pid,
                        "role": "mouse",
                        "name": "MX Anywhere 3S",
                        "features": {str(FEATURE_CHANGE_HOST): 2},
                        "easy_switch_cids": [],
                        "supported_flags": 0,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    controller = PairController(lambda *_: None, debounce_seconds=0)
    cache = DeviceCache(cache_path)
    keyboard = TransportActor(
        kb_path.pid, [kb_path], fake_backend, controller, cache, read_timeout_ms=1, request_timeout_ms=80
    )
    mouse = TransportActor(
        mouse_path.pid, [mouse_path], fake_backend, controller, cache, read_timeout_ms=1, request_timeout_ms=400
    )
    controller.start()
    keyboard.start()
    wait_until(
        lambda: any(
            device.role == DeviceRole.KEYBOARD and device.state == DeviceState.READY
            for device in controller.devices()
        )
    )
    fake_backend.withhold_responses_for.add((b"mouse", 0, 0))
    mouse.start()
    wait_until(
        lambda: any(
            device.identity == mouse_identity
            and device.switch_capable
            and device.state == DeviceState.INITIALIZING
            for device in controller.devices()
        )
    )
    source = next(device for device in controller.devices() if device.role == DeviceRole.KEYBOARD)
    started = time.monotonic()
    controller.submit(HostChange(source.identity, source.role, 2, started))
    wait_until(lambda: len(fake_backend.change_writes(b"mouse")) == 1, timeout=0.2)
    elapsed = time.monotonic() - started
    assert elapsed < 0.2
    assert fake_backend.change_writes(b"kb") == []
    stop_pair(controller, keyboard, mouse)


def test_service_waits_for_slow_actor_close_before_hid_exit(fake_backend, tmp_path):
    path = fake_backend.add(b"mouse", name="MX Anywhere 3S", device_type=3)
    service = LogiPairService(tmp_path / "cache.json", tmp_path / "status.json", backend=fake_backend)
    actor = TransportActor(
        path.pid,
        [path],
        fake_backend,
        service._controller,
        service._cache,
        read_timeout_ms=1,
        request_timeout_ms=80,
    )
    service._actors["mouse"] = actor
    service._controller.start()
    actor.start()
    wait_until(
        lambda: service._controller.devices()
        and service._controller.devices()[0].state == DeviceState.READY
    )
    fake_backend.withhold_responses_for.add((b"mouse", 0, 0))
    fake_backend.slow_read_seconds[b"mouse"] = 0.12
    actor.recover("exercise shutdown during request")
    wait_until(fake_backend.slow_read_started.is_set)
    assert service._shutdown_components(actor_timeout_seconds=1)
    assert not actor.is_alive()
    assert fake_backend.shutdown_count == 1
    assert fake_backend.native_events[-1][0] == "shutdown"
    assert any(event[0] == "close" for event in fake_backend.native_events[:-1])
    assert {owner for _, owner in fake_backend.closes} == {owner for _, owner in fake_backend.opens}
    assert fake_backend.violations == []
    assert service._shutdown_components(actor_timeout_seconds=1)
    assert fake_backend.shutdown_count == 1


def test_queued_and_new_switch_futures_resolve_during_stop(fake_backend, tmp_path):
    path = fake_backend.add(b"mouse", name="MX Anywhere 3S", device_type=3)
    controller = PairController(lambda *_: None)
    actor = TransportActor(path.pid, [path], fake_backend, controller, DeviceCache(tmp_path / "cache.json"))
    queued = actor.switch("missing", 1, time.monotonic())
    actor.stop()
    rejected = actor.switch("missing", 1, time.monotonic())
    actor.start()
    actor.join(timeout=1)
    assert queued.result(timeout=0.1).detail == "actor stopped"
    assert rejected.result(timeout=0.1).detail == "actor stopping or queue full"


def test_service_refuses_hid_exit_while_actor_is_alive(fake_backend, tmp_path):
    class StuckActor:
        name = "stuck-actor"

        def stop(self):
            pass

        def join(self, timeout):
            pass

        def is_alive(self):
            return True

    service = LogiPairService(tmp_path / "cache.json", tmp_path / "status.json", backend=fake_backend)
    service._actors["stuck"] = StuckActor()
    assert not service._shutdown_components(actor_timeout_seconds=0.01)
    assert fake_backend.shutdown_count == 0


@pytest.mark.no_cover
def test_two_thousand_switches_do_not_leak_threads_or_queue(fake_backend, tmp_path):
    controller, keyboard, mouse = start_pair(fake_backend, tmp_path, debounce=0)
    source = next(value for value in controller.devices() if value.role == DeviceRole.KEYBOARD)
    started = time.monotonic()
    for index in range(2000):
        controller.submit(HostChange(source.identity, DeviceRole.KEYBOARD, index % 3, started + index / 100000))
    wait_until(lambda: len(fake_backend.change_writes(b"mouse")) == 2000, timeout=30)
    wait_until(lambda: controller._queue.empty())
    stop_pair(controller, keyboard, mouse)
    assert not controller.is_alive() and not keyboard.is_alive() and not mouse.is_alive()
    assert fake_backend.violations == []
