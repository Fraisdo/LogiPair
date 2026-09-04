from __future__ import annotations

import time
from concurrent.futures import Future

from conftest import wait_until

from logipair.controller import PairController
from logipair.model import DeviceRole, DeviceRuntime, DeviceState, HostChange, SwitchResult


class StubActor:
    def __init__(self):
        self.calls = []
        self.ensure_calls = []
        self.result = SwitchResult(True, "ok", time.monotonic())

    def switch(self, identity, target_host, observed_at):
        self.calls.append((identity, target_host, observed_at))
        future = Future()
        future.set_result(SwitchResult(self.result.ok, self.result.detail, time.monotonic()))
        return future

    def ensure_ready(self, reason):
        self.ensure_calls.append(reason)


def device(identity, role):
    value = DeviceRuntime(identity, 1, 1, 0xFF, "Bluetooth", role, identity)
    value.transition(DeviceState.READY)
    return value


def test_source_exclusion_and_reverse_path():
    controller = PairController(lambda *_: None, debounce_seconds=0)
    keyboard, mouse = device("keyboard", DeviceRole.KEYBOARD), device("mouse", DeviceRole.MOUSE)
    keyboard_actor, mouse_actor = StubActor(), StubActor()
    controller.register(keyboard, keyboard_actor)
    controller.register(mouse, mouse_actor)
    controller.start()
    controller.submit(HostChange("keyboard", DeviceRole.KEYBOARD, 2, time.monotonic()))
    controller.submit(HostChange("mouse", DeviceRole.MOUSE, 1, time.monotonic()))
    wait_until(lambda: len(mouse_actor.calls) == 1 and len(keyboard_actor.calls) == 1)
    controller.stop()
    controller.join()
    assert mouse_actor.calls[0][0:2] == ("mouse", 2)
    assert keyboard_actor.calls[0][0:2] == ("keyboard", 1)


def test_duplicate_notifications_create_one_transaction():
    controller = PairController(lambda *_: None, debounce_seconds=1)
    keyboard, mouse = device("keyboard", DeviceRole.KEYBOARD), device("mouse", DeviceRole.MOUSE)
    mouse_actor = StubActor()
    controller.register(keyboard, StubActor())
    controller.register(mouse, mouse_actor)
    controller.start()
    observed = time.monotonic()
    controller.submit(HostChange("keyboard", DeviceRole.KEYBOARD, 2, observed))
    controller.submit(HostChange("keyboard", DeviceRole.KEYBOARD, 2, observed + 0.01))
    wait_until(lambda: len(mouse_actor.calls) == 1)
    time.sleep(0.05)
    controller.stop()
    controller.join()
    assert len(mouse_actor.calls) == 1


def test_peer_not_ready_triggers_recovery_without_write():
    controller = PairController(lambda *_: None)
    keyboard, mouse = device("keyboard", DeviceRole.KEYBOARD), device("mouse", DeviceRole.MOUSE)
    mouse.transition(DeviceState.RECOVERING)
    mouse_actor = StubActor()
    controller.register(keyboard, StubActor())
    controller.register(mouse, mouse_actor)
    controller.start()
    controller.submit(HostChange("keyboard", DeviceRole.KEYBOARD, 0, time.monotonic()))
    wait_until(lambda: mouse_actor.ensure_calls)
    controller.stop()
    controller.join()
    assert mouse_actor.calls == []


def test_invalid_and_unknown_sources_are_ignored():
    controller = PairController(lambda *_: None, debounce_seconds=0)
    mouse = device("mouse", DeviceRole.MOUSE)
    actor = StubActor()
    controller.register(mouse, actor)
    controller.start()
    controller.submit(HostChange("ghost", DeviceRole.KEYBOARD, 0, time.monotonic()))
    controller.submit(HostChange("ghost", DeviceRole.KEYBOARD, 9, time.monotonic()))
    time.sleep(0.05)
    controller.stop()
    controller.join()
    assert actor.calls == []


def test_missing_peer_is_degraded_without_writing():
    states = []
    controller = PairController(lambda health, _devices: states.append(health))
    keyboard = device("keyboard", DeviceRole.KEYBOARD)
    actor = StubActor()
    controller.register(keyboard, actor)
    controller.start()
    controller.submit(HostChange("keyboard", DeviceRole.KEYBOARD, 1, time.monotonic()))
    wait_until(lambda: controller._queue.empty())
    controller.stop()
    controller.join()
    assert actor.calls == []
    assert states
