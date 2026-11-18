"""Характеризация дефекта: seek отклоняется mpv.

Наблюдение из QA-02: 50 seek подряд и 12 seek в серии «быстрых нажатий»
завершились ошибкой `mpv отклонил seek: error running command`, тогда как
seek после простоя (QA-01) проходил.

Гипотеза: mpv отвечает ошибкой, если seek приходит, пока файл ещё грузится
(mpv в состоянии idle/busy). Проверяем точно: когда seek работает, а когда нет.

Запуск: python qa/defect_seek_after_switch.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qaenv as q  # noqa: E402


def attempt(label: str, fn) -> dict:
    started = time.monotonic()
    try:
        fn()
        return {"case": label, "ok": True, "ms": round((time.monotonic() - started) * 1000, 2)}
    except Exception as exc:  # noqa: BLE001
        return {"case": label, "ok": False, "ms": round((time.monotonic() - started) * 1000, 2),
                "error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    q.ensure_dirs()
    harness = q.build_local_player("qa_defect_seek", repeat="off", volume=30)
    result: dict = {"scenario": "дефект seek", "cases": []}
    try:
        tracks = harness.adapter.tracks()
        harness.player.play_tracks(tracks, start=0)
        time.sleep(2.0)
        result["cases"].append(attempt("seek на прогретом треке", lambda: harness.player.seek(5000)))

        # сразу после переключения трека, без ожидания
        harness.player.next_track()
        result["cases"].append(attempt("seek сразу после next (0 мс)", lambda: harness.player.seek(5000)))
        result["cases"].append(attempt("seek сразу после next (повтор)", lambda: harness.player.seek(8000)))
        time.sleep(1.5)
        result["cases"].append(attempt("seek через 1.5 с после next", lambda: harness.player.seek(5000)))

        # серия seek подряд во время игры
        for index in range(5):
            result["cases"].append(
                attempt(f"seek #{index} во время воспроизведения", lambda: harness.player.seek(3000 + index * 1000))
            )

        # seek во время паузы
        harness.player.toggle_pause()
        time.sleep(0.3)
        result["cases"].append(attempt("seek во время паузы", lambda: harness.player.seek(10000)))
        harness.player.toggle_pause()

        # seek после остановки
        harness.player.stop()
        time.sleep(0.3)
        result["cases"].append(attempt("seek после stop", lambda: harness.player.seek(2000)))

        # снова старт и seek после возобновления
        harness.player.play_tracks(tracks, start=0)
        time.sleep(1.5)
        result["cases"].append(attempt("seek после повторного play", lambda: harness.player.seek(4000)))

        result["final_state"] = {
            "status": harness.player.state.status.value,
            "position_ms": harness.backend.position_ms(),
            "mpv_idle": harness.backend.snapshot_state().get("idle"),
            "mpv_pause": harness.backend.snapshot_state().get("pause"),
        }
        result["errors"] = [c for c in result["cases"] if not c["ok"]]
    finally:
        harness.close()
    path = q.write_result("defect_seek", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"→ {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
