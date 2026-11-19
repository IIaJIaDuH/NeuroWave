"""D-01/D-05 на уровне GUI: «вперёд» + перемотка ползунком.

Сценарий: пользователь нажимает «вперёд» и сразу тянет ползунок позиции.
Ожидание: перемотка происходит. Фактичество (как подозревается): mpv
отклоняет seek, а интерфейс не показывает об этом ничего — статусная строка
молчит, потому что CommandDispatcher создана без on_error.

Запуск: python qa/defect_gui_seek.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qaenv as q  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def main() -> int:
    from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QSlider

    from neurowave.ui.app import _build_window
    from neurowave.ui.viewmodel import PlayerViewModel

    q.ensure_dirs()
    app = QApplication.instance() or QApplication([])
    harness = q.build_local_player("qa_defect_gui", repeat="all", volume=30)
    vm = PlayerViewModel(harness.player)
    window = _build_window(vm)
    window.show()
    app.processEvents()

    buttons = {b.text(): b for b in window.findChildren(QPushButton)}
    sliders = window.findChildren(QSlider)
    status = [lab for lab in window.findChildren(QLabel) if lab.text() == "Готово"]

    result: dict = {"scenario": "GUI: следующий + перемотка"}
    harness.player.play_tracks(harness.adapter.tracks(), start=0)
    time.sleep(2.0)
    window._refresh(); app.processEvents()

    # 1. Нажимаем «вперёд» (реальная кнопка).
    buttons["⏭"].click()
    app.processEvents()
    time.sleep(0.05)
    result["after_next_status"] = status[0].text() if status else None

    # 2. Сразу тянем ползунок позиции (имитация перемотки мышью).
    seek = sliders[0]
    seek.setValue(600)
    seek.sliderReleased.emit()
    app.processEvents()
    result["position_immediately_after_seek_ms"] = harness.backend.position_ms()
    time.sleep(1.0)
    window._refresh(); app.processEvents()

    result["status_after_seek"] = status[0].text() if status else None
    result["position_ms"] = harness.backend.position_ms()
    result["target_position_ms"] = int(0.6 * (harness.player.current_track.duration_ms or 0))
    result["vm_last_error"] = getattr(vm, "last_error", None)
    result["seek_actually_moved"] = harness.backend.position_ms() > 30000
    result["dispatcher_stats"] = dict(vm.dispatcher.stats)
    result["silent_failure"] = (
        result["dispatcher_stats"].get("failed", 0) > 0
        and "seek" not in (result["status_after_seek"] or "").lower()
        and "перемотка" not in (result["status_after_seek"] or "").lower()
    )

    # 3. Контроль: перемотка на прогретом треке — работает.
    time.sleep(1.5)
    seek.setValue(300)
    seek.sliderReleased.emit()
    app.processEvents()
    time.sleep(1.0)
    result["warm_seek_position_ms"] = harness.backend.position_ms()
    result["warm_seek_status"] = status[0].text() if status else None

    # 4. Контроль: тот же сценарий, но с паузой 1.5 с после «вперёд».
    buttons["\u23ed"].click()
    app.processEvents()
    time.sleep(1.5)
    seek.setValue(800)
    seek.sliderReleased.emit()
    app.processEvents()
    time.sleep(0.8)
    result["control_with_pause_position_ms"] = harness.backend.position_ms()
    result["control_target_ms"] = int(0.8 * (harness.player.current_track.duration_ms or 0))
    result["control_seek_worked"] = abs(
        result["control_with_pause_position_ms"] - result["control_target_ms"]) < 4000

    vm.close()
    harness.close()
    path = q.write_result("defect_gui_seek", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"→ {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
