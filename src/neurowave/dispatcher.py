"""Последовательный исполнитель команд плеера.

Все мутации `PlayerService` (play/next/stop/seek/volume/profile/output) идут
через один поток: два одновременных «переключи трек» дают гонку очереди и
двойной `loadfile`. Интерфейс не должен блокироваться на IPC и сети, поэтому
команды только ставятся в очередь, а результат забирается позже.

Что здесь честно ограничено: Python не умеет прерывать выполняющуюся функцию,
поэтому таймаут — это (а) ограничение ожидания результата и (б) счётчик
зависаний, а не kill потока. Устаревшие команды отменяются только до старта.
"""

from __future__ import annotations

import itertools
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .errors import NeuroWaveError


class DispatcherError(NeuroWaveError):
    """Команда не выполнена: очередь закрыта, отменена или истекло ожидание."""


class DispatcherTimeout(DispatcherError):
    """Результат не пришёл за отведённое время."""


@dataclass
class Command:
    """Одна команда в очереди."""

    id: int
    name: str
    action: Callable[[], Any]
    generation: int
    timeout: float | None = None
    done: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: BaseException | None = None
    cancelled: bool = False
    started_at: float | None = None
    finished_at: float | None = None

    @property
    def duration_ms(self) -> int | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return int((self.finished_at - self.started_at) * 1000)


class CommandDispatcher:
    """Один рабочий поток и одна очередь команд."""

    def __init__(
        self,
        on_error: Callable[[Command], None] | None = None,
        default_timeout: float = 15.0,
    ):
        self._queue: queue.Queue[Command | None] = queue.Queue()
        self._ids = itertools.count(1)
        self._generation = 0
        self._lock = threading.Lock()
        self._on_error = on_error
        self.default_timeout = default_timeout
        self._closed = False
        self._active = 0
        self._peak_active = 0
        self.stats: dict[str, int] = {
            "submitted": 0,
            "executed": 0,
            "failed": 0,
            "cancelled": 0,
            "slow": 0,
        }
        self._thread = threading.Thread(target=self._run, name="neurowave-dispatcher", daemon=True)
        self._thread.start()

    # --- публичное -----------------------------------------------------
    @property
    def generation(self) -> int:
        return self._generation

    def bump_generation(self) -> int:
        """Новое поколение: команды старого можно отменять."""
        with self._lock:
            self._generation += 1
            return self._generation

    def submit(
        self,
        name: str,
        action: Callable[[], Any],
        *,
        generation: int | None = None,
        timeout: float | None = None,
    ) -> Command:
        with self._lock:
            if self._closed:
                raise DispatcherError("Диспетчер закрыт")
            command = Command(
                id=next(self._ids),
                name=name,
                action=action,
                generation=self._generation if generation is None else generation,
                timeout=timeout,
            )
            self.stats["submitted"] += 1
        self._queue.put(command)
        return command

    def run(
        self,
        name: str,
        action: Callable[[], Any],
        *,
        timeout: float | None = None,
        generation: int | None = None,
    ) -> Any:
        """Поставить команду и дождаться результата (для CLI и тестов)."""
        command = self.submit(name, action, generation=generation, timeout=timeout)
        return self.result(command, timeout=timeout)

    def result(self, command: Command, timeout: float | None = None) -> Any:
        budget = self.default_timeout if timeout is None else timeout
        if not command.done.wait(budget):
            raise DispatcherTimeout(f"Команда {command.name} не завершилась за {budget:g} с")
        if command.error is not None:
            raise command.error
        if command.cancelled:
            raise DispatcherError(f"Команда {command.name} отменена")
        return command.result

    def cancel_pending(self, keep_generation: int | None = None) -> int:
        """Отменить ещё не начатые команды старого поколения."""
        keep = self._generation if keep_generation is None else keep_generation
        cancelled = 0
        with self._lock:
            pending = list(self._queue.queue)
        for command in pending:
            if command is None or command.generation >= keep:
                continue
            command.cancelled = True
            command.done.set()
            cancelled += 1
            self.stats["cancelled"] += 1
        return cancelled

    def wait_idle(self, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue.empty() and self._active == 0:
                return True
            time.sleep(0.01)
        return False

    def shutdown(self, timeout: float = 5.0) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._queue.put(None)
        self._thread.join(timeout)

    # --- рабочий поток -------------------------------------------------
    def _run(self) -> None:
        while True:
            command = self._queue.get()
            if command is None:
                return
            if command.cancelled:
                continue
            if command.generation < self._generation:
                # Пока команда ждала, пользователь нажал следующую: старое
                # действие уже неактуально и только перебьёт новое.
                command.cancelled = True
                command.done.set()
                self.stats["cancelled"] += 1
                continue
            self._execute(command)

    def _execute(self, command: Command) -> None:
        command.started_at = time.monotonic()
        self._active += 1
        self._peak_active = max(self._peak_active, self._active)
        try:
            command.result = command.action()
            self.stats["executed"] += 1
        except BaseException as exc:  # ошибка команды не должна убивать поток
            command.error = exc
            self.stats["failed"] += 1
            if self._on_error is not None:
                try:
                    self._on_error(command)
                except Exception:
                    pass
        finally:
            command.finished_at = time.monotonic()
            self._active -= 1
            if command.timeout is not None and command.duration_ms is not None:
                if command.duration_ms > command.timeout * 1000:
                    self.stats["slow"] += 1
            command.done.set()

    @property
    def peak_concurrency(self) -> int:
        return self._peak_active
