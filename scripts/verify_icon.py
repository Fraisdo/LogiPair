"""Prove that a built executable really carries the LogiPair icon resource.

Windows caches shell icons aggressively, so what Explorer draws is not evidence.
This reads the RT_GROUP_ICON / RT_ICON resources straight out of the PE file and
compares the embedded bitmaps with assets/logipair.ico.

    python scripts/verify_icon.py dist/LogiPair.exe
"""

from __future__ import annotations

import ctypes
import struct
import sys
from ctypes import wintypes
from pathlib import Path

RT_ICON = 3
RT_GROUP_ICON = 14
LOAD_LIBRARY_AS_DATAFILE = 0x00000002
LOAD_LIBRARY_AS_IMAGE_RESOURCE = 0x00000020

ENUMRESNAMEPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMODULE, wintypes.LPVOID, wintypes.LPVOID, ctypes.c_void_p
)


def _expected_sizes(icon_path: Path) -> set[tuple[int, int]]:
    data = icon_path.read_bytes()
    count = struct.unpack("<H", data[4:6])[0]
    sizes = set()
    for index in range(count):
        entry = data[6 + 16 * index : 22 + 16 * index]
        width, height = entry[0] or 256, entry[1] or 256
        sizes.add((width, height))
    return sizes


def _embedded_sizes(exe_path: Path) -> tuple[set[tuple[int, int]], int]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # Every handle argument must be declared, or ctypes narrows it to a C int and
    # 64-bit module handles overflow.
    kernel32.LoadLibraryExW.argtypes = [wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD]
    kernel32.LoadLibraryExW.restype = wintypes.HMODULE
    kernel32.FreeLibrary.argtypes = [wintypes.HMODULE]
    kernel32.FreeLibrary.restype = wintypes.BOOL
    kernel32.EnumResourceNamesW.argtypes = [wintypes.HMODULE, wintypes.LPCWSTR, ENUMRESNAMEPROC, ctypes.c_void_p]
    kernel32.EnumResourceNamesW.restype = wintypes.BOOL
    kernel32.FindResourceW.argtypes = [wintypes.HMODULE, wintypes.LPCWSTR, wintypes.LPCWSTR]
    kernel32.FindResourceW.restype = wintypes.HANDLE
    kernel32.LoadResource.argtypes = [wintypes.HMODULE, wintypes.HANDLE]
    kernel32.LoadResource.restype = wintypes.HANDLE
    kernel32.LockResource.argtypes = [wintypes.HANDLE]
    kernel32.LockResource.restype = wintypes.LPVOID
    kernel32.SizeofResource.argtypes = [wintypes.HMODULE, wintypes.HANDLE]
    kernel32.SizeofResource.restype = wintypes.DWORD

    def resource_id(value: int) -> wintypes.LPCWSTR:
        """MAKEINTRESOURCE: an integer id passed where a name pointer is expected."""
        return ctypes.cast(ctypes.c_void_p(value), wintypes.LPCWSTR)

    module = kernel32.LoadLibraryExW(
        str(exe_path), None, LOAD_LIBRARY_AS_DATAFILE | LOAD_LIBRARY_AS_IMAGE_RESOURCE
    )
    if not module:
        raise OSError(ctypes.get_last_error(), f"cannot open {exe_path} for resource reading")

    icon_ids: list[int] = []
    group_count = 0
    try:

        def collect(_module, _type, name, _param):
            nonlocal group_count
            group_count += 1
            resource = kernel32.FindResourceW(
                module, ctypes.cast(name, wintypes.LPCWSTR), resource_id(RT_GROUP_ICON)
            )
            handle = kernel32.LoadResource(module, resource)
            size = kernel32.SizeofResource(module, resource)
            pointer = kernel32.LockResource(handle)
            blob = ctypes.string_at(pointer, size)
            entries = struct.unpack("<H", blob[4:6])[0]
            for index in range(entries):
                # GRPICONDIRENTRY is 14 bytes and ends with the RT_ICON id.
                icon_ids.append(struct.unpack("<H", blob[6 + 14 * index + 12 : 6 + 14 * index + 14])[0])
            return True

        if not kernel32.EnumResourceNamesW(module, resource_id(RT_GROUP_ICON), ENUMRESNAMEPROC(collect), None):
            raise OSError("no RT_GROUP_ICON resource in the executable")

        sizes: set[tuple[int, int]] = set()
        for icon_id in icon_ids:
            resource = kernel32.FindResourceW(module, resource_id(icon_id), resource_id(RT_ICON))
            handle = kernel32.LoadResource(module, resource)
            size = kernel32.SizeofResource(module, resource)
            blob = ctypes.string_at(kernel32.LockResource(handle), size)
            if blob[:8] == b"\x89PNG\r\n\x1a\n":
                width, height = struct.unpack(">II", blob[16:24])
            else:
                width, height = struct.unpack("<ii", blob[4:12])
                height //= 2  # BITMAPINFOHEADER stores XOR + AND mask height
            sizes.add((width, height))
    finally:
        kernel32.FreeLibrary(module)
    return sizes, group_count


def main(argv: list[str]) -> int:
    exe_path = Path(argv[1] if len(argv) > 1 else "dist/LogiPair.exe").resolve()
    icon_path = Path(__file__).resolve().parents[1] / "assets" / "logipair.ico"
    if not exe_path.exists():
        print(f"[FAIL] executable not found: {exe_path}", file=sys.stderr)
        return 1

    expected = _expected_sizes(icon_path)
    embedded, groups = _embedded_sizes(exe_path)
    missing = expected - embedded

    for width, height in sorted(embedded):
        print(f"  RT_ICON {width}x{height}")
    if groups != 1:
        print(f"[FAIL] expected exactly one RT_GROUP_ICON, found {groups}", file=sys.stderr)
        return 1
    if missing:
        print(f"[FAIL] sizes missing from {exe_path.name}: {sorted(missing)}", file=sys.stderr)
        return 1
    print(f"[PASS] {exe_path.name} embeds all {len(expected)} icon sizes from assets/logipair.ico")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
