"""Протокол источника экспортируемых файлов.

Источник отвечает только за метаданные и право на файл. Он не скачивает
потоки и не работает с USB; локальный резолвер появится в USB-02.
"""

from __future__ import annotations

from typing import Protocol, Sequence

from neurowave.domain import Track
from neurowave.mixtape.models import ExportAsset, RightsStatus


class ExportSource(Protocol):
    source_id: str

    def list_eligible(self, candidates: Sequence[Track]) -> Sequence[ExportAsset]: ...

    def rights_status(self, asset_id: str) -> RightsStatus: ...
