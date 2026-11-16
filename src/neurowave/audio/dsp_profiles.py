"""Профили звука: типизированная модель, валидация и миграции.

Правило, из которого вырос модуль: параметры DSP не приходят строками. Граф
фильтров собирается только из проверенных чисел, поэтому «пресет» не может
превратиться в произвольную команду mpv.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, ClassVar, Mapping

from ..errors import NeuroWaveError

SCHEMA_VERSION = 1

# Границы выбраны так, чтобы заведомо некорректные значения не доходили до
# libavfilter: частота за пределами слышимого диапазона, отрицательная
# добротность и NaN ломают граф или дают мусор на выходе.
MIN_FREQUENCY_HZ = 20.0
MAX_FREQUENCY_HZ = 20000.0
MAX_BANDS = 16
MAX_ABS_GAIN_DB = 24.0
MIN_PREAMP_DB = -24.0
MAX_PREAMP_DB = 12.0
MIN_Q = 0.1
MAX_Q = 10.0
MIN_CEILING_DBFS = -60.0
MAX_CEILING_DBFS = 0.0
MIN_LOOKAHEAD_MS = 1.0
MAX_LOOKAHEAD_MS = 50.0
MIN_RELEASE_MS = 5.0
MAX_RELEASE_MS = 500.0

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

#: Типы полос, поддержанные FFmpeg `equalizer`/`bass`/`treble` через lavfi.
BAND_KINDS = ("peaking", "lowshelf", "highshelf")


class DspError(NeuroWaveError):
    """Некорректный профиль звука или невозможный граф фильтров."""


class DspMode(str, Enum):
    """Режим тракта: обход наших фильтров или обработка."""

    PURE = "pure"
    ENHANCED = "enhanced"

    @classmethod
    def parse(cls, value: Any) -> "DspMode":
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).lower())
        except ValueError as exc:
            allowed = ", ".join(item.value for item in cls)
            raise DspError(f"Неизвестный режим звука: {value!r} (допустимо: {allowed})") from exc


def _finite(value: Any, name: str) -> float:
    """Число, которое переживёт форматирование в граф.

    bool отвергается отдельно: ``True`` — это не громкость и не частота.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DspError(f"{name}: ожидалось число, получено {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise DspError(f"{name}: значение должно быть конечным, получено {value!r}")
    return number


def _bounded(value: Any, name: str, low: float, high: float) -> float:
    number = _finite(value, name)
    if not low <= number <= high:
        raise DspError(f"{name}: {number:g} вне диапазона {low:g}…{high:g}")
    return number


@dataclass(frozen=True)
class EqBand:
    """Одна полоса параметрического эквалайзера."""

    kind: str
    frequency_hz: float
    gain_db: float = 0.0
    q: float = 0.707

    def __post_init__(self) -> None:
        if self.kind not in BAND_KINDS:
            allowed = ", ".join(BAND_KINDS)
            raise DspError(f"Тип полосы {self.kind!r} не поддержан (допустимо: {allowed})")
        object.__setattr__(
            self, "frequency_hz", _bounded(self.frequency_hz, "частота полосы", MIN_FREQUENCY_HZ, MAX_FREQUENCY_HZ)
        )
        object.__setattr__(
            self, "gain_db", _bounded(self.gain_db, "усиление полосы", -MAX_ABS_GAIN_DB, MAX_ABS_GAIN_DB)
        )
        object.__setattr__(self, "q", _bounded(self.q, "добротность полосы", MIN_Q, MAX_Q))

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "frequency_hz": self.frequency_hz,
            "gain_db": self.gain_db,
            "q": self.q,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EqBand":
        if not isinstance(data, Mapping):
            raise DspError(f"Полоса эквалайзера должна быть объектом, получено {data!r}")
        return cls(
            kind=str(data.get("kind", "")),
            frequency_hz=data.get("frequency_hz", 0),
            gain_db=data.get("gain_db", 0.0),
            q=data.get("q", 0.707),
        )


@dataclass(frozen=True)
class Limiter:
    """Параметры защиты от клиппинга в конце цепи."""

    enabled: bool = False
    ceiling_dbfs: float = -1.0
    lookahead_ms: float = 5.0
    release_ms: float = 50.0

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise DspError(f"limiter.enabled должен быть bool, получено {self.enabled!r}")
        object.__setattr__(
            self,
            "ceiling_dbfs",
            _bounded(self.ceiling_dbfs, "потолок лимитера", MIN_CEILING_DBFS, MAX_CEILING_DBFS),
        )
        object.__setattr__(
            self,
            "lookahead_ms",
            _bounded(self.lookahead_ms, "lookahead лимитера", MIN_LOOKAHEAD_MS, MAX_LOOKAHEAD_MS),
        )
        object.__setattr__(
            self,
            "release_ms",
            _bounded(self.release_ms, "release лимитера", MIN_RELEASE_MS, MAX_RELEASE_MS),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "ceiling_dbfs": self.ceiling_dbfs,
            "lookahead_ms": self.lookahead_ms,
            "release_ms": self.release_ms,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Limiter":
        if not isinstance(data, Mapping):
            raise DspError(f"limiter должен быть объектом, получено {data!r}")
        return cls(
            enabled=bool(data.get("enabled", False)),
            ceiling_dbfs=data.get("ceiling_dbfs", -1.0),
            lookahead_ms=data.get("lookahead_ms", 5.0),
            release_ms=data.get("release_ms", 50.0),
        )


@dataclass(frozen=True)
class AudioProfile:
    """Версионированный профиль: preamp → EQ → лимитер."""

    id: str
    name: str
    mode: DspMode = DspMode.ENHANCED
    preamp_db: float = 0.0
    bands: tuple[EqBand, ...] = field(default_factory=tuple)
    limiter: Limiter = field(default_factory=Limiter)
    schema_version: int = SCHEMA_VERSION

    BUILTINS: ClassVar[dict[str, "AudioProfile"]] = {}

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _ID_RE.match(self.id):
            raise DspError(
                f"id профиля {self.id!r} должен быть латиницей в нижнем регистре, "
                "цифрами, '-', '_' (до 32 символов)"
            )
        if not isinstance(self.name, str) or not self.name.strip():
            raise DspError("У профиля должно быть непустое имя")
        if len(self.name) > 64:
            raise DspError("Имя профиля длиннее 64 символов")
        if not isinstance(self.mode, DspMode):
            object.__setattr__(self, "mode", DspMode.parse(self.mode))
        object.__setattr__(
            self, "preamp_db", _bounded(self.preamp_db, "preamp", MIN_PREAMP_DB, MAX_PREAMP_DB)
        )
        bands = tuple(self.bands)
        if len(bands) > MAX_BANDS:
            raise DspError(f"Полос не больше {MAX_BANDS}, получено {len(bands)}")
        for band in bands:
            if not isinstance(band, EqBand):
                raise DspError(f"Полоса должна быть EqBand, получено {band!r}")
        object.__setattr__(self, "bands", bands)
        if not isinstance(self.limiter, Limiter):
            raise DspError(f"limiter должен быть Limiter, получено {self.limiter!r}")
        if self.schema_version != SCHEMA_VERSION:
            raise DspError(
                f"Схема профиля {self.schema_version} не поддержана (актуальна {SCHEMA_VERSION})"
            )
        freqs = [round(band.frequency_hz, 3) for band in bands]
        if len(set(freqs)) != len(freqs):
            raise DspError("Частоты полос не должны повторяться")

    # --- сериализация -------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "name": self.name,
            "mode": self.mode.value,
            "preamp_db": self.preamp_db,
            "bands": [band.to_dict() for band in self.bands],
            "limiter": self.limiter.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AudioProfile":
        if not isinstance(data, Mapping):
            raise DspError(f"Профиль должен быть объектом, получено {data!r}")
        version = data.get("schema_version", 1)  # старые записи писались без версии
        if isinstance(version, bool) or not isinstance(version, int):
            raise DspError(f"schema_version должен быть целым, получено {version!r}")
        if version > SCHEMA_VERSION:
            raise DspError(
                f"Профиль новее поддерживаемой схемы ({version} > {SCHEMA_VERSION}): "
                "обновите приложение"
            )
        # Миграции: сейчас схема одна, но поле версии есть, чтобы будущие
        # изменения не ломали чтение старых профилей молча.
        bands = data.get("bands") or ()
        if not isinstance(bands, (list, tuple)):
            raise DspError(f"bands должен быть списком, получено {bands!r}")
        limiter = data.get("limiter") or {}
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            mode=DspMode.parse(data.get("mode", DspMode.ENHANCED.value)),
            preamp_db=data.get("preamp_db", 0.0),
            bands=tuple(EqBand.from_dict(item) for item in bands),
            limiter=Limiter.from_dict(limiter),
            schema_version=version,
        )

    def with_changes(self, **changes: Any) -> "AudioProfile":
        """Копия с изменениями: профили неизменяемы, UI правит копию."""
        return replace(self, **changes)

    def is_bypass(self) -> bool:
        """True, если профиль ничего не делает и его можно не применять."""
        return self.mode is DspMode.PURE or (not self.bands and self.preamp_db == 0.0 and not self.limiter.enabled)


def _builtin(
    profile_id: str,
    name: str,
    *,
    mode: DspMode = DspMode.ENHANCED,
    preamp_db: float = 0.0,
    bands: tuple[EqBand, ...] = (),
    limiter: Limiter | None = None,
) -> AudioProfile:
    return AudioProfile(
        id=profile_id,
        name=name,
        mode=mode,
        preamp_db=preamp_db,
        bands=bands,
        limiter=limiter or Limiter(),
    )


BUILTIN_PROFILES: dict[str, AudioProfile] = {
    "flat": _builtin(
        "flat",
        "Без обработки",
        mode=DspMode.PURE,
    ),
    "hifi": _builtin(
        "hifi",
        "Hi-Fi: ровно",
        mode=DspMode.ENHANCED,
        # Никакой окраски: только запас и защита от клиппинга.
        limiter=Limiter(enabled=True, ceiling_dbfs=-1.0),
    ),
    "bluetooth": _builtin(
        "bluetooth",
        "Bluetooth: компенсация",
        mode=DspMode.ENHANCED,
        preamp_db=-4.0,
        bands=(
            EqBand("lowshelf", 120.0, 2.5, 0.707),
            EqBand("peaking", 400.0, -1.0, 1.2),
            EqBand("peaking", 3000.0, 1.0, 1.2),
            EqBand("highshelf", 10000.0, -1.5, 0.707),
        ),
        limiter=Limiter(enabled=True, ceiling_dbfs=-1.5),
    ),
    "night": _builtin(
        "night",
        "Ночь: тихо и без скачков",
        mode=DspMode.ENHANCED,
        preamp_db=-6.0,
        bands=(
            EqBand("lowshelf", 80.0, -3.0, 0.707),
            EqBand("peaking", 2500.0, 1.5, 1.0),
        ),
        limiter=Limiter(enabled=True, ceiling_dbfs=-2.0, lookahead_ms=8.0, release_ms=80.0),
    ),
}
AudioProfile.BUILTINS = BUILTIN_PROFILES

DEFAULT_PROFILE_ID = "flat"


def get_profile(profile_id: str) -> AudioProfile:
    """Встроенный профиль по id; неизвестный id — ошибка, а не тихий откат."""
    try:
        return BUILTIN_PROFILES[str(profile_id)]
    except KeyError as exc:
        known = ", ".join(sorted(BUILTIN_PROFILES))
        raise DspError(f"Неизвестный профиль {profile_id!r} (есть: {known})") from exc


def resolve_profile(profile_id: Any) -> AudioProfile:
    """Профиль для настроек: неизвестный id откатывается на безопасный."""
    try:
        return get_profile(str(profile_id))
    except DspError:
        return BUILTIN_PROFILES[DEFAULT_PROFILE_ID]
