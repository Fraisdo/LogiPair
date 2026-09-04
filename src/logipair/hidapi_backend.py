"""Minimal Windows hidapi 0.15 binding with strict handle ownership.

Every operation touching a ``hid_device*`` verifies that it runs on the thread
that opened it. A process-wide lock also keeps ``hid_enumerate`` away from
open/read/write/close calls in other actors; this deliberately trades at most a
few milliseconds of latency for native-library safety on Windows.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from .constants import HIDPP_USAGE_PAGES, LOGITECH_VENDOR_ID, MAX_READ_SIZE
from .errors import HidApiVersionError, OwnershipError, TransportError
from .model import HidPathInfo

log = logging.getLogger(__name__)
_PROCESS_NATIVE_LOCK = threading.RLock()


class _DeviceInfo(ctypes.Structure):
    pass


_DeviceInfo._fields_ = [
    ("path", ctypes.c_char_p),
    ("vendor_id", ctypes.c_ushort),
    ("product_id", ctypes.c_ushort),
    ("serial_number", ctypes.c_wchar_p),
    ("release_number", ctypes.c_ushort),
    ("manufacturer_string", ctypes.c_wchar_p),
    ("product_string", ctypes.c_wchar_p),
    ("usage_page", ctypes.c_ushort),
    ("usage", ctypes.c_ushort),
    ("interface_number", ctypes.c_int),
    ("next", ctypes.POINTER(_DeviceInfo)),
    ("bus_type", ctypes.c_int),
]


@dataclass
class OwnedHandle:
    pointer: int | None
    path: bytes
    owner_ident: int


def _version_tuple(value: str) -> tuple[int, int, int]:
    parts = value.split(".")
    try:
        return tuple(int(x) for x in (parts + ["0", "0"])[:3])  # type: ignore[return-value]
    except ValueError:
        return (0, 0, 0)


class HidApiBackend:
    """Windows native backend shared by all transport actors."""

    def __init__(self, dll_path: Path | None = None) -> None:
        if sys.platform != "win32":
            raise OSError("LogiPair supports Windows only")
        self._native_lock = _PROCESS_NATIVE_LOCK
        self._lib, self.library_path = self._load_library(dll_path)
        self._bind()
        if self._lib.hid_init() != 0:
            raise TransportError("hid_init failed")
        raw_version = self._lib.hid_version_str()
        self.version = raw_version.decode("ascii", errors="replace") if raw_version else "unknown"
        if _version_tuple(self.version) < (0, 15, 0):
            raise HidApiVersionError(f"hidapi >= 0.15.0 required; loaded {self.version}")
        log.info("hidapi %s loaded from %s", self.version, self.library_path)

    @staticmethod
    def _load_library(explicit: Path | None) -> tuple[ctypes.CDLL, str]:
        candidates: list[Path | str] = []
        if explicit is not None:
            candidates.append(explicit)
        bundle = getattr(sys, "_MEIPASS", None)
        if bundle:
            candidates.append(Path(bundle) / "hidapi.dll")
        candidates.extend(
            [
                Path(sys.executable).resolve().parent / "hidapi.dll",
                Path(__file__).resolve().parents[2] / "vendor" / "hidapi.dll",
                "hidapi.dll",
            ]
        )
        errors: list[str] = []
        for candidate in candidates:
            path = os.fspath(candidate)
            try:
                return ctypes.CDLL(path), path
            except OSError as error:
                errors.append(f"{path}: {error}")
        raise ImportError("Cannot load hidapi.dll 0.15.0. Tried: " + "; ".join(errors))

    def _bind(self) -> None:
        lib = self._lib
        lib.hid_init.argtypes = []
        lib.hid_init.restype = ctypes.c_int
        lib.hid_exit.argtypes = []
        lib.hid_exit.restype = ctypes.c_int
        lib.hid_version_str.argtypes = []
        lib.hid_version_str.restype = ctypes.c_char_p
        lib.hid_enumerate.argtypes = [ctypes.c_ushort, ctypes.c_ushort]
        lib.hid_enumerate.restype = ctypes.POINTER(_DeviceInfo)
        lib.hid_free_enumeration.argtypes = [ctypes.POINTER(_DeviceInfo)]
        lib.hid_free_enumeration.restype = None
        lib.hid_open_path.argtypes = [ctypes.c_char_p]
        lib.hid_open_path.restype = ctypes.c_void_p
        lib.hid_close.argtypes = [ctypes.c_void_p]
        lib.hid_close.restype = None
        lib.hid_read_timeout.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_size_t,
            ctypes.c_int,
        ]
        lib.hid_read_timeout.restype = ctypes.c_int
        lib.hid_write.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_size_t,
        ]
        lib.hid_write.restype = ctypes.c_int
        lib.hid_send_output_report.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_size_t,
        ]
        lib.hid_send_output_report.restype = ctypes.c_int
        lib.hid_error.argtypes = [ctypes.c_void_p]
        lib.hid_error.restype = ctypes.c_wchar_p

    def enumerate(self) -> list[HidPathInfo]:
        with self._native_lock:
            head = self._lib.hid_enumerate(LOGITECH_VENDOR_ID, 0)
            entries: list[HidPathInfo] = []
            seen: set[bytes] = set()
            node = head
            try:
                while node:
                    item = node.contents
                    node = item.next
                    path = item.path
                    if not path or path in seen or item.usage_page not in HIDPP_USAGE_PAGES:
                        continue
                    seen.add(path)
                    entries.append(
                        HidPathInfo(
                            path=bytes(path),
                            vid=item.vendor_id,
                            pid=item.product_id,
                            usage_page=item.usage_page,
                            usage=item.usage,
                            bus_type=item.bus_type,
                            serial=item.serial_number,
                            product=item.product_string,
                        )
                    )
            finally:
                if head:
                    self._lib.hid_free_enumeration(head)
            return entries

    def open(self, path: bytes) -> OwnedHandle:
        owner = threading.get_ident()
        with self._native_lock:
            pointer = self._lib.hid_open_path(path)
            if not pointer:
                raise TransportError(f"hid_open_path failed: {self._error(None)}")
            return OwnedHandle(int(pointer), path, owner)

    def read(self, handle: OwnedHandle, timeout_ms: int = 0) -> bytes | None:
        self._assert_owner(handle)
        with self._native_lock:
            self._assert_open(handle)
            buf = (ctypes.c_ubyte * MAX_READ_SIZE)()
            count = self._lib.hid_read_timeout(handle.pointer, buf, MAX_READ_SIZE, timeout_ms)
            if count < 0:
                raise TransportError(f"hid_read_timeout failed: {self._error(handle.pointer)}")
            return bytes(buf[:count]) if count else None

    def write(self, handle: OwnedHandle, message: bytes, *, output_report: bool) -> None:
        self._assert_owner(handle)
        with self._native_lock:
            self._assert_open(handle)
            buf = (ctypes.c_ubyte * len(message))(*message)
            function = self._lib.hid_send_output_report if output_report else self._lib.hid_write
            count = function(handle.pointer, buf, len(message))
            if count < 0:
                operation = "hid_send_output_report" if output_report else "hid_write"
                raise TransportError(f"{operation} failed: {self._error(handle.pointer)}")

    def close(self, handle: OwnedHandle) -> None:
        self._assert_owner(handle)
        with self._native_lock:
            if handle.pointer is not None:
                self._lib.hid_close(handle.pointer)
                handle.pointer = None

    def shutdown(self) -> None:
        with self._native_lock:
            self._lib.hid_exit()

    def _assert_owner(self, handle: OwnedHandle) -> None:
        current = threading.get_ident()
        if current != handle.owner_ident:
            raise OwnershipError(
                f"handle for {handle.path!r} owned by thread {handle.owner_ident}, called from {current}"
            )

    @staticmethod
    def _assert_open(handle: OwnedHandle) -> None:
        if handle.pointer is None:
            raise TransportError("operation on closed HID handle")

    def _error(self, pointer: int | None) -> str:
        value = self._lib.hid_error(pointer)
        return value or "unknown hidapi error"
