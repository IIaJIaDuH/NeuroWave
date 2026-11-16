"""Машина состояний плеера.

Основное состояние — одно из :class:`PlayerStatus`. Ошибки и деградации не
смешиваются с ним: они живут в :class:`Overlay`, чтобы UI мог, например,
показывать «играет, но сеть недоступна».
"""

from __future__ import annotations

from enum import Enum
from typing import Callable

from .errors import InvalidTransition


class PlayerStatus(str, Enum):
    IDLE = "idle"
    AUTHORIZING = "authorizing"
    READY = "ready"
    BUFFERING = "buffering"
    PLAYING = "playing"
    PAUSED = "paused"
    STOPPED = "stopped"


class Overlay(str, Enum):
    OFFLINE = "offline"
    ERROR_RETRYABLE = "error_retryable"
    AUTH_EXPIRED = "auth_expired"
    SUBSCRIPTION_REQUIRED = "subscription_required"
    TRACK_UNAVAILABLE = "track_unavailable"
    DEVICE_CHANGED = "device_changed"


ALLOWED: dict[PlayerStatus, frozenset[PlayerStatus]] = {
    PlayerStatus.IDLE: frozenset(
        {PlayerStatus.AUTHORIZING, PlayerStatus.READY, PlayerStatus.STOPPED}
    ),
    PlayerStatus.AUTHORIZING: frozenset({PlayerStatus.IDLE, PlayerStatus.READY}),
    PlayerStatus.READY: frozenset(
        {PlayerStatus.BUFFERING, PlayerStatus.STOPPED, PlayerStatus.IDLE, PlayerStatus.AUTHORIZING}
    ),
    PlayerStatus.BUFFERING: frozenset(
        {PlayerStatus.PLAYING, PlayerStatus.READY, PlayerStatus.STOPPED}
    ),
    PlayerStatus.PLAYING: frozenset(
        {PlayerStatus.PAUSED, PlayerStatus.BUFFERING, PlayerStatus.STOPPED, PlayerStatus.READY}
    ),
    PlayerStatus.PAUSED: frozenset(
        {PlayerStatus.PLAYING, PlayerStatus.BUFFERING, PlayerStatus.STOPPED, PlayerStatus.READY}
    ),
    PlayerStatus.STOPPED: frozenset(
        {PlayerStatus.READY, PlayerStatus.BUFFERING, PlayerStatus.IDLE}
    ),
}


class PlayerStateMachine:
    def __init__(self, listener: Callable[[PlayerStatus, Overlay | None], None] | None = None):
        self._status = PlayerStatus.IDLE
        self._overlays: dict[Overlay, str | None] = {}
        self._listener = listener

    @property
    def status(self) -> PlayerStatus:
        return self._status

    @property
    def overlay(self) -> Overlay | None:
        """Самый важный активный overlay (первый по порядку добавления)."""
        return next(iter(self._overlays), None)

    @property
    def overlays(self) -> dict[Overlay, str | None]:
        return dict(self._overlays)

    @property
    def message(self) -> str | None:
        overlay = self.overlay
        return self._overlays.get(overlay) if overlay else None

    @property
    def is_playing(self) -> bool:
        return self._status is PlayerStatus.PLAYING

    def can_transition(self, new: PlayerStatus) -> bool:
        if new is self._status:
            return True
        return new in ALLOWED[self._status]

    def transition(self, new: PlayerStatus | str) -> PlayerStatus:
        target = PlayerStatus(new)
        if target is self._status:
            return self._status
        if not self.can_transition(target):
            raise InvalidTransition(f"{self._status.value} → {target.value} недопустим")
        self._status = target
        self._notify()
        return self._status

    def set_overlay(self, overlay: Overlay, message: str | None = None) -> None:
        self._overlays[overlay] = message
        self._notify()

    def clear_overlay(self, overlay: Overlay | None = None) -> None:
        if overlay is None:
            self._overlays.clear()
        else:
            self._overlays.pop(overlay, None)
        self._notify()

    def describe(self) -> str:
        parts = [self._status.value]
        if self.overlay:
            parts.append(f"overlay={self.overlay.value}")
        return " ".join(parts)

    def _notify(self) -> None:
        if self._listener:
            self._listener(self._status, self.overlay)
