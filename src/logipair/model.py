from __future__ import annotations

import dataclasses
import enum
import threading
import time
from typing import Any

from .constants import RECEIVER_PIDS


class DeviceRole(str, enum.Enum):
    KEYBOARD = "keyboard"
    MOUSE = "mouse"


class DeviceState(str, enum.Enum):
    DISCONNECTED = "DISCONNECTED"
    EXPECTED_DISCONNECTED = "EXPECTED_DISCONNECTED"
    CONNECTING = "CONNECTING"
    INITIALIZING = "INITIALIZING"
    ARMING = "ARMING"
    READY = "READY"
    SWITCHING = "SWITCHING"
    RECOVERING = "RECOVERING"


class PairHealth(str, enum.Enum):
    PAIR_READY = "PAIR_READY"
    DEGRADED = "DEGRADED"
    RECOVERING = "RECOVERING"


@dataclasses.dataclass(frozen=True)
class HidPathInfo:
    path: bytes
    vid: int
    pid: int
    usage_page: int
    usage: int
    bus_type: int
    serial: str | None = None
    product: str | None = None

    @property
    def transport(self) -> str:
        return "receiver" if self.pid in RECEIVER_PIDS else "bluetooth"


@dataclasses.dataclass
class DeviceRuntime:
    identity: str
    pid: int
    wpid: int
    slot: int
    transport: str
    role: DeviceRole | None = None
    name: str | None = None
    state: DeviceState = DeviceState.DISCONNECTED
    feature_indexes: dict[int, int] = dataclasses.field(default_factory=dict)
    easy_switch_cids: tuple[int, ...] = ()
    supported_flags: int = 0
    last_known_host: int | None = None
    reverse_notifications_observed: bool = False
    switch_capable: bool = False
    # True once a live REPORT_LONG handle is open and everything needed to parse an
    # incoming x1814 Easy-Switch notification is known. This is the first milestone
    # after an arrival; full discovery and arming come later.
    observer_capable: bool = False
    # Monotonic deadline until which this device is allowed to vanish without it
    # counting as a transport failure.
    expected_departure_until: float = 0.0
    last_error: str | None = None
    updated_at: float = dataclasses.field(default_factory=time.time)
    _lock: threading.RLock = dataclasses.field(default_factory=threading.RLock, repr=False, compare=False)

    def transition(self, state: DeviceState, error: str | None = None) -> None:
        with self._lock:
            self.state = state
            self.last_error = error
            self.updated_at = time.time()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "identity": self.identity,
                "name": self.name,
                "role": self.role.value if self.role else None,
                "state": self.state.value,
                "transport": self.transport,
                "pid": f"0x{self.pid:04X}",
                "wpid": f"0x{self.wpid:04X}",
                "slot": self.slot,
                "host": self.last_known_host,
                "features": {f"0x{k:04X}": v for k, v in sorted(self.feature_indexes.items())},
                "easy_switch_cids": [f"0x{x:04X}" for x in self.easy_switch_cids],
                "supported_flags": f"0x{self.supported_flags:02X}",
                "reverse_notifications_observed": self.reverse_notifications_observed,
                "switch_capable": self.switch_capable,
                "observer_capable": self.observer_capable,
                "last_error": self.last_error,
                "updated_at": self.updated_at,
            }


@dataclasses.dataclass(frozen=True)
class HostChange:
    source_identity: str
    source_role: DeviceRole
    target_host: int
    observed_at: float


@dataclasses.dataclass(frozen=True)
class SwitchResult:
    ok: bool
    detail: str
    write_at: float
