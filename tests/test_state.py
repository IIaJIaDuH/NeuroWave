from __future__ import annotations

import pytest

from neurowave.errors import InvalidTransition
from neurowave.state import Overlay, PlayerStateMachine, PlayerStatus


def test_starts_idle():
    machine = PlayerStateMachine()
    assert machine.status is PlayerStatus.IDLE
    assert machine.overlay is None


def test_valid_playback_sequence():
    machine = PlayerStateMachine()
    machine.transition(PlayerStatus.READY)
    machine.transition(PlayerStatus.BUFFERING)
    machine.transition(PlayerStatus.PLAYING)
    machine.transition(PlayerStatus.PAUSED)
    machine.transition(PlayerStatus.PLAYING)
    machine.transition(PlayerStatus.STOPPED)
    assert machine.status is PlayerStatus.STOPPED


def test_invalid_transition_rejected():
    machine = PlayerStateMachine()
    with pytest.raises(InvalidTransition):
        machine.transition(PlayerStatus.PLAYING)


def test_same_state_is_noop():
    machine = PlayerStateMachine()
    assert machine.transition(PlayerStatus.IDLE) is PlayerStatus.IDLE


def test_overlays_are_independent_from_status():
    machine = PlayerStateMachine()
    machine.transition(PlayerStatus.READY)
    machine.transition(PlayerStatus.BUFFERING)
    machine.transition(PlayerStatus.PLAYING)
    machine.set_overlay(Overlay.OFFLINE, "нет сети")
    assert machine.status is PlayerStatus.PLAYING
    assert machine.overlay is Overlay.OFFLINE
    assert machine.message == "нет сети"
    machine.clear_overlay(Overlay.OFFLINE)
    assert machine.overlay is None


def test_listener_notified():
    events = []
    machine = PlayerStateMachine(listener=lambda status, overlay: events.append((status, overlay)))
    machine.transition(PlayerStatus.READY)
    machine.set_overlay(Overlay.AUTH_EXPIRED)
    assert events[0][0] is PlayerStatus.READY
    assert events[-1][1] is Overlay.AUTH_EXPIRED
