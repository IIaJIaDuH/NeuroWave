"""Диспетчер команд: один поток, порядок, отмена устаревших, ошибки.

Проверяется то, ради чего он существует: два быстрых «next» не должны
выполняться одновременно, устаревшее действие не перебивает новое, а падение
команды не убивает исполнитель.
"""

from __future__ import annotations

import threading
import time

import pytest

from neurowave.dispatcher import CommandDispatcher, DispatcherError, DispatcherTimeout


@pytest.fixture
def dispatcher():
    instance = CommandDispatcher()
    yield instance
    instance.shutdown()


def test_commands_run_in_order_within_single_thread(dispatcher) -> None:
    order: list[str] = []
    threads: set[int] = set()
    for index in range(5):
        dispatcher.run(
            f"cmd{index}",
            lambda i=index: (order.append(str(i)), threads.add(threading.get_ident())),
        )
    assert order == ["0", "1", "2", "3", "4"]
    assert len(threads) == 1
    assert next(iter(threads)) != threading.get_ident()
    assert dispatcher.peak_concurrency == 1


def test_error_is_returned_and_dispatcher_survives(dispatcher) -> None:
    errors: list[str] = []
    dispatcher._on_error = lambda command: errors.append(command.name)

    def boom() -> None:
        raise ValueError("нет сети")

    with pytest.raises(ValueError):
        dispatcher.run("boom", boom)
    assert dispatcher.run("after", lambda: "ok") == "ok"
    assert errors == ["boom"]
    assert dispatcher.stats["failed"] == 1


def test_stale_commands_are_cancelled(dispatcher) -> None:
    gate = threading.Event()
    started = threading.Event()
    ran: list[str] = []

    def blocker() -> None:
        started.set()
        gate.wait(5)

    first = dispatcher.submit("blocker", blocker)
    assert started.wait(2)

    # Пока исполнитель занят, пользователь нажал «следующий» ещё раз.
    old = dispatcher.submit("next-old", lambda: ran.append("old"), generation=dispatcher.generation)
    dispatcher.bump_generation()
    new = dispatcher.submit("next-new", lambda: ran.append("new"), generation=dispatcher.generation)

    gate.set()
    assert first.done.wait(3)
    assert new.done.wait(3)
    assert ran == ["new"]
    assert old.cancelled is True
    assert dispatcher.stats["cancelled"] == 1


def test_waiting_for_result_times_out(dispatcher) -> None:
    gate = threading.Event()
    dispatcher.submit("slow", lambda: gate.wait(5))
    with pytest.raises(DispatcherTimeout):
        dispatcher.run("queued", lambda: None, timeout=0.2)
    gate.set()


def test_submit_after_shutdown_is_refused() -> None:
    instance = CommandDispatcher()
    instance.shutdown()
    with pytest.raises(DispatcherError):
        instance.submit("late", lambda: None)


def test_slow_command_is_counted(dispatcher) -> None:
    dispatcher.submit("slowish", lambda: time.sleep(0.25), timeout=0.1)
    assert dispatcher.wait_idle(5)
    assert dispatcher.stats["slow"] == 1
