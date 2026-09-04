from __future__ import annotations

import dataclasses
import itertools
import logging
import queue
import threading
import time
from concurrent.futures import Future
from typing import Any, Protocol

from .constants import (
    BACKOFF_SECONDS,
    DEVICE_TYPE_KEYBOARD,
    DEVICE_TYPE_MOUSE,
    DEVICE_TYPE_TRACKBALL,
    DEVICE_TYPE_TRACKPAD,
    DIRECT_DEVICE_SLOT,
    ENABLE_RECEIVER_NOTIFICATIONS,
    ENUMERATE_RECEIVER_DEVICES,
    EXPECTED_DEPARTURE_RETRY_SECONDS,
    EXPECTED_DEPARTURE_SECONDS,
    FEATURE_CHANGE_HOST,
    FEATURE_DEVICE_TYPE_AND_NAME,
    FEATURE_REPROG_CONTROLS_V4,
    HIDPP_USAGE_SHORT,
    HOST_SWITCH_CIDS,
    KEY_FLAG_ANALYTICS,
    KEY_FLAG_DIVERTABLE,
    KEY_FLAG_PERSISTENTLY_DIVERTABLE,
    LONG_USAGES,
    RECEIVER_PIDS,
    REPORT_LONG,
    REPORT_SHORT,
    SW_ID_DIVERT,
    SW_ID_HOST_CHANGE,
    SW_ID_REQUEST,
)
from .controller import PairController
from .errors import ProtocolError, TransportError
from .model import DeviceRole, DeviceRuntime, DeviceState, HidPathInfo, HostChange, SwitchResult
from .protocol import (
    DeviceConnection,
    HidError,
    Notification,
    ReportingChanged,
    Response,
    build_change_host,
    build_cid_reporting,
    build_get_feature,
    build_message,
    notification_target,
    parse_report,
    response_matches,
)
from .storage import DeviceCache

log = logging.getLogger(__name__)


class Backend(Protocol):
    def open(self, path: bytes) -> Any: ...

    def read(self, handle: Any, timeout_ms: int = 0) -> bytes | None: ...

    def write(self, handle: Any, message: bytes, *, output_report: bool) -> None: ...

    def close(self, handle: Any) -> None: ...


@dataclasses.dataclass(frozen=True)
class _Ensure:
    reason: str


@dataclasses.dataclass(frozen=True)
class _Reconcile:
    paths: tuple[HidPathInfo, ...]
    reason: str
    force: bool = False


@dataclasses.dataclass(frozen=True)
class _Arrived:
    """A real Windows HID arrival for this transport. Clears every failure history."""

    reason: str


@dataclasses.dataclass(frozen=True)
class _Switch:
    identity: str
    target_host: int
    observed_at: float
    future: Future[SwitchResult]


@dataclasses.dataclass(frozen=True)
class _Stop:
    pass


class TransportActor(threading.Thread):
    """The sole owner of every HID handle for one PID/transport."""

    def __init__(
        self,
        pid: int,
        paths: list[HidPathInfo],
        backend: Backend,
        controller: PairController,
        cache: DeviceCache,
        *,
        read_timeout_ms: int = 15,
        request_timeout_ms: int = 600,
        rearm_throttle_seconds: float = 2.0,
        expected_departure_seconds: float = EXPECTED_DEPARTURE_SECONDS,
    ) -> None:
        super().__init__(name=f"TransportActor-0x{pid:04X}", daemon=True)
        self.pid = pid
        self._paths = tuple(paths)
        self._backend = backend
        self._controller = controller
        self._cache = cache
        self._read_timeout_ms = read_timeout_ms
        self._request_timeout_ms = request_timeout_ms
        self._rearm_throttle_seconds = rearm_throttle_seconds
        self._expected_departure_seconds = expected_departure_seconds
        self._expected_departure_until = 0.0
        self._selected: dict[int, HidPathInfo] = {}
        self._queue: queue.PriorityQueue[tuple[int, int, object]] = queue.PriorityQueue(maxsize=256)
        self._sequence = itertools.count()
        self._handles: dict[int, Any] = {}
        self._devices: dict[str, DeviceRuntime] = {}
        self._by_slot: dict[int, str] = {}
        self._stop_requested = threading.Event()
        self._ensure_queued = threading.Event()
        self._retry_index = 0
        self._retry_at = 0.0
        self._last_rearm: dict[str, float] = {}
        self._connected_at: dict[str, float] = {}
        self._ready_logged: set[str] = set()
        self._observer_logged: set[str] = set()
        self._receiver = pid in RECEIVER_PIDS
        self._ignored = False
        if not self._receiver:
            self._ensure_direct_runtime()

    def update_paths(self, paths: list[HidPathInfo], reason: str, *, force: bool = False) -> None:
        self._put(0, _Reconcile(tuple(paths), reason, force))

    def recover(self, reason: str) -> None:
        self._put(0, _Reconcile(self._paths, reason, True))

    def device_arrived(self, reason: str) -> None:
        """Windows saw this transport come back; no stale backoff may survive that."""
        self._put(0, _Arrived(reason))

    def ensure_ready(self, reason: str) -> None:
        if self._ensure_queued.is_set() or self._stop_requested.is_set():
            return
        self._ensure_queued.set()
        if not self._put(1, _Ensure(reason)):
            self._ensure_queued.clear()

    def switch(self, identity: str, target_host: int, observed_at: float) -> Future[SwitchResult]:
        future: Future[SwitchResult] = Future()
        if self._stop_requested.is_set() or not self._put(0, _Switch(identity, target_host, observed_at, future)):
            future.set_result(SwitchResult(False, "actor stopping or queue full", time.monotonic()))
        return future

    def stop(self) -> None:
        self._stop_requested.set()
        self._put(0, _Stop(), allow_stopping=True)

    def run(self) -> None:
        try:
            while not self._stop_requested.is_set():
                self._drain_commands(limit=8)
                if self._stop_requested.is_set():
                    break
                if REPORT_LONG not in self._handles:
                    if self._ignored:
                        self._wait_for_command(0.25)
                        continue
                    if time.monotonic() >= self._retry_at:
                        self._connect()
                    self._wait_for_command(0.05)
                    continue
                self._poll_handles()
                if time.monotonic() >= self._retry_at:
                    for device in list(self._devices.values()):
                        if device.state in (DeviceState.RECOVERING, DeviceState.EXPECTED_DISCONNECTED):
                            self._initialize(device)
        except Exception:
            log.exception("Actor pid=0x%04X terminated unexpectedly", self.pid)
            self._mark_transport_lost("actor failure")
        finally:
            self._close_handles()
            self._resolve_queued_switches("actor stopped")

    def _put(self, priority: int, command: object, *, allow_stopping: bool = False) -> bool:
        if self._stop_requested.is_set() and not allow_stopping:
            return False
        try:
            self._queue.put_nowait((priority, next(self._sequence), command))
            return True
        except queue.Full:
            log.critical("Actor queue full pid=0x%04X command=%s", self.pid, type(command).__name__)
            return False

    def _wait_for_command(self, timeout: float) -> None:
        try:
            item = self._queue.get(timeout=timeout)
        except queue.Empty:
            return
        self._handle_command(item[2])

    def _drain_commands(self, limit: int) -> None:
        for _ in range(limit):
            try:
                _, _, command = self._queue.get_nowait()
            except queue.Empty:
                return
            self._handle_command(command)

    def _handle_command(self, command: object) -> None:
        if isinstance(command, _Stop):
            self._stop_requested.set()
            return
        if isinstance(command, _Reconcile):
            self._on_reconcile(command)
            return
        if isinstance(command, _Arrived):
            self._on_arrival(command.reason)
            return
        if isinstance(command, _Ensure):
            self._ensure_queued.clear()
            log.debug("EnsureReady pid=0x%04X reason=%s", self.pid, command.reason)
            if REPORT_LONG not in self._handles:
                self._retry_at = 0.0
            else:
                for device in list(self._devices.values()):
                    if device.state != DeviceState.READY:
                        self._initialize(device)
            return
        if isinstance(command, _Switch):
            try:
                self._perform_switch(command)
            except Exception as error:
                if not command.future.done():
                    command.future.set_result(SwitchResult(False, str(error), time.monotonic()))
                raise

    def _on_reconcile(self, command: _Reconcile) -> None:
        changed = {path.path for path in command.paths} != {path.path for path in self._paths}
        self._paths = command.paths
        if command.force:
            self._ignored = False
            log.info("Transport recovery requested pid=0x%04X reason=%s", self.pid, command.reason)
            self._mark_transport_lost(command.reason, bump_backoff=False)
            return
        if not changed:
            return
        self._ignored = False
        if REPORT_LONG not in self._handles:
            self._retry_at = 0.0
            log.debug("HID paths changed while disconnected pid=0x%04X reason=%s", self.pid, command.reason)
            return
        # Windows renames or re-creates ancillary HID interfaces constantly. Only the
        # collections we actually hold open matter; anything else is cosmetic churn.
        selection = self._select_paths(command.paths, self._selected)
        required = (REPORT_LONG, REPORT_SHORT) if self._receiver else (REPORT_LONG,)
        unchanged = all(
            report_id in selection
            and report_id in self._selected
            and selection[report_id].path == self._selected[report_id].path
            for report_id in required
        )
        if unchanged:
            log.debug(
                "HID path set changed but active HID++ collections are unchanged pid=0x%04X reason=%s",
                self.pid,
                command.reason,
            )
            return
        log.info("Active HID++ collection changed pid=0x%04X reason=%s", self.pid, command.reason)
        self._mark_transport_lost("active HID++ collection changed", bump_backoff=False)

    def _on_arrival(self, reason: str) -> None:
        returning = self._retry_index or self._ignored or REPORT_LONG not in self._handles
        self._retry_index = 0
        self._retry_at = 0.0
        self._ignored = False
        self._clear_expected_departure("device arrival")
        if returning:
            log.info("Device returned pid=0x%04X reason=%s", self.pid, reason)

    def _drain_priority_zero(self, limit: int = 8) -> None:
        """Run urgent commands while a synchronous discovery request is waiting."""
        for _ in range(limit):
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if item[0] != 0:
                try:
                    self._queue.put_nowait(item)
                except queue.Full:
                    if isinstance(item[2], _Ensure):
                        self._ensure_queued.clear()
                    log.critical("Actor queue filled while restoring deferred command pid=0x%04X", self.pid)
                return
            self._handle_command(item[2])
            if self._stop_requested.is_set():
                return

    def _resolve_queued_switches(self, detail: str) -> None:
        while True:
            try:
                _, _, command = self._queue.get_nowait()
            except queue.Empty:
                return
            if isinstance(command, _Switch) and not command.future.done():
                command.future.set_result(SwitchResult(False, detail, time.monotonic()))

    def _select_paths(
        self,
        paths: tuple[HidPathInfo, ...],
        previous: dict[int, HidPathInfo] | None = None,
    ) -> dict[int, HidPathInfo]:
        """Pick the HID++ collections to own, preferring the ones already open.

        Selection is stable: Windows may re-order an enumeration without anything
        relevant having changed, and a reshuffle must never cost us the transport.
        """
        selected: dict[int, HidPathInfo] = {}
        for report_id, usages in ((REPORT_LONG, LONG_USAGES), (REPORT_SHORT, {HIDPP_USAGE_SHORT})):
            candidates = sorted((path for path in paths if path.usage in usages), key=lambda item: item.path)
            if not candidates:
                continue
            keep = previous.get(report_id) if previous else None
            selected[report_id] = next(
                (path for path in candidates if keep is not None and path.path == keep.path),
                candidates[0],
            )
        return selected

    def _connect(self) -> None:
        for device in self._devices.values():
            self._set_switch_capable(device, False)
            self._set_observer_capable(device, False)
            self._transition(device, DeviceState.CONNECTING)
        self._close_handles()
        try:
            selected = self._select_paths(self._paths, self._selected)
            if REPORT_LONG not in selected:
                raise TransportError("long HID++ collection not found")
            if self._receiver and REPORT_SHORT not in selected:
                raise TransportError("receiver short HID++ collection not found")
            for report_id, path in selected.items():
                self._handles[report_id] = self._backend.open(path.path)
            self._selected = selected
            self._retry_index = 0
            self._clear_expected_departure("transport reopened")
            connected_at = time.monotonic()
            for device in self._devices.values():
                self._connected_at[device.identity] = connected_at
                self._ready_logged.discard(device.identity)
                self._observer_logged.discard(device.identity)
            log.info("CONNECTED pid=0x%04X transport=%s", self.pid, "receiver" if self._receiver else "Bluetooth")
            if self._receiver:
                self._backend.write(self._handles[REPORT_SHORT], ENABLE_RECEIVER_NOTIFICATIONS, output_report=False)
                self._backend.write(self._handles[REPORT_SHORT], ENUMERATE_RECEIVER_DEVICES, output_report=False)
            else:
                self._initialize(self._ensure_direct_runtime())
        except Exception as error:
            self._close_handles()
            expected = self._expected_departure()
            delay = self._schedule_retry(expected=expected)
            state = DeviceState.EXPECTED_DISCONNECTED if expected else DeviceState.RECOVERING
            for device in self._devices.values():
                self._transition(device, state, None if expected else str(error))
            if expected:
                log.debug("Transport still away after expected departure pid=0x%04X detail=%s", self.pid, error)
            elif self._receiver and not self._devices and self._retry_index >= len(BACKOFF_SECONDS):
                # A LIGHTSPEED/Unifying dongle that never reports one of our target devices
                # is out of scope for this pair. Stop burning cycles and log lines on it.
                self._ignored = True
                log.info("Receiver pid=0x%04X holds no target device; staying passive (%s)", self.pid, error)
            else:
                log.warning("Connect failed pid=0x%04X retry_in=%.2fs error=%s", self.pid, delay, error)

    def _poll_handles(self) -> None:
        for report_id in (REPORT_LONG, REPORT_SHORT):
            if self._stop_requested.is_set():
                return
            handle = self._handles.get(report_id)
            if handle is None:
                continue
            try:
                raw = self._backend.read(handle, self._read_timeout_ms if report_id == REPORT_LONG else 0)
            except TransportError as error:
                self._mark_transport_lost(str(error))
                return
            if raw:
                self._dispatch(parse_report(raw), raw)

    def _dispatch(self, report, raw: bytes) -> None:
        if report is None:
            log.debug("Ignored malformed/unsupported HID report pid=0x%04X bytes=%s", self.pid, raw.hex())
            return
        if isinstance(report, DeviceConnection):
            self._on_connection(report)
            return
        device = self._device_for_slot(report.slot)
        if device is None:
            log.debug("Report for unknown slot=%d pid=0x%04X", report.slot, self.pid)
            return
        if isinstance(report, HidError):
            if report.sw_id == SW_ID_HOST_CHANGE:
                if self._expected_departure(device):
                    log.debug(
                        "CHANGE_HOST error 0x%02X while %s is leaving; not a cache fault",
                        report.error_code,
                        device.identity,
                    )
                    return
                self._cache.invalidate(device.identity, "CHANGE_HOST protocol error")
                self._set_switch_capable(device, False)
                self._mark_transport_lost(f"CHANGE_HOST HID++ error 0x{report.error_code:02X}")
            return
        if isinstance(report, Notification):
            target = notification_target(report, device)
            if target is not None:
                observed_at = time.monotonic()
                device.last_known_host = target + 1
                if device.role == DeviceRole.MOUSE:
                    device.reverse_notifications_observed = True
                log.info("EasySwitch source=%s target=%d", device.identity, target)
                # The keyboard just told us it is leaving this host. Everything that
                # follows (read failures, path removal, Windows device-removal) is
                # expected, and no further discovery may be started on it.
                self._mark_expected_departure(device, "source", target)
                if device.role is not None:
                    self._controller.submit(HostChange(device.identity, device.role, target, observed_at))
            return
        if isinstance(report, ReportingChanged):
            if report.feature_index != device.feature_indexes.get(FEATURE_REPROG_CONTROLS_V4):
                return
            if not report.compatible:
                now = time.monotonic()
                previous = self._last_rearm.get(device.identity, float("-inf"))
                if now - previous >= self._rearm_throttle_seconds:
                    self._last_rearm[device.identity] = now
                    log.warning(
                        "External reporting flag removal detected for %s CID=0x%04X",
                        device.identity,
                        report.cid,
                    )
                    self._arm(device)

    def _on_connection(self, event: DeviceConnection) -> None:
        if not self._receiver:
            return
        identity = f"receiver:{self.pid:04x}:{event.wpid:04x}"
        device = self._devices.get(identity)
        if not event.connected:
            if device is not None:
                device.switch_capable = False
                self._transition(device, DeviceState.DISCONNECTED, "receiver reports device offline")
            return
        if device is None:
            role = DeviceRole.KEYBOARD if event.device_type == 1 else None
            device = DeviceRuntime(identity, self.pid, event.wpid, event.slot, "receiver", role=role)
            self._devices[identity] = device
        else:
            device.slot = event.slot
        self._by_slot[event.slot] = identity
        self._connected_at[identity] = time.monotonic()
        self._ready_logged.discard(identity)
        self._initialize(device)

    def _initialize(self, device: DeviceRuntime) -> None:
        if REPORT_LONG not in self._handles or self._stop_requested.is_set():
            return
        if self._expected_departure(device):
            log.debug("Discovery deferred; %s is leaving this host", device.identity)
            if device.state != DeviceState.READY:
                # Stay on the run loop's retry list so discovery resumes if the device
                # turns out to stay (e.g. the target host was the current one).
                self._transition(device, DeviceState.EXPECTED_DISCONNECTED)
                self._schedule_retry(expected=True)
            return
        cached = self._cache.get(device.identity, device.wpid)
        if cached:
            old = device.state
            device.transition(DeviceState.INITIALIZING)
            if old != DeviceState.INITIALIZING:
                log.debug("State %s %s -> INITIALIZING", device.identity, old.value)
        else:
            self._transition(device, DeviceState.INITIALIZING)
        if cached:
            try:
                device.role = DeviceRole(cached["role"])
                device.name = cached.get("name")
                device.feature_indexes = {int(k): int(v) for k, v in cached.get("features", {}).items()}
                device.easy_switch_cids = tuple(int(x) for x in cached.get("easy_switch_cids", []))
                device.supported_flags = int(cached.get("supported_flags", 0))
            except (AttributeError, KeyError, TypeError, ValueError):
                self._cache.invalidate(device.identity, "malformed device entry")
                device.feature_indexes = {}
                device.easy_switch_cids = ()
                device.supported_flags = 0
                cached = None
        if cached:
            if self._cached_switch_prerequisites_valid(device):
                self._controller.register(device, self, publish_status=False)
                # Hot path first: with the cache we can already parse x1814 and write
                # CHANGE_HOST. Cosmetic discovery below must not delay either.
                self._refresh_observer_capable(device)
                self._set_switch_capable(device, True)
            else:
                self._cache.invalidate(device.identity, "invalid cached switch prerequisites")
                device.feature_indexes = {}
                device.easy_switch_cids = ()
                device.supported_flags = 0
                device.switch_capable = False
                cached = None

        try:
            type_name_idx = self._resolve_feature(device, FEATURE_DEVICE_TYPE_AND_NAME)
            if type_name_idx is None:
                raise ProtocolError("DEVICE_TYPE_AND_NAME unavailable")
            device.feature_indexes[FEATURE_DEVICE_TYPE_AND_NAME] = type_name_idx
            actual_role = self._get_role(device, type_name_idx)
            if actual_role is None:
                raise ProtocolError("unsupported Logitech device type")
            if cached and device.role != actual_role:
                self._set_switch_capable(device, False)
                self._cache.invalidate(device.identity, "cached device role mismatch")
                device.easy_switch_cids = ()
                device.supported_flags = 0
                cached = None
            device.role = actual_role
            device.name = self._get_name(device, type_name_idx) or device.name
            if not self._is_target_device(device):
                self._set_switch_capable(device, False)
                self._transition(device, DeviceState.DISCONNECTED, f"ignored non-target device: {device.name}")
                if not self._receiver:
                    self._ignored = True
                    self._close_handles()
                return
            self._controller.register(device, self)

            change_host_idx = self._resolve_feature(device, FEATURE_CHANGE_HOST)
            if change_host_idx is None:
                raise ProtocolError("CHANGE_HOST x1814 unavailable")
            if cached and device.feature_indexes.get(FEATURE_CHANGE_HOST) not in (None, change_host_idx):
                self._set_switch_capable(device, False)
                self._cache.invalidate(device.identity, "cached CHANGE_HOST index mismatch")
                device.easy_switch_cids = ()
                device.supported_flags = 0
                cached = None
            device.feature_indexes[FEATURE_CHANGE_HOST] = change_host_idx
            self._refresh_observer_capable(device)
            self._set_switch_capable(device, True)

            if device.role == DeviceRole.KEYBOARD:
                reprog_idx = self._resolve_feature(device, FEATURE_REPROG_CONTROLS_V4)
                if reprog_idx is None:
                    raise ProtocolError("REPROG_CONTROLS_V4 x1B04 unavailable")
                if cached and device.feature_indexes.get(FEATURE_REPROG_CONTROLS_V4) not in (None, reprog_idx):
                    self._cache.invalidate(device.identity, "cached REPROG_CONTROLS_V4 index mismatch")
                    device.easy_switch_cids = ()
                    device.supported_flags = 0
                device.feature_indexes[FEATURE_REPROG_CONTROLS_V4] = reprog_idx
                if not device.easy_switch_cids or not device.supported_flags:
                    device.easy_switch_cids, device.supported_flags = self._discover_cids(device, reprog_idx)
                self._arm(device)
                if device.state != DeviceState.READY:
                    return
            else:
                self._transition(device, DeviceState.READY)
            self._cache.save(device)
            log.info("READY role=%s name=%s identity=%s", device.role.value, device.name, device.identity)
        except (ProtocolError, TransportError, ValueError) as error:
            if self._stop_requested.is_set():
                return
            expected = self._expected_departure(device)
            self._transition(
                device,
                DeviceState.EXPECTED_DISCONNECTED if expected else DeviceState.RECOVERING,
                None if expected else str(error),
            )
            delay = self._schedule_retry(expected=expected)
            if expected:
                log.debug("Discovery abandoned; %s is leaving this host (%s)", device.identity, error)
            else:
                log.warning("EnsureReady failed identity=%s retry_in=%.2fs error=%s", device.identity, delay, error)

    def _resolve_feature(self, device: DeviceRuntime, feature_code: int) -> int | None:
        response = self._request_raw(device, build_get_feature(device.slot, feature_code), 0, 0, SW_ID_REQUEST)
        return response[0] if response and response[0] else None

    def _get_role(self, device: DeviceRuntime, feature_index: int) -> DeviceRole | None:
        response = self._request(device, feature_index, 0x20)
        if not response:
            return None
        if response[0] == DEVICE_TYPE_KEYBOARD:
            return DeviceRole.KEYBOARD
        if response[0] in (DEVICE_TYPE_MOUSE, DEVICE_TYPE_TRACKPAD, DEVICE_TYPE_TRACKBALL):
            return DeviceRole.MOUSE
        return None

    def _get_name(self, device: DeviceRuntime, feature_index: int) -> str | None:
        response = self._request(device, feature_index, 0x00)
        if not response or response[0] == 0:
            return None
        length = response[0]
        value = bytearray()
        while len(value) < length:
            self._raise_if_stopping()
            response = self._request(device, feature_index, 0x10, bytes((len(value),)))
            if not response:
                break
            value.extend(response[: length - len(value)])
        return bytes(value).decode("utf-8", errors="replace").rstrip("\x00") or None

    def _discover_cids(self, device: DeviceRuntime, feature_index: int) -> tuple[tuple[int, ...], int]:
        response = self._request(device, feature_index, 0x00)
        if not response:
            raise ProtocolError("getCidCount timed out")
        found: list[int] = []
        supported = 0
        for index in range(response[0]):
            self._raise_if_stopping()
            info = self._request(device, feature_index, 0x10, bytes((index,)))
            if not info or len(info) < 5:
                continue
            cid = (info[0] << 8) | info[1]
            if cid not in HOST_SWITCH_CIDS:
                continue
            flags = info[4]
            if flags & KEY_FLAG_ANALYTICS:
                supported |= KEY_FLAG_ANALYTICS
            if flags & KEY_FLAG_DIVERTABLE:
                supported |= KEY_FLAG_DIVERTABLE
            if flags & KEY_FLAG_PERSISTENTLY_DIVERTABLE:
                supported |= KEY_FLAG_PERSISTENTLY_DIVERTABLE
            found.append(cid)
        if not found or not supported & (KEY_FLAG_ANALYTICS | KEY_FLAG_DIVERTABLE):
            raise ProtocolError("no usable Easy-Switch CIDs")
        log.debug("Easy-Switch CIDs identity=%s cids=%s flags=0x%02X", device.identity, found, supported)
        return tuple(found), supported

    def _arm(self, device: DeviceRuntime) -> None:
        if self._expected_departure(device):
            log.debug("Re-arming skipped; %s is leaving this host", device.identity)
            self._transition(device, DeviceState.EXPECTED_DISCONNECTED)
            self._schedule_retry(expected=True)
            return
        self._transition(device, DeviceState.ARMING)
        feature_index = device.feature_indexes.get(FEATURE_REPROG_CONTROLS_V4)
        if feature_index is None or not device.easy_switch_cids:
            self._transition(device, DeviceState.RECOVERING, "arming prerequisites missing")
            return
        try:
            for cid in device.easy_switch_cids:
                self._raise_if_stopping()
                message = build_cid_reporting(device.slot, feature_index, cid, device.supported_flags)
                response = self._request_raw(device, message, feature_index, 0x30, SW_ID_DIVERT)
                if response is None:
                    raise ProtocolError(f"no ACK arming CID 0x{cid:04X}")
            self._transition(device, DeviceState.READY)
        except (ProtocolError, TransportError) as error:
            if self._stop_requested.is_set():
                return
            expected = self._expected_departure(device)
            self._transition(
                device,
                DeviceState.EXPECTED_DISCONNECTED if expected else DeviceState.RECOVERING,
                None if expected else str(error),
            )
            self._schedule_retry(expected=expected)

    def _request(
        self,
        device: DeviceRuntime,
        feature_index: int,
        function: int,
        params: bytes = b"",
    ) -> bytes | None:
        message = build_message(device.slot, feature_index, function, params, sw_id=SW_ID_REQUEST)
        return self._request_raw(device, message, feature_index, function, SW_ID_REQUEST)

    def _request_raw(
        self,
        device: DeviceRuntime,
        message: bytes,
        feature_index: int,
        function: int,
        sw_id: int,
    ) -> bytes | None:
        self._raise_if_stopping()
        self._drain_priority_zero()
        self._raise_if_stopping()
        self._raise_if_departing(device)
        handle = self._handles.get(REPORT_LONG)
        if handle is None:
            raise TransportError("long transport unavailable")
        self._backend.write(handle, message, output_report=not self._receiver)
        deadline = time.monotonic() + self._request_timeout_ms / 1000
        while not self._stop_requested.is_set() and time.monotonic() < deadline:
            self._drain_priority_zero()
            self._raise_if_stopping()
            # An Easy-Switch notification may have been dispatched from the read below.
            # Stop talking to a device that just announced it is leaving this host.
            self._raise_if_departing(device)
            if self._handles.get(REPORT_LONG) is not handle:
                raise TransportError("transport changed during request")
            raw = self._backend.read(handle, min(self._read_timeout_ms, 25))
            if not raw:
                continue
            report = parse_report(raw)
            if response_matches(report, device.slot, feature_index, function, sw_id):
                if isinstance(report, HidError):
                    raise ProtocolError(f"HID++ error 0x{report.error_code:02X}")
                return report.payload if isinstance(report, Response) else None
            self._dispatch(report, raw)
        self._raise_if_stopping()
        return None

    def _perform_switch(self, command: _Switch) -> None:
        if self._stop_requested.is_set():
            command.future.set_result(SwitchResult(False, "actor stopping", time.monotonic()))
            return
        device = self._devices.get(command.identity)
        if device is None or not device.switch_capable or REPORT_LONG not in self._handles:
            command.future.set_result(SwitchResult(False, "peer not switch-capable", time.monotonic()))
            return
        feature_index = device.feature_indexes.get(FEATURE_CHANGE_HOST)
        if feature_index is None:
            command.future.set_result(SwitchResult(False, "CHANGE_HOST unavailable", time.monotonic()))
            return
        # Do not run status persistence before the P0 write. The controller records the
        # post-write state after the future completes, keeping disk I/O off the hot path.
        previous_state = device.state
        device.transition(DeviceState.SWITCHING)
        log.debug("State %s %s -> SWITCHING", device.identity, previous_state.value)
        try:
            handle = self._handles[REPORT_LONG]
            message = build_change_host(device.slot, feature_index, command.target_host)
            self._backend.write(handle, message, output_report=not self._receiver)
            wrote_at = time.monotonic()
            device.last_known_host = command.target_host + 1
            # CHANGE_HOST is fire-and-forget. A reply cannot be required after the peer leaves this host.
            device.transition(previous_state)
            command.future.set_result(SwitchResult(True, "write completed", wrote_at))
            # The mouse now leaves this host too. Off the hot path, after the future.
            self._mark_expected_departure(device, "peer", command.target_host)
        except (KeyError, TransportError, ValueError) as error:
            wrote_at = time.monotonic()
            expected = self._expected_departure(device)
            self._set_switch_capable(device, False)
            if not expected:
                self._cache.invalidate(device.identity, "CHANGE_HOST write failed")
            command.future.set_result(SwitchResult(False, str(error), wrote_at))
            self._mark_transport_lost(str(error))

    def _mark_transport_lost(self, reason: str, *, bump_backoff: bool = True) -> None:
        expected = self._expected_departure()
        self._close_handles()
        state = DeviceState.EXPECTED_DISCONNECTED if expected else DeviceState.RECOVERING
        for device in self._devices.values():
            self._set_switch_capable(device, False)
            self._set_observer_capable(device, False)
            self._transition(device, state, None if expected else reason)
        if self._stop_requested.is_set():
            return
        if expected:
            # Nothing is broken: the device left on purpose. Poll back quickly and keep
            # the failure history untouched so its return costs no backoff.
            self._retry_at = time.monotonic() + EXPECTED_DEPARTURE_RETRY_SECONDS
            log.debug("Expected disconnect pid=0x%04X detail=%s", self.pid, reason)
            return
        if not bump_backoff:
            self._retry_at = 0.0
            log.info("Transport reset pid=0x%04X reason=%s", self.pid, reason)
            return
        delay = BACKOFF_SECONDS[min(self._retry_index, len(BACKOFF_SECONDS) - 1)]
        self._retry_index += 1
        self._retry_at = time.monotonic() + delay
        log.warning("Transport lost pid=0x%04X retry_in=%.2fs error=%s", self.pid, delay, reason)

    def _schedule_retry(self, *, expected: bool) -> float:
        if expected:
            self._retry_at = time.monotonic() + EXPECTED_DEPARTURE_RETRY_SECONDS
            return EXPECTED_DEPARTURE_RETRY_SECONDS
        delay = BACKOFF_SECONDS[min(self._retry_index, len(BACKOFF_SECONDS) - 1)]
        self._retry_index += 1
        self._retry_at = time.monotonic() + delay
        return delay

    def _mark_expected_departure(self, device: DeviceRuntime, kind: str, target_host: int) -> None:
        deadline = time.monotonic() + self._expected_departure_seconds
        device.expected_departure_until = deadline
        self._expected_departure_until = max(self._expected_departure_until, deadline)
        log.info("Expected departure %s=%s target=%d", kind, device.identity, target_host)

    def _expected_departure(self, device: DeviceRuntime | None = None) -> bool:
        deadline = device.expected_departure_until if device is not None else self._expected_departure_until
        return time.monotonic() < deadline

    def _clear_expected_departure(self, reason: str) -> None:
        pending = self._expected_departure_until > 0.0 or any(
            device.expected_departure_until > 0.0 for device in self._devices.values()
        )
        if not pending:
            return
        self._expected_departure_until = 0.0
        for device in self._devices.values():
            device.expected_departure_until = 0.0
        log.debug("Expected departure cleared pid=0x%04X reason=%s", self.pid, reason)

    def _close_handles(self) -> None:
        for handle in list(self._handles.values()):
            try:
                self._backend.close(handle)
            except Exception:
                log.exception("Failed closing owned HID handle pid=0x%04X", self.pid)
        self._handles.clear()

    def _transition(self, device: DeviceRuntime, state: DeviceState, error: str | None = None) -> None:
        old = device.state
        device.transition(state, error)
        if old != state:
            log.debug("State %s %s -> %s", device.identity, old.value, state.value)
            if (
                state == DeviceState.READY
                and device.identity in self._connected_at
                and device.identity not in self._ready_logged
            ):
                elapsed_ms = (time.monotonic() - self._connected_at[device.identity]) * 1000
                log.info("connected_to_ready_ms=%.2f identity=%s", elapsed_ms, device.identity)
                self._ready_logged.add(device.identity)
        self._controller.state_changed(device)

    def _set_switch_capable(self, device: DeviceRuntime, capable: bool) -> None:
        changed = device.switch_capable != capable
        device.switch_capable = capable
        if capable and changed:
            started = self._connected_at.get(device.identity)
            elapsed_ms = (time.monotonic() - started) * 1000 if started is not None else 0.0
            log.info("connected_to_switch_capable_ms=%.2f identity=%s", elapsed_ms, device.identity)
        if changed:
            self._controller.capability_changed(device)

    def _refresh_observer_capable(self, device: DeviceRuntime) -> None:
        self._set_observer_capable(
            device,
            REPORT_LONG in self._handles
            and device.role is not None
            and isinstance(device.feature_indexes.get(FEATURE_CHANGE_HOST), int),
        )

    def _set_observer_capable(self, device: DeviceRuntime, capable: bool) -> None:
        if device.observer_capable == capable:
            return
        device.observer_capable = capable
        if not capable or device.identity in self._observer_logged:
            return
        self._observer_logged.add(device.identity)
        started = self._connected_at.get(device.identity)
        elapsed_ms = (time.monotonic() - started) * 1000 if started is not None else 0.0
        log.info("connected_to_observer_capable_ms=%.2f identity=%s", elapsed_ms, device.identity)

    @staticmethod
    def _cached_switch_prerequisites_valid(device: DeviceRuntime) -> bool:
        feature_index = device.feature_indexes.get(FEATURE_CHANGE_HOST)
        return (
            device.role in (DeviceRole.KEYBOARD, DeviceRole.MOUSE)
            and TransportActor._is_target_device(device)
            and isinstance(feature_index, int)
            and 0 < feature_index <= 0xFF
        )

    def _raise_if_stopping(self) -> None:
        if self._stop_requested.is_set():
            raise TransportError("actor stopping")

    def _raise_if_departing(self, device: DeviceRuntime) -> None:
        if self._expected_departure(device):
            raise TransportError("device is leaving this host")

    def _device_for_slot(self, slot: int) -> DeviceRuntime | None:
        if not self._receiver:
            return next(iter(self._devices.values()), None)
        identity = self._by_slot.get(slot) or self._by_slot.get(slot ^ 0xFF)
        return self._devices.get(identity) if identity else None

    def _ensure_direct_runtime(self) -> DeviceRuntime:
        path = self._paths[0] if self._paths else None
        serial = path.serial if path else None
        product = path.product if path else None
        suffix = serial or product or f"pid-{self.pid:04x}"
        identity = f"bluetooth:{self.pid:04x}:{suffix}"
        existing = self._devices.get(identity)
        if existing is not None:
            return existing
        device = DeviceRuntime(identity, self.pid, self.pid, DIRECT_DEVICE_SLOT, "Bluetooth")
        self._devices[identity] = device
        self._by_slot[DIRECT_DEVICE_SLOT] = identity
        return device

    @staticmethod
    def _is_target_device(device: DeviceRuntime) -> bool:
        name = (device.name or "").casefold()
        if device.role == DeviceRole.KEYBOARD:
            return "mx keys" in name and "mini" not in name
        return device.role == DeviceRole.MOUSE and "mx anywhere 3s" in name
