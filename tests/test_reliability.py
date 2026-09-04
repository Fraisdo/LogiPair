"""Rapid-switch reliability: expected departures, lifecycle storms and the hot read path."""

from __future__ import annotations

import collections
import json
import logging
import threading
import time
from types import SimpleNamespace

import pytest
from conftest import wait_until

from logipair import actor as actor_module
from logipair.actor import TransportActor
from logipair.constants import (
    BACKOFF_SECONDS,
    DIRECT_DEVICE_SLOT,
    FEATURE_CHANGE_HOST,
    FEATURE_REPROG_CONTROLS_V4,
    KEY_FLAG_ANALYTICS,
    RECEIVER_PIDS,
    REPORT_LONG,
    V1_IGNORED_PIDS,
)
from logipair.controller import PairController
from logipair.errors import TransportError
from logipair.hidapi_backend import HidApiBackend, OwnedHandle
from logipair.model import DeviceRole, DeviceState, HidPathInfo, PairHealth
from logipair.service import LifecycleCoalescer, LogiPairService
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
    return controller, keyboard, mouse, kb_path, mouse_path


def stop_pair(controller, *actors):
    for actor in actors:
        actor.stop()
    for actor in actors:
        actor.join(timeout=3)
    controller.stop()
    controller.join(timeout=3)


def messages(caplog):
    return [record.getMessage() for record in caplog.records]


# --------------------------------------------------------------------------
# BLOCKER 1 / 6 - an announced departure is not a failure
# --------------------------------------------------------------------------


def test_expected_departure_costs_no_backoff_and_no_warning(fake_backend, tmp_path, caplog):
    controller, keyboard, mouse, _, _ = start_pair(fake_backend, tmp_path)
    with caplog.at_level(logging.DEBUG, logger="logipair.actor"):
        fake_backend.inject(b"kb", notification(2, b"\x00\x02"))
        wait_until(lambda: len(fake_backend.change_writes(b"mouse")) == 1)
        # Both devices now physically leave this host; the reads fail as a result.
        fake_backend.inject(b"kb", TransportError("device left host"))
        fake_backend.inject(b"mouse", TransportError("device left host"))
        wait_until(lambda: len([item for item in messages(caplog) if "Expected disconnect" in item]) >= 2)
        emitted = messages(caplog)
        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.levelno >= logging.WARNING and record.name == "logipair.actor"
        ]
    stop_pair(controller, keyboard, mouse)

    assert any(item.startswith("Expected departure source=") for item in emitted)
    assert any(item.startswith("Expected departure peer=") for item in emitted)
    # The whole point: a normal switch produces no failure history and no WARN noise.
    assert keyboard._retry_index == 0
    assert mouse._retry_index == 0
    assert warnings == []


def test_expected_departure_marks_expected_disconnected_state(fake_backend, tmp_path):
    path = fake_backend.add(b"kb", name="MX Keys", device_type=0)
    controller = PairController(lambda *_: None)
    actor = TransportActor(
        path.pid,
        [path],
        fake_backend,
        controller,
        DeviceCache(tmp_path / "cache.json"),
        read_timeout_ms=1,
        request_timeout_ms=80,
    )
    controller.start()
    actor.start()
    wait_until(lambda: controller.devices() and controller.devices()[0].state == DeviceState.READY)
    device = controller.devices()[0]

    fake_backend.inject(b"kb", notification(2, b"\x00\x01"))
    wait_until(lambda: device.departure_grace_until > 0)
    # Remove the device for good so it cannot immediately reconnect.
    fake_backend.specs.pop(b"kb")
    actor.update_paths([], "device-removal")
    wait_until(lambda: device.state == DeviceState.EXPECTED_DISCONNECTED)

    assert actor._retry_index == 0
    assert device.last_error is None
    # A transient, announced departure must not read as a degradation.
    assert controller.health == PairHealth.RECOVERING
    stop_pair(controller, actor)


def test_departing_source_does_not_rearm_or_rediscover(fake_backend, tmp_path):
    controller, keyboard, mouse, _, _ = start_pair(fake_backend, tmp_path)
    keyboard_device = next(item for item in controller.devices() if item.role == DeviceRole.KEYBOARD)
    fake_backend.inject(b"kb", notification(2, b"\x00\x02"))
    wait_until(lambda: keyboard_device.departure_grace_until > 0)
    baseline_arms = len(fake_backend.arm_writes(b"kb"))
    baseline_writes = len([item for item in fake_backend.writes if item[0] == b"kb"])

    keyboard.ensure_ready("synthetic ensure during departure")
    time.sleep(0.25)

    assert len(fake_backend.arm_writes(b"kb")) == baseline_arms
    assert len([item for item in fake_backend.writes if item[0] == b"kb"]) == baseline_writes
    stop_pair(controller, keyboard, mouse)


def test_returning_device_clears_expected_departure(fake_backend, tmp_path):
    controller, keyboard, mouse, kb_path, _ = start_pair(fake_backend, tmp_path)
    keyboard_device = next(item for item in controller.devices() if item.role == DeviceRole.KEYBOARD)
    fake_backend.inject(b"kb", notification(2, b"\x00\x02"))
    wait_until(lambda: keyboard_device.departure_grace_until > 0)

    keyboard.device_arrived("device-arrival")
    wait_until(lambda: keyboard_device.departure_grace_until == 0)
    wait_until(lambda: keyboard_device.state == DeviceState.READY)
    stop_pair(controller, keyboard, mouse)


# --------------------------------------------------------------------------
# BLOCKER 2 - a real arrival wipes the backoff ladder
# --------------------------------------------------------------------------


def test_device_arrival_resets_a_thirty_second_backoff(fake_backend, tmp_path):
    path = fake_backend.add(b"kb", name="MX Keys", device_type=0)
    controller = PairController(lambda *_: None)
    actor = TransportActor(
        path.pid,
        [path],
        fake_backend,
        controller,
        DeviceCache(tmp_path / "cache.json"),
        read_timeout_ms=1,
        request_timeout_ms=80,
    )
    # Simulate a long absence: the ladder was climbed to its 30 s rung.
    actor._retry_index = len(BACKOFF_SECONDS)
    actor._retry_at = time.monotonic() + BACKOFF_SECONDS[-1]
    actor._ignored = True
    controller.start()
    actor.start()

    actor.device_arrived("device-arrival")
    wait_until(lambda: controller.devices() and controller.devices()[0].state == DeviceState.READY, timeout=3)

    assert actor._retry_index == 0
    assert not actor._ignored
    stop_pair(controller, actor)


def test_service_forwards_arrival_to_actors_present_in_enumeration(fake_backend, tmp_path):
    fake_backend.add(b"kb", name="MX Keys", device_type=0)
    service = LogiPairService(tmp_path / "cache.json", tmp_path / "status.json", backend=fake_backend)
    arrivals: list[str] = []

    class StubActor:
        def update_paths(self, paths, reason, *, force=False):
            pass

        def device_arrived(self, reason):
            arrivals.append(reason)

    key = next(iter(LogiPairService._group_paths(fake_backend.enumerate())))
    service._actors[key] = StubActor()
    service._paths[key] = []

    service._reconcile("poll", arrival=False)
    assert arrivals == []
    service._reconcile("device-arrival x3", arrival=True)
    assert arrivals == ["device-arrival x3"]


# --------------------------------------------------------------------------
# BLOCKER 3 - Windows lifecycle storms are coalesced
# --------------------------------------------------------------------------


def test_lifecycle_coalescer_collapses_a_burst():
    now = [100.0]
    coalescer = LifecycleCoalescer(window_seconds=0.15, max_delay_seconds=0.5, clock=lambda: now[0])
    assert coalescer.due_in() is None
    for _ in range(3):
        coalescer.record("device-removal")
        now[0] += 0.01
        coalescer.record("device-arrival")
        now[0] += 0.01

    assert coalescer.take() is None  # still inside the coalescing window
    now[0] += 0.15
    assert coalescer.take() == ("device-arrival x3,device-removal x3", True)
    assert coalescer.take() is None
    assert coalescer.due_in() is None


def test_lifecycle_coalescer_caps_the_total_delay():
    now = [0.0]
    coalescer = LifecycleCoalescer(window_seconds=0.15, max_delay_seconds=0.5, clock=lambda: now[0])
    for _ in range(6):
        coalescer.record("device-arrival")
        now[0] += 0.1

    # A never-ending storm still reconciles once the cap is reached.
    assert coalescer.take() == ("device-arrival x6", True)


def test_lifecycle_coalescer_reports_removal_only_batches():
    now = [0.0]
    coalescer = LifecycleCoalescer(window_seconds=0.05, max_delay_seconds=0.5, clock=lambda: now[0])
    coalescer.record("device-removal")
    now[0] += 0.05
    assert coalescer.take() == ("device-removal x1", False)


class _NoopWatcher:
    def start(self):
        pass

    def stop(self):
        pass

    def join(self, timeout=None):
        pass


def test_six_device_change_events_produce_one_reconcile(fake_backend, tmp_path, monkeypatch):
    service = LogiPairService(
        tmp_path / "cache.json", tmp_path / "status.json", backend=fake_backend, poll_seconds=30
    )
    monkeypatch.setattr(service, "_lifecycle", _NoopWatcher())
    reasons: list[str] = []
    monkeypatch.setattr(service, "_reconcile", lambda reason, **_: reasons.append(reason))

    thread = threading.Thread(target=service.run, daemon=True)
    thread.start()
    try:
        wait_until(lambda: reasons == ["startup"])
        for index in range(6):
            service._on_lifecycle("device-arrival" if index % 2 else "device-removal")
        wait_until(lambda: len(reasons) == 2, timeout=2)
        time.sleep(0.3)
    finally:
        service.stop()
        thread.join(timeout=5)

    assert reasons == ["startup", "device-arrival x3,device-removal x3"]


# --------------------------------------------------------------------------
# BLOCKER 4 - cosmetic HID path churn must not reset the transport
# --------------------------------------------------------------------------


def test_irrelevant_hid_path_change_keeps_the_transport(fake_backend, tmp_path):
    controller, keyboard, mouse, kb_path, _ = start_pair(fake_backend, tmp_path)
    opens_before = len([item for item in fake_backend.opens if item[0] == b"kb"])
    unrelated = HidPathInfo(b"kb-extra", 0x046D, kb_path.pid, 0xFF43, 0x0001, 2, "kb", "MX Keys")

    keyboard.update_paths([kb_path, unrelated], "device-arrival")
    time.sleep(0.25)

    assert len([item for item in fake_backend.opens if item[0] == b"kb"]) == opens_before
    assert controller.health == PairHealth.PAIR_READY
    assert keyboard._retry_index == 0
    stop_pair(controller, keyboard, mouse)


def test_losing_the_active_long_collection_does_reset_the_transport(fake_backend, tmp_path):
    controller, keyboard, mouse, kb_path, _ = start_pair(fake_backend, tmp_path)
    opens_before = len([item for item in fake_backend.opens if item[0] == b"kb"])
    replacement = HidPathInfo(b"kb2", 0x046D, kb_path.pid, 0xFF43, 0x0202, 2, "kb", "MX Keys")
    fake_backend.specs[b"kb2"] = dict(fake_backend.specs[b"kb"])

    keyboard.update_paths([replacement], "device-arrival")
    wait_until(lambda: len([item for item in fake_backend.opens if item[0] == b"kb2"]) == 1)

    assert len([item for item in fake_backend.opens if item[0] == b"kb"]) == opens_before
    # A forced reconnect for a real reason must still not inherit a backoff.
    assert keyboard._retry_index == 0
    stop_pair(controller, keyboard, mouse)


def test_path_selection_is_stable_across_enumeration_reordering(fake_backend, tmp_path):
    first = HidPathInfo(b"aaa", 0x046D, 0xB35B, 0xFF43, 0x0202, 2)
    second = HidPathInfo(b"bbb", 0x046D, 0xB35B, 0xFF43, 0x0202, 2)
    actor = TransportActor(
        0xB35B, [first, second], fake_backend, PairController(lambda *_: None), DeviceCache(tmp_path / "c.json")
    )
    assert actor._select_paths((first, second))[REPORT_LONG].path == b"aaa"
    # Reordered enumeration, same live handle: keep what is already open.
    assert actor._select_paths((second, first), {REPORT_LONG: second})[REPORT_LONG].path == b"bbb"


# --------------------------------------------------------------------------
# BLOCKER 5 - 0xC547 is a LIGHTSPEED receiver, not a Bluetooth-direct device
# --------------------------------------------------------------------------


def test_pid_c547_is_classified_as_a_receiver(fake_backend, tmp_path):
    info = HidPathInfo(b"c547-long", 0x046D, 0xC547, 0xFF00, 2, 1, None, "USB Receiver")
    assert 0xC547 in RECEIVER_PIDS
    assert info.transport == "receiver"
    # Classified correctly, but out of scope for a V1 that targets one Bluetooth pair:
    # no group means no actor, hence no noise around the switches that matter.
    assert 0xC547 in V1_IGNORED_PIDS
    assert LogiPairService._group_paths([info]) == {}
    keyboard = HidPathInfo(b"kb", 0x046D, 0xB35B, 0xFF43, 0x0202, 2, "one")
    assert set(LogiPairService._group_paths([info, keyboard])) == {"bluetooth:b35b:one"}

    actor = TransportActor(
        0xC547, [info], fake_backend, PairController(lambda *_: None), DeviceCache(tmp_path / "c.json")
    )
    assert actor._receiver
    # No fabricated Bluetooth-direct runtime, hence no DEVICE_TYPE_AND_NAME polling loop.
    assert actor._devices == {}
    assert DIRECT_DEVICE_SLOT not in actor._by_slot


def test_lightspeed_receiver_without_target_devices_goes_passive(fake_backend, tmp_path, caplog):
    info = HidPathInfo(b"c547-long", 0x046D, 0xC547, 0xFF00, 2, 1, None, "USB Receiver")
    actor = TransportActor(
        0xC547,
        [info],
        fake_backend,
        PairController(lambda *_: None),
        DeviceCache(tmp_path / "c.json"),
        # Past the provisional grace a fresh actor gets: these are real failures.
        provisional_arrival_seconds=0.0,
    )
    with caplog.at_level(logging.INFO, logger="logipair.actor"):
        for _ in range(len(BACKOFF_SECONDS)):
            actor._connect()

    assert actor._ignored
    assert fake_backend.writes == []  # never probed a device behind this dongle
    assert any("staying passive" in item for item in messages(caplog))


# --------------------------------------------------------------------------
# BLOCKER 7 - observer-capable is the first milestone after an arrival
# --------------------------------------------------------------------------


def _write_cache(cache_path, identity, pid, role, name, features, cids=(), flags=0):
    cache_path.write_text(
        json.dumps(
            {
                "version": 1,
                "devices": {
                    identity: {
                        "wpid": pid,
                        "role": role,
                        "name": name,
                        "features": {str(key): value for key, value in features.items()},
                        "easy_switch_cids": list(cids),
                        "supported_flags": flags,
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def test_easy_switch_observed_during_p1_discovery_still_switches_the_peer(fake_backend, tmp_path, caplog):
    kb_path = fake_backend.add(b"kb", name="MX Keys Wireless Keyboard", device_type=0)
    mouse_path = fake_backend.add(b"mouse", name="MX Anywhere 3S", device_type=3)
    kb_identity = f"bluetooth:{kb_path.pid:04x}:kb"
    cache_path = tmp_path / "cache.json"
    _write_cache(
        cache_path,
        kb_identity,
        kb_path.pid,
        "keyboard",
        "MX Keys Wireless Keyboard",
        {FEATURE_CHANGE_HOST: 2, FEATURE_REPROG_CONTROLS_V4: 3},
        cids=(0xD1, 0xD2, 0xD3),
        flags=KEY_FLAG_ANALYTICS,
    )
    controller = PairController(lambda *_: None, debounce_seconds=0)
    cache = DeviceCache(cache_path)
    mouse = TransportActor(
        mouse_path.pid, [mouse_path], fake_backend, controller, cache, read_timeout_ms=1, request_timeout_ms=80
    )
    keyboard = TransportActor(
        kb_path.pid, [kb_path], fake_backend, controller, cache, read_timeout_ms=1, request_timeout_ms=400
    )
    controller.start()
    mouse.start()
    wait_until(lambda: any(item.role == DeviceRole.MOUSE and item.switch_capable for item in controller.devices()))

    # Stall P1 discovery (root getFeature) so the keyboard stays in INITIALIZING.
    fake_backend.withhold_responses_for.add((b"kb", 0, 0))
    with caplog.at_level(logging.INFO, logger="logipair.actor"):
        keyboard.start()
        wait_until(
            lambda: any(
                item.identity == kb_identity
                and item.observer_capable
                and item.state == DeviceState.INITIALIZING
                for item in controller.devices()
            )
        )
        # The keyboard is mid-discovery, yet must already observe the next Easy-Switch.
        fake_backend.inject(b"kb", notification(2, b"\x00\x02"))
        wait_until(lambda: len(fake_backend.change_writes(b"mouse")) == 1, timeout=2)
        observer_logs = [item for item in messages(caplog) if "connected_to_observer_capable_ms=" in item]

    assert fake_backend.change_writes(b"mouse")[0][1][4] == 2
    assert any(kb_identity in item for item in observer_logs)
    stop_pair(controller, keyboard, mouse)


def test_observer_capability_drops_with_the_transport(fake_backend, tmp_path):
    controller, keyboard, mouse, _, _ = start_pair(fake_backend, tmp_path)
    device = next(item for item in controller.devices() if item.role == DeviceRole.KEYBOARD)
    assert device.observer_capable
    fake_backend.specs.pop(b"kb")
    keyboard.update_paths([], "device-removal")
    wait_until(lambda: not device.observer_capable)
    stop_pair(controller, keyboard, mouse)


# --------------------------------------------------------------------------
# HIDAPI backend: per-handle concurrency and read-scoped diagnostics
# --------------------------------------------------------------------------


class _FakeLib:
    """Stand-in for hidapi.dll that records how much work overlaps."""

    def __init__(self, *, delay: float = 0.0, read_result: int = 0, write_result: int = 0) -> None:
        self._lock = threading.Lock()
        self._delay = delay
        self._read_result = read_result
        self._write_result = write_result
        self._active: collections.Counter[str] = collections.Counter()
        self.max_concurrent: collections.Counter[str] = collections.Counter()
        self.read_error_calls = 0
        self.error_calls = 0

    def _run(self, name: str):
        with self._lock:
            self._active[name] += 1
            total = sum(self._active.values())
            self.max_concurrent[name] = max(self.max_concurrent[name], total)
        time.sleep(self._delay)
        with self._lock:
            self._active[name] -= 1

    def hid_read_timeout(self, pointer, buf, size, timeout_ms):
        self._run("read")
        return self._read_result

    def hid_write(self, pointer, buf, size):
        self._run("write")
        return self._write_result

    def hid_send_output_report(self, pointer, buf, size):
        self._run("write")
        return self._write_result

    def hid_open_path(self, path):
        self._run("open")
        return 0x1000

    def hid_close(self, pointer):
        self._run("close")

    def hid_error(self, pointer):
        self.error_calls += 1
        return "HidD_SetOutputReport"

    def hid_read_error(self, pointer):
        self.read_error_calls += 1
        return "read-scoped failure"


def _backend_with(lib: _FakeLib) -> HidApiBackend:
    backend = HidApiBackend.__new__(HidApiBackend)
    backend._native_lock = threading.RLock()
    backend._lib = lib
    backend._read_error_fn = lib.hid_read_error
    backend._shutdown = False
    backend.library_path = "fake"
    backend.version = "0.15.0"
    return backend


def _run_concurrently(worker, count: int = 2) -> list[BaseException]:
    errors: list[BaseException] = []
    barrier = threading.Barrier(count)

    def entry(index: int) -> None:
        barrier.wait()
        try:
            worker(index)
        except BaseException as error:  # noqa: BLE001 - surfaced through the assertion below
            errors.append(error)

    threads = [threading.Thread(target=entry, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    return errors


def test_reads_and_writes_on_distinct_handles_do_not_serialize():
    lib = _FakeLib(delay=0.1)
    backend = _backend_with(lib)
    handles = [OwnedHandle(0x100 + index, f"h{index}".encode(), 0) for index in range(2)]

    def worker(index: int) -> None:
        handles[index].owner_ident = threading.get_ident()
        backend.read(handles[index], 5)
        backend.write(handles[index], b"\x11\xff", output_report=True)

    assert _run_concurrently(worker) == []
    # An MX Keys read must never queue behind an MX Anywhere read or write.
    assert lib.max_concurrent["read"] == 2
    assert lib.max_concurrent["write"] == 2


def test_open_and_close_stay_globally_serialized():
    lib = _FakeLib(delay=0.1)
    backend = _backend_with(lib)

    def worker(index: int) -> None:
        handle = backend.open(f"p{index}".encode())
        backend.close(handle)

    assert _run_concurrently(worker) == []
    assert lib.max_concurrent["open"] == 1
    assert lib.max_concurrent["close"] == 1


def test_read_failure_reports_the_read_scoped_error():
    lib = _FakeLib(read_result=-1)
    backend = _backend_with(lib)
    handle = OwnedHandle(0x100, b"h", threading.get_ident())

    with pytest.raises(TransportError) as excinfo:
        backend.read(handle, 5)

    # hid_error() here would return the stale "HidD_SetOutputReport" from an earlier write.
    assert str(excinfo.value) == "hid_read_timeout failed: read-scoped failure"
    assert (lib.read_error_calls, lib.error_calls) == (1, 0)


def test_write_failure_reports_the_handle_scoped_error():
    lib = _FakeLib(write_result=-1)
    backend = _backend_with(lib)
    handle = OwnedHandle(0x100, b"h", threading.get_ident())

    with pytest.raises(TransportError) as excinfo:
        backend.write(handle, b"\x11\xff", output_report=True)

    assert str(excinfo.value) == "hid_send_output_report failed: HidD_SetOutputReport"
    assert (lib.read_error_calls, lib.error_calls) == (0, 1)


def test_read_error_falls_back_to_hid_error_on_older_hidapi():
    lib = _FakeLib(read_result=-1)
    backend = _backend_with(lib)
    backend._read_error_fn = None
    handle = OwnedHandle(0x100, b"h", threading.get_ident())

    with pytest.raises(TransportError, match="HidD_SetOutputReport"):
        backend.read(handle, 5)
    assert (lib.read_error_calls, lib.error_calls) == (0, 1)


def test_discovery_is_abandoned_when_the_source_announces_a_departure(fake_backend, tmp_path, caplog):
    kb_path = fake_backend.add(b"kb", name="MX Keys Wireless Keyboard", device_type=0)
    kb_identity = f"bluetooth:{kb_path.pid:04x}:kb"
    cache_path = tmp_path / "cache.json"
    _write_cache(
        cache_path,
        kb_identity,
        kb_path.pid,
        "keyboard",
        "MX Keys Wireless Keyboard",
        {FEATURE_CHANGE_HOST: 2, FEATURE_REPROG_CONTROLS_V4: 3},
        cids=(0xD1, 0xD2, 0xD3),
        flags=KEY_FLAG_ANALYTICS,
    )
    controller = PairController(lambda *_: None, debounce_seconds=0)
    actor = TransportActor(
        kb_path.pid,
        [kb_path],
        fake_backend,
        controller,
        DeviceCache(cache_path),
        read_timeout_ms=1,
        request_timeout_ms=400,
    )
    fake_backend.withhold_responses_for.add((b"kb", 0, 0))
    controller.start()
    with caplog.at_level(logging.DEBUG, logger="logipair.actor"):
        actor.start()
        wait_until(lambda: controller.devices() and controller.devices()[0].observer_capable)
        fake_backend.inject(b"kb", notification(2, b"\x00\x02"))
        wait_until(lambda: any("Discovery abandoned" in item for item in messages(caplog)))
        writes_at_departure = len([item for item in fake_backend.writes if item[0] == b"kb"])
        time.sleep(0.25)
        still = len([item for item in fake_backend.writes if item[0] == b"kb"])

    # No re-arming, no further probing while the keyboard is on its way out.
    assert still == writes_at_departure
    assert actor._retry_index == 0
    assert not any("EnsureReady failed" in item for item in messages(caplog))
    stop_pair(controller, actor)


def test_twenty_rapid_transitions_with_device_churn_never_drop_a_switch(fake_backend, tmp_path, caplog):
    controller, keyboard, mouse, _, _ = start_pair(fake_backend, tmp_path, debounce=0)
    opens_before = len(fake_backend.opens)
    started = time.monotonic()
    with caplog.at_level(logging.DEBUG, logger="logipair.actor"):
        caplog.clear()
        for index in range(20):
            fake_backend.inject(b"kb", notification(2, bytes((0x00, index % 3))))
            wait_until(lambda expected=index + 1: len(fake_backend.change_writes(b"mouse")) == expected, timeout=5)
            # Both devices really leave the host after every hop, then come straight back.
            fake_backend.inject(b"kb", TransportError("device left host"))
            fake_backend.inject(b"mouse", TransportError("device left host"))
            wait_until(lambda seen=opens_before: len(fake_backend.opens) > seen, timeout=5)
            opens_before = len(fake_backend.opens)
            wait_until(lambda: controller.health == PairHealth.PAIR_READY, timeout=5)
        elapsed = time.monotonic() - started
        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.levelno >= logging.WARNING and record.name.startswith("logipair")
        ]
    stop_pair(controller, keyboard, mouse)

    assert len(fake_backend.change_writes(b"mouse")) == 20
    assert fake_backend.change_writes(b"kb") == []
    # No hop may inherit a backoff from the previous hop's expected departure.
    assert keyboard._retry_index == 0 and mouse._retry_index == 0
    assert warnings == []
    assert elapsed < 20
    assert fake_backend.violations == []


def test_a_device_that_does_not_actually_leave_resumes_discovery(fake_backend, tmp_path):
    """A CHANGE_HOST to the current host means no departure; discovery must resume."""
    path = fake_backend.add(b"kb", name="MX Keys", device_type=0)
    controller = PairController(lambda *_: None)
    actor = TransportActor(
        path.pid,
        [path],
        fake_backend,
        controller,
        DeviceCache(tmp_path / "cache.json"),
        read_timeout_ms=1,
        request_timeout_ms=80,
        departure_grace_seconds=0.3,
    )
    controller.start()
    actor.start()
    wait_until(lambda: controller.devices() and controller.devices()[0].state == DeviceState.READY)
    device = controller.devices()[0]

    fake_backend.inject(b"kb", notification(2, b"\x00\x01"))
    wait_until(lambda: device.departure_grace_until > 0)
    actor.ensure_ready("post-notification check")
    # The device never went away, so once the window lapses it must reach READY again.
    wait_until(lambda: device.state == DeviceState.READY, timeout=3)

    assert actor._retry_index == 0
    stop_pair(controller, actor)


# --------------------------------------------------------------------------
# RC2 root cause 1/2 - provisional Windows arrivals
# --------------------------------------------------------------------------


def _cached_pair(fake_backend, tmp_path, *, debounce=0.0):
    """Mouse READY, keyboard not started yet but fully cached."""
    kb_path = fake_backend.add(b"kb", name="MX Keys Wireless Keyboard", device_type=0)
    mouse_path = fake_backend.add(b"mouse", name="MX Anywhere 3S", device_type=3)
    kb_identity = f"bluetooth:{kb_path.pid:04x}:kb"
    cache_path = tmp_path / "cache.json"
    _write_cache(
        cache_path,
        kb_identity,
        kb_path.pid,
        "keyboard",
        "MX Keys Wireless Keyboard",
        {FEATURE_CHANGE_HOST: 2, FEATURE_REPROG_CONTROLS_V4: 3},
        cids=(0xD1, 0xD2, 0xD3),
        flags=KEY_FLAG_ANALYTICS,
    )
    controller = PairController(lambda *_: None, debounce_seconds=debounce)
    cache = DeviceCache(cache_path)
    mouse = TransportActor(
        mouse_path.pid, [mouse_path], fake_backend, controller, cache, read_timeout_ms=1, request_timeout_ms=80
    )
    keyboard = TransportActor(
        kb_path.pid, [kb_path], fake_backend, controller, cache, read_timeout_ms=1, request_timeout_ms=200
    )
    controller.start()
    mouse.start()
    wait_until(lambda: any(item.role == DeviceRole.MOUSE and item.switch_capable for item in controller.devices()))
    return controller, keyboard, mouse, kb_identity


def test_stale_windows_arrival_never_claims_observer_capable_then_recovers(fake_backend, tmp_path, caplog):
    """The exact RC2 miss: Windows exposes the path early, the handle is unusable.

    Host A READY -> switch to B -> devices leave -> Windows re-exposes the keyboard
    path prematurely -> the cached handle opens -> the user immediately presses B->A.
    Before the fix the actor claimed observer_capable here and the press was lost.
    """
    controller, keyboard, mouse, kb_identity = _cached_pair(fake_backend, tmp_path)
    # Every read fails with ERROR_DEVICE_NOT_CONNECTED, exactly as the hardware did.
    fake_backend.stale_reads[b"kb"] = 8
    runtime = next(iter(keyboard._devices.values()))

    with caplog.at_level(logging.DEBUG, logger="logipair.actor"):
        try:
            keyboard.start()
            wait_until(lambda: len([item for item in fake_backend.opens if item[0] == b"kb"]) >= 3)

            # The handle opened three times and was never usable: claiming otherwise is
            # exactly the false positive that lost the user's press.
            assert not runtime.observer_capable
            assert not runtime.switch_capable
            assert keyboard._retry_index == 0  # provisional, never the backoff ladder
            assert not [
                record
                for record in caplog.records
                if record.levelno >= logging.WARNING and record.name == "logipair.actor"
            ]
            assert any("Provisional connection not usable yet" in item for item in messages(caplog))

            # Windows settles; the very next probe succeeds.
            fake_backend.stale_reads.pop(b"kb", None)
            wait_until(lambda: runtime.observer_capable, timeout=3)

            # An Easy-Switch pressed right now must be seen and must move the mouse.
            fake_backend.inject(b"kb", notification(2, b"\x00\x02"))
            wait_until(lambda: len(fake_backend.change_writes(b"mouse")) == 1, timeout=3)
        finally:
            stop_pair(controller, keyboard, mouse)

    assert fake_backend.change_writes(b"mouse")[0][1][4] == 2
    assert any("connected_to_observer_capable_ms=" in item for item in messages(caplog))
    assert fake_backend.violations == []


def test_observer_capable_requires_a_validated_handle_not_just_an_open(fake_backend, tmp_path):
    controller, keyboard, mouse, _ = _cached_pair(fake_backend, tmp_path)
    fake_backend.stale_reads[b"kb"] = 4
    runtime = next(iter(keyboard._devices.values()))
    try:
        keyboard.start()
        # Opens succeed throughout; only validation stands between us and a false claim.
        wait_until(lambda: len([item for item in fake_backend.opens if item[0] == b"kb"]) >= 2)
        assert not runtime.observer_capable
        wait_until(lambda: runtime.observer_capable, timeout=3)
        assert not fake_backend.stale_reads.get(b"kb")
    finally:
        stop_pair(controller, keyboard, mouse)


def test_provisional_failures_use_a_fast_retry_not_the_backoff_ladder(fake_backend, tmp_path):
    path = fake_backend.add(b"kb", name="MX Keys", device_type=0)
    controller = PairController(lambda *_: None)
    actor = TransportActor(
        path.pid,
        [path],
        fake_backend,
        controller,
        DeviceCache(tmp_path / "cache.json"),
        read_timeout_ms=1,
        request_timeout_ms=80,
    )
    fake_backend.stale_reads[b"kb"] = 5
    started = time.monotonic()
    controller.start()
    try:
        actor.start()
        wait_until(lambda: len([item for item in fake_backend.opens if item[0] == b"kb"]) >= 5, timeout=3)
        # Five ladder rungs would already be 0.1+0.25+0.5+1+2 = 3.85 s.
        assert time.monotonic() - started < 1.5
        assert actor._retry_index == 0
        wait_until(lambda: controller.devices() and controller.devices()[0].state == DeviceState.READY, timeout=3)
    finally:
        stop_pair(controller, actor)


def test_a_newer_arrival_supersedes_a_pending_provisional_retry(fake_backend, tmp_path):
    path = fake_backend.add(b"kb", name="MX Keys", device_type=0)
    controller = PairController(lambda *_: None)
    actor = TransportActor(
        path.pid,
        [path],
        fake_backend,
        controller,
        DeviceCache(tmp_path / "cache.json"),
        read_timeout_ms=1,
        request_timeout_ms=80,
    )
    # A provisional retry is pending far in the future, and the ladder is dirty.
    actor._retry_at = time.monotonic() + 30.0
    actor._retry_index = 5
    controller.start()
    try:
        actor.start()
        actor.device_arrived("device-arrival")
        # The second Windows arrival must reconnect now, not in 30 s.
        wait_until(lambda: controller.devices() and controller.devices()[0].state == DeviceState.READY, timeout=3)
        assert actor._retry_index == 0
    finally:
        stop_pair(controller, actor)


# --------------------------------------------------------------------------
# RC2 root cause 3 - "away" has no deadline
# --------------------------------------------------------------------------


class _FakeClock:
    """Controllable monotonic clock, so an arbitrarily long absence stays deterministic."""

    def __init__(self, start: float = 10_000.0) -> None:
        self.value = start

    def monotonic(self) -> float:
        return self.value

    def advance(self, seconds: float, *, settle: float = 0.05) -> None:
        self.value += seconds
        time.sleep(settle)  # let the actor's run loop observe the new time


def _start_single_keyboard(fake_backend, tmp_path, **kwargs):
    path = fake_backend.add(b"kb", name="MX Keys", device_type=0)
    controller = PairController(lambda *_: None)
    actor = TransportActor(
        path.pid,
        [path],
        fake_backend,
        controller,
        DeviceCache(tmp_path / "cache.json"),
        read_timeout_ms=1,
        request_timeout_ms=80,
        **kwargs,
    )
    controller.start()
    actor.start()
    wait_until(lambda: controller.devices() and controller.devices()[0].state == DeviceState.READY)
    return controller, actor, path


def test_five_minutes_away_produces_zero_connect_attempts(fake_backend, tmp_path, monkeypatch, caplog):
    """The pair can sit on the other PC for as long as the user likes.

    RC2 hardware showed the old host climbing 0.1 -> 30 s while the devices were simply
    still connected elsewhere. Away has no TTL, so there must be no ladder at all.
    """
    clock = _FakeClock()
    monkeypatch.setattr(actor_module, "time", SimpleNamespace(monotonic=clock.monotonic))
    controller, actor, _ = _start_single_keyboard(fake_backend, tmp_path)
    runtime = controller.devices()[0]

    with caplog.at_level(logging.DEBUG, logger="logipair.actor"):
        try:
            fake_backend.inject(b"kb", notification(2, b"\x00\x01"))
            wait_until(lambda: runtime.away_expected)
            # Windows removes the device: it is now on the other host.
            fake_backend.specs.pop(b"kb")
            actor.update_paths([], "device-removal")
            wait_until(lambda: runtime.state == DeviceState.EXPECTED_DISCONNECTED)
            opens_before = len(fake_backend.opens)

            for _ in range(30):  # five minutes, ten seconds at a time
                clock.advance(10.0)

            assert len(fake_backend.opens) == opens_before  # not one attempt
            assert actor._retry_index == 0  # not one rung
            assert runtime.state == DeviceState.EXPECTED_DISCONNECTED
            assert runtime.away_expected
            warnings = [
                record.getMessage()
                for record in caplog.records
                if record.levelno >= logging.WARNING and record.name == "logipair.actor"
            ]
            assert warnings == []
            assert len([item for item in messages(caplog) if "Expected away" in item]) == 1
        finally:
            stop_pair(controller, actor)


def test_a_genuine_arrival_exits_expected_away_immediately(fake_backend, tmp_path, monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(actor_module, "time", SimpleNamespace(monotonic=clock.monotonic))
    controller, actor, path = _start_single_keyboard(fake_backend, tmp_path)
    runtime = controller.devices()[0]
    try:
        fake_backend.inject(b"kb", notification(2, b"\x00\x01"))
        wait_until(lambda: runtime.away_expected)
        fake_backend.specs.pop(b"kb")
        actor.update_paths([], "device-removal")
        wait_until(lambda: runtime.state == DeviceState.EXPECTED_DISCONNECTED)
        clock.advance(600.0)  # ten minutes on the other PC
        opens_before = len(fake_backend.opens)

        # The user comes back to this PC.
        fake_backend.add(b"kb", name="MX Keys", device_type=0)
        actor.update_paths([path], "device-arrival")
        actor.device_arrived("device-arrival")

        wait_until(lambda: not runtime.away_expected)
        wait_until(lambda: runtime.state == DeviceState.READY, timeout=3)
        assert len(fake_backend.opens) > opens_before
        assert actor._retry_index == 0
    finally:
        stop_pair(controller, actor)


def test_enumeration_alone_ends_expected_away(fake_backend, tmp_path):
    """A poll that lists the device again is proof of presence; no arrival event needed."""
    controller, actor, path = _start_single_keyboard(fake_backend, tmp_path)
    runtime = controller.devices()[0]
    try:
        fake_backend.inject(b"kb", notification(2, b"\x00\x01"))
        wait_until(lambda: runtime.away_expected)
        fake_backend.specs.pop(b"kb")
        actor.update_paths([], "device-removal")
        wait_until(lambda: runtime.state == DeviceState.EXPECTED_DISCONNECTED)

        fake_backend.add(b"kb", name="MX Keys", device_type=0)
        actor.update_paths([path], "poll")
        wait_until(lambda: runtime.state == DeviceState.READY, timeout=3)
        assert not runtime.away_expected
        assert actor._retry_index == 0
    finally:
        stop_pair(controller, actor)


def test_a_device_that_keeps_responding_is_not_treated_as_away(fake_backend, tmp_path):
    """A hop to the host we are already on means no departure at all."""
    controller, actor, _ = _start_single_keyboard(fake_backend, tmp_path, departure_grace_seconds=0.2)
    runtime = controller.devices()[0]
    try:
        fake_backend.inject(b"kb", notification(2, b"\x00\x01"))
        wait_until(lambda: runtime.away_expected)
        time.sleep(0.3)  # outlive the grace window
        fake_backend.inject(b"kb", notification(3, b"\x00\x00\x00\x00\x00\x00", function=3))
        wait_until(lambda: not runtime.away_expected)
        assert runtime.state == DeviceState.READY
    finally:
        stop_pair(controller, actor)


def test_keyboard_is_never_observer_capable_before_arming_succeeds(fake_backend, tmp_path):
    """A validated handle and a complete cache still do not make a keyboard observable.

    This keyboard is seen through diverted x1B04 events, so until the CIDs are actually
    ACKed we cannot see its next hop - and must not claim we can.
    """
    controller, keyboard, mouse, _ = _cached_pair(fake_backend, tmp_path)
    fake_backend.withhold_arm_ack.add(b"kb")
    runtime = next(iter(keyboard._devices.values()))
    try:
        keyboard.start()
        # The transport itself is fine: it opens, validates, and CHANGE_HOST is writable.
        wait_until(lambda: runtime.switch_capable)
        time.sleep(0.3)
        assert not runtime.observer_capable
        assert runtime.state != DeviceState.READY

        fake_backend.withhold_arm_ack.discard(b"kb")
        wait_until(lambda: runtime.observer_capable, timeout=3)
        wait_until(lambda: runtime.state == DeviceState.READY, timeout=3)
    finally:
        stop_pair(controller, keyboard, mouse)


def test_rapid_a_to_b_to_a_across_a_real_departure_and_a_stale_return(fake_backend, tmp_path, caplog):
    """The full RC2 scenario, end to end.

    A READY -> switch to B -> both devices leave A -> Windows re-exposes the keyboard
    path prematurely -> the user immediately presses B->A. The mouse must follow.
    """
    controller, keyboard, mouse, kb_path, mouse_path = start_pair(fake_backend, tmp_path, debounce=0)

    with caplog.at_level(logging.DEBUG, logger="logipair.actor"):
        try:
            # --- A -> B -------------------------------------------------------
            fake_backend.inject(b"kb", notification(2, b"\x00\x01"))
            wait_until(lambda: len(fake_backend.change_writes(b"mouse")) == 1)
            keyboard_runtime = next(item for item in controller.devices() if item.role == DeviceRole.KEYBOARD)
            mouse_runtime = next(item for item in controller.devices() if item.role == DeviceRole.MOUSE)
            wait_until(lambda: keyboard_runtime.away_expected and mouse_runtime.away_expected)

            # --- both devices really leave this host ---------------------------
            fake_backend.specs.pop(b"kb")
            fake_backend.specs.pop(b"mouse")
            keyboard.update_paths([], "device-removal")
            mouse.update_paths([], "device-removal")
            wait_until(lambda: keyboard_runtime.state == DeviceState.EXPECTED_DISCONNECTED)
            wait_until(lambda: mouse_runtime.state == DeviceState.EXPECTED_DISCONNECTED)
            opens_while_away = len(fake_backend.opens)
            time.sleep(0.4)
            assert len(fake_backend.opens) == opens_while_away  # quiet while elsewhere

            # --- back to A, keyboard path exposed before it is usable ----------
            fake_backend.add(b"kb", name="MX Keys Wireless Keyboard", device_type=0)
            fake_backend.add(b"mouse", name="MX Anywhere 3S", device_type=3)
            fake_backend.stale_reads[b"kb"] = 4
            for actor, path in ((keyboard, kb_path), (mouse, mouse_path)):
                actor.update_paths([path], "device-arrival")
                actor.device_arrived("device-arrival")

            # --- B -> A pressed as soon as the keyboard can really observe -----
            wait_until(lambda: keyboard_runtime.observer_capable, timeout=5)
            wait_until(lambda: mouse_runtime.switch_capable, timeout=5)
            fake_backend.inject(b"kb", notification(2, b"\x00\x00"))
            wait_until(lambda: len(fake_backend.change_writes(b"mouse")) == 2, timeout=5)

            warnings = [
                record.getMessage()
                for record in caplog.records
                if record.levelno >= logging.WARNING and record.name == "logipair.actor"
            ]
        finally:
            stop_pair(controller, keyboard, mouse)

    assert [item[1][4] for item in fake_backend.change_writes(b"mouse")] == [1, 0]
    assert fake_backend.change_writes(b"kb") == []  # source exclusion holds
    assert keyboard._retry_index == 0 and mouse._retry_index == 0
    assert warnings == []
    assert fake_backend.violations == []


def test_writes_failing_just_after_a_provisional_connect_stay_quiet(fake_backend, tmp_path, caplog):
    """RC2 log: CONNECTED, then EnsureReady failed ... HidD_SetOutputReport.

    A read probe can pass while the collection is still settling and the first output
    reports fail. That is Windows, not a fault, so it must not produce WARN spam or a
    backoff ladder - the provisional window covers the whole unsettled period.
    """
    controller, keyboard, mouse, _ = _cached_pair(fake_backend, tmp_path)
    fake_backend.stale_writes[b"kb"] = 4
    runtime = next(iter(keyboard._devices.values()))

    with caplog.at_level(logging.DEBUG, logger="logipair.actor"):
        try:
            keyboard.start()
            wait_until(lambda: any("CONNECTED" in item for item in messages(caplog)))
            wait_until(lambda: not fake_backend.stale_writes.get(b"kb"), timeout=3)
            wait_until(lambda: runtime.observer_capable, timeout=3)
            warnings = [
                record.getMessage()
                for record in caplog.records
                if record.levelno >= logging.WARNING and record.name == "logipair.actor"
            ]
        finally:
            stop_pair(controller, keyboard, mouse)

    assert keyboard._retry_index == 0
    assert warnings == []
    assert any("Discovery deferred; Windows still settling" in item for item in messages(caplog))


# --------------------------------------------------------------------------
# RC3 - a press consumed BY the read probe must still reach the peer
# --------------------------------------------------------------------------


def test_easy_switch_consumed_by_the_read_probe_reaches_the_peer_before_discovery(
    fake_backend, tmp_path, caplog
):
    """The press is read by _validate_transport itself, not by the poll loop.

    Host A READY -> A->B -> both devices leave -> the keyboard returns with the user's
    B->A notification already waiting on the wire, so the validation probe consumes it
    before observer_capable is published and before _initialize does anything. That
    report must reach PairController and move the mouse, not sit behind P1 discovery.
    """
    controller, keyboard, mouse, kb_path, mouse_path = start_pair(fake_backend, tmp_path, debounce=0)

    with caplog.at_level(logging.DEBUG, logger="logipair.actor"):
        try:
            # --- A -> B -------------------------------------------------------
            fake_backend.inject(b"kb", notification(2, b"\x00\x01"))
            wait_until(lambda: len(fake_backend.change_writes(b"mouse")) == 1)
            kb_runtime = next(item for item in controller.devices() if item.role == DeviceRole.KEYBOARD)
            mouse_runtime = next(item for item in controller.devices() if item.role == DeviceRole.MOUSE)

            # --- both devices really leave this host --------------------------
            fake_backend.specs.pop(b"kb")
            fake_backend.specs.pop(b"mouse")
            keyboard.update_paths([], "device-removal")
            mouse.update_paths([], "device-removal")
            wait_until(lambda: kb_runtime.state == DeviceState.EXPECTED_DISCONNECTED)
            wait_until(lambda: mouse_runtime.state == DeviceState.EXPECTED_DISCONNECTED)

            # The mouse is back first, so the peer is writable when the press lands.
            fake_backend.add(b"mouse", name="MX Anywhere 3S", device_type=3)
            mouse.update_paths([mouse_path], "device-arrival")
            mouse.device_arrived("device-arrival")
            wait_until(lambda: mouse_runtime.switch_capable, timeout=3)

            # --- the user's B->A press is already queued when the path returns --
            fake_backend.add(b"kb", name="MX Keys Wireless Keyboard", device_type=0)
            fake_backend.inject(b"kb", notification(2, b"\x00\x00"))
            mark = len(fake_backend.timeline)
            keyboard.update_paths([kb_path], "device-arrival")
            keyboard.device_arrived("device-arrival")

            wait_until(lambda: len(fake_backend.change_writes(b"mouse")) == 2, timeout=5)
            timeline = fake_backend.timeline[mark:]
            emitted = messages(caplog)
            warnings = [
                record.getMessage()
                for record in caplog.records
                if record.levelno >= logging.WARNING and record.name == "logipair.actor"
            ]
        finally:
            stop_pair(controller, keyboard, mouse)

    # The invariant, proven on the wire: the probe consumed the press, and NO keyboard
    # discovery write got between that read and the peer's CHANGE_HOST. Before the fix
    # the whole of P1 discovery ran in that gap while the press sat in a buffer.
    read_at = next(
        index
        for index, (kind, path, payload) in enumerate(timeline)
        if kind == "read" and path == b"kb" and payload[2] == 2 and payload[4:6] == b"\x00\x00"
    )
    peer_write_at = next(
        index
        for index, (kind, path, payload) in enumerate(timeline)
        if kind == "write" and path == b"mouse" and payload[2] == 2 and (payload[3] & 0xF0) == 0x10
    )
    assert read_at < peer_write_at
    blocking = [entry for entry in timeline[read_at:peer_write_at] if entry[0] == "write" and entry[1] == b"kb"]
    assert blocking == [], f"{len(blocking)} keyboard discovery writes delayed the peer switch"

    # The probe - not the poll loop - is what read it.
    assert any("Read probe consumed 1 report(s)" in item for item in emitted)

    # It was dispatched exactly once, and it moved the peer the right way.
    assert len([item for item in emitted if "EasySwitch source=" in item and "target=0" in item]) == 1
    assert [item[1][4] for item in fake_backend.change_writes(b"mouse")] == [1, 0]
    assert fake_backend.change_writes(b"kb") == []  # source exclusion holds

    # The departure the press implies is marked, and costs nothing.
    assert kb_runtime.away_expected
    assert kb_runtime.departure_grace_until > 0
    assert keyboard._retry_index == 0 and mouse._retry_index == 0
    assert warnings == []
    assert fake_backend.violations == []


def test_a_cold_connect_does_not_discard_reports_read_by_the_probe(fake_backend, tmp_path):
    """With no cache the press cannot be interpreted at probe time, so it is dispatched
    once discovery has filled the feature indexes in - never dropped."""
    kb_path = fake_backend.add(b"kb", name="MX Keys Wireless Keyboard", device_type=0)
    mouse_path = fake_backend.add(b"mouse", name="MX Anywhere 3S", device_type=3)
    controller = PairController(lambda *_: None, debounce_seconds=0)
    cache = DeviceCache(tmp_path / "cache.json")  # empty: nothing has ever been seen
    mouse = TransportActor(
        mouse_path.pid, [mouse_path], fake_backend, controller, cache, read_timeout_ms=1, request_timeout_ms=80
    )
    keyboard = TransportActor(
        kb_path.pid, [kb_path], fake_backend, controller, cache, read_timeout_ms=1, request_timeout_ms=80
    )
    controller.start()
    mouse.start()
    wait_until(lambda: any(item.role == DeviceRole.MOUSE and item.switch_capable for item in controller.devices()))

    # Already on the wire when the probe runs, before anything is known about this device.
    fake_backend.inject(b"kb", notification(2, b"\x00\x02"))
    try:
        keyboard.start()
        wait_until(lambda: len(fake_backend.change_writes(b"mouse")) == 1, timeout=5)
    finally:
        stop_pair(controller, keyboard, mouse)

    assert fake_backend.change_writes(b"mouse")[0][1][4] == 2
    assert fake_backend.change_writes(b"kb") == []
