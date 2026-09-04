from __future__ import annotations

import json
from pathlib import Path

from logipair import cli
from logipair.model import HidPathInfo
from logipair.service import LogiPairService


def test_status_and_diagnostics_do_not_start_service(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    status = tmp_path / "LogiPair" / "status.json"
    status.parent.mkdir(parents=True)
    status.write_text(
        json.dumps(
            {
                "version": 1,
                "pair": "PAIR_READY",
                "keyboard": {"name": "MX Keys", "state": "READY", "transport": "Bluetooth", "host": 2},
                "mouse": {"name": "MX Anywhere 3S", "state": "READY", "transport": "Bluetooth"},
            }
        ),
        encoding="utf-8",
    )
    assert cli.main(["--status"]) == 0
    output = capsys.readouterr().out
    assert "Keyboard: MX Keys" in output and "Pair: PAIR_READY" in output
    assert cli.main(["--diagnostics"]) == 0
    assert '"pair": "PAIR_READY"' in capsys.readouterr().out


def test_status_before_first_start(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert cli.main(["--status"]) == 0
    assert "has not written status" in capsys.readouterr().out


def test_version_reports_bundled_hidapi(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    class Backend:
        version = "0.15.0"

        def shutdown(self):
            pass

    monkeypatch.setattr(cli, "HidApiBackend", Backend)
    assert cli.main(["--version"]) == 0
    assert "LogiPair 1.0.0; hidapi 0.15.0" in capsys.readouterr().out


def test_normal_start_uses_single_instance_and_service(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    calls = []

    class Instance:
        def __enter__(self):
            calls.append("lock")
            return self

        def __exit__(self, *_args):
            calls.append("unlock")

    class Service:
        def __init__(self, cache_path, status_path):
            calls.append((Path(cache_path).name, Path(status_path).name))

        def run(self):
            calls.append("run")

        def stop(self):
            calls.append("stop")

    monkeypatch.setattr(cli, "SingleInstance", Instance)
    monkeypatch.setattr(cli, "LogiPairService", Service)
    assert cli.main([]) == 0
    assert calls == ["lock", ("device-cache.json", "status.json"), "run", "unlock"]


def test_second_instance_exits_before_hid(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    def duplicate():
        raise RuntimeError("already active")

    monkeypatch.setattr(cli, "SingleInstance", duplicate)
    assert cli.main(["--background"]) == 2


def test_transport_grouping_keeps_receiver_collections_together():
    short = HidPathInfo(b"short", 0x046D, 0xC52F, 0xFF00, 1, 1)
    long = HidPathInfo(b"long", 0x046D, 0xC52F, 0xFF00, 2, 1)
    keyboard = HidPathInfo(b"kb", 0x046D, 0xB35B, 0xFF43, 0x0202, 2, "one")
    mouse = HidPathInfo(b"mouse", 0x046D, 0xB025, 0xFF43, 0x0202, 2, "two")
    groups = LogiPairService._group_paths([short, long, keyboard, mouse])
    assert set(groups) == {"receiver:c52f", "bluetooth:b35b:one", "bluetooth:b025:two"}
    assert {item.path for item in groups["receiver:c52f"]} == {b"short", b"long"}
