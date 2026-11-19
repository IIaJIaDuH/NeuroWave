"""Синтетический ввод X11 (XTEST): клик и перетаскивание по окну.

Нужен для GUI-проверки без xdotool. Координаты задаются **внутри окна**
(пиксели клиентской области) и переводятся в абсолютные через геометрию
окна, поэтому клик всегда попадает в нужное окно, а не в то, что случайно
в фокусе.

Запуск (системный python3, где доступен Xlib):
  python3 qa/x11_click.py click   <window_id_hex> <x> <y>
  python3 qa/x11_click.py drag    <window_id_hex> <x1> <y1> <x2> <y2>
  python3 qa/x11_click.py activate <window_id_hex>
"""

from __future__ import annotations

import subprocess
import sys
import time

from Xlib import X, display


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    action = sys.argv[1]
    window_id = int(sys.argv[2], 16)
    disp = display.Display()
    root = disp.screen().root
    window = disp.create_resource_object("window", window_id)

    if action == "activate":
        subprocess.run(["wmctrl", "-i", "-a", hex(window_id)], timeout=20, check=False)
        time.sleep(0.8)
        print("activated")
        return 0

    # Окно переподчинено менеджеру: абсолютные координаты — через translate_coords.
    shifted = root.translate_coords(window, 0, 0)
    abs_x, abs_y = shifted.x, shifted.y

    values = [int(value) for value in sys.argv[3:]]

    def move(x: int, y: int) -> None:
        disp.xtest_fake_input(X.MotionNotify, root=root, x=abs_x + x, y=abs_y + y)

    if action == "click":
        x, y = values
        move(x, y)
        disp.xtest_fake_input(X.ButtonPress, 1)
        time.sleep(0.08)
        disp.xtest_fake_input(X.ButtonRelease, 1)
    elif action == "drag":
        x1, y1, x2, y2 = values
        move(x1, y1)
        disp.xtest_fake_input(X.ButtonPress, 1)
        time.sleep(0.08)
        for step in range(1, 11):
            move(x1 + (x2 - x1) * step // 10, y1 + (y2 - y1) * step // 10)
            time.sleep(0.03)
        disp.xtest_fake_input(X.ButtonRelease, 1)
    else:
        print(f"неизвестное действие: {action}", file=sys.stderr)
        return 2
    disp.sync()
    time.sleep(0.3)
    print(f"ok (окно в {abs_x},{abs_y})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
