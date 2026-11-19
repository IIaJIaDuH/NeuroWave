"""Снимок одного окна X11 (без остального экрана).

Используется PySide6: QScreen.grabWindow(window_id) захватывает только
указанное окно. Снимок всего экрана недопустим: на рабочем столе могут быть
личные окна.

Запуск: python qa/x11_capture.py <window_id_hex> <out.png>
"""

from __future__ import annotations

import sys

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: x11_capture.py <window_id_hex> <out.png>", file=sys.stderr)
        return 2
    window_id = int(sys.argv[1], 16)
    out = sys.argv[2]
    app = QApplication(sys.argv[:1])
    screen = QGuiApplication.primaryScreen()
    pixmap = screen.grabWindow(window_id)
    if pixmap.isNull() or pixmap.width() < 10:
        print("не удалось захватить окно", file=sys.stderr)
        return 1
    pixmap.save(out, "PNG")
    print(f"{out} {pixmap.width()}x{pixmap.height()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
