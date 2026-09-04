from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import __version__
from .constants import APP_NAME
from .hidapi_backend import HidApiBackend
from .instance import SingleInstance
from .service import LogiPairService
from .storage import StatusStore


def data_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise OSError("LOCALAPPDATA is unavailable")
    return Path(local) / APP_NAME


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="LogiPair", description="Keep MX Keys and MX Anywhere 3S on one host")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--status", action="store_true", help="show current pair status without opening HID devices")
    mode.add_argument("--diagnostics", action="store_true", help="show cached runtime diagnostics")
    mode.add_argument("--version", action="store_true", help="show LogiPair and bundled hidapi versions")
    parser.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--debug", action="store_true", help="enable DEBUG logging")
    return parser


def setup_logging(log_path: Path, *, background: bool, debug: bool) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-8s %(threadName)s %(name)s: %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )
    rotating = RotatingFileHandler(log_path, maxBytes=3 * 1024 * 1024, backupCount=4, encoding="utf-8")
    rotating.setFormatter(formatter)
    root.addHandler(rotating)
    if not background:
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        root.addHandler(console)


def render_status(value: dict | None, *, diagnostics: bool, log_path: Path) -> str:
    if value is None:
        return f"LogiPair has not written status yet.\nLog: {log_path}"
    if diagnostics:
        return json.dumps({**value, "log": str(log_path)}, indent=2, sort_keys=True)
    lines: list[str] = []
    for key, label in (("keyboard", "Keyboard"), ("mouse", "Mouse")):
        device = value.get(key)
        if not device:
            lines.extend((f"{label}: not detected", "State: DISCONNECTED", ""))
            continue
        lines.append(f"{label}: {device.get('name') or 'unknown'}")
        lines.append(f"State: {device.get('state')}")
        lines.append(f"Transport: {device.get('transport')}")
        if device.get("host") is not None:
            lines.append(f"Host: {device['host']}")
        lines.append("")
    lines.append(f"Pair: {value.get('pair', 'DEGRADED')}")
    lines.append(f"Log: {log_path}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = data_dir()
    log_path = root / "logs" / "logipair.log"
    status_path = root / "status.json"
    cache_path = root / "device-cache.json"

    if args.status or args.diagnostics:
        print(render_status(StatusStore(status_path).read(), diagnostics=args.diagnostics, log_path=log_path))
        return 0
    if args.version:
        try:
            backend = HidApiBackend()
            hidapi_version = backend.version
            backend.shutdown()
        except Exception as error:
            hidapi_version = f"unavailable ({error})"
        print(f"LogiPair {__version__}; hidapi {hidapi_version}")
        return 0
    if sys.platform != "win32":
        print("LogiPair supports Windows only", file=sys.stderr)
        return 1

    setup_logging(log_path, background=args.background, debug=args.debug)
    log = logging.getLogger(__name__)
    try:
        instance = SingleInstance()
    except RuntimeError as error:
        log.warning("%s", error)
        if not args.background:
            print(error, file=sys.stderr)
        return 2

    with instance:
        try:
            service = LogiPairService(cache_path, status_path)
        except Exception:
            log.exception("Startup failed")
            return 1

        def stop_service(*_args) -> None:
            service.stop()

        for signal_name in ("SIGINT", "SIGTERM"):
            value = getattr(signal, signal_name, None)
            if value is not None:
                signal.signal(value, stop_service)
        try:
            service.run()
        except KeyboardInterrupt:
            service.stop()
        except Exception:
            log.exception("Fatal service error")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
