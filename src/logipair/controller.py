from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import Future, TimeoutError
from typing import Protocol

from .constants import VALID_HOSTS
from .model import DeviceRole, DeviceRuntime, DeviceState, HostChange, PairHealth, SwitchResult

log = logging.getLogger(__name__)


class SwitchTarget(Protocol):
    def switch(self, identity: str, target_host: int, observed_at: float) -> Future[SwitchResult]: ...

    def ensure_ready(self, reason: str) -> None: ...


class PairController(threading.Thread):
    """Serializes the one hot path: source notification -> peer write."""

    def __init__(self, status_changed, *, debounce_seconds: float = 0.4) -> None:
        super().__init__(name="PairController", daemon=True)
        self._queue: queue.Queue[HostChange | None] = queue.Queue()
        self._lock = threading.RLock()
        self._devices: dict[str, tuple[DeviceRuntime, SwitchTarget]] = {}
        self._by_role: dict[DeviceRole, str] = {}
        self._debounce_seconds = debounce_seconds
        self._last_events: dict[tuple[str, int], float] = {}
        self._health = PairHealth.DEGRADED
        self._status_changed = status_changed

    @property
    def health(self) -> PairHealth:
        with self._lock:
            return self._health

    def devices(self) -> list[DeviceRuntime]:
        with self._lock:
            return [entry[0] for entry in self._devices.values()]

    def register(self, device: DeviceRuntime, target: SwitchTarget) -> None:
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
        self.state_changed(device)

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
        self._status_changed(health, devices)

    def submit(self, event: HostChange) -> None:
        self._queue.put(event)

    def stop(self) -> None:
        self._queue.put(None)

    def run(self) -> None:
        while True:
            event = self._queue.get()
            if event is None:
                return
            try:
                self._switch_pair(event)
            except Exception:
                log.exception("Unexpected PairController failure")

    def _switch_pair(self, event: HostChange) -> None:
        if event.target_host not in VALID_HOSTS:
            log.warning("Rejected invalid target host %s from %s", event.target_host, event.source_identity)
            return
        now = time.monotonic()
        duplicate_key = (event.source_identity, event.target_host)
        previous = self._last_events.get(duplicate_key, float("-inf"))
        if now - previous < self._debounce_seconds:
            log.debug("Duplicate switch suppressed source=%s target=%s", event.source_identity, event.target_host)
            return
        self._last_events[duplicate_key] = now

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
                "Switch source=%s target=%d peer=%s result=missing",
                event.source_identity,
                event.target_host,
                peer_role.value,
            )
            self.state_changed(source[0])
            return

        peer, actor = peer_entry
        if peer.identity == event.source_identity:
            raise AssertionError("source exclusion invariant violated")
        if peer.state != DeviceState.READY:
            log.warning(
                "Switch source=%s target=%d peer=%s result=not-ready state=%s",
                event.source_identity,
                event.target_host,
                peer.identity,
                peer.state.value,
            )
            actor.ensure_ready("switch requested while peer not READY")
            self.state_changed(peer)
            return

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
            peer.transition(DeviceState.RECOVERING, "switch write timeout")
            actor.ensure_ready("switch timeout")
            self.state_changed(peer)
            return

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
            peer.transition(DeviceState.RECOVERING, result.detail)
            actor.ensure_ready("peer write failed")
        self.state_changed(peer)

    def _selected(self, role: DeviceRole) -> DeviceRuntime | None:
        identity = self._by_role.get(role)
        entry = self._devices.get(identity) if identity else None
        return entry[0] if entry else None
