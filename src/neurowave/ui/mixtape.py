"""Экран USB Mixtape (Qt Widgets) поверх Qt-независимого моста.

Окно только рисует снимок :class:`~neurowave.mixtape.bridge.BridgeState` по
таймеру GUI-потока и передаёт действия мосту. Вся работа с библиотекой, USB и
sha256 идёт в фоновом потоке моста, поэтому mpv и отрисовка не блокируются.

Согласие не обходится: «Записать на USB» спрашивает подтверждение, без
отметки о правах кнопка выключена, «Извлечь» доступна только когда мост не
занят, а прогресс и причины пропуска видны до записи.
"""

from __future__ import annotations

from typing import Any

from ..mixtape.bridge import BridgeStatus, MixtapeBridge, PlanPreview, describe_skips
from ..mixtape.models import PROFILES, MixtapeRequest

TITLE = "USB Mixtape"
REFRESH_MS = 200
FORMATS = (("original", "Как в библиотеке"), ("mp3", "MP3 320"), ("flac", "FLAC (только lossless)"))


def is_available() -> tuple[bool, str]:
    try:
        from PySide6.QtWidgets import QDialog  # noqa: F401
    except Exception as exc:
        return False, (
            f"PySide6 недоступен ({type(exc).__name__}: {exc}). "
            "Установите: pip install -e '.[ui]'"
        )
    return True, ""


def _duration(ms: int) -> str:
    total = max(0, int(ms)) // 1000
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    return f"{hours} ч {minutes:02d} мин" if hours else f"{minutes} мин"


def _size(value: int) -> str:
    number = max(0, int(value))
    if number >= 1024 ** 3:
        return f"{number / 1024 ** 3:.1f} ГБ"
    return f"{number / 1024 ** 2:.1f} МБ"


def _speed(value: float) -> str:
    if value <= 0:
        return ""
    return f"{value / 1024 ** 2:.1f} МБ/с"


def _ask(parent: Any, text: str) -> bool:
    """Подтверждение действия; в тестах подменяется без модального окна."""

    from PySide6.QtWidgets import QMessageBox

    return (
        QMessageBox.question(parent, TITLE, text, QMessageBox.Yes | QMessageBox.No)
        == QMessageBox.Yes
    )


def _device_text(device: Any) -> str:
    return (
        f"{device.label} · {device.filesystem or '?'} · "
        f"{_size(device.free_bytes)} свободно · {device.object_path}"
    )


def _build_dialog(bridge: MixtapeBridge) -> Any:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import (
        QAbstractItemView,
        QCheckBox,
        QComboBox,
        QDialog,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QProgressBar,
        QPushButton,
        QSpinBox,
        QTableWidget,
        QTableWidgetItem,
        QVBoxLayout,
    )

    class _Dialog(QDialog):
        """Закрытие окна контролирует мост, а не Qt-умолчание."""

        def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
            handler = getattr(self, "_close_handler", None)
            if handler is None:
                event.accept()
                return
            handler(event)

    dialog = _Dialog()
    dialog.setWindowTitle(TITLE)
    dialog.resize(820, 560)
    layout = QVBoxLayout(dialog)

    title = QLabel(TITLE)
    title.setStyleSheet("font-size: 18px; font-weight: 600;")
    subtitle = QLabel(
        "Локальная библиотека → USB. Запись начинается только после подтверждения."
    )
    subtitle.setStyleSheet("color: #8b97a8; font-size: 12px;")
    subtitle.setWordWrap(True)
    layout.addWidget(title)
    layout.addWidget(subtitle)

    device_row = QHBoxLayout()
    device_combo = QComboBox()
    refresh_button = QPushButton("Обновить")
    device_row.addWidget(QLabel("USB"))
    device_row.addWidget(device_combo, 1)
    device_row.addWidget(refresh_button)
    layout.addLayout(device_row)

    library_row = QHBoxLayout()
    library_edit = QLineEdit(str(bridge.library))
    browse_button = QPushButton("Выбрать папку")
    library_row.addWidget(QLabel("Музыка"))
    library_row.addWidget(library_edit, 1)
    library_row.addWidget(browse_button)
    layout.addLayout(library_row)

    params = QHBoxLayout()
    profile_combo = QComboBox()
    for profile_id in sorted(PROFILES):
        profile_combo.addItem(f"{profile_id} — {PROFILES[profile_id].description}", profile_id)
    hours = QSpinBox()
    hours.setRange(1, 12)
    hours.setValue(4)
    hours.setSuffix(" ч")
    format_combo = QComboBox()
    for format_id, label in FORMATS:
        format_combo.addItem(label, format_id)
    rights = QCheckBox("У меня есть право копировать эти файлы")
    plan_button = QPushButton("Построить план")
    params.addWidget(QLabel("Профиль"))
    params.addWidget(profile_combo, 1)
    params.addWidget(QLabel("Длительность"))
    params.addWidget(hours)
    params.addWidget(QLabel("Формат"))
    params.addWidget(format_combo)
    params.addWidget(plan_button)
    layout.addLayout(params)
    layout.addWidget(rights)

    table = QTableWidget(0, 4)
    table.setHorizontalHeaderLabels(["#", "Трек", "Исполнитель", "Файл на USB"])
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setColumnWidth(0, 40)
    table.setColumnWidth(1, 220)
    table.setColumnWidth(2, 160)
    layout.addWidget(table, 1)

    summary = QLabel("План ещё не построен")
    summary.setWordWrap(True)
    layout.addWidget(summary)
    skips = QLabel("")
    skips.setStyleSheet("color: #e0b341; font-size: 12px;")
    skips.setWordWrap(True)
    layout.addWidget(skips)

    progress = QProgressBar()
    progress.setRange(0, 1000)
    progress.setValue(0)
    progress.setFormat("%p%")
    progress_label = QLabel("")
    progress_label.setStyleSheet("color: #8b97a8; font-size: 12px;")
    progress_label.setWordWrap(True)
    layout.addWidget(progress)
    layout.addWidget(progress_label)

    actions = QHBoxLayout()
    export_button = QPushButton("Записать на USB")
    cancel_button = QPushButton("Отмена")
    verify_button = QPushButton("Проверить")
    eject_button = QPushButton("Извлечь")
    for button in (export_button, cancel_button, verify_button, eject_button):
        actions.addWidget(button)
    layout.addLayout(actions)

    status = QLabel("Готово")
    status.setStyleSheet("color: #8b97a8; font-size: 12px;")
    status.setWordWrap(True)
    layout.addWidget(status)

    def selected_object() -> str:
        return str(device_combo.currentData() or "")

    def refresh_devices() -> None:
        devices = bridge.refresh_devices()
        current = device_combo.currentData() or selected_object()
        device_combo.blockSignals(True)
        device_combo.clear()
        for device in devices:
            device_combo.addItem(_device_text(device), device.object_path)
        index = device_combo.findData(current)
        if index >= 0:
            device_combo.setCurrentIndex(index)
        device_combo.blockSignals(False)
        bridge.select_device(selected_object())

    def fill_preview(preview: PlanPreview | None) -> None:
        table.setRowCount(0)
        if preview is None:
            summary.setText("План ещё не построен")
            skips.setText("")
            return
        table.setRowCount(len(preview.tracks))
        for row, track in enumerate(preview.tracks):
            values = (str(row + 1), track.title, track.artists, track.destination)
            for column, value in enumerate(values):
                table.setItem(row, column, QTableWidgetItem(value))
        table.resizeColumnToContents(3)
        text = (
            f"Файлов: {preview.item_count} · {_duration(preview.estimated_duration_ms)} "
            f"из {_duration(preview.target_duration_ms)} · {_size(preview.estimated_bytes)} · "
            f"кандидатов: {preview.candidate_count}, допущено: {preview.eligible_count}"
        )
        if preview.shortage_ms:
            text += f" · нехватка {_duration(preview.shortage_ms)}"
        summary.setText(text)
        reasons = describe_skips(preview)
        skips.setText(f"Пропуски — {reasons}" if reasons else "")

    shown_devices: list[tuple[tuple[str, str], ...]] = [()]

    def refresh() -> None:
        state = bridge.snapshot()
        signature = tuple((device.object_path, _device_text(device)) for device in state.devices)
        if signature != shown_devices[0]:
            shown_devices[0] = signature
            device_combo.blockSignals(True)
            device_combo.clear()
            for device in state.devices:
                device_combo.addItem(_device_text(device), device.object_path)
            device_combo.blockSignals(False)
        if state.selected and device_combo.currentData() != state.selected:
            index = device_combo.findData(state.selected)
            if index >= 0:
                device_combo.setCurrentIndex(index)
        if library_edit.text() != state.library and not library_edit.hasFocus():
            library_edit.setText(state.library)
        rights.blockSignals(True)
        rights.setChecked(state.rights_confirmed)
        rights.blockSignals(False)
        if state.preview != preview_shown[0]:
            preview_shown[0] = state.preview
            fill_preview(state.preview)
        progress_row = state.progress
        if progress_row is not None:
            progress.blockSignals(True)
            progress.setValue(int(progress_row.percent * 10))
            progress.blockSignals(False)
            parts = [
                f"{progress_row.percent:.0f} %",
                f"{progress_row.items_done}/{progress_row.items_total} файлов",
                f"{_size(progress_row.bytes_done)} из {_size(progress_row.bytes_total)}",
            ]
            speed = _speed(progress_row.speed_bps)
            if speed:
                parts.append(speed)
            if progress_row.eta_s:
                parts.append(f"~{_duration(int(progress_row.eta_s * 1000))}")
            if progress_row.current:
                parts.append(progress_row.current)
            progress_label.setText(" · ".join(parts))
        else:
            progress.setValue(0)
            progress_label.setText("")
        if state.error:
            status.setText(state.error)
        else:
            summary_row = state.summary
            prefix = {
                BridgeStatus.PARTIAL: "Частично: ",
                BridgeStatus.FAILED: "Ошибка: ",
                BridgeStatus.CANCELLED: "Отменено: ",
            }.get(state.status, "")
            text = prefix + state.message
            if summary_row is not None:
                text += (
                    f" · проверено: {summary_row.verified}, ошибок: {summary_row.failed}, "
                    f"пропущено: {summary_row.skipped}"
                )
                if summary_row.playlist:
                    text += f" · плейлист: {summary_row.playlist}"
                if summary_row.playlist_error:
                    text += f" · плейлист не записан: {summary_row.playlist_error}"
            if state.verification is not None and state.verification.ok:
                text += f" · проверка файлов OK ({state.verification.checked})"
            status.setText(text)
        plan_button.setEnabled(state.can_plan)
        export_button.setEnabled(state.can_export)
        cancel_button.setEnabled(state.can_cancel)
        verify_button.setEnabled(state.can_verify)
        eject_button.setEnabled(state.can_eject)

    preview_shown: list[PlanPreview | None] = [None]

    def make_request() -> MixtapeRequest:
        return MixtapeRequest(
            profile=str(profile_combo.currentData() or "road"),
            target_duration_ms=int(hours.value()) * 3_600_000,
            output_format=str(format_combo.currentData() or "original"),
        )

    def on_plan() -> None:
        bridge.set_library(library_edit.text().strip())
        bridge.set_rights_confirmed(rights.isChecked())
        bridge.request_plan(request=make_request(), rights_confirmed=rights.isChecked())
        preview_shown[0] = None
        refresh()

    def ask_confirm(text: str) -> bool:
        return _ask(dialog, text)

    def on_export() -> None:
        state = bridge.snapshot()
        preview = state.preview
        if preview is None or not state.can_export:
            refresh()
            return
        device = next(
            (item for item in state.devices if item.object_path == selected_object()), None
        )
        where = device.mountpoint if device else selected_object()
        question = (
            f"Записать {preview.item_count} файлов ({_size(preview.estimated_bytes)}) "
            f"в {where}?"
        )
        if not ask_confirm(question):
            return
        bridge.start_export(object_path=selected_object(), confirm=True)
        refresh()

    def on_browse() -> None:
        from PySide6.QtWidgets import QFileDialog

        chosen = QFileDialog.getExistingDirectory(dialog, "Папка музыки", library_edit.text())
        if chosen:
            library_edit.setText(chosen)
            bridge.set_library(chosen)
            on_plan()

    def on_cancel() -> None:
        bridge.request_cancel()
        refresh()

    def on_verify() -> None:
        bridge.request_verify()
        refresh()

    def on_eject() -> None:
        if not ask_confirm(f"Отключить том {selected_object()}?"):
            return
        bridge.eject(object_path=selected_object(), confirm=True)
        refresh()

    plan_button.clicked.connect(on_plan)
    export_button.clicked.connect(on_export)
    cancel_button.clicked.connect(on_cancel)
    verify_button.clicked.connect(on_verify)
    eject_button.clicked.connect(on_eject)
    browse_button.clicked.connect(on_browse)
    refresh_button.clicked.connect(lambda: (refresh_devices(), refresh()))
    device_combo.currentIndexChanged.connect(
        lambda _index: (bridge.select_device(selected_object()), refresh())
    )
    rights.toggled.connect(lambda checked: (bridge.set_rights_confirmed(checked), refresh()))
    library_edit.editingFinished.connect(
        lambda: (bridge.set_library(library_edit.text().strip()), refresh())
    )

    timer = QTimer(dialog)
    closing = [False]

    def on_close(event) -> None:
        """Не терять worker: отмена с ожиданием либо отказ от закрытия."""

        state = bridge.snapshot()
        if not state.busy:
            closing[0] = False
            timer.stop()
            bridge.close()
            event.accept()
            return
        if not ask_confirm("Операция ещё идёт. Отменить её и закрыть окно?"):
            event.ignore()
            return
        closing[0] = True
        if state.can_cancel:
            bridge.request_cancel()
        # Без блокировки GUI: таймер закроет окно, когда worker остановится.
        event.ignore()
        refresh()

    def finish_close_if_idle() -> None:
        if not closing[0]:
            return
        if bridge.snapshot().busy:
            return
        closing[0] = False
        timer.stop()
        bridge.close()
        dialog.close()

    dialog._close_handler = on_close  # type: ignore[attr-defined]
    timer.timeout.connect(finish_close_if_idle)
    timer.timeout.connect(refresh)
    timer.start(REFRESH_MS)
    refresh_devices()
    refresh()

    # Для тестов: кадр обновления и подтверждение без модального окна.
    dialog._refresh = refresh  # type: ignore[attr-defined]
    dialog._ask_confirm = ask_confirm  # type: ignore[attr-defined]
    dialog._bridge = bridge  # type: ignore[attr-defined]
    dialog._preview_shown = preview_shown  # type: ignore[attr-defined]
    dialog._device_combo = device_combo  # type: ignore[attr-defined]
    dialog._library_edit = library_edit  # type: ignore[attr-defined]
    dialog._rights = rights  # type: ignore[attr-defined]
    dialog._plan_button = plan_button  # type: ignore[attr-defined]
    dialog._export_button = export_button  # type: ignore[attr-defined]
    dialog._cancel_button = cancel_button  # type: ignore[attr-defined]
    dialog._verify_button = verify_button  # type: ignore[attr-defined]
    dialog._eject_button = eject_button  # type: ignore[attr-defined]
    dialog._table = table  # type: ignore[attr-defined]
    dialog._summary = summary  # type: ignore[attr-defined]
    dialog._skips = skips  # type: ignore[attr-defined]
    dialog._progress = progress  # type: ignore[attr-defined]
    dialog._progress_label = progress_label  # type: ignore[attr-defined]
    dialog._status = status  # type: ignore[attr-defined]
    dialog._timer = timer  # type: ignore[attr-defined]
    dialog._finish_close = finish_close_if_idle  # type: ignore[attr-defined]
    dialog._closing = closing  # type: ignore[attr-defined]
    dialog._on_plan = on_plan  # type: ignore[attr-defined]
    dialog._on_export = on_export  # type: ignore[attr-defined]
    dialog._on_cancel = on_cancel  # type: ignore[attr-defined]
    dialog._on_verify = on_verify  # type: ignore[attr-defined]
    dialog._on_eject = on_eject  # type: ignore[attr-defined]
    return dialog


def open_mixtape(parent: Any = None, bridge: MixtapeBridge | None = None) -> Any:
    """Показать экран USB Mixtape (немодально, рядом с окном плеера)."""

    available, reason = is_available()
    if not available:
        raise RuntimeError(reason)
    from PySide6.QtCore import Qt

    dialog = _build_dialog(bridge or MixtapeBridge())
    if parent is not None:
        # Qt-родитель держит диалог живым: Python-ссылка не нужна, а цикл
        # «окно → кнопка → замыкание → окно» ломал завершение приложения.
        dialog.setParent(parent)
    dialog.setWindowModality(Qt.NonModal)
    dialog.show()
    return dialog
