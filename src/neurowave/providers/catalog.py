"""Провайдеры, которые осознанно не подключены, и причина этого.

Список — не обещание сроков, а честная фиксация: какие сервисы вообще могут
дать плееру поток, а какие разрешают только каталог или управление своим
устройством. Данные сверены с документацией сервисов в ноябре 2026
(подробности и ссылки — ADR-0004).
"""

from __future__ import annotations

from .base import AuthKind, Capability, ProviderInfo

PLANNED: tuple[ProviderInfo, ...] = (
    ProviderInfo(
        id="spotify",
        title="Spotify",
        capabilities=frozenset(
            {
                Capability.CATALOG,
                Capability.LIBRARY,
                Capability.PLAYLISTS,
                Capability.DELEGATED_PLAYBACK,
            }
        ),
        auth=AuthKind.OAUTH_BROWSER,
        implemented=False,
        summary="Только каталог и управление своим устройством Spotify Connect",
        notes=(
            "Официального способа отдать поток стороннему плееру нет: Web Playback "
            "SDK работает в браузере и не выдаёт ссылку. Стриминг вне сервиса "
            "запрещён условиями, поэтому режим возможен только как «пульт»."
        ),
    ),
    ProviderInfo(
        id="deezer",
        title="Deezer",
        capabilities=frozenset(
            {Capability.CATALOG, Capability.LIBRARY, Capability.PLAYLISTS, Capability.DELEGATED_PLAYBACK}
        ),
        auth=AuthKind.OAUTH_BROWSER,
        implemented=False,
        summary="Каталог и библиотека; REST отдаёт только 30-секундные превью",
        notes="Полное воспроизведение — только через официальный плеер/SDK Deezer.",
    ),
    ProviderInfo(
        id="tidal",
        title="TIDAL",
        capabilities=frozenset(
            {Capability.CATALOG, Capability.LIBRARY, Capability.PLAYLISTS, Capability.DELEGATED_PLAYBACK}
        ),
        auth=AuthKind.OAUTH_BROWSER,
        implemented=False,
        summary="Каталог и метаданные; поток — только внутри официального Player SDK",
        notes="Условия разработчика ограничивают некоммерческое использование и интеграции.",
    ),
    ProviderInfo(
        id="youtube-music",
        title="YouTube Music",
        capabilities=frozenset({Capability.CATALOG, Capability.PLAYLISTS}),
        auth=AuthKind.OAUTH_BROWSER,
        implemented=False,
        summary="Официального API именно музыки нет; извлечение аудио запрещено политикой",
        notes="Неофициальные библиотеки строятся на недокументированных интерфейсах — не основа.",
    ),
    ProviderInfo(
        id="vk",
        title="VK Музыка",
        capabilities=frozenset(),
        auth=AuthKind.OAUTH_BROWSER,
        implemented=False,
        summary="Публичного музыкального API нет: ни каталога, ни потока",
        notes=(
            "Публичные аудио-методы закрыты с 2016 года. Подключение возможно только "
            "по отдельной договорённости с VK; обход не поддерживается."
        ),
    ),
)
