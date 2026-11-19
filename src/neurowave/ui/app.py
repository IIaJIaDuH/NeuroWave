"""Окно NeuroWave (Qt Widgets). PySide6 подключается лениво.

Правила, ради которых окно такое «тонкое»:

* ни одного обращения к IPC mpv из GUI-потока — состояние берётся из снимков
  `PlayerViewModel`, позиция читается фоновым потоком;
* все команды идут через последовательный диспетчер;
* ошибка любого действия показывается в статусной строке и не роняет окно.
"""

from __future__ import annotations

import sys
from typing import Any

from .viewmodel import PlayerViewModel, UiState

TITLE = "NeuroWave"
REFRESH_MS = 200


class UiUnavailable(RuntimeError):
    """Qt недоступен: нет PySide6 или графической сессии."""


def is_available() -> tuple[bool, str]:
    try:
        from PySide6.QtWidgets import QApplication  # noqa: F401
    except Exception as exc:
        return False, (
            f"PySide6 недоступен ({type(exc).__name__}: {exc}). "
            "Установите: pip install -e '.[ui]'"
        )
    return True, ""


def _mmss(milliseconds: int | None) -> str:
    """Время в ``м:сс``; неизвестная длительность — ``--:--``.

    Маркер совпадает с :func:`neurowave.domain.format_duration`.
    """
    if milliseconds is None:
        return "--:--"
    seconds = max(0, int(milliseconds) // 1000)
    return f"{seconds // 60}:{seconds % 60:02d}"


def _build_window(vm: PlayerViewModel) -> Any:
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import (
        QCheckBox,
        QComboBox,
        QHBoxLayout,
        QLabel,
        QMainWindow,
        QPushButton,
        QSlider,
        QVBoxLayout,
        QWidget,
    )

    from ..audio.dsp_profiles import BUILTIN_PROFILES

    window = QMainWindow()
    window.setWindowTitle(TITLE)
    window.resize(520, 360)
    central = QWidget()
    layout = QVBoxLayout(central)
    layout.setContentsMargins(20, 18, 20, 14)
    layout.setSpacing(10)

    title = QLabel("NeuroWave")
    title.setStyleSheet("font-size: 22px; font-weight: 600;")
    subtitle = QLabel("Волна ещё не запущена")
    subtitle.setStyleSheet("color: #8b97a8;")
    layout.addWidget(title)
    layout.addWidget(subtitle)

    seek = QSlider(Qt.Horizontal)
    seek.setRange(0, 1000)
    time_label = QLabel("0:00 / 0:00")
    time_label.setStyleSheet("color: #8b97a8; font-size: 12px;")
    layout.addWidget(seek)
    layout.addWidget(time_label)

    transport = QHBoxLayout()
    prev_button = QPushButton("⏮")
    play_button = QPushButton("▶")
    next_button = QPushButton("⏭")
    stop_button = QPushButton("⏹")
    wave_button = QPushButton("Волна")
    lab_button = QPushButton("Sound Lab")
    wave_button.setMinimumHeight(36)
    for button in (prev_button, play_button, next_button, stop_button):
        button.setMinimumHeight(36)
        transport.addWidget(button)
    transport.addWidget(wave_button)
    transport.addWidget(lab_button)
    layout.addLayout(transport)

    volume_row = QHBoxLayout()
    volume_row.addWidget(QLabel("Громкость"))
    volume = QSlider(Qt.Horizontal)
    volume.setRange(0, 130)
    volume.setValue(80)
    volume_label = QLabel("80 %")
    volume_row.addWidget(volume, 1)
    volume_row.addWidget(volume_label)
    layout.addLayout(volume_row)

    limit_note = QLabel("")
    limit_note.setStyleSheet("color: #e0b341; font-size: 12px;")
    limit_note.setWordWrap(True)
    layout.addWidget(limit_note)

    sound_row = QHBoxLayout()
    profile = QComboBox()
    for profile_id, item in BUILTIN_PROFILES.items():
        profile.addItem(item.name, profile_id)
    pure = QCheckBox("Pure (без обработки)")
    sound_row.addWidget(QLabel("Звук"))
    sound_row.addWidget(profile, 1)
    sound_row.addWidget(pure)
    layout.addLayout(sound_row)

    output_row = QHBoxLayout()
    output = QComboBox()
    output_row.addWidget(QLabel("Выход"))
    output_row.addWidget(output, 1)
    layout.addLayout(output_row)

    status = QLabel("Готово")
    status.setStyleSheet("color: #8b97a8; font-size: 12px;")
    status.setWordWrap(True)
    layout.addWidget(status)
    layout.addStretch(1)
    window.setCentralWidget(central)

    state = {
        "profile": "",
        "output": "",
        "dragging": False,
        "length": 0,
        "seekable": False,
    }

    def guarded(action: str, fn: Any) -> None:
        try:
            fn()
        except Exception as exc:
            status.setText(f"{action}: {exc}")

    def refresh_outputs(items: list[tuple[str, str]]) -> None:
        current = output.currentData()
        output.blockSignals(True)
        output.clear()
        for output_id, label in items:
            output.addItem(label, output_id)
        index = output.findData(current)
        if index >= 0:
            output.setCurrentIndex(index)
        output.blockSignals(False)

    def refresh() -> None:
        snap = vm.snapshot()
        seekable = snap.duration_known and snap.duration_ms > 0
        state["seekable"] = seekable
        state["length"] = snap.duration_ms if seekable else 0
        title.setText(snap.title)
        subtitle.setText(snap.subtitle or ("Источник: " + snap.quality if snap.quality else "Волна ещё не запущена"))
        duration_text = _mmss(snap.duration_ms if snap.duration_known else None)
        time_label.setText(f"{_mmss(snap.position_ms)} / {duration_text}")
        seek.setEnabled(seekable)
        if not state["dragging"]:
            seek.blockSignals(True)
            seek.setValue(int(snap.progress * 1000))
            seek.blockSignals(False)
        play_button.setText("⏸" if snap.is_playing else "▶")
        volume.blockSignals(True)
        volume.setValue(snap.volume)
        volume.blockSignals(False)
        volume_label.setText(f"{snap.volume} %" + ("" if snap.volume == snap.applied_volume else f" → {snap.applied_volume} %"))
        limit_note.setText(
            f"Усиление выше 100 % выключено: играет {snap.applied_volume} %. "
            "Лимитер ещё не подтверждён измерениями."
            if snap.volume_limited
            else ""
        )
        if snap.profile_id != state["profile"]:
            state["profile"] = snap.profile_id
            index = profile.findData(snap.profile_id)
            if index >= 0:
                profile.blockSignals(True)
                profile.setCurrentIndex(index)
                profile.blockSignals(False)
        pure.blockSignals(True)
        pure.setChecked(snap.profile_mode == "pure")
        pure.blockSignals(False)
        if snap.output_id != state["output"]:
            state["output"] = snap.output_id
        events = vm.drain()
        if events:
            last = events[-1]
            status.setText(f"{last.name}" + (f" · {last.payload.get('track').label}" if last.payload.get("track") else ""))

    def load_outputs() -> None:
        try:
            items = [(item.id, item.label()) for item in vm.player.outputs()]
        except Exception:
            items = []
        if items:
            refresh_outputs(items)

    prev_button.clicked.connect(lambda: guarded("Предыдущий", vm.previous))
    play_button.clicked.connect(lambda: guarded("Пауза", vm.toggle))
    next_button.clicked.connect(lambda: guarded("Следующий", vm.next))
    stop_button.clicked.connect(lambda: guarded("Стоп", vm.stop))
    wave_button.clicked.connect(lambda: guarded("Волна", vm.start_wave))

    def open_lab() -> None:
        from .soundlab import open_soundlab

        open_soundlab(vm)
        refresh()

    lab_button.clicked.connect(open_lab)
    volume.valueChanged.connect(lambda value: guarded("Громкость", lambda: vm.set_volume(value)))
    seek.sliderPressed.connect(lambda: state.__setitem__("dragging", True))
    seek.sliderReleased.connect(lambda: _seek_released())

    def _seek_released() -> None:
        state["dragging"] = False
        # Длительность неизвестна или нулевая: перемотка недоступна, и
        # отпускание ползунка не должно отправлять ложный seek(0).
        if not state["seekable"] or state["length"] <= 0:
            return
        guarded("Перемотка", lambda: vm.seek(int(seek.value() * state["length"] / 1000)))

    profile.currentIndexChanged.connect(
        lambda index: guarded("Профиль", lambda: vm.set_profile(profile.itemData(index)))
    )
    pure.toggled.connect(lambda checked: guarded("Pure", lambda: vm.set_mode("pure" if checked else "enhanced")))
    output.currentIndexChanged.connect(
        lambda index: guarded("Выход", lambda: vm.set_output(output.itemData(index)))
    )

    timer = QTimer(window)
    timer.timeout.connect(refresh)
    timer.start(REFRESH_MS)
    output_timer = QTimer(window)
    output_timer.timeout.connect(load_outputs)
    output_timer.start(5000)
    load_outputs()
    refresh()
    # Для тестов: один кадр обновления без запуска таймера.
    window._refresh = refresh  # type: ignore[attr-defined]
    window._viewmodel = vm  # type: ignore[attr-defined]
    return window


def run_window(player: Any) -> int:
    """Показать окно и блокироваться до закрытия."""
    import signal

    available, reason = is_available()
    if not available:
        raise UiUnavailable(reason)

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(TITLE)
    viewmodel = PlayerViewModel(player)
    viewmodel.start_polling()
    window = _build_window(viewmodel)
    window.show()

    # Qt-цикл выполняется в C++ и не даёт Python обработать сигнал: без
    # «сердцебиения» SIGTERM/SIGINT не закрывают окно, и процесс остаётся
    # висеть после выхода из сессии.
    heartbeat = QTimer()
    heartbeat.timeout.connect(lambda: None)
    heartbeat.start(150)

    previous: dict[int, Any] = {}

    def request_quit(*_args: Any) -> None:
        app.quit()

    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, request_quit)
        except (ValueError, OSError):
            pass
    try:
        return app.exec()
    finally:
        heartbeat.stop()
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                pass
        viewmodel.close()
