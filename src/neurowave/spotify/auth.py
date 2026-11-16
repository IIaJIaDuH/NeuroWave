"""Учётные данные Spotify: access token, refresh token и срок жизни.

Хранится всё в одном слоте `TokenStore`, что и у других провайдеров: либо
плоский access token (старые входы), либо JSON с refresh-полями. Значения
никогда не печатаются и не попадают в сообщения об ошибках.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

STORAGE_VERSION = 1


@dataclass
class SpotifyCredentials:
    """Токены Spotify; ``refresh_token`` позволяет пережить истечение access."""

    access_token: str = ""
    refresh_token: str | None = None
    client_id: str | None = None
    expires_at: float | None = None

    @property
    def can_refresh(self) -> bool:
        """Для обновления нужны и refresh token, и client_id приложения."""
        return bool(self.refresh_token and self.client_id)

    def expired(self, now: float, skew: float = 60.0) -> bool:
        """Считаем токен истёкшим заранее: сеть и часы не идеальны."""
        return self.expires_at is not None and now >= self.expires_at - skew

    @classmethod
    def from_storage(cls, raw: str | None) -> "SpotifyCredentials":
        text = (raw or "").strip()
        if not text:
            return cls()
        if text.startswith("{"):
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                return cls(access_token=text)
            if isinstance(data, dict):
                access = str(data.get("access_token") or "").strip()
                if access:
                    expires = data.get("expires_at")
                    return cls(
                        access_token=access,
                        refresh_token=_optional(data.get("refresh_token")),
                        client_id=_optional(data.get("client_id")),
                        expires_at=(
                            float(expires)
                            if isinstance(expires, (int, float))
                            else None
                        ),
                    )
        return cls(access_token=text)

    def to_storage(self) -> str:
        if not (self.refresh_token or self.client_id or self.expires_at):
            return self.access_token
        payload: dict[str, Any] = {
            "version": STORAGE_VERSION,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "expires_at": self.expires_at,
        }
        return json.dumps(payload, ensure_ascii=False)


def _optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
