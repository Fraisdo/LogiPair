from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import Future, TimeoutError
from dataclasses import dataclass
from typing import Protocol

from .constants import VALID_HOSTS
from .model import DeviceRole, DeviceRuntime, DeviceState, HostChange, PairHealth, SwitchResult

log = logging.getLogger(__name__)


class SwitchTarget(Protocol):
    def switch(self, identity: str, target_host: int, observed_at: float) -> Future[SwitchResult]: ...

    def ensure_ready(self, reason: str) -> None: ...


@dataclass(frozen=True)
class _PeerProgress:
    role: DeviceRole


@dataclass(frozen=True)
class _PendingSwitch:
    event: HostChange
    expires_at: float
    attempts: int = 0


class PairController(threading.Thread):
    """Serializes the one hot path: source notification -> peer write."""

    def __init__(
        self,
        status_changed,
        *,
        debounce_seconds: float = 0.4,
        pending_ttl_seconds: float = 3.0,
    ) -> None:
        super().__init__(name="PairController", daemon=True)
        self._queue: queue.Queue[HostChange | _PeerProgress | None] = queue.Queue(maxsize=4096)
        self._lock = threading.RLock()
        self._devices: dict[str, tuple[DeviceRuntime, SwitchTarget]] = {}
        self._by_role: dict[DeviceRole, str] = {}
        self._debounce_seconds = debounce_seconds
        self._pending_ttl_seconds = pending_ttl_seconds
        self._last_event: tuple[str, int, float] | None = None
        self._pending: dict[DeviceRole, _PendingSwitch] = {}
        self._progress_queued: set[DeviceRole] = set()
        self._stop_requested = threading.Event()
        self._health = PairHealth.DEGRADED
        self._status_changed = status_changed

    @property
    def health(self) -> PairHealth:
        with self._lock:
            return self._health

    def devices(self) -> list[DeviceRuntime]:
        with self._lock:
            return [entry[0] for entry in self._devices.values()]

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def register(self, device: DeviceRuntime, target: SwitchTarget, *, publish_status: bool = True) -> None:
        if device.role is None:
            return
        with self._lock:
            existing = self._by_role.get(device.role)
            if existing and existing != device.identity:
                old = self._devices[existing][0]
                if old.state == DeviceState.READY and device.state != DeviceState.READY:
                    return
            self._devices[device.identity] = (device, target)
            self._by_role[device.role] = device.identity
        if publish_status:
            self.state_changed(device)
        else:
            self._notify_progress(device.role)

    def capability_changed(self, device: DeviceRuntime) -> None:
        if device.role is not None:
            self._notify_progress(device.role)

    def unregister(self, identity: str) -> None:
        with self._lock:
            entry = self._devices.pop(identity, None)
            if entry is not None and entry[0].role is not None and self._by_role.get(entry[0].role) == identity:
                self._by_role.pop(entry[0].role, None)

    def state_changed(self, device: DeviceRuntime) -> None:
        with self._lock:
            if device.role is not None and device.identity in self._devices:
                self._by_role[device.role] = device.identity
            old_health = self._health
            selected = [self._selected(role) for role in (DeviceRole.KEYBOARD, DeviceRole.MOUSE)]
            if all(item is not None and item.state == DeviceState.READY for item in selected):
                self._health = PairHealth.PAIR_READY
            elif any(
                item is not None
                and item.state
                in {
                    DeviceState.CONNECTING,
                    DeviceState.INITIALIZING,
                    DeviceState.ARMING,
                    DeviceState.RECOVERING,
                    DeviceState.SWITCHING,
                    # An expected Easy-Switch departure is transient, not a degradation.
                    DeviceState.EXPECTED_DISCONNECTED,
                }
                for item in selected
            ):
                self._health = PairHealth.RECOVERING
            else:
                self._health = PairHealth.DEGRADED
            health = self._health
            devices = [entry[0] for entry in self._devices.values()]
        if health != old_health:
            log.info("Pair health %s -> %s", old_health.value, health.value)
        if device.role is not None:
            self._notify_progress(device.role)
        self._status_changed(health, devices)

    def submit(self, event: HostChange) -> None:
        if not self._stop_requested.is_set():
            self._put(event)

    def stop(self) -> None:
        self._stop_requested.set()
        self._put(None)

    def run(self) -> None:
        while not self._stop_requested.is_set():
            try:
                command = self._queue.get(timeout=self._pending_timeout())
            except queue.Empty:
                self._expire_pending()
                continue
            if command is None:
                break
            try:
                if isinstance(command, _PeerProgress):
                    with self._lock:
                        self._progress_queued.discard(command.role)
                    self._try_pending(command.role)
                else:
                    self._switch_pair(command)
            except Exception:
                log.exception("Unexpected PairController failure")
            self._expire_pending()

    def _switch_pair(self, event: HostChange) -> None:
        if event.target_host not in VALID_HOSTS:
            log.warning("Rejected invalid target host %s from %s", event.target_host, event.source_identity)
            return
        now = time.monotonic()
        previous = self._last_event
        if (
            previous is not None
            and previous[0:2] == (event.source_identity, event.target_host)
            and now - previous[2] < self._debounce_seconds
        ):
            log.debug("Duplicate switch suppressed source=%s target=%s", event.source_identity, event.target_host)
            return
        self._last_event = (event.source_identity, event.target_host, now)

        peer_role = DeviceRole.MOUSE if event.source_role == DeviceRole.KEYBOARD else DeviceRole.KEYBOARD
        with self._lock:
            source = self._devices.get(event.source_identity)
            peer_identity = self._by_role.get(peer_role)
            peer_entry = self._devices.get(peer_identity) if peer_identity else None
        if source is None:
            log.warning("Ignoring switch from unknown source=%s", event.source_identity)
            return
        if peer_entry is None:
            log.warning(
                "Switch source=%s target=%d peer=%s result=pending-missing",
                event.source_identity,
                event.target_host,
                peer_role.value,
            )
            self._remember_pending(peer_role, event)
            self.state_changed(source[0])
            return

        peer, actor = peer_entry
        if peer.identity == event.source_identity:
            raise AssertionError("source exclusion invariant violated")
        if not peer.switch_capable:
            log.warning(
                "Switch source=%s target=%d peer=%s result=pending state=%s",
                event.source_identity,
                event.target_host,
                peer.identity,
                peer.state.value,
            )
            self._remember_pending(peer_role, event)
            actor.ensure_ready("switch requested while peer not READY")
            self.state_changed(peer)
            return

        if not self._execute_switch(event, peer, actor):
            self._remember_pending(peer_role, event, attempts=1)

    def _execute_switch(self, event: HostChange, peer: DeviceRuntime, actor: SwitchTarget) -> bool:
        if peer.identity == event.source_identity:
            raise AssertionError("source exclusion invariant violated")

        queued_at = time.monotonic()
        future = actor.switch(peer.identity, event.target_host, event.observed_at)
        try:
            result = future.result(timeout=2.0)
        except TimeoutError:
            log.error(
                "Switch source=%s target=%d peer=%s result=timeout",
                event.source_identity,
                event.target_host,
                peer.identity,
            )
            peer.switch_capable = False
            peer.transition(DeviceState.RECOVERING, "switch write timeout")
            actor.ensure_ready("switch timeout")
            self.state_changed(peer)
            return False

        event_to_write_ms = (result.write_at - event.observed_at) * 1000
        enqueue_ms = (queued_at - event.observed_at) * 1000
        log.info(
            "Switch source=%s target=%d peer=%s event_to_enqueue_ms=%.2f event_to_write_ms=%.2f result=%s detail=%s",
            event.source_identity,
            event.target_host,
            peer.identity,
            enqueue_ms,
            event_to_write_ms,
            "ok" if result.ok else "failed",
            result.detail,
        )
        if not result.ok:
            peer.switch_capable = False
            peer.transition(DeviceState.RECOVERING, result.detail)
            actor.ensure_ready("peer write failed")
        self.state_changed(peer)
        return result.ok

    def _remember_pending(self, peer_role: DeviceRole, event: HostChange, *, attempts: int = 0) -> None:
        with self._lock:
            current = self._pending.get(peer_role)
            if current is not None and (
                current.event.source_identity,
                current.event.target_host,
            ) == (event.source_identity, event.target_host):
                return
            self._pending[peer_role] = _PendingSwitch(
                event,
                event.observed_at + self._pending_ttl_seconds,
                attempts,
            )
        if current is not None:
            log.info(
                "Pending switch replaced peer=%s old_target=%d new_target=%d",
                peer_role.value,
                current.event.target_host,
                event.target_host,
            )
        self._notify_progress(peer_role)

    def _try_pending(self, peer_role: DeviceRole) -> None:
        with self._lock:
            pending = self._pending.get(peer_role)
            peer_identity = self._by_role.get(peer_role)
            peer_entry = self._devices.get(peer_identity) if peer_identity else None
            if pending is None or peer_entry is None or not peer_entry[0].switch_capable:
                return
            if time.monotonic() >= pending.expires_at:
                self._pending.pop(peer_role, None)
                log.warning("Pending switch expired peer=%s target=%d", peer_role.value, pending.event.target_host)
                return
            if pending.attempts >= 2:
                return
            self._pending.pop(peer_role, None)
        peer, actor = peer_entry
        if not self._execute_switch(pending.event, peer, actor):
            with self._lock:
                current = self._pending.get(peer_role)
                if current is None and time.monotonic() < pending.expires_at:
                    self._pending[peer_role] = _PendingSwitch(
                        pending.event,
                        pending.expires_at,
                        pending.attempts + 1,
                    )
            actor.ensure_ready("pending switch attempt failed")

    def _expire_pending(self) -> None:
        now = time.monotonic()
        with self._lock:
            expired = [(role, item) for role, item in self._pending.items() if now >= item.expires_at]
            for role, _ in expired:
                self._pending.pop(role, None)
        for role, item in expired:
            log.warning("Pending switch expired peer=%s target=%d", role.value, item.event.target_host)

    def _pending_timeout(self) -> float:
        with self._lock:
            if not self._pending:
                return 0.25
            deadline = min(item.expires_at for item in self._pending.values())
        return max(0.0, min(0.25, deadline - time.monotonic()))

    def _notify_progress(self, role: DeviceRole) -> None:
        with self._lock:
            if role not in self._pending or role in self._progress_queued:
                return
            self._progress_queued.add(role)
        if not self._put(_PeerProgress(role)):
            with self._lock:
                self._progress_queued.discard(role)

    def _put(self, command: HostChange | _PeerProgress | None) -> bool:
        try:
            self._queue.put_nowait(command)
            return True
        except queue.Full:
            try:
                dropped = self._queue.get_nowait()
                if isinstance(dropped, _PeerProgress):
                    with self._lock:
                        self._progress_queued.discard(dropped.role)
                log.error("Controller queue full; discarded oldest command type=%s", type(dropped).__name__)
                self._queue.put_nowait(command)
                return True
            except (queue.Empty, queue.Full):
                log.critical("Controller queue remained full; command rejected type=%s", type(command).__name__)
                return False

    def _selected(self, role: DeviceRole) -> DeviceRuntime | None:
        identity = self._by_role.get(role)
        entry = self._devices.get(identity) if identity else None
        return entry[0] if entry else None
