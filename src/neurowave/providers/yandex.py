"""Яндекс Музыка как провайдер: единственный, у кого сейчас есть поток в mpv."""

from __future__ import annotations

from typing import Callable

from ..yandex import YandexMusicAdapter
from .base import AuthKind, Capability, ProviderContext, ProviderInfo, ProviderSpec

YANDEX_INFO = ProviderInfo(
    id="yandex",
    title="Яндекс Музыка",
    capabilities=frozenset(
        {
            Capability.CATALOG,
            Capability.LIBRARY,
            Capability.PLAYLISTS,
            Capability.PERSONAL_RADIO,
            Capability.DIRECT_STREAM,
        }
    ),
    auth=AuthKind.OAUTH_DEVICE,
    implemented=True,
    summary="«Моя волна», поиск, библиотека и playlists; поток играет mpv",
    notes=(
        "Неофициальный API: функции могут меняться. Lossless показывается только "
        "по фактическому кодеку потока."
    ),
)


def build(context: ProviderContext) -> YandexMusicAdapter:
    """Адаптер для сессии. Токен берётся из хранилища провайдера."""
    return YandexMusicAdapter(token=context.token)


def login(context: ProviderContext, on_code: Callable[[str, str], None]) -> str:
    """OAuth Device Flow: вернуть токен, не показывая и не логируя его."""
    return YandexMusicAdapter().device_login(on_code)


YANDEX_SPEC = ProviderSpec(info=YANDEX_INFO, factory=build, login=login)
