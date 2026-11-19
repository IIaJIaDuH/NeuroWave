"""QA-02: стресс управления плеером.

Серии: 100 переключений Next/Previous, 50 seek, 30 циклов Pause/Resume,
переключение Pure/Enhanced, смена пресетов Sound Lab, серия «быстрых
нажатий», поведение очереди после последнего трека.

Параллельно снимаются признаки аварии: двойные end-file, рассинхрон
состояния плеера и mpv, осиротевшие процессы mpv, оставшиеся IPC-сокеты,
пропуск/дублирование треков, ошибки.

Запуск: python qa/qa02_stress.py
"""

from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qaenv as q  # noqa: E402

RNG = random.Random(20261118)


def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.03)
    return False


def state_snapshot(harness) -> dict:
    mpv = harness.backend.snapshot_state()
    return {
        "player_status": harness.player.state.status.value,
        "player_track": harness.player.current_track.id if harness.player.current_track else None,
        "queue_position": harness.player.queue.position,
        "queue_size": len(harness.player.queue),
        "mpv_pause": mpv.get("pause"),
        "mpv_idle": mpv.get("idle"),
        "mpv_position_ms": harness.backend.position_ms(),
        "mpv_af": mpv.get("af"),
        "mpv_running": harness.backend.is_running,
    }


def phase_next_previous(harness) -> dict:
    """100 переключений Next/Previous."""
    latencies: list[int] = []
    errors: list[str] = []
    mismatch: list[dict] = []
    steps = 0
    for index in range(100):
        try:
            t0 = time.monotonic()
            if index % 2 == 0:
                harness.player.next_track()
            else:
                harness.player.previous_track()
            latencies.append(round((time.monotonic() - t0) * 1000, 2))
            steps += 1
        except Exception as exc:  # noqa: BLE001 — в QA важна причина
            errors.append(f"{type(exc).__name__}: {exc}")
        snap = state_snapshot(harness)
        if snap["player_status"] not in {"playing", "buffering", "paused", "ready", "stopped"}:
            mismatch.append(snap)
    return {
        "steps": steps, "errors": errors, "invalid_states": mismatch,
        "latency_ms_max": max(latencies) if latencies else None,
        "latency_ms_avg": round(sum(latencies) / len(latencies), 2) if latencies else None,
        "latency_ms_p95": round(sorted(latencies)[int(len(latencies) * 0.95) - 1], 2) if latencies else None,
        "final": state_snapshot(harness),
    }


def phase_seeks(harness) -> dict:
    """50 seek на разные позиции."""
    latencies: list[float] = []
    deltas: list[int] = []
    errors: list[str] = []
    state_broken = 0
    for _ in range(50):
        duration = harness.player.current_track.duration_ms or 30000
        target = RNG.randint(0, max(0, duration - 500))
        try:
            t0 = time.monotonic()
            harness.player.seek(target)
            latencies.append(round((time.monotonic() - t0) * 1000, 2))
            actual = harness.backend.position_ms()
            deltas.append(abs(actual - target))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")
            continue
        if harness.player.state.status.value not in {"playing", "buffering", "paused"}:
            state_broken += 1
    deltas.sort()
    return {
        "count": len(latencies), "errors": errors, "invalid_state_count": state_broken,
        "latency_ms_max": max(latencies) if latencies else None,
        "offset_ms_median": deltas[len(deltas) // 2] if deltas else None,
        "offset_ms_p95": deltas[int(len(deltas) * 0.95) - 1] if deltas else None,
        "offset_ms_max": deltas[-1] if deltas else None,
    }


def phase_pause_resume(harness) -> dict:
    """30 циклов Pause/Resume."""
    errors: list[str] = []
    frozen_ok = 0
    resumed_ok = 0
    latencies: list[float] = []
    for _ in range(30):
        try:
            t0 = time.monotonic()
            harness.player.toggle_pause()
            latencies.append(round((time.monotonic() - t0) * 1000, 2))
            if harness.player.state.status.value != "paused":
                errors.append(f"ожидалось paused, получено {harness.player.state.status.value}")
            before = harness.backend.position_ms()
            time.sleep(0.35)
            after = harness.backend.position_ms()
            if abs(after - before) < 250:
                frozen_ok += 1
            t0 = time.monotonic()
            harness.player.toggle_pause()
            latencies.append(round((time.monotonic() - t0) * 1000, 2))
            if harness.player.state.status.value != "playing":
                errors.append(f"ожидалось playing, получено {harness.player.state.status.value}")
            if wait_for(lambda: harness.backend.position_ms() > before + 200, timeout=3.0):
                resumed_ok += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")
    return {
        "cycles": 30, "errors": errors, "pause_frozen_ok": frozen_ok, "resume_moved_ok": resumed_ok,
        "latency_ms_max": max(latencies) if latencies else None,
    }


def phase_dsp_modes(harness) -> dict:
    """Переключение Pure/Enhanced во время воспроизведения."""
    results: list[dict] = []
    errors: list[str] = []
    for index in range(10):
        mode = "pure" if index % 2 == 0 else "enhanced"
        try:
            t0 = time.monotonic()
            status = harness.player.set_dsp_mode(mode)
            results.append({
                "requested": mode,
                "mode": status.mode.value,
                "confirmed": status.confirmed,
                "fallback": status.fallback,
                "reason": status.reason,
                "graph_len": len(status.graph or ""),
                "latency_ms": round((time.monotonic() - t0) * 1000, 2),
                "mpv_af": harness.backend.audio_filter(),
                "still_playing": harness.player.state.status.value,
                "position_ms": harness.backend.position_ms(),
            })
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")
    return {"attempts": results, "errors": errors}


def phase_presets(harness) -> dict:
    """Смена встроенных и пользовательских пресетов Sound Lab."""
    results: list[dict] = []
    errors: list[str] = []
    profiles = harness.player.available_profiles()
    for profile in profiles:
        try:
            t0 = time.monotonic()
            status = harness.player.set_dsp_profile(profile.id)
            results.append({
                "id": profile.id, "name": profile.name,
                "mode": status.mode.value, "confirmed": status.confirmed,
                "fallback": status.fallback, "reason": status.reason,
                "graph": (status.graph or "")[:160],
                "latency_ms": round((time.monotonic() - t0) * 1000, 2),
            })
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{profile.id}: {type(exc).__name__}: {exc}")
    return {
        "available": [p.id for p in profiles],
        "results": results, "errors": errors,
    }


def phase_rapid_burst(harness) -> dict:
    """50 действий подряд без пауз: имитация быстрых нажатий."""
    errors: list[str] = []
    t0 = time.monotonic()
    for index in range(50):
        action = index % 4
        try:
            if action == 0:
                harness.player.next_track()
            elif action == 1:
                harness.player.toggle_pause()
            elif action == 2:
                harness.player.seek(RNG.randint(0, 20000))
            else:
                harness.player.previous_track()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{index}/{action}: {type(exc).__name__}: {exc}")
    elapsed = round((time.monotonic() - t0) * 1000, 2)
    return {
        "actions": 50, "errors": errors, "total_ms": elapsed,
        "final": state_snapshot(harness),
    }


def phase_queue_after_last(harness) -> dict:
    """Очередь после последнего трека."""
    tracks = harness.adapter.tracks()
    harness.player.play_tracks(tracks, start=len(tracks) - 1)
    wait_for(lambda: harness.backend.position_ms() > 100, timeout=8.0)
    before = harness.recorder.count("finished")
    finished = wait_for(lambda: harness.recorder.count("finished") > before, timeout=45)
    after_end_next = None
    try:
        result = harness.player.next_track()
        after_end_next = result.id if result else None
    except Exception as exc:  # noqa: BLE001
        after_end_next = f"{type(exc).__name__}: {exc}"
    return {
        "finished_event": finished,
        "state": harness.player.state.status.value,
        "next_after_end": after_end_next,
        "queue_position": harness.player.queue.position,
    }


def analyse(harness, result: dict) -> dict:
    """Сквозные проверки: дефекты, осиротевшие процессы, сокеты, события."""
    events = harness.recorder.as_list()
    track_events = [e for e in events if e["event"] == "track"]
    ids = [e["payload"].get("track", {}).get("id") for e in track_events]
    duplicates = [i for i in set(ids) if ids.count(i) > 1]
    errors = [e for e in events if e["event"] in {"error", "failed"}]
    result["analyse"] = {
        "events_total": len(events),
        "track_events": len(track_events),
        "distinct_tracks_played": len(set(ids)),
        "duplicated_track_events": len(duplicates),
        "error_events": errors,
        "mpv_processes_now": q.mpv_processes(),
        "stale_ipc_sockets": q.stale_ipc_sockets(),
    }
    return result


def main() -> int:
    q.ensure_dirs()
    result: dict = {"scenario": "QA-02 стресс управления"}
    harness = q.build_local_player("qa02_stress", repeat="off", volume=30)
    sampler = q.ResourceSampler(
        q.ARTIFACTS / "qa02_stress_resources.csv", interval=5.0,
        mpv_pid=lambda: harness.backend._process.pid if harness.backend._process else 0,
        label="qa02",
    )
    try:
        with sampler:
            harness.player.play_tracks(harness.adapter.tracks(), start=0)
            wait_for(lambda: harness.backend.position_ms() > 200, timeout=8.0)
            result["next_previous"] = phase_next_previous(harness)
            result["seeks"] = phase_seeks(harness)
            result["pause_resume"] = phase_pause_resume(harness)
            result["dsp_modes"] = phase_dsp_modes(harness)
            result["presets"] = phase_presets(harness)
            result["rapid_burst"] = phase_rapid_burst(harness)
            result["queue_after_last"] = phase_queue_after_last(harness)
            result["final_state"] = state_snapshot(harness)
        result = analyse(harness, result)
    finally:
        harness.close()
        time.sleep(1.0)
        result["after_close"] = {
            "mpv_processes": q.mpv_processes(),
            "stale_ipc_sockets": q.stale_ipc_sockets(),
        }
    path = q.write_result("qa02_stress", result)
    print(json.dumps(result, ensure_ascii=False, indent=2)[:15000])
    print(f"→ {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
