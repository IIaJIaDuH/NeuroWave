"""Портативные имена для FAT32/exFAT и проверка относительных путей.

Планировщик уже готовит имена, но writer не доверяет плану: он повторно
отклоняет абсолютные пути, ``..``, NUL и управляющие символы, а слишком
длинные имена укорачивает по UTF-8, сохраняя расширение.
"""

from __future__ import annotations

from pathlib import PurePosixPath

MAX_COMPONENT_BYTES = 240
_INVALID = set('<>:"/\\|?*')


def validate_relative(destination: str, subdir: str) -> tuple[str, ...]:
    """Проверить путь и вернуть его компоненты; вне ``subdir`` — ошибка."""

    if not isinstance(destination, str) or not destination or "\x00" in destination:
        raise ValueError("пустой или содержащий NUL путь")
    if any(ord(char) < 32 for char in destination):
        raise ValueError("управляющие символы в пути")
    path = PurePosixPath(destination)
    if path.is_absolute():
        raise ValueError("абсолютный путь")
    if any(part in ("", ".", "..") for part in path.parts):
        raise ValueError("недопустимый сегмент пути")
    prefix = PurePosixPath(subdir).parts
    if path.parts[: len(prefix)] != prefix:
        raise ValueError("путь вне целевого каталога")
    if len(path.parts) <= len(prefix):
        raise ValueError("путь не содержит имени файла")
    return path.parts


def portable_component(component: str, *, max_bytes: int = MAX_COMPONENT_BYTES) -> str:
    """Заменить опасные символы и ограничить длину в байтах UTF-8."""

    cleaned = "".join("_" if char in _INVALID or ord(char) < 32 else char for char in component)
    cleaned = cleaned.strip(" .") or "_"
    if len(cleaned.encode("utf-8")) <= max_bytes:
        return cleaned

    suffix = PurePosixPath(cleaned).suffix
    if len(suffix.encode("utf-8")) >= max_bytes:
        suffix = ""
    stem = cleaned[: len(cleaned) - len(suffix)] if suffix else cleaned
    budget = max_bytes - len(suffix.encode("utf-8"))
    encoded = stem.encode("utf-8")[:budget]
    stem = encoded.decode("utf-8", errors="ignore").rstrip(" .")
    return f"{stem or 'track'}{suffix}"


def portable_destination(parts: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        portable_component(part) if index == len(parts) - 1 else portable_component(part, max_bytes=100)
        for index, part in enumerate(parts)
    )
