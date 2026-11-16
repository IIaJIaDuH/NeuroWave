"""Spotify как «пульт»: каталог и управление Spotify Connect без потока.

Никакого извлечения аудио: поток остаётся в приложении Spotify, ядро только
командует устройством (`DELEGATED_PLAYBACK`). Сеть живёт в
:mod:`neurowave.spotify.client` и подменяется транспортом в тестах.
"""

from .adapter import SpotifyAdapter, SpotifyDevice
from .client import HttpResponse, SpotifyClient, UrllibTransport

__all__ = ["SpotifyAdapter", "SpotifyDevice", "SpotifyClient", "HttpResponse", "UrllibTransport"]
