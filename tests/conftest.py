from __future__ import annotations

import collections
import dataclasses
import queue
import threading
import time
from pathlib import Path

import pytest

from logipair.constants import (
    FEATURE_CHANGE_HOST,
    FEATURE_DEVICE_TYPE_AND_NAME,
    FEATURE_REPROG_CONTROLS_V4,
    KEY_FLAG_ANALYTICS,
    REPORT_LONG,
)
from logipair.errors import TransportError
from logipair.model import HidPathInfo


@dataclasses.dataclass
class FakeHandle:
    path: bytes
    owner: int
    closed: bool = False


class FakeBackend:
    version = "0.15.0"

    def __init__(self) -> None:
        self.specs: dict[bytes, dict] = {}
        self.queues: dict[bytes, queue.Queue[bytes | Exception]] = collections.defaultdict(queue.Queue)
        self.writes: list[tuple[bytes, bytes, bool, int]] = []
        self.opens: list[tuple[bytes, int]] = []
        self.closes: list[tuple[bytes, int]] = []
        self.violations: list[str] = []
        self.fail_change_for: set[bytes] = set()
        self.withhold_arm_ack: set[bytes] = set()
        self._lock = threading.Lock()

    def add(self, path: bytes, *, name: str, device_type: int) -> HidPathInfo:
        pid = 0xB35B if device_type == 0 else 0xB025
        if b"2" in path:
            pid += 1
        self.specs[path] = {"name": name, "type": device_type, "pid": pid}
        return HidPathInfo(path, 0x046D, pid, 0xFF43, 0x0202, 2, path.decode(), name)

    def enumerate(self):
        return [
            HidPathInfo(path, 0x046D, spec["pid"], 0xFF43, 0x0202, 2, path.decode(), spec["name"])
            for path, spec in self.specs.items()
        ]

    def open(self, path: bytes) -> FakeHandle:
        handle = FakeHandle(path, threading.get_ident())
        self.opens.append((path, handle.owner))
        return handle

    def read(self, handle: FakeHandle, timeout_ms: int = 0) -> bytes | None:
        self._check(handle, "read")
        try:
            if timeout_ms <= 1:
                item = self.queues[handle.path].get_nowait()
            else:
                item = self.queues[handle.path].get(timeout=timeout_ms / 1000)
        except queue.Empty:
            time.sleep(0)
            return None
        if isinstance(item, Exception):
            raise item
        return item

    def write(self, handle: FakeHandle, message: bytes, *, output_report: bool) -> None:
        self._check(handle, "write")
        with self._lock:
            self.writes.append((handle.path, bytes(message), output_report, threading.get_ident()))
        if message[0] != REPORT_LONG:
            return
        feature, function, sw_id = message[2], message[3] & 0xF0, message[3] & 0x0F
        if feature == 2 and function == 0x10:
            if handle.path in self.fail_change_for:
                self.fail_change_for.remove(handle.path)
                raise TransportError("simulated peer departure")
            return
        if feature == 3 and function == 0x30 and handle.path in self.withhold_arm_ack:
            return
        payload = self._response_payload(handle.path, message)
        response = bytes([REPORT_LONG, message[1], feature, function | sw_id]) + payload.ljust(16, b"\0")
        self.queues[handle.path].put(response)

    def close(self, handle: FakeHandle) -> None:
        self._check(handle, "close")
        handle.closed = True
        self.closes.append((handle.path, threading.get_ident()))

    def shutdown(self) -> None:
        pass

    def inject(self, path: bytes, report: bytes | Exception) -> None:
        self.queues[path].put(report)

    def change_writes(self, path: bytes | None = None):
        return [
            item
            for item in self.writes
            if item[1][0] == REPORT_LONG
            and item[1][2] == 2
            and (item[1][3] & 0xF0) == 0x10
            and (path is None or item[0] == path)
        ]

    def arm_writes(self, path: bytes):
        return [item for item in self.writes if item[0] == path and item[1][2] == 3 and (item[1][3] & 0xF0) == 0x30]

    def _check(self, handle: FakeHandle, operation: str) -> None:
        if handle.owner != threading.get_ident():
            self.violations.append(f"{operation} from non-owner")
            raise TransportError(self.violations[-1])
        if handle.closed:
            self.violations.append(f"{operation} after close")
            raise TransportError(self.violations[-1])

    def _response_payload(self, path: bytes, message: bytes) -> bytes:
        spec = self.specs[path]
        feature, function = message[2], message[3] & 0xF0
        if feature == 0 and function == 0:
            code = (message[4] << 8) | message[5]
            return bytes(
                (
                    {
                        FEATURE_DEVICE_TYPE_AND_NAME: 1,
                        FEATURE_CHANGE_HOST: 2,
                        FEATURE_REPROG_CONTROLS_V4: 3,
                    }.get(code, 0),
                )
            )
        if feature == 1 and function == 0x20:
            return bytes((spec["type"],))
        if feature == 1 and function == 0:
            return bytes((len(spec["name"].encode()),))
        if feature == 1 and function == 0x10:
            offset = message[4]
            return spec["name"].encode()[offset : offset + 16]
        if feature == 3 and function == 0:
            return b"\x03"
        if feature == 3 and function == 0x10:
            index = message[4]
            cid = (0x00D1, 0x00D2, 0x00D3)[index]
            return bytes((cid >> 8, cid & 0xFF, 0, 0, KEY_FLAG_ANALYTICS))
        if feature == 3 and function == 0x30:
            return message[4:10]
        return b"\0"


def wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true")


@pytest.fixture
def fake_backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def temp_paths(tmp_path: Path):
    return tmp_path / "cache.json", tmp_path / "status.json"
