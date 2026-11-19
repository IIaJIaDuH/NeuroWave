"""QA-TRIAGE-02 / D-06: неизвестная длительность композиции в реальном окне.

Сценарий: длительность трека неизвестна (`duration_ms=None`) — так бывает у
провайдеров, которые не отдают длительность. Настоящее окно Qt на живом
экране (не offscreen), настоящий mpv, синтетические аудиофайлы.

Приложение не изменяется: используется штатная точка входа
`neurowave.ui.app.run_window`, длительность подменяется на уровне данных —
трек с той же длительностью приходит из провайдера «как есть».

Стенд не блокирует GUI-поток: все ожидания сделаны на таймерах, иначе
Qt-цикл не может отвечать и замер «отзывчивости» был бы артефактом стенда.

Запуск: python qa/triage02_gui_duration.py
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qaenv as q  # noqa: E402

MEDIA = Path(os.environ.get("QA_MEDIA") or (q.SHARED / "media-verify"))
SHOTS = q.ROOT / "docs" / "qa" / "screenshots"
LOG = q.ROOT / "docs" / "qa" / "logs"
LONG_MS = 6 * 60 * 60 * 1000  # шесть часов
BEAT_MS = 150


class ExceptionCounter:
    """Считает исключения, дошедшие до sys.excepthook (слоты Qt)."""

    def __init__(self) -> None:
        self.items: list[dict] = []
        self._previous = None

    def install(self) -> None:
        self._previous = sys.excepthook
        sys.excepthook = self._hook

    def restore(self) -> None:
        if self._previous is not None:
            sys.excepthook = self._previous

    def _hook(self, exc_type, exc_value, exc_tb) -> None:
        self.items.append({
            "at": round(time.monotonic(), 3),
            "type": exc_type.__name__,
            "message": str(exc_value)[:200],
            "location": "".join(traceback.format_exception(exc_type, exc_value, exc_tb))[-600:],
        })
        if self._previous is not None:
            self._previous(exc_type, exc_value, exc_tb)

    def count(self) -> int:
        return len(self.items)

    def since(self, mark: int) -> int:
        return len(self.items) - mark

    def types(self, mark: int) -> dict:
        counts: dict[str, int] = {}
        for item in self.items[mark:]:
            counts[item["type"]] = counts.get(item["type"], 0) + 1
        return counts


def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    LOG.mkdir(parents=True, exist_ok=True)
    os.environ["PATH"] = f"{q.MPV.parent}:{os.environ['PATH']}"

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QSlider

    from neurowave.ui.app import run_window

    harness = q.build_local_player("triage02_gui", repeat="all", volume=30, media_dir=MEDIA)
    player = harness.player
    tracks = sorted(harness.adapter.tracks(), key=lambda t: -(t.duration_ms or 0))
    normal = tracks[0]
    second = tracks[1] if len(tracks) > 1 else normal
    unknown = dataclasses.replace(normal, duration_ms=None)
    zero = dataclasses.replace(normal, duration_ms=0)
    very_long = dataclasses.replace(normal, duration_ms=LONG_MS)

    counter = ExceptionCounter()
    counter.install()

    beats = {"n": 0}
    steps: list[dict] = []
    state: dict = {"mark": 0, "t0": time.monotonic(), "prev_beat": 0, "prev_t": 0.0}

    def window():
        for widget in QApplication.instance().topLevelWidgets():
            if widget.__class__.__name__ == "QMainWindow":
                return widget
        return None

    def labels() -> dict:
        """Состояние, которое видит пользователь: время, кнопки, ползунок, статус."""
        win = window()
        if win is None:
            return {}
        found: dict = {}
        texts = [str(label.text()) for label in win.findChildren(QLabel)]
        time_texts = [t for t in texts if " / " in t]
        if time_texts:
            found["time"] = time_texts[0]
        found["buttons"] = [b.text() for b in win.findChildren(QPushButton)]
        sliders = win.findChildren(QSlider)
        found["seek_value"] = sliders[0].value() if sliders else None
        for label in win.findChildren(QLabel):
            if "8b97a8" in (label.styleSheet() or ""):
                found["status"] = label.text()
                break
        return found

    def snapshot(name: str, note: str) -> dict:
        win = window()
        now = time.monotonic()
        beats_delta = beats["n"] - state["prev_beat"]
        elapsed = now - state["prev_t"] if state["prev_t"] else 0.0
        entry = {
            "step": name,
            "note": note,
            "at": round(now - state["t0"], 2),
            "exceptions_total": counter.count(),
            "exceptions_new": counter.since(state["mark"]),
            "exception_types": counter.types(state["mark"]),
            "last_exception": counter.items[-1] if counter.items else None,
            "qt_beats": beats_delta,
            "qt_beats_per_second": round(beats_delta / elapsed, 2) if elapsed > 0.2 else None,
            "player_status": getattr(player.state.status, "value", str(player.state.status)),
            "position_ms": player.backend.position_ms(),
            "current_track_id": getattr(player.current_track, "id", None),
            "current_duration_ms": getattr(player.current_track, "duration_ms", None),
            "labels": labels(),
            "window_title": win.windowTitle() if win else None,
        }
        steps.append(entry)
        state["prev_beat"] = beats["n"]
        state["prev_t"] = now
        state["mark"] = counter.count()
        return entry

    def wait_until(cond, then, timeout: float = 8.0, interval: int = 100) -> None:
        """Ожидание без блокировки GUI-потока: проверка по таймеру."""
        deadline = time.monotonic() + timeout

        def check() -> None:
            if cond() or time.monotonic() > deadline:
                then()
            else:
                QTimer.singleShot(interval, check)

        QTimer.singleShot(interval, check)

    def shot(name: str) -> dict:
        """Снимок только окна приложения (без лишних областей экрана)."""
        win = window()
        if win is None:
            return {"file": None}
        window_id = int(win.winId())
        code, out, _ = q.run(["xwininfo", "-id", str(window_id)], timeout=20)
        geom = {}
        for line in out.splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            key, value = key.strip(), value.strip()
            mapping = {
                "Width": "width", "Height": "height",
                "Absolute upper-left X": "x", "Absolute upper-left Y": "y",
            }
            if key in mapping and value.isdigit():
                geom[mapping[key]] = int(value)
        target = SHOTS / name
        if len(geom) == 4:
            subprocess.run(["wmctrl", "-i", "-a", str(window_id)], timeout=20)
            QTimer.singleShot(700, lambda: None)
            code, _, stderr = q.run([
                "ffmpeg", "-y", "-v", "error", "-f", "x11grab",
                "-video_size", "1920x1080", "-i", os.environ.get("DISPLAY", ":0"),
                "-vf", f"crop={geom['width']}:{geom['height']}:{geom['x']}:{geom['y']}",
                "-frames:v", "1", str(target),
            ], timeout=60)
            return {"file": target.name, "geometry": geom, "code": code,
                    "bytes": target.stat().st_size if target.exists() else 0}
        return {"file": None, "reason": "нет геометрии окна", "code": code}

    # --- лестница сценариев (только таймеры, без блокировок) -----------
    def step_empty_queue() -> None:
        snapshot("S1_empty_queue", "очередь пуста: длительности нет вовсе")
        QTimer.singleShot(1500, step_play_normal)

    def step_play_normal() -> None:
        player.play_tracks([normal], start=0)
        wait_until(
            lambda: getattr(player.current_track, "duration_ms", None) == normal.duration_ms,
            lambda: QTimer.singleShot(800, lambda: (
                snapshot("S2_normal_duration", "обычный трек, длительность известна"),
                step_switch_unknown(),
            )),
        )

    def step_switch_unknown() -> None:
        player.play_tracks([normal, unknown], start=1)
        wait_until(
            lambda: getattr(player.current_track, "duration_ms", None) is None,
            lambda: QTimer.singleShot(800, lambda: (
                snapshot("S3_unknown_duration", "переключились на трек с duration_ms=None"),
                step_pause_in_error_state(),
            )),
        )

    def step_pause_in_error_state() -> None:
        before = labels()
        player.toggle_pause()
        QTimer.singleShot(1200, lambda: (
            step_pause_snapshot(before),
            step_resume_and_recover(),
        ))

    def step_pause_snapshot(before: dict) -> None:
        entry = snapshot("S4_pause_in_error_state", "пауза при необработанной ошибке")
        entry["labels_before_pause"] = before
        entry["gui_changed_after_pause"] = before != entry["labels"]
        entry["screenshot"] = shot("triage02_d06_error_state.png")

    def step_resume_and_recover() -> None:
        player.toggle_pause()
        QTimer.singleShot(1000, lambda: (
            snapshot("S5_resume_in_error_state", "возобновление при той же ошибке"),
            step_back_to_normal(),
        ))

    def step_back_to_normal() -> None:
        player.play_tracks([normal], start=0)
        wait_until(
            lambda: getattr(player.current_track, "duration_ms", None) == normal.duration_ms,
            lambda: QTimer.singleShot(1000, lambda: (
                snapshot("S6_back_to_normal", "вернулись к треку с известной длительностью"),
                step_zero_duration(),
            )),
        )

    def step_zero_duration() -> None:
        player.play_tracks([zero], start=0)
        wait_until(
            lambda: getattr(player.current_track, "duration_ms", None) == 0,
            lambda: QTimer.singleShot(800, lambda: (
                snapshot("S7_zero_duration", "duration_ms = 0"),
                step_very_long(),
            )),
        )

    def step_very_long() -> None:
        player.play_tracks([very_long], start=0)
        wait_until(
            lambda: getattr(player.current_track, "duration_ms", None) == LONG_MS,
            lambda: QTimer.singleShot(800, lambda: (
                step_very_long_snapshot(),
                step_flood_measure(),
            )),
        )

    def step_very_long_snapshot() -> None:
        entry = snapshot("S8_very_long_track", "трек на 6 часов")
        entry["screenshot"] = shot("triage02_d06_very_long.png")

    def step_flood_measure() -> None:
        player.play_tracks([unknown], start=0)
        wait_until(
            lambda: getattr(player.current_track, "duration_ms", None) is None,
            lambda: QTimer.singleShot(300, lambda: (
                step_flood_measure_finish(),
                None,
            )),
        )

    def step_flood_measure_finish() -> None:
        entry = snapshot("S9_exception_flood_5s", "5 секунд с треком без длительности")
        entry["exceptions_per_second"] = round(entry["exceptions_new"] / 5.0, 2)
        QTimer.singleShot(5000, step_finish)

    def step_finish() -> None:
        player.play_tracks([second], start=0)
        QTimer.singleShot(1000, lambda: (
            snapshot("S10_final", "финальное состояние: обычный трек"),
            QApplication.instance().quit(),
        ))

    app = QApplication.instance() or QApplication(sys.argv)
    hb = QTimer()
    hb.timeout.connect(lambda: beats.__setitem__("n", beats["n"] + 1))
    hb.start(BEAT_MS)

    QTimer.singleShot(800, step_empty_queue)

    try:
        run_window(player)
    finally:
        hb.stop()
        counter.restore()
        harness.close()

    payload = {
        "scenario": "QA-TRIAGE-02 / D-06: неизвестная длительность в GUI",
        "sha": "7a82119d23775f7ed02db14e232dcefbc8ade3d9",
        "display": os.environ.get("DISPLAY", ":0"),
        "qt_platform": os.environ.get("QT_QPA_PLATFORM", "(по умолчанию xcb)"),
        "beat_interval_ms": BEAT_MS,
        "exceptions_total": counter.count(),
        "exception_types": counter.types(0),
        "exception_samples": counter.items[:3],
        "steps": steps,
    }
    path = q.write_result("triage02_gui_duration", payload)
    (LOG / "triage02_gui_duration_exceptions.log").write_text(
        "\n".join(item["location"] for item in counter.items), encoding="utf-8"
    )
    print(json.dumps({
        "exceptions_total": counter.count(),
        "exception_types": counter.types(0),
        "steps": [
            {
                "step": s["step"],
                "exceptions_new": s["exceptions_new"],
                "qt_beats_per_second": s["qt_beats_per_second"],
                "time_label": s["labels"].get("time"),
                "buttons": s["labels"].get("buttons"),
                "seek_value": s["labels"].get("seek_value"),
                "status": s["player_status"],
                "duration_ms": s["current_duration_ms"],
            }
            for s in steps
        ],
    }, ensure_ascii=False, indent=2))
    print(f"→ {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
