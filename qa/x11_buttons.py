"""Определение координат кнопок транспорта по снимку окна NeuroWave.

Ширина окна меняется (длинный заголовок растягивает окно — U-01), поэтому
координаты кнопок нельзя задавать константой: они вычисляются по фактическому
снимку окна.

Ряд кнопок находится поиском горизонтальных участков с повышенной яркостью
(фон кнопок светлее фона окна); выбирается строка, где участков ровно шесть.
Возвращаются центры участков в координатах окна (пиксели клиентской области).

Запуск: python qa/x11_buttons.py <window_id_hex> [out.png]
Вывод: JSON {"buttons": {"previous": [x, y], ...}, "window": [w, h]}
"""

from __future__ import annotations

import json
import sys

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

BUTTON_NAMES = ["previous", "play_pause", "next", "stop", "wave", "sound_lab"]


def luminance(pixel: int) -> float:
    red = (pixel >> 16) & 0xFF
    green = (pixel >> 8) & 0xFF
    blue = pixel & 0xFF
    return 0.299 * red + 0.587 * green + 0.114 * blue


def row_runs(image, y: int, threshold: float, min_width: int = 12) -> list[tuple[int, int]]:
    width = image.width()
    runs: list[tuple[int, int]] = []
    start = None
    for x in range(width):
        if luminance(image.pixel(x, y)) >= threshold:
            if start is None:
                start = x
        elif start is not None:
            if x - start >= min_width:
                runs.append((start, x - 1))
            start = None
    if start is not None and width - start >= min_width:
        runs.append((start, width - 1))
    return runs


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    window_id = int(sys.argv[1], 16)
    app = QApplication(sys.argv[:1])
    screen = QGuiApplication.primaryScreen()
    pixmap = screen.grabWindow(window_id)
    if pixmap.isNull() or pixmap.width() < 10:
        print("не удалось захватить окно", file=sys.stderr)
        return 1
    image = pixmap.toImage()
    if len(sys.argv) > 2:
        pixmap.save(sys.argv[2], "PNG")

    background = luminance(image.pixel(5, image.height() - 5))
    threshold = background + 12
    candidates = []
    for y in range(110, min(190, image.height() - 1)):
        runs = row_runs(image, y, threshold)
        if runs:
            candidates.append((len(runs), y, runs))
    exact = [item for item in candidates if item[0] == 6]
    if exact:
        _, y, runs = exact[len(exact) // 2]
    elif candidates:
        _, y, runs = max(candidates, key=lambda item: item[0])
    else:
        print(json.dumps({"error": "ряд кнопок не найден",
                          "background": background, "threshold": threshold}))
        return 1
    buttons = {}
    for name, (left, right) in zip(BUTTON_NAMES, runs[:6]):
        buttons[name] = [(left + right) // 2, y]

    # Ползунок громкости: строка ниже кнопок; ищем его границы по отличию
    # пикселей от фона окна.
    volume = None
    for volume_y in range(y + 25, min(y + 55, image.height() - 1)):
        left = None
        right = None
        for x in range(image.width()):
            if luminance(image.pixel(x, volume_y)) >= background + 4:
                if left is None:
                    left = x
                right = x
        # Дорожка ползунка широкая; подпись значения ("40 %") — узкая.
        if left is not None and right is not None and right - left > 200:
            volume = {"y": volume_y, "x1": left, "x2": right}
            break
    # Бегунок ползунка громкости — серый кружок на дорожке: ищем самый
    # «серый» пиксель (у красной заливки минимальный канал равен нулю).
    handle = None
    if volume:
        best = -1
        for x in range(80, max(81, image.width() - 80)):
            pixel = image.pixel(x, volume["y"])
            grey = min((pixel >> 16) & 0xFF, (pixel >> 8) & 0xFF, pixel & 0xFF)
            if grey > best:
                best = grey
                handle = x
        if handle is not None:
            volume["handle_x"] = handle
    print(json.dumps({"buttons": buttons, "window": [image.width(), image.height()],
                      "row_y": y, "runs": len(runs), "volume_track": volume}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
