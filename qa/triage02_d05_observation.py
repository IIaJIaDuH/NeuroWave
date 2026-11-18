"""QA-TRIAGE-02 / D-05 (наблюдение): теряются ли ошибки асинхронных команд.

Наблюдение, не исправление: проверяется фактический путь исключения от
команды плеера до статусной строки окна.

Сценарий: пользователь выбирает аудиовыход, которого уже нет (устройство
отключили). `player.set_output()` выбрасывает PlayerError. Команда уходит в
CommandDispatcher и выполняется в отдельном потоке.

Проверяется:
  * что происходит в CommandDispatcher (счётчики, command.error);
  * доходит ли исключение до статусной строки окна;
  * заполняется ли vm.last_error;
  * как тот же отказ выглядит в CLI (там исключение поднимается наружу).

Запуск: python qa/triage02_d05_observation.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qaenv as q  # noqa: E402

MEDIA = Path(os.environ.get("QA_MEDIA") or (q.SHARED / "media-verify"))
SHOTS = q.ROOT / "docs" / "qa" / "screenshots"
BAD_OUTPUT = "выход-которого-нет-qa"


def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    os.environ["PATH"] = f"{q.MPV.parent}:{os.environ['PATH']}"

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QLabel

    from neurowave.ui.app import run_window
    from neurowave.ui.viewmodel import PlayerViewModel

    harness = q.build_local_player("triage02_d05", repeat="all", volume=30, media_dir=MEDIA)
    player = harness.player
    result: dict = {}

    # --- 1. Чистый путь: ViewModel + CommandDispatcher без GUI ----------
    vm_plain = PlayerViewModel(player)
    vm_plain.start_polling()
    before_failed = vm_plain.dispatcher.stats["failed"]
    vm_plain.set_output(BAD_OUTPUT)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and vm_plain.dispatcher.stats["failed"] == before_failed:
        time.sleep(0.05)
    time.sleep(0.4)
    result["viewmodel_layer"] = {
        "command_failed_counter_before": before_failed,
        "command_failed_counter_after": vm_plain.dispatcher.stats["failed"],
        "vm_last_error": vm_plain.last_error,
        "events_in_queue": [e.name for e in vm_plain.drain()],
        "вывод_в_статусную_строку": None,
    }
    vm_plain.close()

    # --- 2. То же самое в настоящем окне --------------------------------
    app = QApplication.instance() or QApplication(sys.argv)
    state: dict = {}

    def find_window():
        for widget in QApplication.instance().topLevelWidgets():
            if widget.__class__.__name__ == "QMainWindow":
                return widget
        return None

    def status_text() -> str | None:
        win = find_window()
        if win is None:
            return None
        for label in win.findChildren(QLabel):
            if "8b97a8" in (label.styleSheet() or ""):
                return label.text()
        return None

    def step_start() -> None:
        player.play_tracks(sorted(harness.adapter.tracks(), key=lambda t: -(t.duration_ms or 0))[:1], start=0)
        QTimer.singleShot(2500, step_fire)

    def step_fire() -> None:
        win = find_window()
        vm = win._viewmodel
        state["status_before"] = status_text()
        state["failed_before"] = vm.dispatcher.stats["failed"]
        # Ровно то, что делает комбобокс выхода: vm.set_output(itemData(index)).
        vm.set_output(BAD_OUTPUT)
        QTimer.singleShot(2500, step_collect)

    def step_collect() -> None:
        win = find_window()
        vm = win._viewmodel
        state["status_after"] = status_text()
        state["failed_after"] = vm.dispatcher.stats["failed"]
        state["vm_last_error"] = vm.last_error
        state["dispatcher_has_on_error"] = vm.dispatcher._on_error is not None
        state["events_in_queue"] = [e.name for e in vm.drain()]
        # Снимок окна: пользователь должен увидеть хоть какое-то сообщение.
        window_id = int(win.winId())
        code, out, _ = q.run(["xwininfo", "-id", str(window_id)], timeout=20)
        geom = {}
        for line in out.splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            key, value = key.strip(), value.strip()
            mapping = {"Width": "width", "Height": "height",
                       "Absolute upper-left X": "x", "Absolute upper-left Y": "y"}
            if key in mapping and value.isdigit():
                geom[mapping[key]] = int(value)
        if len(geom) == 4:
            subprocess.run(["wmctrl", "-i", "-a", str(window_id)], timeout=20)
            time.sleep(0.6)
            q.run(["ffmpeg", "-y", "-v", "error", "-f", "x11grab",
                   "-video_size", "1920x1080", "-i", os.environ.get("DISPLAY", ":0"),
                   "-vf", f"crop={geom['width']}:{geom['height']}:{geom['x']}:{geom['y']}",
                   "-frames:v", "1", str(SHOTS / "triage02_d05_status_line.png")], timeout=60)
        result["gui_layer"] = state
        QApplication.instance().quit()

    QTimer.singleShot(700, step_start)

    try:
        run_window(player)
    finally:
        harness.close()

    # --- 3. Тот же отказ в CLI ------------------------------------------
    home = q.qa_home("triage02_d05_cli")
    (home / "config").mkdir(parents=True, exist_ok=True)
    (home / "config" / "config.json").write_text(json.dumps({
        "provider": "local", "local_music_dir": str(MEDIA), "volume": 30,
    }), encoding="utf-8")
    env = {
        "NEUROWAVE_HOME": str(home),
        "PATH": f"{q.MPV.parent}:{os.environ['PATH']}",
        "PYTHONPATH": str(q.ROOT / "src"),
        "DISPLAY": os.environ.get("DISPLAY", ":0"),
    }
    proc = subprocess.run(
        [str(q.ROOT / ".venv" / "bin" / "neurowave"), "--provider", "local",
         "output", "--set", BAD_OUTPUT],
        env=env, capture_output=True, text=True, timeout=90,
    )
    result["cli_layer"] = {
        "exit_code": proc.returncode,
        "stderr_tail": proc.stderr.strip()[-400:],
        "has_traceback": "Traceback (most recent call last)" in proc.stderr,
    }

    result["scenario"] = "QA-TRIAGE-02 / D-05: наблюдение — путь ошибки команды"
    result["sha"] = "7a82119d23775f7ed02db14e232dcefbc8ade3d9"
    path = q.write_result("triage02_d05_observation", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"→ {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
