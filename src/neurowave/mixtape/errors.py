"""Ошибки планировщика USB Mixtape."""

from __future__ import annotations


class MixtapeError(ValueError):
    """Некорректный запрос подборки или невозможный план."""


class PlanError(MixtapeError):
    """Планировщик не может построить план по заданным ограничениям."""
