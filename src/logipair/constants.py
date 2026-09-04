from __future__ import annotations

APP_NAME = "LogiPair"
MUTEX_NAME = r"Local\LogiPair.SingleInstance"
STATUS_SCHEMA_VERSION = 1
CACHE_SCHEMA_VERSION = 1

LOGITECH_VENDOR_ID = 0x046D
BOLT_PID = 0xC548
UNIFYING_PIDS = frozenset({0xC52B, 0xC52F, 0xC532, 0xC534})
# LIGHTSPEED/Nano dongles. They enumerate as "USB Receiver" and must never be mistaken
# for a Bluetooth-direct device on slot 0xFF (0xC547 is the one seen on this machine).
LIGHTSPEED_PIDS = frozenset({0xC539, 0xC53A, 0xC53D, 0xC53F, 0xC541, 0xC545, 0xC547, 0xC54D})
RECEIVER_PIDS = frozenset({BOLT_PID, *UNIFYING_PIDS, *LIGHTSPEED_PIDS})

HIDPP_USAGE_PAGES = frozenset({0xFF00, 0xFF43})
HIDPP_USAGE_SHORT = 0x0001
HIDPP_USAGE_LONG = 0x0002
HIDPP_BT_USAGE_LONG = 0x0202
LONG_USAGES = frozenset({HIDPP_USAGE_LONG, HIDPP_BT_USAGE_LONG})

REPORT_SHORT = 0x10
REPORT_LONG = 0x11
REPORT_DJ = 0x20
REPORT_LENGTHS = {REPORT_SHORT: 7, REPORT_LONG: 20, REPORT_DJ: 15}
MAX_READ_SIZE = 32
DIRECT_DEVICE_SLOT = 0xFF

FEATURE_ROOT = 0x0000
FEATURE_DEVICE_TYPE_AND_NAME = 0x0005
FEATURE_CHANGE_HOST = 0x1814
FEATURE_REPROG_CONTROLS_V4 = 0x1B04
FEATURE_WIRELESS_DEVICE_STATUS = 0x1D4B

DEVICE_TYPE_KEYBOARD = 0
DEVICE_TYPE_MOUSE = 3
DEVICE_TYPE_TRACKPAD = 4
DEVICE_TYPE_TRACKBALL = 5

HOST_SWITCH_CIDS = {0x00D1: 0, 0x00D2: 1, 0x00D3: 2}
VALID_HOSTS = frozenset(HOST_SWITCH_CIDS.values())

KEY_FLAG_ANALYTICS = 0x04
KEY_FLAG_DIVERTABLE = 0x20
KEY_FLAG_PERSISTENTLY_DIVERTABLE = 0x40
ANALYTICS_BYTE9 = 0x03
ANALYTICS_AVALID = 0x04
ANALYTICS_KEY_EVT = 0x02
MAP_FLAG_DIVERTED = 0x01

SW_ID_REQUEST = 0x08
SW_ID_DIVERT = 0x0E
SW_ID_HOST_CHANGE = 0x0F
SW_ID_MASK = 0x08
CHANGE_HOST_FN_SET = 0x10

ENABLE_RECEIVER_NOTIFICATIONS = bytes([0x10, 0xFF, 0x80, 0x00, 0x00, 0x09, 0x00])
ENUMERATE_RECEIVER_DEVICES = bytes([0x10, 0xFF, 0x80, 0x02, 0x02, 0x00, 0x00])

BACKOFF_SECONDS = (0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0)

# Two distinct notions, deliberately separated.
#
# The grace window only classifies the errors Windows produces while it tears the old
# live handle down. It is short because it is about handle teardown, not about the user.
DEPARTURE_GRACE_SECONDS = 2.5
DEPARTURE_GRACE_RETRY_SECONDS = 0.1
#
# "Away" has no deadline at all: after an Easy-Switch the pair sits on another host for
# as long as the user wants - seconds, or hours. While the device is away AND absent from
# enumeration there is nothing to open, so the actor must stay quiet instead of climbing
# a retry ladder against a device that is simply elsewhere.

# Windows can expose a Bluetooth HID path before the collection is usable: the open
# succeeds and the first real operation fails with ERROR_DEVICE_NOT_CONNECTED (0x48F).
# Failures this soon after an arrival are evidence of an unsettled enumeration, so they
# get a fast bounded retry instead of the exponential ladder.
PROVISIONAL_ARRIVAL_SECONDS = 1.0
PROVISIONAL_RETRY_SECONDS = 0.05
# Non-blocking reads used to prove a freshly opened REPORT_LONG path is really alive.
PROVISIONAL_VALIDATION_READS = 3
# While the pair is away but enumeration has not caught up yet, poll presence at a
# calm fixed rate instead of climbing the ladder against a device that is elsewhere.
AWAY_PRESENCE_POLL_SECONDS = 1.0

# Windows raises several WM_DEVICECHANGE messages per physical device because each HID
# interface appears/disappears separately. One burst must yield one reconciliation.
LIFECYCLE_COALESCE_SECONDS = 0.15
LIFECYCLE_COALESCE_MAX_SECONDS = 0.5

# V1 targets exactly one pair on direct Bluetooth: MX Keys + MX Anywhere 3S. The
# LIGHTSPEED dongle on this machine holds neither, and any actor for it is pure noise.
V1_IGNORED_PIDS = frozenset({0xC547})
