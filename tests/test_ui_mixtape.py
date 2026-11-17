"""USB-07: экран USB Mixtape в offscreen-режиме.

Проверяются связки, а не внешний вид: устройство, папка, треки, объём и причины
пропуска видны до записи; без прав кнопка записи выключена; запись идёт в фоне
моста, прогресс появляется в окне; «Извлечь» недоступна, пока worker занят.
Без PySide6 тест пропускается.
"""

from __future__ import annotations

import math
import os
import struct
import wave
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests.conftest import require_qt  # noqa: E402

require_qt()

from neurowave.mixtape.bridge import MixtapeBridge  # noqa: E402
from neurowave.mixtape.devices import FakeUsbDeviceService  # noqa: E402
from neurowave.mixtape.jobs import JobStore  # noqa: E402
from neurowave.mixtape.models import DeviceIdentity, MixtapeRequest  # noqa: E402

OBJECT = "/org/freedesktop/UDisks2/block_devices/sdb1"


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def make_wav(path: Path, *, duration_ms: int = 5_000, rate: int = 8_000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(rate * duration_ms / 1000)
    data = b"".join(
        struct.pack("<h", int(1_000 * math.sin(index / 10))) for index in range(frames)
    )
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(data)
    return path


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "Music"
    make_wav(root / "Artist One - Song One.wav")
    make_wav(root / "Artist Two - Song Two.wav")
    return root


@pytest.fixture
def usb(tmp_path):
    root = tmp_path / "usb"
    root.mkdir()
    return root


@pytest.fixture
def service(usb):
    return FakeUsbDeviceService(
        [
            DeviceIdentity(
                udisks_object_path=OBJECT,
                block_device="/dev/sdb1",
                mountpoint=usb.resolve(),
                filesystem_type="vfat",
                filesystem_uuid="1234-ABCD",
                drive_id="USB_Stick",
                removable=True,
                writable=True,
                capacity_bytes=16_000_000_000,
                free_bytes=8_000_000_000,
            )
        ]
    )


@pytest.fixture
def dialog(qapp, service, tmp_path, library):
    from neurowave.ui.mixtape import _build_dialog

    bridge = MixtapeBridge(
        device_service=service, job_store=JobStore(tmp_path / "jobs"), library=library
    )
    window = _build_dialog(bridge)
    yield window, bridge, library
    window.close()
    bridge.close()


def build_plan(dialog, bridge) -> None:
    dialog._rights.setChecked(True)
    dialog._on_plan()
    assert bridge.wait(10)
    dialog._refresh()


def test_screen_shows_device_folder_and_plan(dialog, usb):
    window, bridge, library = dialog
    window._refresh()

    assert window._device_combo.count() == 1
    assert OBJECT in window._device_combo.currentText()
    assert window._library_edit.text() == str(library)
    assert window._export_button.isEnabled() is False

    build_plan(window, bridge)

    assert window._table.rowCount() == 2
    titles = {window._table.item(row, 1).text() for row in range(2)}
    assert titles == {"Song One", "Song Two"}
    destinations = {window._table.item(row, 3).text() for row in range(2)}
    assert all(item.startswith("MUSIC/NeuroWave/") for item in destinations)
    assert "Файлов: 2" in window._summary.text()
    assert "МБ" in window._summary.text()
    assert window._export_button.isEnabled() is True
    assert window._eject_button.isEnabled() is True


def test_skip_reasons_visible_before_write(dialog):
    window, bridge, _library = dialog
    window._rights.setChecked(True)
    assert bridge.request_plan(
        rights_confirmed=True,
        request=MixtapeRequest(
            target_duration_ms=3_600_000, excluded_artists=("Artist One",)
        ),
    )
    assert bridge.wait(10)
    window._refresh()

    assert window._table.rowCount() == 1
    assert "excluded_artist: 1" in window._skips.text()


def test_export_requires_confirmation_dialog(dialog, usb, monkeypatch):
    window, bridge, _library = dialog
    from neurowave.ui import mixtape as mixtape_ui

    build_plan(window, bridge)
    asked: list[str] = []

    def refuse(parent, text):
        asked.append(text)
        return False

    monkeypatch.setattr(mixtape_ui, "_ask", refuse)
    window._on_export()
    assert asked and "Записать 2 файлов" in asked[0]
    assert bridge.snapshot().status.value == "planned"
    assert not (usb / "MUSIC").exists()


def test_export_progress_and_eject_state(dialog, usb, monkeypatch):
    window, bridge, _library = dialog
    from neurowave.ui import mixtape as mixtape_ui

    monkeypatch.setattr(mixtape_ui, "_ask", lambda parent, text: True)
    build_plan(window, bridge)
    window._on_export()
    assert bridge.wait(20)
    window._refresh()

    assert (usb / "MUSIC/NeuroWave").is_dir()
    assert "100 %" in window._progress_label.text()
    assert "Проверено" not in window._status.text()
    assert "Записано файлов: 2" in window._status.text()
    assert window._progress.value() == 1000
    assert window._verify_button.isEnabled() is True
    assert window._cancel_button.isEnabled() is False


def test_verify_from_screen(dialog, monkeypatch):
    window, bridge, _library = dialog
    from neurowave.ui import mixtape as mixtape_ui

    monkeypatch.setattr(mixtape_ui, "_ask", lambda parent, text: True)
    build_plan(window, bridge)
    window._on_export()
    assert bridge.wait(20)
    window._refresh()

    window._on_verify()
    assert bridge.wait(20)
    window._refresh()

    assert "проверка файлов OK" in window._status.text()


def test_eject_disabled_while_exporting(dialog, monkeypatch):
    window, bridge, _library = dialog
    from neurowave.mixtape.export import SafeWriter
    from neurowave.ui import mixtape as mixtape_ui

    monkeypatch.setattr(mixtape_ui, "_ask", lambda parent, text: True)
    started = __import__("threading").Event()
    release = __import__("threading").Event()
    real_write = SafeWriter.write

    def blocked(self, *args, **kwargs):
        started.set()
        release.wait(10)
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(SafeWriter, "write", blocked)
    build_plan(window, bridge)
    window._on_export()
    try:
        assert started.wait(10)
        window._refresh()
        assert window._eject_button.isEnabled() is False
        assert window._cancel_button.isEnabled() is True
    finally:
        release.set()
        assert bridge.wait(20)
    window._refresh()
    assert window._eject_button.isEnabled() is True


# --- USB-FIX-06: закрытие окна не оставляет worker ------------------------


def test_close_stops_refresh_timer_and_bridge(dialog):
    window, bridge, _library = dialog

    assert window._timer.isActive() is True

    window.close()

    assert window._timer.isActive() is False
    assert bridge.snapshot().busy is False
    assert window._closing[0] is False


def _blocked_export(dialog, monkeypatch):
    window, bridge, _library = dialog
    from neurowave.mixtape.export import SafeWriter
    from neurowave.ui import mixtape as mixtape_ui

    started = __import__("threading").Event()
    release = __import__("threading").Event()
    real_write = SafeWriter.write

    def blocked(self, *args, **kwargs):
        started.set()
        release.wait(10)
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(SafeWriter, "write", blocked)
    monkeypatch.setattr(mixtape_ui, "_ask", lambda parent, text: True)
    build_plan(window, bridge)
    window._on_export()
    assert started.wait(10)
    window._refresh()
    return window, bridge, release


def test_close_while_exporting_can_be_cancelled_by_user(dialog, monkeypatch):
    from neurowave.ui import mixtape as mixtape_ui

    window, bridge, release = _blocked_export(dialog, monkeypatch)
    monkeypatch.setattr(mixtape_ui, "_ask", lambda parent, text: False)
    try:
        window.close()

        assert window._closing[0] is False
        assert window._timer.isActive() is True
        assert bridge.snapshot().status.value == "exporting"
    finally:
        release.set()
        assert bridge.wait(20)


def test_close_while_exporting_cancels_and_finishes(dialog, monkeypatch):
    window, bridge, release = _blocked_export(dialog, monkeypatch)

    window.close()

    assert window._closing[0] is True
    assert bridge.snapshot().status.value == "exporting"

    release.set()
    assert bridge.wait(20)
    window._finish_close()

    assert window._closing[0] is False
    assert window._timer.isActive() is False
    assert bridge.snapshot().busy is False
    assert bridge.snapshot().status.value == "cancelled"
