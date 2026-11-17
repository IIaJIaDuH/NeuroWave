"""Провайдер локальных файлов: второй настоящий источник музыки."""

from __future__ import annotations

from ..local import LocalMusicAdapter, default_root
from .base import AuthKind, Capability, ProviderContext, ProviderInfo, ProviderSpec

LOCAL_INFO = ProviderInfo(
    id="local",
    title="Локальные файлы",
    capabilities=frozenset(
        {
            Capability.CATALOG,
            Capability.DIRECT_STREAM,
            Capability.SHUFFLE_LIBRARY,
        }
    ),
    auth=AuthKind.NONE,
    implemented=True,
    cache_allowed=False,
    unavailable_hint=(
        "Папка музыки не найдена: укажите local_music_dir в config.json "
        "или создайте ~/Music."
    ),
    summary="Файлы из локальной папки: без сети, аккаунта и DRM",
    notes=(
        "Кеш не используется — файл уже на диске. Теги читаются через tinytag (MIT) "
        "(extras [local]); без него берутся «Исполнитель - Название» и папка "
        "альбома. «Волна» здесь — случайная выборка, а не персонализация."
    ),
)


def build(context: ProviderContext) -> LocalMusicAdapter:
    return LocalMusicAdapter(default_root(context.settings))


LOCAL_SPEC = ProviderSpec(info=LOCAL_INFO, factory=build)
