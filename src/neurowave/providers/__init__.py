"""Провайдеры музыки: контракт, реестр и встроенный Яндекс.

Пример подключения стороннего провайдера — в :mod:`neurowave.providers.registry`
(entry points группы ``neurowave.providers``).
"""

from __future__ import annotations

from .base import (
    AuthKind,
    Capability,
    MusicProvider,
    ProviderContext,
    ProviderInfo,
    ProviderSpec,
)
from .registry import (
    ProviderNotImplemented,
    UnknownProvider,
    all_specs,
    create,
    discover,
    get_spec,
    planned,
    plugin_errors,
    register,
    registered_ids,
)

__all__ = [
    "AuthKind",
    "Capability",
    "MusicProvider",
    "ProviderContext",
    "ProviderInfo",
    "ProviderNotImplemented",
    "ProviderSpec",
    "UnknownProvider",
    "all_specs",
    "create",
    "discover",
    "get_spec",
    "planned",
    "plugin_errors",
    "register",
    "registered_ids",
]
