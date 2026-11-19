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


# --- D-05: доставка ошибок асинхронных команд --------------------------
def _raiser(text: str):
    def action() -> None:
        raise RuntimeError(text)

    return action


def test_error_listeners_are_notified_alongside_existing_callback(dispatcher) -> None:
    """Подписка дополняет on_error, а не перезаписывает его."""
    legacy: list[int] = []
    extra: list[int] = []
    dispatcher._on_error = lambda command: legacy.append(command.id)
    dispatcher.add_error_listener(lambda command: extra.append(command.id))

    with pytest.raises(RuntimeError):
        dispatcher.run("broken", _raiser("нет устройства"))

    assert legacy and extra
    assert legacy == extra
    assert dispatcher.stats["failed"] == 1
    assert dispatcher.run("after", lambda: "ok") == "ok"


def test_several_listeners_get_the_failed_command(dispatcher) -> None:
    first: list[str] = []
    second: list[str] = []
    dispatcher.add_error_listener(lambda command: first.append(command.name))
    dispatcher.add_error_listener(lambda command: second.append(command.name))

    with pytest.raises(RuntimeError):
        dispatcher.run("output", _raiser("сбой"))

    assert first == ["output"]
    assert second == ["output"]


def test_broken_listener_does_not_stop_worker_or_other_listeners(dispatcher) -> None:
    seen: list[str] = []

    def angry(command) -> None:
        raise ValueError("подписчик сломан")

    dispatcher.add_error_listener(angry)
    dispatcher.add_error_listener(lambda command: seen.append(command.name))

    with pytest.raises(RuntimeError):
        dispatcher.run("broken", _raiser("сбой"))

    assert seen == ["broken"]
    assert dispatcher.run("after", lambda: 42) == 42


def test_removed_listener_stops_receiving_errors(dispatcher) -> None:
    seen: list[str] = []
    dispatcher.add_error_listener(seen.append)
    dispatcher.remove_error_listener(seen.append)

    with pytest.raises(RuntimeError):
        dispatcher.run("broken", _raiser("сбой"))

    assert seen == []
    # Повторное удаление не должно падать.
    dispatcher.remove_error_listener(seen.append)


def test_cancelled_command_is_not_reported_as_error(dispatcher) -> None:
    """Отменённая до запуска команда — не ошибка выполнения."""
    seen: list[str] = []
    dispatcher.add_error_listener(lambda command: seen.append(command.name))
    gate = threading.Event()
    started = threading.Event()

    def blocker() -> None:
        started.set()
        gate.wait(5)

    running = dispatcher.submit("blocker", blocker)
    assert started.wait(2)
    stale = dispatcher.submit("stale", lambda: None, generation=dispatcher.generation)
    dispatcher.bump_generation()
    gate.set()

    assert running.done.wait(3)
    assert dispatcher.wait_idle(3)
    assert stale.cancelled is True
    assert seen == []
    assert dispatcher.stats["cancelled"] == 1


# --- D-05-R1: уведомление о завершении успешных команд ------------------
def test_completion_listener_sees_successes_in_execution_order(dispatcher) -> None:
    seen: list[tuple[str, int]] = []
    dispatcher.add_completion_listener(
        lambda command: seen.append((command.name, command.seq))
    )

    assert dispatcher.run("first", lambda: "a") == "a"
    assert dispatcher.run("second", lambda: "b") == "b"

    assert [name for name, _ in seen] == ["first", "second"]
    sequences = [seq for _, seq in seen]
    assert sequences == sorted(sequences)
    assert sequences[0] < sequences[-1]
    assert dispatcher.run("third", lambda: "c") == "c"
    assert seen[-1][1] > sequences[-1]


def test_completion_listener_is_not_called_for_errors(dispatcher) -> None:
    done: list[str] = []
    failed: list[str] = []
    dispatcher.add_completion_listener(lambda command: done.append(command.name))
    dispatcher.add_error_listener(lambda command: failed.append(command.name))

    with pytest.raises(RuntimeError):
        dispatcher.run("broken", _raiser("сбой"))

    assert done == []
    assert failed == ["broken"]


def test_cancelled_command_is_not_reported_as_completed(dispatcher) -> None:
    done: list[str] = []
    dispatcher.add_completion_listener(lambda command: done.append(command.name))
    gate = threading.Event()
    started = threading.Event()

    def blocker() -> None:
        started.set()
        gate.wait(5)

    running = dispatcher.submit("blocker", blocker)
    assert started.wait(2)
    stale = dispatcher.submit("stale", lambda: None, generation=dispatcher.generation)
    dispatcher.bump_generation()
    gate.set()

    assert running.done.wait(3)
    assert dispatcher.wait_idle(3)
    assert stale.cancelled is True
    assert done == ["blocker"]


def test_broken_completion_listener_does_not_stop_worker(dispatcher) -> None:
    seen: list[str] = []

    def angry(command) -> None:
        raise ValueError("подписчик сломан")

    dispatcher.add_completion_listener(angry)
    dispatcher.add_completion_listener(lambda command: seen.append(command.name))

    assert dispatcher.run("ok", lambda: 1) == 1
    assert seen == ["ok"]


def test_removed_completion_listener_stops_receiving_events(dispatcher) -> None:
    seen: list[str] = []
    dispatcher.add_completion_listener(seen.append)
    dispatcher.remove_completion_listener(seen.append)

    assert dispatcher.run("quiet", lambda: None) is None
    assert seen == []
    dispatcher.remove_completion_listener(seen.append)  # повторное удаление безопасно
