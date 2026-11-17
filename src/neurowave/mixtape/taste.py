"""Read-only интерфейс сигналов вкуса для планировщика."""

from __future__ import annotations

from typing import Protocol, Sequence

from neurowave.domain import Track
from neurowave.mixtape.models import TasteSnapshot


class TasteSignals(Protocol):
    """Будущий адаптер к истории/лайкам; реализация появится вместе с сервисом."""

    def read_preferences(self) -> TasteSnapshot: ...

    def local_candidates(self) -> Sequence[Track]: ...
