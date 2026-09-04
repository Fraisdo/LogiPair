from __future__ import annotations

import pytest

from logipair.constants import FEATURE_CHANGE_HOST, FEATURE_REPROG_CONTROLS_V4, REPORT_LONG
from logipair.model import DeviceRole, DeviceRuntime
from logipair.protocol import Notification, build_change_host, notification_target, parse_report


@pytest.mark.parametrize("raw", [b"", b"\x11", b"\x11\xff\x02", bytes(19), bytes(21)])
def test_malformed_reports_are_ignored(raw):
    assert parse_report(raw) is None


def test_change_host_notification_uses_payload_one():
    device = DeviceRuntime("keys", 1, 1, 0xFF, "Bluetooth", role=DeviceRole.KEYBOARD)
    device.feature_indexes = {FEATURE_CHANGE_HOST: 7}
    report = Notification(0xFF, 7, 0, bytes((0, 2)) + bytes(14))
    assert notification_target(report, device) == 2


def test_reprog_press_and_release_are_distinguished():
    device = DeviceRuntime("keys", 1, 1, 0xFF, "Bluetooth", role=DeviceRole.KEYBOARD)
    device.feature_indexes = {FEATURE_REPROG_CONTROLS_V4: 9}
    assert notification_target(Notification(0xFF, 9, 2, b"\x00\xd2\x01" + bytes(13)), device) == 1
    assert notification_target(Notification(0xFF, 9, 2, b"\x00\xd2\x00" + bytes(13)), device) is None


def test_change_host_validates_target_and_builds_fire_and_forget_request():
    message = build_change_host(0xFF, 6, 2)
    assert len(message) == 20
    assert message[:5] == bytes((REPORT_LONG, 0xFF, 6, 0x1F, 2))
    with pytest.raises(ValueError):
        build_change_host(0xFF, 6, 3)


def test_external_reporting_removal_is_parsed():
    payload = bytes((0x00, 0xD1, 0x02, 0, 0, 0)) + bytes(10)
    report = parse_report(bytes((0x11, 0xFF, 3, 0x30)) + payload)
    assert report is not None
    assert report.compatible is False


def test_external_reporting_with_high_software_id_is_not_mistaken_for_our_ack():
    payload = bytes((0x00, 0xD2, 0x02, 0, 0, 0)) + bytes(10)
    report = parse_report(bytes((0x11, 0xFF, 3, 0x38)) + payload)
    assert report is not None
    assert report.compatible is False
