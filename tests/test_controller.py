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
    value.switch_capable = True
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


def test_pending_switch_runs_once_when_peer_becomes_switch_capable():
    controller = PairController(lambda *_: None, pending_ttl_seconds=1)
    keyboard, mouse = device("keyboard", DeviceRole.KEYBOARD), device("mouse", DeviceRole.MOUSE)
    mouse.switch_capable = False
    mouse.transition(DeviceState.RECOVERING)
    keyboard_actor = StubActor()
    mouse_actor = StubActor()
    controller.register(keyboard, keyboard_actor)
    controller.register(mouse, mouse_actor)
    controller.start()
    controller.submit(HostChange("keyboard", DeviceRole.KEYBOARD, 0, time.monotonic()))
    wait_until(lambda: mouse_actor.ensure_calls and controller.pending_count == 1)
    mouse.switch_capable = True
    mouse.transition(DeviceState.INITIALIZING)
    controller.state_changed(mouse)
    wait_until(lambda: len(mouse_actor.calls) == 1)
    controller.stop()
    controller.join()
    assert mouse_actor.calls[0][0:2] == ("mouse", 0)
    assert keyboard_actor.calls == []
    assert controller.pending_count == 0


def test_latest_pending_intent_wins():
    controller = PairController(lambda *_: None, debounce_seconds=0, pending_ttl_seconds=1)
    keyboard, mouse = device("keyboard", DeviceRole.KEYBOARD), device("mouse", DeviceRole.MOUSE)
    mouse.switch_capable = False
    actor = StubActor()
    controller.register(keyboard, StubActor())
    controller.register(mouse, actor)
    controller.start()
    now = time.monotonic()
    controller.submit(HostChange("keyboard", DeviceRole.KEYBOARD, 1, now))
    controller.submit(HostChange("keyboard", DeviceRole.KEYBOARD, 2, now + 0.01))
    wait_until(lambda: controller._last_event is not None and controller._last_event[1] == 2)
    mouse.switch_capable = True
    controller.state_changed(mouse)
    wait_until(lambda: len(actor.calls) == 1)
    controller.stop()
    controller.join()
    assert [call[1] for call in actor.calls] == [2]


def test_expired_pending_intent_is_not_replayed():
    controller = PairController(lambda *_: None, pending_ttl_seconds=0.03)
    keyboard, mouse = device("keyboard", DeviceRole.KEYBOARD), device("mouse", DeviceRole.MOUSE)
    mouse.switch_capable = False
    actor = StubActor()
    controller.register(keyboard, StubActor())
    controller.register(mouse, actor)
    controller.start()
    controller.submit(HostChange("keyboard", DeviceRole.KEYBOARD, 1, time.monotonic()))
    wait_until(lambda: controller.pending_count == 1)
    wait_until(lambda: controller.pending_count == 0)
    mouse.switch_capable = True
    controller.state_changed(mouse)
    time.sleep(0.05)
    controller.stop()
    controller.join()
    assert actor.calls == []


def test_source_disappearing_does_not_cancel_pending_intent():
    controller = PairController(lambda *_: None, pending_ttl_seconds=1)
    keyboard, mouse = device("keyboard", DeviceRole.KEYBOARD), device("mouse", DeviceRole.MOUSE)
    mouse.switch_capable = False
    actor = StubActor()
    controller.register(keyboard, StubActor())
    controller.register(mouse, actor)
    controller.start()
    controller.submit(HostChange("keyboard", DeviceRole.KEYBOARD, 2, time.monotonic()))
    wait_until(lambda: controller.pending_count == 1)
    controller.unregister("keyboard")
    mouse.switch_capable = True
    controller.state_changed(mouse)
    wait_until(lambda: len(actor.calls) == 1)
    controller.stop()
    controller.join()
    assert actor.calls[0][0:2] == ("mouse", 2)


def test_debounce_allows_real_a_b_a_sequence():
    controller = PairController(lambda *_: None, debounce_seconds=1)
    keyboard, mouse = device("keyboard", DeviceRole.KEYBOARD), device("mouse", DeviceRole.MOUSE)
    actor = StubActor()
    controller.register(keyboard, StubActor())
    controller.register(mouse, actor)
    controller.start()
    now = time.monotonic()
    for target in (0, 1, 0):
        controller.submit(HostChange("keyboard", DeviceRole.KEYBOARD, target, now))
    wait_until(lambda: len(actor.calls) == 3)
    controller.stop()
    controller.join()
    assert [call[1] for call in actor.calls] == [0, 1, 0]


def test_failed_pending_switch_has_bounded_replay():
    controller = PairController(lambda *_: None, debounce_seconds=0, pending_ttl_seconds=0.15)
    keyboard, mouse = device("keyboard", DeviceRole.KEYBOARD), device("mouse", DeviceRole.MOUSE)
    actor = StubActor()
    actor.result = SwitchResult(False, "write failed", time.monotonic())
    controller.register(keyboard, StubActor())
    controller.register(mouse, actor)
    controller.start()
    controller.submit(HostChange("keyboard", DeviceRole.KEYBOARD, 2, time.monotonic()))
    wait_until(lambda: len(actor.calls) == 1 and controller.pending_count == 1)
    mouse.switch_capable = True
    controller.state_changed(mouse)
    wait_until(lambda: len(actor.calls) == 2)
    mouse.switch_capable = True
    controller.state_changed(mouse)
    time.sleep(0.05)
    assert len(actor.calls) == 2
    wait_until(lambda: controller.pending_count == 0)
    controller.stop()
    controller.join()


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
