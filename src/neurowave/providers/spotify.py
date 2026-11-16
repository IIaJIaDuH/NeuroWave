"""Провайдер Spotify: каталог и пульт Spotify Connect (без потока)."""

from __future__ import annotations

from ..spotify import SpotifyAdapter
from .base import AuthKind, Capability, ProviderContext, ProviderInfo, ProviderSpec

SPOTIFY_INFO = ProviderInfo(
    id="spotify",
    title="Spotify",
    capabilities=frozenset({Capability.CATALOG, Capability.DELEGATED_PLAYBACK}),
    auth=AuthKind.API_TOKEN,
    implemented=True,
    cache_allowed=False,
    subscription="Premium",
    summary="Каталог и пульт Spotify Connect: звук остаётся в приложении Spotify",
    notes=(
        "Web API не отдаёт ссылку на поток, поэтому mpv не участвует и кеша нет. "
        "Управление воспроизведением требует Premium и активного устройства; "
        "Linux-SDK Spotify нет, поэтому включён только режим «пульт»."
    ),
)


def build(context: ProviderContext) -> SpotifyAdapter:
    return SpotifyAdapter(token=context.token)


SPOTIFY_SPEC = ProviderSpec(info=SPOTIFY_INFO, factory=build)
