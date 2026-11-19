"""QA-TRIAGE-02 / D-06: темп Qt-цикла и стоимость кадра обновления по фазам.

Три фазы по 10 секунд с включённым таймером обновления окна (200 мс):
  A — очередь пуста;
  B — обычный трек, длительность известна;
  C — трек с duration_ms=None (дефект D-06).

Меряется: тики «сердцебиения» (150 мс) в секунду — отвечает ли Qt-цикл;
длительность одного refresh() — что именно занимает время;
число исключений — растёт ли оно.

Стенд не блокирует GUI-поток (никаких busy-wait): иначе темп цикла измерял бы
сам стенд. Сэмплер каждой фазы останавливается вместе с фазой.

Запуск: python qa/triage02_gui_timing.py
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qaenv as q  # noqa: E402

MEDIA = Path(os.environ.get("QA_MEDIA") or (q.SHARED / "media-verify"))
PHASE_SECONDS = 10.0
BEAT_MS = 150


def main() -> int:
    os.environ["PATH"] = f"{q.MPV.parent}:{os.environ['PATH']}"
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from neurowave.ui.app import run_window

    harness = q.build_local_player("triage02_timing", repeat="all", volume=30, media_dir=MEDIA)
    player = harness.player
    tracks = sorted(harness.adapter.tracks(), key=lambda t: -(t.duration_ms or 0))
    normal = tracks[0]
    unknown = dataclasses.replace(normal, duration_ms=None)

    app = QApplication.instance() or QApplication(sys.argv)
    beats = {"n": 0}
    hb = QTimer()
    hb.timeout.connect(lambda: beats.__setitem__("n", beats["n"] + 1))
    hb.start(BEAT_MS)

    report: dict = {"phase_seconds": PHASE_SECONDS, "beat_interval_ms": BEAT_MS, "phases": {}}

    def find_window():
        for widget in QApplication.instance().topLevelWidgets():
            if widget.__class__.__name__ == "QMainWindow":
                return widget
        return None

    def run_phase(key: str, description: str, prepare, next_step) -> None:
        """Одна фаза: темп цикла, стоимость refresh, число исключений."""
        win = find_window()
        if prepare is not None:
            prepare()
        start_beats = beats["n"]
        start = time.monotonic()
        samples: list[float] = []
        errors: list[str] = []
        running = {"on": True}

        def sample() -> None:
            if not running["on"]:
                return
            began = time.monotonic()
            try:
                win._refresh()
            except Exception as exc:  # noqa: BLE001
                errors.append(type(exc).__name__)
            samples.append((time.monotonic() - began) * 1000)
            QTimer.singleShot(200, sample)

        def finish() -> None:
            running["on"] = False
            elapsed = time.monotonic() - start
            beat_delta = beats["n"] - start_beats
            track = player.current_track
            report["phases"][key] = {
                "description": description,
                "track_id": track.id if track else None,
                "duration_ms": track.duration_ms if track else None,
                "beat_ticks": beat_delta,
                "beats_per_second": round(beat_delta / elapsed, 2) if elapsed else None,
                "expected_beats_per_second": round(1000 / BEAT_MS, 2),
                "refresh_manual_ms": {
                    "min": round(min(samples), 1) if samples else None,
                    "max": round(max(samples), 1) if samples else None,
                    "calls": len(samples),
                },
                "refresh_errors": len(errors),
                "refresh_error_types": sorted(set(errors)),
                "position_ms": player.backend.position_ms(),
                "status": getattr(player.state.status, "value", str(player.state.status)),
            }
            next_step()

        QTimer.singleShot(200, sample)
        QTimer.singleShot(int(PHASE_SECONDS * 1000), finish)

    def step_a() -> None:
        run_phase("A_empty_queue", "очередь пуста, трек не играет", None, lag(step_b))

    def step_b() -> None:
        run_phase(
            "B_known_duration",
            "обычный трек, длительность известна",
            lambda: player.play_tracks([normal], start=0),
            lag(step_c),
        )

    def step_c() -> None:
        run_phase(
            "C_unknown_duration",
            "трек с duration_ms=None (D-06)",
            lambda: player.play_tracks([unknown], start=0),
            lag(step_end, 2000),
        )

    def step_end() -> None:
        QApplication.instance().quit()

    def lag(func, ms: int = 1200):
        """Отложить следующий шаг: дать плееру переключить трек."""
        def wrapper() -> None:
            QTimer.singleShot(ms, func)
        return wrapper

    QTimer.singleShot(1000, step_a)

    try:
        run_window(player)
    finally:
        hb.stop()
        harness.close()

    path = q.write_result("triage02_gui_timing", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"→ {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
