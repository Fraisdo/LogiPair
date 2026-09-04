from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from pathlib import Path

from .actor import TransportActor
from .controller import PairController
from .hidapi_backend import HidApiBackend
from .lifecycle import WindowsLifecycleWatcher
from .model import HidPathInfo, PairHealth
from .storage import DeviceCache, StatusStore

log = logging.getLogger(__name__)


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
        self._backend_shutdown = False

    def run(self) -> None:
        self._controller.start()
        self._lifecycle.start()
        self._write_status(PairHealth.DEGRADED, [])
        log.info("LogiPair service started")
        try:
            while not self._shutdown.is_set():
                self._reconcile("startup-or-poll")
                self._wake.wait(self._poll_seconds)
                self._wake.clear()
        finally:
            self._lifecycle.stop()
            self._lifecycle.join(timeout=3.0)
            self._shutdown_components()

    def stop(self) -> None:
        self._shutdown.set()
        self._wake.set()

    def _on_lifecycle(self, reason: str) -> None:
        log.info("Windows lifecycle event: %s", reason)
        if reason.startswith("resume"):
            for actor in list(self._actors.values()):
                actor.recover(reason)
        self._wake.set()

    def _reconcile(self, reason: str) -> None:
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
            if actor is not None and self._path_set(paths) != self._path_set(self._paths.get(key, [])):
                self._paths[key] = paths
                actor.update_paths(paths, reason)

    @staticmethod
    def _group_paths(paths: list[HidPathInfo]) -> dict[str, list[HidPathInfo]]:
        groups: dict[str, list[HidPathInfo]] = defaultdict(list)
        for path in paths:
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
