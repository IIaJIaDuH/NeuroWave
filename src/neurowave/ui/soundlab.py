"""Sound Lab: редактор профиля — преамп, полосы эквалайзера, лимитер.

Диалог ничего не решает про звук сам: он собирает `AudioProfile` (проверяемый
тип из `audio/dsp_profiles.py`) и отправляет его в плеер через диспетчер.
Ошибки валидации показываются рядом с кнопкой, а не всплывающим окном: их
нужно видеть вместе с тем, что ввели.
"""

from __future__ import annotations

from typing import Any

from ..audio.dsp_profiles import (
    BAND_KINDS,
    MAX_BANDS,
    MAX_FREQUENCY_HZ,
    MIN_FREQUENCY_HZ,
    AudioProfile,
    DspError,
    DspMode,
    EqBand,
    Limiter,
)
from .viewmodel import PlayerViewModel

BAND_LABELS = {
    "peaking": "пик",
    "lowshelf": "низкая полка",
    "highshelf": "высокая полка",
}


def _build_dialog(vm: PlayerViewModel) -> Any:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (
        QCheckBox,
        QComboBox,
        QDialog,
        QDoubleSpinBox,
        QGroupBox,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QPushButton,
        QTableWidget,
        QTableWidgetItem,
        QVBoxLayout,
    )

    dialog = QDialog()
    dialog.setWindowTitle("Sound Lab")
    dialog.resize(760, 520)
    root = QHBoxLayout(dialog)

    listing = QListWidget()
    listing.setMinimumWidth(220)
    root.addWidget(listing)

    form = QVBoxLayout()
    root.addLayout(form, 1)

    from PySide6.QtWidgets import QFormLayout

    meta = QFormLayout()
    name_edit = QLineEdit()
    id_edit = QLineEdit()
    id_edit.setPlaceholderText("латиница, цифры, - и _")
    id_edit.setEnabled(False)
    preamp = QDoubleSpinBox()
    preamp.setRange(-24.0, 12.0)
    preamp.setSuffix(" дБ")
    preamp.setDecimals(1)
    meta.addRow("Название", name_edit)
    meta.addRow("id", id_edit)
    meta.addRow("Preamp", preamp)
    form.addLayout(meta)

    bands = QTableWidget(0, 4)
    bands.setHorizontalHeaderLabels(["Тип", "Частота, Гц", "Усиление, дБ", "Q"])
    bands.horizontalHeader().setStretchLastSection(True)
    form.addWidget(QLabel("Полосы эквалайзера"))
    form.addWidget(bands, 1)

    band_buttons = QHBoxLayout()
    add_band = QPushButton("+ полоса")
    remove_band = QPushButton("− полоса")
    band_buttons.addWidget(add_band)
    band_buttons.addWidget(remove_band)
    band_buttons.addStretch(1)
    form.addLayout(band_buttons)

    limiter_box = QGroupBox("Лимитер (защита от клиппинга)")
    limiter_form = QFormLayout(limiter_box)
    limiter_on = QCheckBox("включён")
    ceiling = QDoubleSpinBox()
    ceiling.setRange(-60.0, 0.0)
    ceiling.setSuffix(" dBFS")
    ceiling.setDecimals(1)
    lookahead = QDoubleSpinBox()
    lookahead.setRange(1.0, 50.0)
    lookahead.setSuffix(" мс")
    release = QDoubleSpinBox()
    release.setRange(5.0, 500.0)
    release.setSuffix(" мс")
    limiter_form.addRow(limiter_on)
    limiter_form.addRow("Потолок", ceiling)
    limiter_form.addRow("Lookahead", lookahead)
    limiter_form.addRow("Release", release)
    form.addWidget(limiter_box)

    state = {"selected": "flat", "saved_id": ""}

    status = QLabel("")
    status.setWordWrap(True)
    status.setStyleSheet("font-size: 12px;")
    form.addWidget(status)

    actions = QHBoxLayout()
    apply_button = QPushButton("Применить")
    bypass_button = QPushButton("Обход (Pure)")
    save_button = QPushButton("Сохранить свой")
    delete_button = QPushButton("Удалить")
    close_button = QPushButton("Закрыть")
    for button in (apply_button, bypass_button, save_button, delete_button):
        actions.addWidget(button)
    actions.addStretch(1)
    actions.addWidget(close_button)
    form.addLayout(actions)

    def show_status(text: str, error: bool = False) -> None:
        status.setStyleSheet("font-size: 12px; color: #c0392b;" if error else "font-size: 12px; color: #7f8c8d;")
        status.setText(text)

    def fill_list() -> None:
        listing.blockSignals(True)
        listing.clear()
        for profile_id, name, builtin in vm.profiles():
            item = QListWidgetItem(("★ " if builtin else "• ") + name)
            item.setData(Qt.UserRole, profile_id)
            item.setData(Qt.UserRole + 1, builtin)
            listing.addItem(item)
        listing.blockSignals(False)

    def load_profile(profile_id: str) -> None:
        profile = vm.player.profiles.resolve(profile_id)
        state["selected"] = profile.id
        name_edit.setText(profile.name)
        id_edit.setText(profile.id)
        id_edit.setEnabled(not vm.player.profiles.is_builtin(profile.id))
        preamp.setValue(profile.preamp_db)
        bands.setRowCount(0)
        for band in profile.bands:
            add_row(band)
        limiter_on.setChecked(profile.limiter.enabled)
        ceiling.setValue(profile.limiter.ceiling_dbfs)
        lookahead.setValue(profile.limiter.lookahead_ms)
        release.setValue(profile.limiter.release_ms)
        mode = profile.mode.value
        show_status(
            f"{profile.name} · режим {mode} · "
            + ("встроенный профиль: можно править копию" if vm.player.profiles.is_builtin(profile.id) else "свой профиль")
        )

    def add_row(band: EqBand | None = None) -> None:
        row = bands.rowCount()
        bands.insertRow(row)
        kind = QComboBox()
        for value in BAND_KINDS:
            kind.addItem(BAND_LABELS.get(value, value), value)
        if band is not None:
            kind.setCurrentIndex(max(0, kind.findData(band.kind)))
        bands.setCellWidget(row, 0, kind)
        frequency = QDoubleSpinBox()
        frequency.setRange(MIN_FREQUENCY_HZ, MAX_FREQUENCY_HZ)
        frequency.setValue(band.frequency_hz if band else 1000.0)
        gain = QDoubleSpinBox()
        gain.setRange(-24.0, 24.0)
        gain.setValue(band.gain_db if band else 0.0)
        quality = QDoubleSpinBox()
        quality.setRange(0.1, 10.0)
        quality.setDecimals(2)
        quality.setValue(band.q if band else 1.0)
        bands.setCellWidget(row, 1, frequency)
        bands.setCellWidget(row, 2, gain)
        bands.setCellWidget(row, 3, quality)

    def collect() -> AudioProfile:
        if bands.rowCount() > MAX_BANDS:
            raise DspError(f"Полос не больше {MAX_BANDS}")
        collected: list[EqBand] = []
        for row in range(bands.rowCount()):
            kind = bands.cellWidget(row, 0)
            frequency = bands.cellWidget(row, 1)
            gain = bands.cellWidget(row, 2)
            quality = bands.cellWidget(row, 3)
            collected.append(
                EqBand(
                    kind=kind.currentData(),
                    frequency_hz=frequency.value(),
                    gain_db=gain.value(),
                    q=quality.value(),
                )
            )
        return AudioProfile(
            id=id_edit.text().strip() or state["selected"],
            name=name_edit.text().strip(),
            mode=DspMode.ENHANCED,
            preamp_db=preamp.value(),
            bands=tuple(collected),
            limiter=Limiter(
                enabled=limiter_on.isChecked(),
                ceiling_dbfs=ceiling.value(),
                lookahead_ms=lookahead.value(),
                release_ms=release.value(),
            ),
        )

    def on_apply() -> None:
        try:
            profile = collect()
        except DspError as exc:
            show_status(f"Проверьте параметры: {exc}", error=True)
            return
        vm.apply_profile(profile)
        show_status(f"Отправлено в плеер: {profile.name}. Статус появится в окне.")

    def on_bypass() -> None:
        vm.set_mode("pure")
        show_status("Наши фильтры сняты (Pure). Вернуть — кнопкой «Применить».")

    def on_save() -> None:
        try:
            profile = collect()
            if vm.player.profiles.is_builtin(profile.id):
                show_status("Это встроенный профиль: задайте свой id, чтобы сохранить копию.", error=True)
                return
            vm.save_profile(profile)
        except DspError as exc:
            show_status(f"Не сохранено: {exc}", error=True)
            return
        state["saved_id"] = profile.id
        fill_list()
        show_status(f"Сохранено: {profile.id}. Применится при следующем воспроизведении.")

    def on_delete() -> None:
        current = state["selected"]
        if vm.player.profiles.is_builtin(current):
            show_status("Встроенные профили не удаляются.", error=True)
            return
        try:
            vm.delete_profile(current)
        except Exception as exc:
            show_status(f"Не удалено: {exc}", error=True)
            return
        fill_list()
        show_status(f"Удалено: {current}")

    add_band.clicked.connect(lambda: add_row())
    remove_band.clicked.connect(lambda: bands.removeRow(bands.currentRow()) if bands.currentRow() >= 0 else None)
    apply_button.clicked.connect(on_apply)
    bypass_button.clicked.connect(on_bypass)
    save_button.clicked.connect(on_save)
    delete_button.clicked.connect(on_delete)
    close_button.clicked.connect(dialog.accept)
    listing.currentItemChanged.connect(
        lambda item, _prev: item is not None and load_profile(item.data(Qt.UserRole))
    )

    fill_list()
    load_profile(vm.player.settings.dsp_profile)
    dialog._load_profile = load_profile  # type: ignore[attr-defined]
    dialog._on_apply = on_apply  # type: ignore[attr-defined]
    dialog._on_save = on_save  # type: ignore[attr-defined]
    dialog._collect = collect  # type: ignore[attr-defined]
    dialog._bands = bands  # type: ignore[attr-defined]
    dialog._status = status  # type: ignore[attr-defined]
    dialog._add_row = add_row  # type: ignore[attr-defined]
    return dialog


def open_soundlab(vm: PlayerViewModel) -> Any:
    dialog = _build_dialog(vm)
    dialog.exec()
    return dialog
