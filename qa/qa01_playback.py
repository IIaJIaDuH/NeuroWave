"""QA-01: реальное воспроизведение, транспорт, seek, автопереход.

Все проверки идут через настоящий mpv с настоящим аудиовыходом (HDMI sink
системы). В конце сценария Sink снимается монитором — сигнал на выходе
должен быть ненулевым, иначе mpv «играет в никуда».

Запуск: python qa/qa01_playback.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qaenv as q  # noqa: E402
from probe_audio import monitor_source, peak_dbfs  # noqa: E402

RATE = 48000
CHANNELS = 2


def wait_for(predicate, timeout: float = 5.0, step: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


def section_formats(harness) -> list[dict]:
    """Каждый файл библиотеки: старт, позиция, параметры звука, корректность."""
    out: list[dict] = []
    tracks = harness.adapter.tracks()
    for track in tracks:
        started = time.monotonic()
        harness.player.play_tracks([track], start=0)
        advanced = wait_for(lambda: harness.backend.position_ms() > 300, timeout=8.0)
        first_position = harness.backend.position_ms()
        state = harness.backend.snapshot_state()
        time.sleep(1.2)
        second_position = harness.backend.position_ms()
        out.append({
            "id": track.id,
            "title": track.title,
            "duration_ms": track.duration_ms,
            "play_latency_ms": round((time.monotonic() - started) * 1000),
            "state_playing": harness.player.state.status.value,
            "position_advanced": advanced,
            "position_first_ms": first_position,
            "position_after_1_2s_ms": second_position,
            "position_advances": second_position > first_position,
            "mpv": {
                "running": state.get("running"), "idle": state.get("idle"),
                "pause": state.get("pause"), "volume": state.get("volume"),
                "audio_device": state.get("audio_device"),
                "audio_params": state.get("audio_params"),
                "af": state.get("af"), "last_error": state.get("last_error"),
            },
        })
    return out


def section_transport(harness) -> dict:
    """Старт/пауза/продолжение/стоп/next/previous с замером задержек."""
    out: dict = {}
    tracks = harness.adapter.tracks()
    harness.player.play_tracks(tracks, start=0)
    wait_for(lambda: harness.backend.position_ms() > 300, timeout=8.0)

    t0 = time.monotonic()
    harness.player.toggle_pause()
    out["pause_latency_ms"] = round((time.monotonic() - t0) * 1000)
    out["state_after_pause"] = harness.player.state.status.value
    paused_at = harness.backend.position_ms()
    time.sleep(1.0)
    out["position_frozen_while_paused"] = abs(harness.backend.position_ms() - paused_at) < 250
    out["position_paused_ms"] = paused_at

    t0 = time.monotonic()
    harness.player.toggle_pause()
    out["resume_latency_ms"] = round((time.monotonic() - t0) * 1000)
    out["state_after_resume"] = harness.player.state.status.value
    wait_for(lambda: harness.backend.position_ms() > paused_at + 400, timeout=5.0)
    out["position_resumed_moved"] = harness.backend.position_ms() > paused_at + 300

    # Next / Previous
    before = harness.player.current_track.id
    t0 = time.monotonic()
    nxt = harness.player.next_track()
    out["next_latency_ms"] = round((time.monotonic() - t0) * 1000)
    out["next"] = {"before": before, "after": nxt.id if nxt else None,
                   "queue_position": harness.player.queue.position}
    wait_for(lambda: harness.backend.position_ms() > 200, timeout=8.0)
    t0 = time.monotonic()
    prev = harness.player.previous_track()
    out["previous_latency_ms"] = round((time.monotonic() - t0) * 1000)
    out["previous"] = {"after": prev.id if prev else None,
                       "queue_position": harness.player.queue.position}

    # Stop
    t0 = time.monotonic()
    harness.player.stop()
    out["stop_latency_ms"] = round((time.monotonic() - t0) * 1000)
    out["state_after_stop"] = harness.player.state.status.value
    out["position_after_stop_ms"] = harness.backend.position_ms()
    return out


def section_seek(harness) -> list[dict]:
    """Seek в начало, середину и последние секунды."""
    out: list[dict] = []
    track = harness.adapter.tracks()[0]
    harness.player.play_track(track)
    wait_for(lambda: harness.backend.position_ms() > 300, timeout=8.0)
    duration = track.duration_ms or 30000
    for label, target in [("start", 0), ("middle", duration // 2), ("near_end", duration - 2000)]:
        t0 = time.monotonic()
        harness.player.seek(target)
        latency = round((time.monotonic() - t0) * 1000)
        time.sleep(0.6)
        actual = harness.backend.position_ms()
        out.append({
            "target": label,
            "target_ms": target,
            "latency_ms": latency,
            "actual_ms": actual,
            "delta_ms": actual - target,
            "within_1s": abs(actual - target) < 1000,
            "still_playing": harness.player.state.status.value,
        })
    return out


def section_auto_transition(harness) -> dict:
    """Конец трека → автоматический переход, ровно один переход за eof."""
    short = [t for t in harness.adapter.tracks() if (t.duration_ms or 0) < 5000]
    other = [t for t in harness.adapter.tracks() if (t.duration_ms or 0) >= 5000]
    if not short:
        return {"skipped": "нет короткого трека"}
    harness.player.play_tracks(short + other, start=0)
    before = harness.recorder.count("track")
    t0 = time.monotonic()
    switched = wait_for(
        lambda: harness.recorder.count("track") > before, timeout=short[0].duration_ms / 1000 + 12
    )
    out = {
        "short_track": short[0].id,
        "switched": switched,
        "switch_latency_ms": round((time.monotonic() - t0) * 1000),
        "track_events_total": harness.recorder.count("track"),
        "current": harness.player.current_track.id if harness.player.current_track else None,
        "state": harness.player.state.status.value,
    }
    # Ровно один переход: событий track должно быть на один больше, чем до конца трека
    out["single_transition"] = harness.recorder.count("track") == before + 1
    return out


def section_queue_end(harness) -> dict:
    """Поведение после последнего трека в очереди (repeat=off)."""
    tracks = harness.adapter.tracks()
    harness.player.play_tracks(tracks, start=len(tracks) - 1)
    wait_for(lambda: harness.backend.position_ms() > 200, timeout=8.0)
    finished_before = harness.recorder.count("finished")
    finished = wait_for(lambda: harness.recorder.count("finished") > finished_before, timeout=45)
    return {
        "finished_event": finished,
        "state": harness.player.state.status.value,
        "current": harness.player.current_track.id if harness.player.current_track else None,
        "position_ms": harness.backend.position_ms(),
        "mpv_running": harness.backend.is_running,
    }


def main() -> int:
    q.ensure_dirs()
    result: dict = {"scenario": "QA-01 воспроизведение"}
    sink = q.run(["pactl", "get-default-sink"])[1].strip()
    source = monitor_source()
    raw_path = q.ARTIFACTS / "qa01_sink.raw"
    record = subprocess.Popen(
        ["parec", "--device", source, "--format=s16le", "--rate", str(RATE),
         "--channels", str(CHANNELS), "--raw"],
        stdout=raw_path.open("wb"), stderr=subprocess.DEVNULL,
    )
    time.sleep(1.0)
    harness = q.build_local_player("qa01_playback", repeat="off", volume=30)
    try:
        result["sink"] = sink
        result["formats"] = section_formats(harness)
        result["transport"] = section_transport(harness)
        result["seek"] = section_seek(harness)
        result["auto_transition"] = section_auto_transition(harness)
        result["queue_end"] = section_queue_end(harness)
        result["events"] = harness.recorder.as_list()
        result["error_events"] = [
            e for e in harness.recorder.as_list() if e["event"] in {"error", "failed"}
        ]
    finally:
        harness.close()
        time.sleep(0.6)
        record.terminate()
        try:
            record.wait(timeout=5)
        except subprocess.TimeoutExpired:
            record.kill()
    raw = raw_path.read_bytes()
    db_peak, db_rms, frames = peak_dbfs(raw)
    result["sink_capture"] = {
        "frames": frames, "peak_dbfs": round(db_peak, 2), "rms_dbfs": round(db_rms, 2),
        "silent": db_peak < -80,
    }
    raw_path.unlink(missing_ok=True)
    path = q.write_result("qa01_playback", result)
    print(json.dumps({k: v for k, v in result.items() if k != "events"},
                     ensure_ascii=False, indent=2)[:14000])
    print(f"→ {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
