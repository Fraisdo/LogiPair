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
    FEATURE_CHANGE_HOST,
    FEATURE_DEVICE_TYPE_AND_NAME,
    FEATURE_REPROG_CONTROLS_V4,
    HOST_SWITCH_CIDS,
    KEY_FLAG_ANALYTICS,
    KEY_FLAG_DIVERTABLE,
    KEY_FLAG_PERSISTENTLY_DIVERTABLE,
    LONG_USAGES,
    RECEIVER_PIDS,
    REPORT_LONG,
    REPORT_SHORT,
    SW_ID_DIVERT,
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
        self._queue: queue.PriorityQueue[tuple[int, int, object]] = queue.PriorityQueue()
        self._sequence = itertools.count()
        self._handles: dict[int, Any] = {}
        self._devices: dict[str, DeviceRuntime] = {}
        self._by_slot: dict[int, str] = {}
        self._stopping = False
        self._retry_index = 0
        self._retry_at = 0.0
        self._last_rearm: dict[str, float] = {}
        self._receiver = pid in RECEIVER_PIDS
        self._ignored = False
        if not self._receiver:
            self._ensure_direct_runtime()

    def update_paths(self, paths: list[HidPathInfo], reason: str, *, force: bool = False) -> None:
        self._put(0, _Reconcile(tuple(paths), reason, force))

    def recover(self, reason: str) -> None:
        self._put(0, _Reconcile(self._paths, reason, True))

    def ensure_ready(self, reason: str) -> None:
        self._put(1, _Ensure(reason))

    def switch(self, identity: str, target_host: int, observed_at: float) -> Future[SwitchResult]:
        future: Future[SwitchResult] = Future()
        self._put(0, _Switch(identity, target_host, observed_at, future))
        return future

    def stop(self) -> None:
        self._put(0, _Stop())

    def run(self) -> None:
        try:
            while not self._stopping:
                self._drain_commands(limit=8)
                if self._stopping:
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
                        if device.state == DeviceState.RECOVERING:
                            self._initialize(device)
        except Exception:
            log.exception("Actor pid=0x%04X terminated unexpectedly", self.pid)
            self._mark_transport_lost("actor failure")
        finally:
            self._close_handles()

    def _put(self, priority: int, command: object) -> None:
        self._queue.put((priority, next(self._sequence), command))

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
            self._stopping = True
            return
        if isinstance(command, _Reconcile):
            changed = {path.path for path in command.paths} != {path.path for path in self._paths}
            self._paths = command.paths
            if changed or command.force:
                self._ignored = False
                log.info("HID paths changed pid=0x%04X reason=%s", self.pid, command.reason)
                self._mark_transport_lost("HID path changed")
                self._retry_at = 0.0
            return
        if isinstance(command, _Ensure):
            log.debug("EnsureReady pid=0x%04X reason=%s", self.pid, command.reason)
            if REPORT_LONG not in self._handles:
                self._retry_at = 0.0
            else:
                for device in list(self._devices.values()):
                    if device.state != DeviceState.READY:
                        self._initialize(device)
            return
        if isinstance(command, _Switch):
            self._perform_switch(command)

    def _connect(self) -> None:
        for device in self._devices.values():
            self._transition(device, DeviceState.CONNECTING)
        self._close_handles()
        try:
            selected: dict[int, HidPathInfo] = {}
            for path in self._paths:
                if path.usage in LONG_USAGES and REPORT_LONG not in selected:
                    selected[REPORT_LONG] = path
                elif path.usage == 1 and REPORT_SHORT not in selected:
                    selected[REPORT_SHORT] = path
            if REPORT_LONG not in selected:
                raise TransportError("long HID++ collection not found")
            if self._receiver and REPORT_SHORT not in selected:
                raise TransportError("receiver short HID++ collection not found")
            for report_id, path in selected.items():
                self._handles[report_id] = self._backend.open(path.path)
            self._retry_index = 0
            log.info("CONNECTED pid=0x%04X transport=%s", self.pid, "receiver" if self._receiver else "Bluetooth")
            if self._receiver:
                self._backend.write(self._handles[REPORT_SHORT], ENABLE_RECEIVER_NOTIFICATIONS, output_report=False)
                self._backend.write(self._handles[REPORT_SHORT], ENUMERATE_RECEIVER_DEVICES, output_report=False)
            else:
                self._initialize(self._ensure_direct_runtime())
        except Exception as error:
            self._close_handles()
            delay = BACKOFF_SECONDS[min(self._retry_index, len(BACKOFF_SECONDS) - 1)]
            self._retry_index += 1
            self._retry_at = time.monotonic() + delay
            for device in self._devices.values():
                self._transition(device, DeviceState.RECOVERING, str(error))
            log.warning("Connect failed pid=0x%04X retry_in=%.2fs error=%s", self.pid, delay, error)

    def _poll_handles(self) -> None:
        for report_id in (REPORT_LONG, REPORT_SHORT):
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
        if isinstance(report, Notification):
            target = notification_target(report, device)
            if target is not None:
                observed_at = time.monotonic()
                device.last_known_host = target + 1
                if device.role == DeviceRole.MOUSE:
                    device.reverse_notifications_observed = True
                log.info("EasySwitch source=%s target=%d", device.identity, target)
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
                self._transition(device, DeviceState.DISCONNECTED, "receiver reports device offline")
            return
        if device is None:
            role = DeviceRole.KEYBOARD if event.device_type == 1 else None
            device = DeviceRuntime(identity, self.pid, event.wpid, event.slot, "receiver", role=role)
            self._devices[identity] = device
        else:
            device.slot = event.slot
        self._by_slot[event.slot] = identity
        self._initialize(device)

    def _initialize(self, device: DeviceRuntime) -> None:
        if REPORT_LONG not in self._handles or self._stopping:
            return
        self._transition(device, DeviceState.INITIALIZING)
        cached = self._cache.get(device.identity, device.wpid)
        if cached:
            try:
                device.role = DeviceRole(cached["role"])
                device.name = cached.get("name")
                device.feature_indexes = {int(k): int(v) for k, v in cached.get("features", {}).items()}
                device.easy_switch_cids = tuple(int(x) for x in cached.get("easy_switch_cids", []))
                device.supported_flags = int(cached.get("supported_flags", 0))
            except (KeyError, TypeError, ValueError):
                self._cache.invalidate(device.identity, "malformed device entry")
                device.feature_indexes = {}
                device.easy_switch_cids = ()
                device.supported_flags = 0
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
                self._cache.invalidate(device.identity, "cached device role mismatch")
                device.easy_switch_cids = ()
                device.supported_flags = 0
                cached = None
            device.role = actual_role
            device.name = self._get_name(device, type_name_idx) or device.name
            if not self._is_target_device(device):
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
                self._cache.invalidate(device.identity, "cached CHANGE_HOST index mismatch")
                device.easy_switch_cids = ()
                device.supported_flags = 0
                cached = None
            device.feature_indexes[FEATURE_CHANGE_HOST] = change_host_idx

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
            self._transition(device, DeviceState.RECOVERING, str(error))
            delay = BACKOFF_SECONDS[min(self._retry_index, len(BACKOFF_SECONDS) - 1)]
            self._retry_index += 1
            self._retry_at = time.monotonic() + delay
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
        self._transition(device, DeviceState.ARMING)
        feature_index = device.feature_indexes.get(FEATURE_REPROG_CONTROLS_V4)
        if feature_index is None or not device.easy_switch_cids:
            self._transition(device, DeviceState.RECOVERING, "arming prerequisites missing")
            return
        try:
            for cid in device.easy_switch_cids:
                message = build_cid_reporting(device.slot, feature_index, cid, device.supported_flags)
                response = self._request_raw(device, message, feature_index, 0x30, SW_ID_DIVERT)
                if response is None:
                    raise ProtocolError(f"no ACK arming CID 0x{cid:04X}")
            self._transition(device, DeviceState.READY)
        except (ProtocolError, TransportError) as error:
            self._transition(device, DeviceState.RECOVERING, str(error))
            delay = BACKOFF_SECONDS[min(self._retry_index, len(BACKOFF_SECONDS) - 1)]
            self._retry_index += 1
            self._retry_at = time.monotonic() + delay

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
        handle = self._handles.get(REPORT_LONG)
        if handle is None:
            raise TransportError("long transport unavailable")
        self._backend.write(handle, message, output_report=not self._receiver)
        deadline = time.monotonic() + self._request_timeout_ms / 1000
        while time.monotonic() < deadline:
            raw = self._backend.read(handle, min(self._read_timeout_ms, 25))
            if not raw:
                continue
            report = parse_report(raw)
            if response_matches(report, device.slot, feature_index, function, sw_id):
                if isinstance(report, HidError):
                    raise ProtocolError(f"HID++ error 0x{report.error_code:02X}")
                return report.payload if isinstance(report, Response) else None
            self._dispatch(report, raw)
        return None

    def _perform_switch(self, command: _Switch) -> None:
        device = self._devices.get(command.identity)
        if device is None or device.state != DeviceState.READY:
            command.future.set_result(SwitchResult(False, "peer not READY", time.monotonic()))
            return
        feature_index = device.feature_indexes.get(FEATURE_CHANGE_HOST)
        if feature_index is None:
            command.future.set_result(SwitchResult(False, "CHANGE_HOST unavailable", time.monotonic()))
            return
        # Do not run status persistence before the P0 write. The controller records the
        # post-write state after the future completes, keeping disk I/O off the hot path.
        device.transition(DeviceState.SWITCHING)
        log.debug("State %s READY -> SWITCHING", device.identity)
        try:
            handle = self._handles[REPORT_LONG]
            message = build_change_host(device.slot, feature_index, command.target_host)
            self._backend.write(handle, message, output_report=not self._receiver)
            wrote_at = time.monotonic()
            device.last_known_host = command.target_host + 1
            # CHANGE_HOST is fire-and-forget. A reply cannot be required after the peer leaves this host.
            device.transition(DeviceState.READY)
            command.future.set_result(SwitchResult(True, "write completed", wrote_at))
        except (KeyError, TransportError, ValueError) as error:
            wrote_at = time.monotonic()
            self._transition(device, DeviceState.RECOVERING, str(error))
            command.future.set_result(SwitchResult(False, str(error), wrote_at))
            self._mark_transport_lost(str(error))

    def _mark_transport_lost(self, reason: str) -> None:
        self._close_handles()
        for device in self._devices.values():
            self._transition(device, DeviceState.RECOVERING, reason)
        delay = BACKOFF_SECONDS[min(self._retry_index, len(BACKOFF_SECONDS) - 1)]
        self._retry_index += 1
        self._retry_at = time.monotonic() + delay
        log.warning("Transport lost pid=0x%04X retry_in=%.2fs error=%s", self.pid, delay, reason)

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
        self._controller.state_changed(device)

    def _device_for_slot(self, slot: int) -> DeviceRuntime | None:
        if not self._receiver:
            return next(iter(self._devices.values()), None)
        identity = self._by_slot.get(slot)
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
