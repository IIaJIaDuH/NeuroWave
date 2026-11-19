"""QA-06: длительная сессия с замером ресурсов.

Сессия идёт на реальном аудиовыходе и в фоне снимает ресурсы приложения и
mpv раз в 5 секунд: CPU, RSS, PSS, VmSwap, SwapPss, дескрипторы, потоки,
число процессов mpv. Параллельно выполняются реалистичные действия
(переключение трека, пауза, перемотка, смена громкости и DSP) — их отклик
тоже замеряется.

Длительность задаётся аргументом (по умолчанию 40 минут).

Запуск: python qa/qa06_session.py [минуты]
"""

from __future__ import annotations

import json
import random
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qaenv as q  # noqa: E402


def summarise(rows: list[dict], prefix: str) -> dict:
    if not rows:
        return {}
    out: dict = {}
    for key in (f"{prefix}_rss_kb", f"{prefix}_pss_kb", f"{prefix}_swap_kb",
                f"{prefix}_swap_pss_kb", f"{prefix}_fds", f"{prefix}_threads",
                f"{prefix}_cpu_ticks"):
        values = [float(row.get(key, 0) or 0) for row in rows]
        if not values or all(v == 0 for v in values):
            continue
        out[key.replace(f"{prefix}_", "")] = {
            "min": round(min(values), 1), "max": round(max(values), 1),
            "avg": round(statistics.fmean(values), 1),
            "first": round(values[0], 1), "last": round(values[-1], 1),
            "delta_last_minus_first": round(values[-1] - values[0], 1),
        }
    return out


def main() -> int:
    minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 40.0
    q.ensure_dirs()
    result: dict = {"scenario": "QA-06 длинная сессия", "minutes": minutes}
    csv_path = q.ARTIFACTS / "qa06_session_resources.csv"
    events_path = q.ARTIFACTS / "qa06_session_events.jsonl"

    harness = q.build_local_player("qa06_session", repeat="all", volume=30)
    recorder = q.EventRecorder(events_path)
    harness.player.add_listener(recorder)
    sampler = q.ResourceSampler(
        csv_path, interval=5.0,
        mpv_pid=lambda: harness.backend._process.pid if harness.backend._process else 0,
        label="qa06",
    )
    latencies: dict[str, list[float]] = {"next": [], "pause": [], "seek": [], "volume": [], "dsp": []}
    errors: list[str] = []
    rng = random.Random(20261118)
    started = time.monotonic()
    try:
        with sampler:
            t0 = time.monotonic()
            harness.player.play_tracks(harness.adapter.tracks(), start=0)
            first_position = 0
            while first_position <= 0:
                first_position = harness.backend.position_ms()
                time.sleep(0.05)
            result["startup_to_first_audio_ms"] = round((time.monotonic() - t0) * 1000)
            deadline = started + minutes * 60
            actions = 0
            while time.monotonic() < deadline:
                time.sleep(rng.uniform(8.0, 25.0))
                action = rng.choice(["next", "pause", "seek", "volume", "dsp"])
                try:
                    t = time.monotonic()
                    if action == "next":
                        harness.player.next_track()
                    elif action == "pause":
                        harness.player.toggle_pause()
                    elif action == "seek":
                        harness.player.seek(rng.randint(0, 20000))
                    elif action == "volume":
                        harness.player.set_volume(rng.choice([20, 30, 45, 60]))
                    else:
                        harness.player.set_dsp_mode(rng.choice(["pure", "enhanced"]))
                    latencies[action].append(round((time.monotonic() - t) * 1000, 2))
                    actions += 1
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{action}: {type(exc).__name__}: {exc}")
        result["actions"] = actions
        result["action_errors"] = errors
        result["latency_ms"] = {
            key: {"count": len(values),
                  "max": max(values) if values else None,
                  "avg": round(statistics.fmean(values), 2) if values else None}
            for key, values in latencies.items()
        }
        result["events_total"] = len(recorder.as_list())
        result["events_by_type"] = {
            kind: sum(1 for e in recorder.as_list() if e["event"] == kind)
            for kind in sorted({e["event"] for e in recorder.as_list()})
        }
    finally:
        harness.close()
        time.sleep(1.0)
    rows = list(sampler._rows)
    result["resources"] = {"samples": len(rows), "py": summarise(rows, "py"),
                            "mpv": summarise(rows, "mpv")}
    result["after_session"] = {
        "mpv_processes": q.mpv_processes(),
        "stale_ipc_sockets": q.stale_ipc_sockets(),
    }
    result["swap_pressure"] = {
        "system_swap_used_kb": int(subprocess.run(["free", "-k"], capture_output=True, text=True)
                                  .stdout.splitlines()[2].split()[2]),
        "note": "RSS/PSS без учёта свопа искажаются при нехватке памяти",
    }
    path = q.write_result("qa06_session", result)
    print(json.dumps(result, ensure_ascii=False, indent=2)[:8000])
    print(f"→ {path} · CSV: {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
