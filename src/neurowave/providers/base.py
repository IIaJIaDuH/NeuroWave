"""Контракт музыкального провайдера.

Провайдер — это не «всё умеет». Возможности объявляются явно
(:class:`Capability`), потому что сервисы различаются принципиально:

* Яндекс Музыка отдаёт ссылку на поток, и её играет `mpv` (DIRECT_STREAM);
* Spotify официально не отдаёт поток стороннему плееру: только каталог,
  библиотека и управление чужим устройством через Spotify Connect
  (CATALOG + DELEGATED_PLAYBACK);
* у VK Music публичного музыкального API нет вообще (ни одной возможности).

Ядро обязано выбирать путь воспроизведения по возможностям, а не по имени
сервиса. Это же правило не даёт пообещать пользователю то, чего сервис не
разрешает.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol, runtime_checkable

from ..config import Paths, Settings


class Capability(str, Enum):
    """Что провайдер умеет на самом деле."""

    CATALOG = "catalog"  # поиск, метаданные, обложки
    LIBRARY = "library"  # лайки и своя библиотека
    PLAYLISTS = "playlists"  # свои плейлисты
    PERSONAL_RADIO = "personal_radio"  # персональный поток («Моя волна»)
    DIRECT_STREAM = "direct_stream"  # ссылка, которую играет mpv
    DELEGATED_PLAYBACK = "delegated_playback"  # управление чужим плеером/устройством
    SHUFFLE_LIBRARY = "shuffle_library"  # случайная выборка из своей библиотеки
    LYRICS = "lyrics"

    @property
    def title(self) -> str:
        return {
            Capability.CATALOG: "каталог и поиск",
            Capability.LIBRARY: "библиотека и лайки",
            Capability.PLAYLISTS: "плейлисты",
            Capability.PERSONAL_RADIO: "персональный поток",
            Capability.DIRECT_STREAM: "поток в mpv",
            Capability.DELEGATED_PLAYBACK: "управление устройством сервиса",
            Capability.SHUFFLE_LIBRARY: "случайная выборка из библиотеки",
            Capability.LYRICS: "тексты",
        }[self]


class AuthKind(str, Enum):
    OAUTH_DEVICE = "oauth_device"  # код на экране, подтверждение в браузере
    OAUTH_BROWSER = "oauth_browser"  # redirect flow с localhost/callback
    API_TOKEN = "api_token"  # ключ приложения или пользователя
    NONE = "none"  # учётная запись не нужна


@dataclass(frozen=True)
class ProviderInfo:
    """Паспорт провайдера: что это и на что он имеет право."""

    id: str
    title: str
    capabilities: frozenset[Capability] = frozenset()
    auth: AuthKind = AuthKind.OAUTH_DEVICE
    implemented: bool = True
    cache_allowed: bool = True
    summary: str = ""
    notes: str = ""
    #: Название подписки сервиса («Плюс»), если она вообще существует.
    #: None — у провайдера нет подписки, и CLI не должен про неё спрашивать.
    subscription: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "capabilities": sorted(capability.value for capability in self.capabilities),
            "auth": self.auth.value,
            "implemented": self.implemented,
            "cache_allowed": self.cache_allowed,
            "plays_in_mpv": self.plays_in_mpv,
            "summary": self.summary,
            "notes": self.notes,
        }

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    @property
    def plays_in_mpv(self) -> bool:
        """Может ли ядро играть трек своим аудиовыходом."""
        return self.supports(Capability.DIRECT_STREAM)

    @property
    def capabilities_text(self) -> str:
        if not self.capabilities:
            return "нет доступных возможностей"
        order = list(Capability)
        items = sorted(self.capabilities, key=order.index)
        return ", ".join(item.title for item in items)


@dataclass(frozen=True)
class ProviderContext:
    """Всё, что нужно провайдеру для работы: токен, пути, настройки."""

    token: str | None = None
    paths: Paths | None = None
    settings: Settings = field(default_factory=Settings)


@dataclass(frozen=True)
class ProviderSpec:
    """Регистрационная запись: паспорт + способы создать адаптер и войти."""

    info: ProviderInfo
    factory: Callable[[ProviderContext], Any] | None = None
    login: Callable[[ProviderContext, Callable[[str, str], None]], str] | None = None

    @property
    def id(self) -> str:
        return self.info.id


@runtime_checkable
class MusicProvider(Protocol):
    """Минимальный контракт, который ожидает `PlayerService` от адаптера.

    Реализация провайдера не обязана уметь всё: отсутствующие операции
    объявляются в :attr:`ProviderInfo.capabilities`, а ядро не должно их
    вызывать.
    """

    def wave_tracks(self) -> list[Any]: ...

    def wave_feedback(self, kind: str, track_id: str | None = ..., total_played_seconds: float | None = ...) -> bool: ...

    def stream_url(self, track_id: str) -> Any: ...
