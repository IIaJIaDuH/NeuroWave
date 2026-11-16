"""Системный трей (Qt). PySide6 подключается лениво.

Модуль импортируется и без PySide6: :func:`is_available` сообщает причину, а
ядро, CLI и тесты остаются работоспособными без GUI. Трей не трогает
аудиопоток: он лишь вызывает публичные методы `PlayerService`.
"""

from __future__ import annotations

import sys
from typing import Any

TITLE = "NeuroWave"
UPDATE_INTERVAL_MS = 1000


class TrayUnavailable(RuntimeError):
    """Трей нельзя показать: нет PySide6, нет системного лотка."""


def is_available() -> tuple[bool, str]:
    """Можно ли показать трей в этом окружении (без создания окна)."""
    try:
        # Именно QtWidgets: сам пакет PySide6 может импортироваться, а Qt — нет
        # (например, без libxkbcommon или без X/Wayland-библиотек).
        from PySide6.QtWidgets import QApplication  # noqa: F401
    except Exception as exc:
        return False, (
            f"PySide6 недоступен ({type(exc).__name__}: {exc}). "
            "Установите: pip install -e '.[ui]'"
        )
    return True, ""


def _make_icon() -> Any:
    """Иконка рисуется кодом: не зависим от путей внутри репозитория."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QBrush, QColor, QIcon, QLinearGradient, QPainter, QPen, QPixmap

    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    gradient = QLinearGradient(0, 0, 64, 64)
    gradient.setColorAt(0.0, QColor("#4fd1ff"))
    gradient.setColorAt(1.0, QColor("#8b5cf6"))
    painter.setPen(Qt.NoPen)
    painter.setBrush(QBrush(gradient))
    painter.drawEllipse(QRectF(3, 3, 58, 58))
    painter.setPen(QPen(QColor("#08111a"), 5))
    for index, height in enumerate((16, 30, 22)):
        x = 21 + index * 8
        painter.drawLine(x, 32 - height // 2, x, 32 + height // 2)
    painter.end()
    return QIcon(pixmap)


def run_tray(player: Any, on_quit: Any = None) -> int:
    """Показать трей и блокироваться до «Выхода».

    Сессия ядра уже должна играть: трей только управляет ею.
    """
    available, reason = is_available()
    if not available:
        raise TrayUnavailable(reason)

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(TITLE)
    app.setQuitOnLastWindowClosed(False)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        raise TrayUnavailable("системный лоток недоступен (нет панели/статус-нотификатора)")

    def guard(action: str, fn: Any) -> None:
        try:
            fn()
        except Exception as exc:  # ошибка действия не должна ронять трей
            tray.showMessage(TITLE, f"{action}: {exc}", QSystemTrayIcon.Warning, 4000)

    menu = QMenu()
    play_action = menu.addAction("Пауза")
    play_action.triggered.connect(lambda: guard("Пауза", player.toggle_pause))
    next_action = menu.addAction("Следующий")
    next_action.triggered.connect(lambda: guard("Следующий", player.next_track))
    previous_action = menu.addAction("Предыдущий")
    previous_action.triggered.connect(lambda: guard("Предыдущий", player.previous_track))
    menu.addSeparator()
    quality_action = menu.addAction("Источник: —")
    quality_action.setEnabled(False)
    menu.addSeparator()
    quit_action = menu.addAction("Выход")
    quit_action.triggered.connect(app.quit)

    tray = QSystemTrayIcon(_make_icon(), app)
    tray.setToolTip(TITLE)
    tray.setContextMenu(menu)
    tray.show()
    tray.showMessage(TITLE, "«Моя волна» в трее", QSystemTrayIcon.Information, 3000)

    def refresh() -> None:
        track = player.current_track
        playing = getattr(player.state, "status", None)
        play_action.setText("Пауза" if getattr(playing, "value", "") == "playing" else "Играть")
        quality = player.current_quality
        quality_action.setText(f"Источник: {quality.label()}" if quality else "Источник: —")
        tray.setToolTip(f"{TITLE} — {track.label}" if track else TITLE)

    timer = QTimer()
    timer.timeout.connect(refresh)
    timer.start(UPDATE_INTERVAL_MS)
    refresh()

    code = app.exec()
    timer.stop()
    tray.hide()
    if on_quit is not None:
        on_quit()
    return int(code)
