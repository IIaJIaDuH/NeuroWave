"""QA-07: восстановление после сбоев.

Все проверки идут в изолированном QA-профиле (NEUROWAVE_HOME в отдельном
каталоге): реальный профиль пользователя не трогаем, системную сеть не
отключаем.

Сценарии: битый путь к файлу, удалённый файл из очереди, недоступный
аудиовыход, аварийное завершение и повторный старт, битые настройки,
старт без токена.

Запуск: python qa/qa07_recovery.py
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qaenv as q  # noqa: E402


def section_bad_path(harness) -> dict:
    """Файл исчез или путь битый: приложение не должно падать."""
    from neurowave.domain import Track

    bad = Track(id="../outside-library.mp3", title="broken", provider="local")
    out: dict = {}
    try:
        harness.player.play_track(bad)
        out["no_raise"] = True
    except Exception as exc:  # noqa: BLE001
        out["no_raise"] = False
        out["error"] = f"{type(exc).__name__}: {exc}"
    time.sleep(1.0)
    out["status"] = harness.player.state.status.value
    out["overlay"] = str(harness.player.state.overlay)
    out["events"] = [e for e in harness.recorder.as_list()
                     if e["event"] in {"error", "failed", "finished"}][-5:]
    out["mpv_alive"] = harness.backend.is_running
    return out


def section_deleted_file(harness) -> dict:
    """Файл удалили, пока он в очереди."""
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="nwqa-"))
    copy = tmp / "qa_tone_b220_short.mp3"
    shutil.copy(q.MEDIA / "qa_tone_b220_short.mp3", copy)
    media_dir = Path(harness.settings.local_music_dir) / "temp"
    media_dir.mkdir(parents=True, exist_ok=True)
    target = media_dir / "qa_tone_b220_short.mp3"
    shutil.copy(copy, target)
    harness.adapter.refresh()
    out: dict = {"file": str(target)}
    try:
        harness.player.play_tracks(harness.adapter.tracks(), start=0)
        time.sleep(1.0)
        target.unlink()
        out["deleted_while_playing"] = True
        before = harness.recorder.count("track")
        for _ in range(3):
            harness.player.next_track()
            time.sleep(1.0)
        out["track_events_after_delete"] = harness.recorder.count("track") - before
        out["status"] = harness.player.state.status.value
        out["errors"] = [e for e in harness.recorder.as_list()
                         if e["event"] in {"error", "failed"}][-5:]
        out["still_alive"] = harness.backend.is_running
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(media_dir, ignore_errors=True)
        harness.adapter.refresh()
    return out


def section_bad_output(harness) -> dict:
    """Недоступный аудиовыход."""
    out: dict = {}
    try:
        harness.player.set_output("pulse/alsa_output.does-not-exist")
        out["accepted"] = True
    except Exception as exc:  # noqa: BLE001
        out["accepted"] = False
        out["error"] = f"{type(exc).__name__}: {exc}"
    time.sleep(1.5)
    out["mpv_state"] = harness.backend.snapshot_state()
    out["status"] = harness.player.state.status.value
    out["overlay"] = str(harness.player.state.overlay)
    out["position_ms"] = harness.backend.position_ms()
    # вернуть рабочий выход
    try:
        harness.player.set_output("auto")
        out["restored"] = True
    except Exception as exc:  # noqa: BLE001
        out["restored"] = False
        out["restore_error"] = f"{type(exc).__name__}: {exc}"
    return out


def section_crash_restart() -> dict:
    """Аварийное завершение (SIGKILL) и повторный старт."""
    out: dict = {}
    home = q.qa_home("qa07_crash")
    env = {
        "NEUROWAVE_HOME": str(home),
        "PATH": f"{q.MPV.parent}:{os.environ['PATH']}",
        "PYTHONPATH": str(q.ROOT / "src"),
    }
    script = (
        "import time, sys;"
        "sys.path.insert(0, %r);" % str(q.ROOT / "src") +
        "from neurowave.config import Paths;"
        "from neurowave.local.adapter import LocalMusicAdapter;"
        "from neurowave.audio.mpv import MpvBackend;"
        "from neurowave.player import PlayerService;"
        "paths = Paths.resolve().ensure();"
        "a = LocalMusicAdapter(root=%r);" % str(q.MEDIA) +
        "b = MpvBackend(binary=%r);" % str(q.MPV) +
        "p = PlayerService(adapter=a, backend=b, prefetch=False);"
        "p.play_tracks(a.tracks(), start=0);"
        "print('started', flush=True);"
        "time.sleep(600)"
    )
    proc = subprocess.Popen([str(q.ROOT / ".venv" / "bin" / "python"), "-c", script],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    line = proc.stdout.readline()
    out["start_line"] = line.strip()
    time.sleep(3.0)
    out["mpv_before_kill"] = q.mpv_processes()
    out["sockets_before_kill"] = q.stale_ipc_sockets()
    proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=10)
    time.sleep(2.0)
    out["mpv_after_kill"] = q.mpv_processes()
    out["sockets_after_kill"] = q.stale_ipc_sockets()
    # повторный старт в том же профиле
    proc2 = subprocess.Popen([str(q.ROOT / ".venv" / "bin" / "python"), "-c", script],
                             env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        line2 = proc2.stdout.readline()
    except Exception as exc:  # noqa: BLE001
        line2 = f"ошибка чтения: {exc}"
    out["restart_line"] = line2.strip()
    time.sleep(3.0)
    out["mpv_after_restart"] = q.mpv_processes()
    out["sockets_after_restart"] = q.stale_ipc_sockets()
    proc2.send_signal(signal.SIGKILL)
    proc2.wait(timeout=10)
    time.sleep(1.5)
    out["mpv_final"] = q.mpv_processes()
    out["sockets_final"] = q.stale_ipc_sockets()
    return out


def section_broken_settings() -> dict:
    """Битые настройки в изолированном профиле: старт не должен падать."""
    out: dict = {}
    cases = {
        "невалидный JSON": "{ это не json",
        "пустой объект": "{}",
        "неверные типы": json.dumps({"volume": "громко", "repeat": 5, "shuffle": "да"}),
        "неизвестный провайдер": json.dumps({"provider": "нет-такого"}),
        "битый local_music_dir": json.dumps({"local_music_dir": "/nonexistent/path"}),
    }
    for label, payload in cases.items():
        home = q.qa_home("qa07_settings")
        (home / "config").mkdir(parents=True, exist_ok=True)
        (home / "config" / "config.json").write_text(payload, encoding="utf-8")
        code, stdout, stderr = q.run(
            [str(q.ROOT / ".venv" / "bin" / "neurowave"), "status"],
            timeout=60,
            env={"NEUROWAVE_HOME": str(home),
                 "PATH": f"{q.MPV.parent}:{os.environ['PATH']}",
                 "PYTHONPATH": str(q.ROOT / "src")},
        )
        code2, stdout2, stderr2 = q.run(
            [str(q.ROOT / ".venv" / "bin" / "neurowave"), "--provider", "local", "search", "x"],
            timeout=60,
            env={"NEUROWAVE_HOME": str(home),
                 "PATH": f"{q.MPV.parent}:{os.environ['PATH']}",
                 "PYTHONPATH": str(q.ROOT / "src")},
        )
        out[label] = {
            "status_code": code, "status_out": stdout.strip()[:400], "status_err": stderr.strip()[:400],
            "search_code": code2, "search_out": stdout2.strip()[:300], "search_err": stderr2.strip()[:400],
        }
    return out


def section_no_token() -> dict:
    """Старт без действующего токена Яндекса."""
    home = q.qa_home("qa07_notoken")
    env = {"NEUROWAVE_HOME": str(home), "PATH": f"{q.MPV.parent}:{os.environ['PATH']}",
           "PYTHONPATH": str(q.ROOT / "src")}
    out = {}
    for args in (["wave"], ["me"], ["play", "--provider", "yandex", "test"]):
        code, stdout, stderr = q.run(
            [str(q.ROOT / ".venv" / "bin" / "neurowave"), *args], timeout=60, env=env)
        out[" ".join(args)] = {"code": code, "stdout": stdout.strip()[:300], "stderr": stderr.strip()[:300]}
    return out


def main() -> int:
    q.ensure_dirs()
    result: dict = {"scenario": "QA-07 восстановление после сбоев"}
    harness = q.build_local_player("qa07_recovery", repeat="off", volume=30)
    try:
        harness.player.play_tracks(harness.adapter.tracks(), start=0)
        time.sleep(1.5)
        result["bad_path"] = section_bad_path(harness)
        result["deleted_file"] = section_deleted_file(harness)
        result["bad_output"] = section_bad_output(harness)
    finally:
        harness.close()
        time.sleep(0.8)
    result["crash_restart"] = section_crash_restart()
    result["broken_settings"] = section_broken_settings()
    result["no_token"] = section_no_token()
    path = q.write_result("qa07_recovery", result)
    print(json.dumps(result, ensure_ascii=False, indent=2)[:14000])
    print(f"→ {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
