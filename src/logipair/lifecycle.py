"""Windows power and HID arrival/removal notifications."""

from __future__ import annotations

import ctypes
import logging
import threading
import time
import uuid
from collections.abc import Callable
from ctypes import wintypes

log = logging.getLogger(__name__)

WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_TIMER = 0x0113
WM_DEVICECHANGE = 0x0219
WM_POWERBROADCAST = 0x0218
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012
DBT_DEVICEARRIVAL = 0x8000
DBT_DEVICEREMOVECOMPLETE = 0x8004
DBT_DEVTYP_DEVICEINTERFACE = 0x00000005
DEVICE_NOTIFY_WINDOW_HANDLE = 0x00000000
GUID_DEVINTERFACE_HID = uuid.UUID("4d1e55b2-f16f-11cf-88cb-001111000030")


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_uuid(cls, value: uuid.UUID) -> GUID:
        return cls.from_buffer_copy(value.bytes_le)


class DEV_BROADCAST_DEVICEINTERFACE(ctypes.Structure):
    _fields_ = [
        ("dbcc_size", wintypes.DWORD),
        ("dbcc_devicetype", wintypes.DWORD),
        ("dbcc_reserved", wintypes.DWORD),
        ("dbcc_classguid", GUID),
        ("dbcc_name", wintypes.WCHAR * 1),
    ]


WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t,
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
)


class WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class WindowsLifecycleWatcher(threading.Thread):
    def __init__(self, callback: Callable[[str], None], *, resume_gap_seconds: float = 5.0) -> None:
        super().__init__(name="WindowsLifecycleWatcher", daemon=True)
        self._callback = callback
        self._resume_gap_seconds = resume_gap_seconds
        self._hwnd: int | None = None
        self._ready = threading.Event()
        self._last_tick = time.monotonic()
        self._wndproc_ref = WNDPROC(self._wndproc)
        self._notification_handle: int | None = None

    def run(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
        user32.RegisterClassW.restype = wintypes.ATOM
        user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        user32.UnregisterClassW.restype = wintypes.BOOL
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            ctypes.c_void_p,
        ]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.RegisterDeviceNotificationW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        user32.RegisterDeviceNotificationW.restype = wintypes.HANDLE
        user32.UnregisterDeviceNotification.argtypes = [wintypes.HANDLE]
        user32.UnregisterDeviceNotification.restype = wintypes.BOOL
        user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
        user32.SetTimer.restype = ctypes.c_size_t
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.restype = wintypes.BOOL
        user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.TranslateMessage.restype = wintypes.BOOL
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DispatchMessageW.restype = ctypes.c_ssize_t
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.PostMessageW.restype = wintypes.BOOL
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        user32.DestroyWindow.restype = wintypes.BOOL
        user32.PostQuitMessage.argtypes = [ctypes.c_int]
        user32.PostQuitMessage.restype = None
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        class_name = f"LogiPairLifecycle-{id(self)}"
        instance = kernel32.GetModuleHandleW(None)
        window_class = WNDCLASS()
        window_class.lpfnWndProc = self._wndproc_ref
        window_class.hInstance = instance
        window_class.lpszClassName = class_name
        atom = user32.RegisterClassW(ctypes.byref(window_class))
        if not atom:
            log.error("RegisterClassW failed; lifecycle watcher unavailable")
            self._ready.set()
            return
        self._hwnd = user32.CreateWindowExW(
            0,
            class_name,
            "LogiPairLifecycle",
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            instance,
            None,
        )
        if not self._hwnd:
            log.error("CreateWindowExW failed; lifecycle watcher unavailable")
            self._ready.set()
            return
        filter_value = DEV_BROADCAST_DEVICEINTERFACE()
        filter_value.dbcc_size = ctypes.sizeof(filter_value)
        filter_value.dbcc_devicetype = DBT_DEVTYP_DEVICEINTERFACE
        filter_value.dbcc_classguid = GUID.from_uuid(GUID_DEVINTERFACE_HID)
        self._notification_handle = user32.RegisterDeviceNotificationW(
            self._hwnd,
            ctypes.byref(filter_value),
            DEVICE_NOTIFY_WINDOW_HANDLE,
        )
        user32.SetTimer(self._hwnd, 1, 1000, None)
        self._ready.set()
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), 0, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
        if self._notification_handle:
            user32.UnregisterDeviceNotification(self._notification_handle)
        user32.UnregisterClassW(class_name, instance)

    def stop(self) -> None:
        self._ready.wait(2.0)
        if self._hwnd:
            ctypes.windll.user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)

    def _wndproc(self, hwnd, message, wparam, lparam):
        if message == WM_POWERBROADCAST and wparam in (PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC):
            self._safe_callback("resume")
            return 1
        if message == WM_DEVICECHANGE and wparam in (DBT_DEVICEARRIVAL, DBT_DEVICEREMOVECOMPLETE):
            self._safe_callback("device-arrival" if wparam == DBT_DEVICEARRIVAL else "device-removal")
            return 1
        if message == WM_TIMER:
            now = time.monotonic()
            gap = now - self._last_tick
            self._last_tick = now
            if gap > self._resume_gap_seconds:
                self._safe_callback("resume-gap")
            return 0
        if message == WM_CLOSE:
            ctypes.windll.user32.DestroyWindow(hwnd)
            return 0
        if message == WM_DESTROY:
            ctypes.windll.user32.PostQuitMessage(0)
            return 0
        return ctypes.windll.user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _safe_callback(self, reason: str) -> None:
        try:
            self._callback(reason)
        except Exception:
            log.exception("Lifecycle callback failed reason=%s", reason)
