"""Rapid-switch reliability: expected departures, lifecycle storms and the hot read path."""

from __future__ import annotations

import collections
import json
import logging
import threading
import time

import pytest
from conftest import wait_until

from logipair.actor import TransportActor
from logipair.constants import (
    BACKOFF_SECONDS,
    DIRECT_DEVICE_SLOT,
    FEATURE_CHANGE_HOST,
    FEATURE_REPROG_CONTROLS_V4,
    KEY_FLAG_ANALYTICS,
    RECEIVER_PIDS,
    REPORT_LONG,
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
    wait_until(lambda: device.expected_departure_until > 0)
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
    wait_until(lambda: keyboard_device.expected_departure_until > 0)
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
    wait_until(lambda: keyboard_device.expected_departure_until > 0)

    keyboard.device_arrived("device-arrival")
    wait_until(lambda: keyboard_device.expected_departure_until == 0)
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
    assert set(LogiPairService._group_paths([info])) == {"receiver:c547"}

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
        0xC547, [info], fake_backend, PairController(lambda *_: None), DeviceCache(tmp_path / "c.json")
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
        expected_departure_seconds=0.3,
    )
    controller.start()
    actor.start()
    wait_until(lambda: controller.devices() and controller.devices()[0].state == DeviceState.READY)
    device = controller.devices()[0]

    fake_backend.inject(b"kb", notification(2, b"\x00\x01"))
    wait_until(lambda: device.expected_departure_until > 0)
    actor.ensure_ready("post-notification check")
    # The device never went away, so once the window lapses it must reach READY again.
    wait_until(lambda: device.state == DeviceState.READY, timeout=3)

    assert actor._retry_index == 0
    stop_pair(controller, actor)
