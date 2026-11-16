"""Провайдер SoundCloud: каталог и прямой поток для playable-треков."""

from __future__ import annotations

from ..soundcloud import SoundCloudAdapter
from .base import AuthKind, Capability, ProviderContext, ProviderInfo, ProviderSpec

SOUNDCLOUD_INFO = ProviderInfo(
    id="soundcloud",
    title="SoundCloud",
    capabilities=frozenset({Capability.CATALOG, Capability.DIRECT_STREAM}),
    auth=AuthKind.API_TOKEN,
    implemented=True,
    cache_allowed=False,
    summary="Поиск и поток доступных треков в mpv; нужен ключ приложения (Artist Pro)",
    notes=(
        "Кеш отключён: условия API запрещают постоянное хранение. Превью и "
        "заблокированные треки не попадают в очередь, у треков сохраняется "
        "ссылка на страницу автора — атрибуция обязательна."
    ),
)


def build(context: ProviderContext) -> SoundCloudAdapter:
    return SoundCloudAdapter(token=context.token)


SOUNDCLOUD_SPEC = ProviderSpec(info=SOUNDCLOUD_INFO, factory=build)
