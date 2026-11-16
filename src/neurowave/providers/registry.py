"""Реестр провайдеров: встроенные, планируемые и сторонние плагины.

Встроенные провайдеры регистрируются при импорте пакета. Сторонние
подключаются точками входа (entry points) группы ``neurowave.providers``:
достаточно объявить в своём пакете

.. code-block:: toml

    [project.entry-points."neurowave.providers"]
    soundcloud = "my_soundcloud_provider:spec"

где ``spec`` — объект :class:`ProviderSpec` или функция без аргументов,
возвращающая его. Ядро менять не нужно.
"""

from __future__ import annotations

from typing import Any, Iterable

from ..errors import NeuroWaveError
from .base import ProviderContext, ProviderInfo, ProviderSpec
from .local import LOCAL_SPEC
from .soundcloud import SOUNDCLOUD_SPEC
from .spotify import SPOTIFY_SPEC
from .yandex import YANDEX_SPEC

ENTRY_POINT_GROUP = "neurowave.providers"

_REGISTRY: dict[str, ProviderSpec] = {}
_PLUGIN_ERRORS: list[str] = []


def register(spec: ProviderSpec, *, replace: bool = False) -> ProviderSpec:
    """Добавить провайдера. Дубликат — ошибка, если не ``replace``."""
    if not spec.id:
        raise ValueError("у провайдера должен быть id")
    if spec.id in _REGISTRY and not replace:
        raise ValueError(f"провайдер «{spec.id}» уже зарегистрирован")
    _REGISTRY[spec.id] = spec
    return spec


def get_spec(provider_id: str) -> ProviderSpec:
    """Найти провайдера или объяснить, какие есть и почему нет запрошенного."""
    spec = _REGISTRY.get(provider_id)
    if spec is not None:
        return spec
    for info in planned():
        if info.id == provider_id:
            raise ProviderNotImplemented(
                f"Провайдер «{info.title}» ещё не подключён. {info.summary}".strip()
            )
    known = ", ".join(sorted(_REGISTRY)) or "нет"
    raise UnknownProvider(
        f"Неизвестный провайдер «{provider_id}». Доступны: {known}. "
        "Список: neurowave providers"
    )


def create(spec: ProviderSpec, context: ProviderContext) -> Any:
    """Создать адаптер провайдера (объект для `PlayerService`)."""
    if spec.factory is None:
        raise ProviderNotImplemented(
            f"Провайдер «{spec.info.title}» ещё не реализован: "
            "нет способа создать адаптер."
        )
    return spec.factory(context)


def all_specs() -> list[ProviderSpec]:
    return [_REGISTRY[key] for key in sorted(_REGISTRY)]


def registered_ids() -> list[str]:
    return sorted(_REGISTRY)


def planned() -> tuple[ProviderInfo, ...]:
    """Провайдеры, которые осознанно не подключены, с причиной."""
    from .catalog import PLANNED

    return tuple(
        info for info in PLANNED if info.id not in _REGISTRY
    )


def plugin_errors() -> list[str]:
    return list(_PLUGIN_ERRORS)


def discover(group: str = ENTRY_POINT_GROUP) -> list[str]:
    """Подхватить сторонние провайдеры из entry points.

    Ошибка одного плагина не отменяет остальные: причина складывается в
    :func:`plugin_errors`, чтобы её можно было показать в `neurowave providers`.
    """
    discovered: list[str] = []
    for spec in _iter_entry_point_specs(group):
        try:
            register(spec, replace=True)
            discovered.append(spec.id)
        except Exception as exc:  # noqa: BLE001 - плагин чужой
            _PLUGIN_ERRORS.append(f"{spec.id}: {type(exc).__name__}: {exc}")
    return discovered


def _iter_entry_point_specs(group: str) -> Iterable[ProviderSpec]:
    try:
        from importlib.metadata import entry_points
    except Exception:  # pragma: no cover - Python < 3.8 невозможен
        return []

    try:
        selected = entry_points(group=group)
    except TypeError:  # старый интерфейс: словарь групп
        selected = entry_points().get(group, [])  # type: ignore[attr-defined]

    for entry in selected:
        name = getattr(entry, "name", "?")
        try:
            loaded = entry.load()
            if callable(loaded) and not isinstance(loaded, ProviderSpec):
                loaded = loaded()
            if not isinstance(loaded, ProviderSpec):
                raise TypeError("entry point должен возвращать ProviderSpec")
            yield loaded
        except Exception as exc:  # noqa: BLE001
            _PLUGIN_ERRORS.append(f"{name}: {type(exc).__name__}: {exc}")


class UnknownProvider(NeuroWaveError):
    """Запрошен провайдер, которого нет в реестре."""


class ProviderNotImplemented(NeuroWaveError):
    """Провайдер известен, но адаптера у него ещё нет."""


# Встроенные провайдеры.
register(YANDEX_SPEC)
register(LOCAL_SPEC)
register(SOUNDCLOUD_SPEC)
register(SPOTIFY_SPEC)
