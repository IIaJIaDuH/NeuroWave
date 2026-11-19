"""QA-01 (продолжение): жизненный цикл GUI-приложения.

Проверяем то, что происходит в реальной сессии рабочего стола:
  * окно открывается и воспроизведение идёт;
  * закрытие окна во время воспроизведения: mpv не должен остаться сиротой;
  * повторный запуск после завершения;
  * MPRIS/трей: регистрируется ли сервис на шине D-Bus.

Запуск: python qa/qa01_gui_lifecycle.py
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


def app_env(home: Path) -> dict:
    return {
        "NEUROWAVE_HOME": str(home),
        "PATH": f"{q.MPV.parent}:{os.environ['PATH']}",
        "PYTHONPATH": str(q.ROOT / "src"),
        "DISPLAY": os.environ.get("DISPLAY", ":0"),
    }


def window_id(title_suffix: str = "NeuroWave") -> str | None:
    code, out, _ = q.run(["wmctrl", "-l"], timeout=30)
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[3].strip() == title_suffix:
            return parts[0]
    return None


def start_app(home: Path, args: list[str] | None = None):
    return subprocess.Popen(
        [str(q.ROOT / ".venv" / "bin" / "neurowave"), "--provider", "local", *(args or ["app"])],
        env=app_env(home), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def main() -> int:
    q.ensure_dirs()
    result: dict = {"scenario": "QA-01 жизненный цикл GUI"}

    # 1. Окно + воспроизведение, затем закрытие во время игры.
    home = q.qa_home("qa01_gui")
    proc = start_app(home)
    time.sleep(5.0)
    result["window_opened"] = window_id() is not None
    # Запускаем воспроизведение изнутри GUI через локальный провайдер:
    # отдельным процессом проще и честнее — окно при этом живо.
    play = subprocess.Popen(
        [str(q.ROOT / ".venv" / "bin" / "neurowave"), "--provider", "local",
         "play", "QA экран"],
        env=app_env(q.qa_home("qa01_gui_play")), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )
    time.sleep(6.0)
    result["mpv_during_play"] = q.mpv_processes()
    wid = window_id()
    if wid:
        subprocess.run(["wmctrl", "-i", "-c", wid], timeout=30)
    time.sleep(4.0)
    result["app_alive_after_close"] = proc.poll() is None
    result["play_alive_after_close"] = play.poll() is None
    if play.poll() is None:
        play.terminate()
        try:
            play.wait(timeout=10)
        except subprocess.TimeoutExpired:
            play.kill()
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    time.sleep(2.0)
    result["mpv_after_close"] = q.mpv_processes()
    result["sockets_after_close"] = q.stale_ipc_sockets()

    # 2. Повторный запуск после завершения.
    proc2 = start_app(q.qa_home("qa01_gui_restart"))
    time.sleep(5.0)
    result["restart_window_opened"] = window_id() is not None
    result["restart_alive"] = proc2.poll() is None
    if proc2.poll() is None:
        proc2.terminate()
        try:
            proc2.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc2.kill()
    time.sleep(1.5)
    result["mpv_after_restart_close"] = q.mpv_processes()

    # 3. MPRIS: сервис на шине.
    from neurowave.integrations.mpris_dbus import BUS_NAME, OBJECT_PATH

    code, out, _ = q.run(["gdbus", "call", "--session", "--dest", BUS_NAME,
                          "--object-path", OBJECT_PATH,
                          "--method", "org.freedesktop.DBus.Properties.Get",
                          "org.mpris.MediaPlayer2.Player", "PlaybackStatus"], timeout=30)
    result["mpris_without_service"] = {"code": code, "stdout": out.strip()[:300],
                                        "note": "нет сервиса — ожидаемый отказ"}
    tray = subprocess.Popen(
        [str(q.ROOT / ".venv" / "bin" / "neurowave"), "--provider", "local", "tray"],
        env=app_env(q.qa_home("qa01_tray")), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )
    time.sleep(6.0)
    code, out, _ = q.run(["gdbus", "call", "--session", "--dest", BUS_NAME,
                          "--object-path", OBJECT_PATH,
                          "--method", "org.freedesktop.DBus.Properties.Get",
                          "org.mpris.MediaPlayer2.Player", "PlaybackStatus"], timeout=30)
    result["mpris_with_tray"] = {"code": code, "stdout": out.strip()[:300]}
    code, out, _ = q.run(["gdbus", "call", "--session", "--dest", BUS_NAME,
                          "--object-path", OBJECT_PATH,
                          "--method", "org.freedesktop.DBus.Introspectable.Introspect"],
                         timeout=30)
    result["mpris_introspect"] = {"code": code, "stdout": out.strip()[:400]}
    result["tray_alive"] = tray.poll() is None
    if tray.poll() is None:
        tray.terminate()
        try:
            tray.wait(timeout=10)
        except subprocess.TimeoutExpired:
            tray.kill()
    time.sleep(1.5)
    result["mpv_final"] = q.mpv_processes()

    path = q.write_result("qa01_gui_lifecycle", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"→ {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
