"""Политика прав на локальные файлы.

Право на прослушивание и право скопировать файл — разные вещи. Для MVP
источник прав — явное подтверждение пользователя, что файл принадлежит ему
и может быть скопирован в личную подборку. Без подтверждения статус
``UNKNOWN``: планировщик такой файл не берёт.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from neurowave.mixtape.models import ExportRight, RightsStatus

USER_OWNED = "USER_OWNED"
NOT_CONFIRMED = "NOT_CONFIRMED"


class ExportRightsPolicy(Protocol):
    """Политика источника; вызывается для каждого разрешённого пути."""

    @property
    def default_status(self) -> RightsStatus: ...

    def right_for(self, path: Path, root: Path) -> ExportRight: ...


class LocalExportRights:
    """Права на approved local root: только после подтверждения пользователя."""

    def __init__(
        self,
        *,
        confirmed: bool,
        basis: str = USER_OWNED,
        detail: str = "Локальная библиотека пользователя",
    ) -> None:
        self._confirmed = bool(confirmed)
        self._basis = str(basis or USER_OWNED)
        self._detail = str(detail or "")

    @property
    def confirmed(self) -> bool:
        return self._confirmed

    @property
    def default_status(self) -> RightsStatus:
        return RightsStatus.PERMITTED if self._confirmed else RightsStatus.UNKNOWN

    def right_for(self, path: Path, root: Path) -> ExportRight:
        if not self._confirmed:
            return ExportRight(
                status=RightsStatus.UNKNOWN,
                basis=NOT_CONFIRMED,
                detail="Пользователь не подтвердил право на копирование файла",
            )
        return ExportRight(status=RightsStatus.PERMITTED, basis=self._basis, detail=self._detail)
