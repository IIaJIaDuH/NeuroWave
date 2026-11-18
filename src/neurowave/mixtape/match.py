"""Сопоставление записей между провайдерами без сетевых запросов.

Сравнивается музыкальная запись, а не ``Track.key``: у Яндекса, SoundCloud и
локальной библиотеки один и тот же трек имеет разные ключи. Версия/ремастер с
заметно другой длительностью остаётся отдельным кандидатом.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Sequence

DURATION_TOLERANCE_MS = 3_000

_FEATURING = re.compile(r"\b(feat(?:uring)?|ft|при участии)\b.*$", re.IGNORECASE)
_NON_WORD = re.compile(r"[^\w\s]+", re.UNICODE)
_SPACES = re.compile(r"\s+")


def normalize_text(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = _FEATURING.sub(" ", text)
    text = _NON_WORD.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


def primary_artist(artists: Sequence[str]) -> str:
    return normalize_text(artists[0]) if artists else ""


def recording_identity(title: str | None, artists: Sequence[str]) -> tuple[str, str]:
    """Нормализованная пара «исполнитель — название» для дедупликации."""

    return primary_artist(artists), normalize_text(title)


def same_recording(
    duration_a_ms: int | None,
    duration_b_ms: int | None,
    tolerance_ms: int = DURATION_TOLERANCE_MS,
) -> bool:
    """Одинаковая длительность (в пределах допуска) — тот же файл записи.

    Неизвестная длительность не даёт права отбрасывать кандидата: лучше
    показать лишний вариант, чем потерять реальный другой трек.
    """

    if duration_a_ms is None or duration_b_ms is None:
        return False
    return abs(int(duration_a_ms) - int(duration_b_ms)) <= max(0, int(tolerance_ms))
