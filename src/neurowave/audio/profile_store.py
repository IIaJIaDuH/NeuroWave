"""Пользовательские профили звука: хранение, проверка и поиск по id.

Встроенные пресеты — код, пользовательские — данные. Поэтому профили из
настроек проверяются заново при каждом чтении: испорченный или устаревший
JSON не должен ронять плеер, но и применяться молча не должен.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from ..config import Settings
from ..errors import NeuroWaveError
from .dsp_profiles import BUILTIN_PROFILES, DEFAULT_PROFILE_ID, AudioProfile, DspError


class ProfileStoreError(NeuroWaveError):
    """Профиль нельзя сохранить или удалить."""


@dataclass
class ProfileRegistry:
    """Встроенные и пользовательские профили в одном пространстве id."""

    custom: dict[str, AudioProfile] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @classmethod
    def from_settings(cls, settings: Settings) -> "ProfileRegistry":
        registry = cls()
        for item in getattr(settings, "custom_profiles", []) or []:
            try:
                profile = AudioProfile.from_dict(item)
            except (DspError, TypeError, ValueError) as exc:
                registry.errors.append(f"профиль пропущен: {exc}")
                continue
            if profile.id in BUILTIN_PROFILES:
                registry.errors.append(f"профиль {profile.id!r} совпадает со встроенным — пропущен")
                continue
            registry.custom[profile.id] = profile
        return registry

    # --- чтение --------------------------------------------------------
    def get(self, profile_id: str) -> AudioProfile | None:
        custom = self.custom.get(str(profile_id))
        if custom is not None:
            return custom
        return BUILTIN_PROFILES.get(str(profile_id))

    def resolve(self, profile_id: str) -> AudioProfile:
        """Профиль по id; неизвестный — безопасный встроенный."""
        return self.get(profile_id) or BUILTIN_PROFILES[DEFAULT_PROFILE_ID]

    def all(self) -> list[AudioProfile]:
        return list(BUILTIN_PROFILES.values()) + list(self.custom.values())

    def is_builtin(self, profile_id: str) -> bool:
        return str(profile_id) in BUILTIN_PROFILES

    # --- запись --------------------------------------------------------
    def add(self, profile: AudioProfile) -> AudioProfile:
        if self.is_builtin(profile.id):
            raise ProfileStoreError(f"id {profile.id!r} занят встроенным профилем")
        if profile.id in self.custom:
            raise ProfileStoreError(f"профиль {profile.id!r} уже существует")
        self.custom[profile.id] = profile
        return profile

    def update(self, profile: AudioProfile) -> AudioProfile:
        if self.is_builtin(profile.id):
            raise ProfileStoreError("встроенные профили не изменяются")
        if profile.id not in self.custom:
            raise ProfileStoreError(f"профиля {profile.id!r} нет")
        self.custom[profile.id] = profile
        return profile

    def remove(self, profile_id: str) -> bool:
        if self.is_builtin(profile_id):
            raise ProfileStoreError("встроенные профили не удаляются")
        return self.custom.pop(str(profile_id), None) is not None

    def save(self, settings: Settings) -> None:
        settings.custom_profiles = [
            profile.to_dict() for profile in self.custom.values()  # type: ignore[attr-defined]
        ]

    def to_list(self) -> list[dict[str, Any]]:
        return [profile.to_dict() for profile in self.all()]

    def describe(self) -> Iterable[tuple[str, str, bool]]:
        """(id, имя, встроенный) для интерфейса."""
        for profile in BUILTIN_PROFILES.values():
            yield profile.id, profile.name, True
        for profile in self.custom.values():
            yield profile.id, profile.name, False
