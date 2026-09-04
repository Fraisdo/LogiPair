from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from pathlib import Path

from .actor import TransportActor
from .constants import LIFECYCLE_COALESCE_MAX_SECONDS, LIFECYCLE_COALESCE_SECONDS, V1_IGNORED_PIDS
from .controller import PairController
from .hidapi_backend import HidApiBackend
from .lifecycle import WindowsLifecycleWatcher
from .model import HidPathInfo, PairHealth
from .storage import DeviceCache, StatusStore

log = logging.getLogger(__name__)


class LifecycleCoalescer:
    """Collapses a WM_DEVICECHANGE burst into a single reconciliation.

    One physical device produces several HID interface arrivals/removals, so Windows
    fires several messages within a few milliseconds. Reconciling once per message
    turns one Easy-Switch hop into a handful of redundant recovery cycles.
    """

    def __init__(
        self,
        *,
        window_seconds: float = LIFECYCLE_COALESCE_SECONDS,
        max_delay_seconds: float = LIFECYCLE_COALESCE_MAX_SECONDS,
        clock=time.monotonic,
    ) -> None:
        self._window = window_seconds
        self._max_delay = max_delay_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}
        self._first_at = 0.0
        self._deadline = 0.0
        self._arrival = False

    def record(self, reason: str) -> None:
        now = self._clock()
        with self._lock:
            if not self._counts:
                self._first_at = now
            self._counts[reason] = self._counts.get(reason, 0) + 1
            self._arrival = self._arrival or reason == "device-arrival"
            # A continuous storm must not postpone reconciliation forever.
            self._deadline = min(now + self._window, self._first_at + self._max_delay)

    def due_in(self) -> float | None:
        """Seconds until the pending batch is due, or None when nothing is pending."""
        with self._lock:
            if not self._counts:
                return None
            return max(0.0, self._deadline - self._clock())

    def take(self) -> tuple[str, bool] | None:
        """Return (reason, saw_arrival) once the batch settled, else None."""
        with self._lock:
            if not self._counts or self._clock() < self._deadline:
                return None
            reason = ",".join(f"{name} x{count}" for name, count in sorted(self._counts.items()))
            arrival = self._arrival
            self._counts = {}
            self._arrival = False
            self._deadline = 0.0
            return reason, arrival


class LogiPairService:
    def __init__(
        self,
        cache_path: Path,
        status_path: Path,
        *,
        backend: HidApiBackend | None = None,
        poll_seconds: float = 5.0,
    ) -> None:
        self._backend = backend or HidApiBackend()
        self._cache = DeviceCache(cache_path)
        self._status = StatusStore(status_path)
        self._poll_seconds = poll_seconds
        self._wake = threading.Event()
        self._shutdown = threading.Event()
        self._actors: dict[str, TransportActor] = {}
        self._paths: dict[str, list[HidPathInfo]] = {}
        self._controller = PairController(self._write_status)
        self._lifecycle = WindowsLifecycleWatcher(self._on_lifecycle)
        self._lifecycle_events = LifecycleCoalescer()
        self._backend_shutdown = False

    def run(self) -> None:
        self._controller.start()
        self._lifecycle.start()
        self._write_status(PairHealth.DEGRADED, [])
        log.info("LogiPair service started")
        try:
            self._reconcile("startup")
            next_poll = time.monotonic() + self._poll_seconds
            while not self._shutdown.is_set():
                self._wake.wait(self._wait_seconds(next_poll))
                self._wake.clear()
                if self._shutdown.is_set():
                    break
                batch = self._lifecycle_events.take()
                if batch is not None:
                    self._reconcile(batch[0], arrival=batch[1])
                    continue
                if time.monotonic() >= next_poll:
                    next_poll = time.monotonic() + self._poll_seconds
                    self._reconcile("poll")
        finally:
            self._lifecycle.stop()
            self._lifecycle.join(timeout=3.0)
            self._shutdown_components()

    def stop(self) -> None:
        self._shutdown.set()
        self._wake.set()

    def _wait_seconds(self, next_poll: float) -> float:
        remaining = max(0.0, next_poll - time.monotonic())
        due = self._lifecycle_events.due_in()
        return remaining if due is None else min(due, remaining)

    def _on_lifecycle(self, reason: str) -> None:
        if reason.startswith("resume"):
            log.info("Windows lifecycle event: %s", reason)
            for actor in list(self._actors.values()):
                actor.recover(reason)
            self._wake.set()
            return
        log.debug("Windows lifecycle event: %s", reason)
        self._lifecycle_events.record(reason)
        self._wake.set()

    def _reconcile(self, reason: str, *, arrival: bool = False) -> None:
        try:
            groups = self._group_paths(self._backend.enumerate())
        except Exception as error:
            log.warning("HID enumeration failed: %s", error)
            return
        all_keys = set(groups) | set(self._actors)
        for key in all_keys:
            paths = groups.get(key, [])
            actor = self._actors.get(key)
            if actor is None and paths:
                actor = TransportActor(paths[0].pid, paths, self._backend, self._controller, self._cache)
                self._actors[key] = actor
                self._paths[key] = paths
                actor.start()
                continue
            if actor is None:
                continue
            if self._path_set(paths) != self._path_set(self._paths.get(key, [])):
                self._paths[key] = paths
                actor.update_paths(paths, reason)
            if arrival and paths:
                # Enumeration confirms this transport is really back: no failure history
                # from its absence may delay the reconnect.
                actor.device_arrived(reason)

    @staticmethod
    def _group_paths(paths: list[HidPathInfo]) -> dict[str, list[HidPathInfo]]:
        groups: dict[str, list[HidPathInfo]] = defaultdict(list)
        for path in paths:
            if path.pid in V1_IGNORED_PIDS:
                # Out of scope for V1, which targets one Bluetooth-direct pair. Creating
                # an actor for it only produces noise around the switches that matter.
                continue
            if path.transport == "receiver":
                key = f"receiver:{path.pid:04x}"
            else:
                # A direct device normally has one PID. Serial keeps two identical models separate.
                key = f"bluetooth:{path.pid:04x}:{path.serial or ''}"
            groups[key].append(path)
        return dict(groups)

    @staticmethod
    def _path_set(paths: list[HidPathInfo]) -> set[bytes]:
        return {entry.path for entry in paths}

    def _write_status(self, health, devices) -> None:
        try:
            self._status.write(health, devices, hidapi_version=self._backend.version)
        except OSError as error:
            log.warning("Could not write status: %s", error)

    def _shutdown_components(self, actor_timeout_seconds: float = 5.0) -> bool:
        actors = list(self._actors.values())
        for actor in actors:
            actor.stop()
        deadline = time.monotonic() + actor_timeout_seconds
        for actor in actors:
            actor.join(timeout=max(0.0, deadline - time.monotonic()))
        alive = [actor.name for actor in actors if actor.is_alive()]
        if alive:
            log.critical(
                "HID actors did not stop; refusing hid_exit to avoid native race actors=%s",
                ",".join(alive),
            )
            return False

        self._controller.stop()
        if self._controller.ident is not None:
            self._controller.join(timeout=3.0)
        if self._controller.is_alive():
            log.critical("PairController did not stop; refusing hid_exit")
            return False

        if not self._backend_shutdown:
            self._backend.shutdown()
            self._backend_shutdown = True
        log.info("LogiPair service stopped")
        return True
