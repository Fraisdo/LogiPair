"""Small HID++ subset used by MX Keys and MX Anywhere 3S."""

from __future__ import annotations

import dataclasses
import struct

from .constants import (
    ANALYTICS_AVALID,
    ANALYTICS_BYTE9,
    ANALYTICS_KEY_EVT,
    CHANGE_HOST_FN_SET,
    FEATURE_CHANGE_HOST,
    FEATURE_REPROG_CONTROLS_V4,
    HOST_SWITCH_CIDS,
    KEY_FLAG_ANALYTICS,
    KEY_FLAG_DIVERTABLE,
    MAP_FLAG_DIVERTED,
    REPORT_LENGTHS,
    REPORT_LONG,
    REPORT_SHORT,
    SW_ID_DIVERT,
    SW_ID_HOST_CHANGE,
    SW_ID_MASK,
    SW_ID_REQUEST,
    VALID_HOSTS,
)
from .model import DeviceRuntime


@dataclasses.dataclass(frozen=True)
class Response:
    slot: int
    feature_index: int
    function: int
    sw_id: int
    payload: bytes


@dataclasses.dataclass(frozen=True)
class Notification:
    slot: int
    feature_index: int
    function: int
    payload: bytes


@dataclasses.dataclass(frozen=True)
class DeviceConnection:
    slot: int
    connected: bool
    wpid: int
    device_type: int | None


@dataclasses.dataclass(frozen=True)
class HidError:
    slot: int
    sw_id: int
    error_code: int


@dataclasses.dataclass(frozen=True)
class ReportingChanged:
    slot: int
    feature_index: int
    cid: int
    compatible: bool


ParsedReport = Response | Notification | DeviceConnection | HidError | ReportingChanged


def build_message(slot: int, feature_index: int, function: int, params: bytes = b"", *, sw_id: int) -> bytes:
    request_id = (feature_index << 8) | (function & 0xF0) | (sw_id & 0x0F)
    data = struct.pack("!H", request_id) + params
    return struct.pack("!BB18s", REPORT_LONG, slot, data)


def build_get_feature(slot: int, feature_code: int) -> bytes:
    return build_message(
        slot,
        0,
        0,
        bytes((feature_code >> 8, feature_code & 0xFF, 0)),
        sw_id=SW_ID_REQUEST,
    )


def build_change_host(slot: int, feature_index: int, target_host: int) -> bytes:
    if target_host not in VALID_HOSTS:
        raise ValueError(f"invalid target host {target_host}; expected 0, 1 or 2")
    return build_message(
        slot,
        feature_index,
        CHANGE_HOST_FN_SET,
        bytes((target_host,)),
        sw_id=SW_ID_HOST_CHANGE,
    )


def build_cid_reporting(
    slot: int,
    feature_index: int,
    cid: int,
    supported_flags: int,
    *,
    enable: bool = True,
) -> bytes:
    if supported_flags & KEY_FLAG_ANALYTICS:
        params = struct.pack("!HBHB", cid, 0, 0, ANALYTICS_BYTE9 if enable else ANALYTICS_AVALID)
    elif supported_flags & KEY_FLAG_DIVERTABLE:
        bfield = MAP_FLAG_DIVERTED << 1
        if enable:
            bfield |= MAP_FLAG_DIVERTED
        params = struct.pack("!HBH", cid, bfield, 0)
    else:
        raise ValueError(f"CID 0x{cid:04X} has no supported reporting mode")
    return build_message(slot, feature_index, 0x30, params, sw_id=SW_ID_DIVERT)


def parse_report(raw: bytes) -> ParsedReport | None:
    if not raw:
        return None
    expected = REPORT_LENGTHS.get(raw[0])
    if expected is None or len(raw) != expected:
        return None

    slot = raw[1]
    feature = raw[2]
    if raw[0] == REPORT_SHORT:
        if feature == 0x41:
            flags = raw[4]
            return DeviceConnection(
                slot=slot,
                connected=(flags & 0x40) == 0,
                wpid=(raw[6] << 8) | raw[5],
                device_type=(flags & 0x0F) or None,
            )
        if feature == 0x8F:
            return HidError(slot=slot, sw_id=raw[3] & 0x0F, error_code=raw[5])
        return None

    sw_id = raw[3] & 0x0F
    function = (raw[3] & 0xF0) >> 4
    payload = raw[4:]
    if feature == 0xFF:
        return HidError(slot=slot, sw_id=sw_id, error_code=raw[5])
    if function == 3 and sw_id != SW_ID_DIVERT and len(payload) >= 6:
        cid = (payload[0] << 8) | payload[1]
        if cid in HOST_SWITCH_CIDS:
            bfield = payload[2]
            byte9 = payload[5]
            analytics_ok = bool(byte9 & ANALYTICS_AVALID and byte9 & ANALYTICS_KEY_EVT)
            divert_ok = bool(bfield & (MAP_FLAG_DIVERTED << 1) and bfield & MAP_FLAG_DIVERTED)
            return ReportingChanged(slot, feature, cid, analytics_ok or divert_ok)

    if sw_id & SW_ID_MASK:
        return Response(slot, feature, function, sw_id, payload)

    if sw_id == 0:
        return Notification(slot, feature, function, payload)
    return None


def notification_target(report: Notification, device: DeviceRuntime) -> int | None:
    change_host_index = device.feature_indexes.get(FEATURE_CHANGE_HOST)
    if report.feature_index == change_host_index and report.function == 0 and len(report.payload) >= 2:
        # x1814 reports [departing/current host, target host]. Destination is payload[1].
        target = report.payload[1]
        return target if target in VALID_HOSTS else None

    reprog_index = device.feature_indexes.get(FEATURE_REPROG_CONTROLS_V4)
    if report.feature_index != reprog_index or report.function not in (0, 2) or len(report.payload) < 2:
        return None
    cid = (report.payload[0] << 8) | report.payload[1]
    target = HOST_SWITCH_CIDS.get(cid)
    if target is None:
        return None
    if report.function == 2 and (len(report.payload) < 3 or report.payload[2] != 1):
        return None
    return target


def response_matches(report: ParsedReport, slot: int, feature_index: int, function: int, sw_id: int) -> bool:
    if isinstance(report, HidError):
        return report.slot == slot and report.sw_id == sw_id
    return (
        isinstance(report, Response)
        and report.slot in (slot, slot ^ 0xFF)
        and report.feature_index == feature_index
        and report.function == ((function & 0xF0) >> 4)
        and report.sw_id == sw_id
    )
