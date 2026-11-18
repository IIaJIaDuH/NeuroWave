"""QA-VERIFY-01: проверка исправления D-01 (seek после переключения трека).

Проверяемая ветка: fix/qa-seek-readiness-01, SHA e0aa7a3580f1cda9f5d0f601451fa6dfcadbbc71.
Сценарии и библиотека — из PR #32 (qa/make_media.py, qa/defect_seek_after_switch.py).

Как измеряется «seek применился»:
  * позиция опрашивается каждые 5 мс; применение seek — это разрыв позиции
    (естественное воспроизведение растёт на 5 мс за 5 мс, скачок — сотни мс);
  * «фактическая позиция с учётом прошедшего времени» = позиция − время,
    прошедшее с момента вызова seek; она должна совпасть с запросом;
  * какой файл реально играет, спрашивается у самого mpv по его IPC-сокету
    (свойства filename и playlist-entry-id) — это независимая проверка,
    что seek не уехал на чужой трек.

Сценарии:
  A. 50 × Next → немедленный Seek (без задержки между командами)
  B. 50 × Previous → немедленный Seek
  C. Пять быстрых Seek подряд во время загрузки — должен выиграть последний
  D. Stop, затем Seek
  E. Закрытие GUI во время загрузки
  F. Переход на другой файл с ещё ожидающей перемоткой (seek не должен уехать)
  G. Двойные end-file, утечки процессов mpv и IPC-сокетов

Запуск: python qa/verify_d01.py
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qaenv as q  # noqa: E402

MEDIA = Path("/home/zoz/.deepai-code/scratch/2026-11-18-wo03/media-verify")
BASE_SHA = "e0aa7a3580f1cda9f5d0f601451fa6dfcadbbc71"
POSITION_TOLERANCE_MS = 1000  # критерий приёмки из ТЗ
JUMP_THRESHOLD_MS = 400       # разрыв позиции, за которым виден seek


class MpvProbe:
    """Прямой клиент IPC mpv: спрашиваем, какой файл реально играет."""

    def __init__(self) -> None:
        self.path = Path("/tmp") / f"neurowave-mpv-{os.getpid()}.sock"
        self._request_id = 0
        self._buffer = b""

    def _connect(self) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(1.0)
        sock.connect(str(self.path))
        return sock

    def get(self, prop: str) -> Any:
        """Значение свойства mpv (None, если свойства нет)."""
        try:
            sock = self._connect()
        except OSError:
            return None
        try:
            self._request_id += 1
            request_id = self._request_id
            # IPC mpv построчный: команда — JSON-объект с переводом строки.
            sock.sendall(
                (json.dumps({"command": ["get_property", prop], "request_id": request_id}) + "\n").encode()
            )
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                try:
                    chunk = sock.recv(65536)
                except socket.timeout:
                    break
                if not chunk:
                    break
                self._buffer += chunk
                while b"\n" in self._buffer:
                    line, _, self._buffer = self._buffer.partition(b"\n")
                    text = line.decode("utf-8", "replace").strip()
                    if not text:
                        continue
                    try:
                        message = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    if message.get("request_id") == request_id:
                        return message.get("data")
            return None
        finally:
            sock.close()

    def filename(self) -> str | None:
        value = self.get("filename")
        return value if isinstance(value, str) else None


def now() -> float:
    return time.monotonic()


def current_id(harness) -> str | None:
    track = harness.player.current_track
    return track.id if track else None


def expected_path(track) -> str:
    """Имя файла, как его возвращает mpv (basename)."""
    return Path(track.id).name


def target_window(track) -> tuple[int, float, float]:
    """Цель, порог и окно ожидания для трека.

    Порог = target − 1000 мс; окно таково, что естественное воспроизведение
    до порога не доходит.
    """
    duration = track.duration_ms or 30000
    target = max(1500, int(duration * 0.6))
    threshold = max(0, target - POSITION_TOLERANCE_MS)
    window = min(3.0, max(0.5, (threshold / 1000.0) * 0.9))
    return target, threshold, window


def observe_seek(harness, probe: MpvProbe, target: int, window: float) -> dict:
    """Ждём применения seek и возвращаем измерения.

    Возвращает: applied, applied_position_ms, offset_ms (позиция минус
    прошедшее время), residual_ms (offset минус цель), latency_ms, filename.
    """
    start = now()
    previous = float(harness.backend.position_ms())
    applied = False
    applied_position: float | None = None
    offset: float | None = None
    latency: float | None = None
    file_at_apply: str | None = None
    jumps: list[dict] = []
    while now() - start < window:
        position = float(harness.backend.position_ms())
        elapsed = (now() - start) * 1000.0
        delta = position - previous
        if abs(delta) >= JUMP_THRESHOLD_MS:
            candidate_offset = position - elapsed
            jumps.append({
                "at_ms": round(elapsed), "position_ms": position,
                "delta_ms": round(delta), "offset_ms": round(candidate_offset),
            })
            if abs(candidate_offset - target) <= POSITION_TOLERANCE_MS:
                applied = True
                applied_position = position
                offset = candidate_offset
                latency = elapsed
                file_at_apply = probe.filename()
                break
        previous = position
        time.sleep(0.005)
    return {
        "applied": applied,
        "applied_position_ms": applied_position,
        "offset_ms": round(offset, 1) if offset is not None else None,
        "residual_ms": round(offset - target, 1) if offset is not None else None,
        "latency_ms": round(latency, 1) if latency is not None else None,
        "filename": file_at_apply,
        "jumps": jumps[:6],
        "elapsed_ms": round((now() - start) * 1000, 1),
    }


def seek_case(harness, probe: MpvProbe, switch: str, index: int) -> dict:
    """Один прогон: переключение трека → немедленный seek."""
    before_track = current_id(harness)
    before_position = harness.backend.position_ms()
    if switch == "next":
        harness.player.next_track()
    else:
        harness.player.previous_track()
    after_track = current_id(harness)
    track = harness.player.current_track
    duration = track.duration_ms if track else 30000
    target, threshold, window = target_window(track)

    error: str | None = None
    seek_started = now()
    try:
        harness.player.seek(target)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"

    observation = observe_seek(harness, probe, target, window)
    filename = observation.get("filename")
    expected = expected_path(track) if track else None
    wrong_track = bool(filename and expected and filename != expected)
    seek_call_ms = (time.monotonic() - seek_started) * 1000
    return {
        "index": index,
        "switch": switch,
        "track_before": before_track,
        "track_after_switch": after_track,
        "duration_ms": duration,
        "requested_ms": target,
        "threshold_ms": threshold,
        "seek_call_ms": round(seek_call_ms, 2),
        "error": error,
        "applied": observation["applied"],
        "applied_position_ms": observation["applied_position_ms"],
        "offset_ms": observation["offset_ms"],
        "residual_ms": observation["residual_ms"],
        "latency_ms": observation["latency_ms"],
        "filename_at_apply": filename,
        "expected_filename": expected,
        "wrong_track": wrong_track,
        "jumps": observation["jumps"],
    }


def scenario_ab(harness, probe: MpvProbe, switch: str, count: int = 50) -> list[dict]:
    return [seek_case(harness, probe, switch, index) for index in range(count)]


def scenario_c_five_seeks(harness, probe: MpvProbe) -> dict:
    """Пять быстрых seek подряд во время загрузки: выигрывает последний."""
    tracks = sorted(harness.adapter.tracks(), key=lambda t: -(t.duration_ms or 0))
    long_track = tracks[0]
    harness.player.play_tracks([long_track], start=0)
    time.sleep(1.5)
    # Переключаемся на другой трек и сразу шлём пять seek'ов.
    harness.player.play_tracks([long_track, long_track], start=0)
    time.sleep(1.0)
    harness.player.next_track()
    targets = [5000, 10000, 15000, 20000, 25000]
    errors: list[str] = []
    for target in targets:
        try:
            harness.player.seek(target)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{target}: {type(exc).__name__}: {exc}")
    last_call = now()
    time.sleep(1.2)
    # К моменту замера прошло время воспроизведения — вычитаем его.
    position = float(harness.backend.position_ms())
    elapsed_ms = (now() - last_call) * 1000.0
    offset = position - elapsed_ms
    return {
        "targets_ms": targets,
        "last_target_ms": targets[-1],
        "position_at_measure_ms": position,
        "elapsed_since_last_seek_ms": round(elapsed_ms, 1),
        "offset_ms": round(offset, 1),
        "residual_ms": round(offset - targets[-1], 1),
        "errors": errors,
        "filename": probe.filename(),
        "last_wins": abs(offset - targets[-1]) <= POSITION_TOLERANCE_MS,
    }


def scenario_d_stop_then_seek(harness) -> dict:
    """Stop, затем seek: команда не должна ронять плеер."""
    harness.player.play_tracks(harness.adapter.tracks(), start=0)
    time.sleep(1.5)
    harness.player.stop()
    time.sleep(0.4)
    error: str | None = None
    try:
        harness.player.seek(10000)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    time.sleep(0.8)
    return {
        "seek_after_stop_error": error,
        "position_ms": harness.backend.position_ms(),
        "status": harness.player.state.status.value,
        "mpv_running": harness.backend.is_running,
        "passed": error is None,
    }


def scenario_f_pending_seek_next_file(harness, probe: MpvProbe) -> dict:
    """Переход на другой файл с ещё ожидающей перемоткой."""
    tracks = sorted(harness.adapter.tracks(), key=lambda t: -(t.duration_ms or 0))
    first, second = tracks[0], tracks[1]
    harness.player.play_tracks([second], start=0)
    time.sleep(0.5)
    harness.player.seek(12000)          # отложенная перемотка
    harness.player.play_tracks([first], start=0)   # смена файла
    time.sleep(1.5)
    samples: list[float] = []
    start = now()
    while now() - start < 1.5:
        samples.append(float(harness.backend.position_ms()))
        time.sleep(0.05)
    natural = max(samples) if samples else 0.0
    return {
        "switched_to": first.id,
        "pending_target_ms": 12000,
        "max_position_ms": natural,
        "stale_seek_applied": natural >= 12000 - POSITION_TOLERANCE_MS,
        "filename": probe.filename(),
        "expected_filename": expected_path(first),
        "passed": natural < 12000 - POSITION_TOLERANCE_MS,
    }


def scenario_e_gui_close_during_load() -> dict:
    """Закрытие окна во время загрузки: без сирот и сокетов."""
    home = q.qa_home("verify_gui")
    env = {
        "NEUROWAVE_HOME": str(home),
        "PATH": f"{q.MPV.parent}:{os.environ['PATH']}",
        "PYTHONPATH": str(q.ROOT / "src"),
        "DISPLAY": os.environ.get("DISPLAY", ":0"),
    }
    (home / "config").mkdir(parents=True, exist_ok=True)
    (home / "config" / "config.json").write_text(
        json.dumps({"provider": "local", "local_music_dir": str(MEDIA), "volume": 30}),
        encoding="utf-8",
    )
    before = set(q.mpv_processes())
    proc = subprocess.Popen(
        [str(q.ROOT / ".venv" / "bin" / "neurowave"), "--provider", "local", "app"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    time.sleep(6.0)
    code, stdout, _ = q.run(["wmctrl", "-l"], timeout=30)
    window = None
    for line in stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[3].strip() == "NeuroWave":
            window = parts[0]
    during = set(q.mpv_processes())
    if window:
        subprocess.run(["wmctrl", "-i", "-c", window], timeout=30)
    try:
        exit_code = proc.wait(timeout=25)
    except subprocess.TimeoutExpired:
        proc.kill()
        exit_code = "timeout"
    time.sleep(2.5)
    sockets_2s = q.stale_ipc_sockets()
    time.sleep(3.0)
    sockets_5s = q.stale_ipc_sockets()
    after = set(q.mpv_processes())
    return {
        "window_found": bool(window),
        "exit_code": exit_code,
        "mpv_during": sorted(during - before),
        "mpv_after": sorted(after - before),
        "orphan_mpv": len(after - before) > 0,
        "sockets_after_2_5s": sockets_2s,
        "sockets_after_5_5s": sockets_5s,
        "passed": len(after - before) == 0 and exit_code == 0 and not sockets_5s,
    }


def summarize(cases: list[dict]) -> dict:
    ok = [c for c in cases if c["applied"] and not c["wrong_track"] and not c["error"]]
    errors = [c for c in cases if c["error"]]
    wrong = [c for c in cases if c["wrong_track"]]
    not_applied = [c for c in cases if not c["applied"]]
    off = [c for c in cases if c["applied"] and abs(c["residual_ms"] or 0) > POSITION_TOLERANCE_MS]
    latencies = [c["latency_ms"] for c in cases if c["latency_ms"] is not None]
    return {
        "total": len(cases),
        "passed": len(ok),
        "failures": len(cases) - len(ok),
        "errors": len(errors),
        "wrong_track": len(wrong),
        "not_applied": len(not_applied),
        "off_tolerance": len(off),
        "latency_ms_max": round(max(latencies), 1) if latencies else None,
        "latency_ms_avg": round(sum(latencies) / len(latencies), 1) if latencies else None,
        "residual_ms_max": max((abs(c["residual_ms"]) for c in cases if c["residual_ms"] is not None),
                               default=None),
        "error_samples": errors[:4],
        "not_applied_samples": not_applied[:4],
        "wrong_track_samples": wrong[:4],
    }


def main() -> int:
    q.ensure_dirs()
    result: dict = {
        "scenario": "QA-VERIFY-01 исправление D-01",
        "base_sha": BASE_SHA,
        "branch": "fix/qa-seek-readiness-01",
        "media_dir": str(MEDIA),
        "position_tolerance_ms": POSITION_TOLERANCE_MS,
    }
    harness = q.build_local_player("verify_d01", repeat="all", volume=30, media_dir=MEDIA)
    recorder = q.EventRecorder(q.ARTIFACTS / "verify_d01_events.jsonl")
    harness.player.add_listener(recorder)
    probe = MpvProbe()
    try:
        harness.player.play_tracks(harness.adapter.tracks(), start=0)
        time.sleep(1.5)
        result["mpv_probe_ok"] = probe.filename() is not None

        result["next_seek"] = scenario_ab(harness, probe, "next", 50)
        result["previous_seek"] = scenario_ab(harness, probe, "previous", 50)
        result["five_rapid_seeks"] = scenario_c_five_seeks(harness, probe)
        result["stop_then_seek"] = scenario_d_stop_then_seek(harness)
        result["pending_seek_next_file"] = scenario_f_pending_seek_next_file(harness, probe)
        result["gui_close_during_load"] = scenario_e_gui_close_during_load()

        events = recorder.as_list()
        track_events = [e for e in events if e["event"] == "track"]
        ids = [e["payload"].get("track", {}).get("id") for e in track_events]
        result["events"] = {
            "total": len(events),
            "track_events": len(track_events),
            "error_events": [e for e in events if e["event"] in {"error", "failed"}],
            "double_end_file_suspects": [
                ids[i] for i in range(1, len(ids))
                if ids[i] == ids[i - 1] and (track_events[i]["t"] - track_events[i - 1]["t"]) < 0.2
            ],
        }
    finally:
        harness.close()
        time.sleep(1.5)
    result["after_run"] = {
        "mpv_processes": q.mpv_processes(),
        "stale_ipc_sockets": q.stale_ipc_sockets(),
    }
    result["summary"] = {
        "next_seek": summarize(result["next_seek"]),
        "previous_seek": summarize(result["previous_seek"]),
        "five_rapid_seeks": result["five_rapid_seeks"]["last_wins"],
        "stop_then_seek": result["stop_then_seek"]["passed"],
        "pending_seek_next_file": result["pending_seek_next_file"]["passed"],
        "gui_close": result["gui_close_during_load"]["passed"],
        "no_orphan_mpv": not result["after_run"]["mpv_processes"],
        "no_stale_sockets": not result["after_run"]["stale_ipc_sockets"],
        "no_double_end_file": not result["events"]["double_end_file_suspects"],
        "no_error_events": not result["events"]["error_events"],
    }
    s = result["summary"]
    criteria = (
        s["next_seek"]["passed"] == 50
        and s["previous_seek"]["passed"] == 50
        and s["next_seek"]["wrong_track"] == 0
        and s["previous_seek"]["wrong_track"] == 0
        and s["next_seek"]["errors"] == 0
        and s["previous_seek"]["errors"] == 0
        and s["five_rapid_seeks"]
        and s["stop_then_seek"]
        and s["pending_seek_next_file"]
        and s["gui_close"]
        and s["no_orphan_mpv"]
        and s["no_stale_sockets"]
        and s["no_double_end_file"]
    )
    result["verdict"] = "PASS" if criteria else "FAIL"
    result["criteria"] = {
        "next_50_of_50": s["next_seek"]["passed"] == 50,
        "previous_50_of_50": s["previous_seek"]["passed"] == 50,
        "position_within_1s": s["next_seek"]["off_tolerance"] == 0 and s["previous_seek"]["off_tolerance"] == 0,
        "no_seek_on_wrong_track": s["next_seek"]["wrong_track"] == 0 and s["previous_seek"]["wrong_track"] == 0,
        "no_exceptions": s["next_seek"]["errors"] == 0 and s["previous_seek"]["errors"] == 0,
    }
    path = q.write_result("verify_d01", result)
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    print("КРИТЕРИИ:", json.dumps(result["criteria"], ensure_ascii=False))
    print("ВЕРДИКТ:", result["verdict"])
    print(f"→ {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
