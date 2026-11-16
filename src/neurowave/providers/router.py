"""Маршрутизация вызовов к нужному провайдеру.

`PlayerService` работает с треками разных провайдеров в одной очереди.
Роутер смотрит на ``track.provider`` (или на ключ ``provider:track_id``) и
вызывает адаптер этого провайдера; адаптеры создаются лениво и кешируются.

Ошибки здесь не глотаются: если трек требует провайдера, которого нет,
пользователь должен увидеть причину, а не тихую остановку.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from ..domain import StreamInfo, Track
from ..errors import NeuroWaveError
from .base import Capability, ProviderInfo
from .registry import ProviderNotImplemented, get_spec


class ProviderRouter:
    """Адаптеры по требованию + маршрутизация по провайдеру трека."""

    def __init__(
        self,
        builders: Mapping[str, Callable[[], Any]],
        default: str = "yandex",
    ):
        self._builders = dict(builders)
        self._default = default
        self._adapters: dict[str, Any] = {}

    # --- адаптеры -----------------------------------------------------
    @property
    def default(self) -> str:
        return self._default

    def providers(self) -> list[str]:
        return sorted(self._builders)

    def adapter(self, provider_id: str | None) -> Any:
        pid = provider_id or self._default
        adapter = self._adapters.get(pid)
        if adapter is not None:
            return adapter
        builder = self._builders.get(pid)
        if builder is None:
            known = ", ".join(self.providers()) or "нет"
            raise ProviderNotImplemented(
                f"Трек требует провайдера «{pid}», который не подключён. Доступны: {known}."
            )
        adapter = builder()
        self._adapters[pid] = adapter
        return adapter

    def adapter_for(self, track: Track) -> Any:
        return self.adapter(track.provider)

    # --- данные -------------------------------------------------------
    def stream_url(self, track: Track) -> StreamInfo:
        """Ссылка на поток именно у того провайдера, которому принадлежит трек."""
        adapter = self.adapter_for(track)
        stream_url = getattr(adapter, "stream_url", None)
        if stream_url is None:
            info = self.info(track.provider)
            title = info.title if info else (track.provider or self._default)
            raise ProviderNotImplemented(
                f"«{title}» не отдаёт поток: воспроизведение возможно только "
                "в плеере сервиса."
            )
        return stream_url(track.id)

    def wave_tracks(self) -> list[Track]:
        adapter = self.adapter(self._default)
        wave_tracks = getattr(adapter, "wave_tracks", None)
        if wave_tracks is None:
            raise ProviderNotImplemented(
                f"«{self.info(self._default).title if self.info(self._default) else self._default}» "
                "не поддерживает персональный поток."
            )
        return list(wave_tracks())

    def wave_feedback(
        self,
        kind: str,
        track_key: str | None,
        total_played_seconds: float | None = None,
    ) -> bool:
        provider, raw_id = Track.split_key(track_key or "")
        adapter = self.adapter(provider)
        feedback = getattr(adapter, "wave_feedback", None)
        if feedback is None:
            return False
        return bool(feedback(kind, raw_id, total_played_seconds))

    # --- политика -----------------------------------------------------
    def info(self, provider_id: str | None) -> ProviderInfo | None:
        pid = provider_id or self._default
        try:
            return get_spec(pid).info
        except NeuroWaveError:
            return None

    def supports(self, capability: Capability, provider_id: str | None = None) -> bool:
        info = self.info(provider_id)
        return bool(info and info.supports(capability))

    def cache_allowed(self, track: Track | None) -> bool:
        """Разрешено ли кешировать этот трек (локальные файлы — незачем)."""
        if track is None:
            return False
        info = self.info(track.provider)
        return True if info is None else bool(info.cache_allowed)
