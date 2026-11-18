"""QA-01: завершение приложения всеми способами и судьба mpv.

Закрытие окна, Ctrl-C (SIGINT), SIGTERM и «убийство» (SIGKILL) — после
каждого проверяем, остались ли процессы mpv и IPC-сокеты.

Запуск: python qa/qa01_termination.py
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qaenv as q  # noqa: E402

MEDIA = "/home/zoz/.deepai-code/scratch/2026-11-18-wo03/media"


def make_home(name: str) -> Path:
    home = q.qa_home(name)
    (home / "config").mkdir(parents=True, exist_ok=True)
    (home / "config" / "config.json").write_text(
        json.dumps({"provider": "local", "local_music_dir": MEDIA, "volume": 30}),
        encoding="utf-8",
    )
    return home


def env_for(home: Path) -> dict:
    return {
        "NEUROWAVE_HOME": str(home),
        "PATH": f"{q.MPV.parent}:{os.environ['PATH']}",
        "PYTHONPATH": str(q.ROOT / "src"),
        "DISPLAY": os.environ.get("DISPLAY", ":0"),
    }


def scenario(label: str, command: list[str], how: str) -> dict:
    before = set(q.mpv_processes())
    home = make_home(f"qa01_term_{label}")
    proc = subprocess.Popen(command, env=env_for(home), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    time.sleep(7.0)
    during = set(q.mpv_processes())
    out: dict = {"label": label, "command": " ".join(command), "mpv_during": sorted(during - before)}
    if how == "wm-close":
        code, stdout, _ = q.run(["wmctrl", "-l"], timeout=30)
        window = None
        for line in stdout.splitlines():
            parts = line.split(None, 3)
            if len(parts) == 4 and parts[3].strip() == "NeuroWave":
                window = parts[0]
        out["window"] = window
        if window:
            subprocess.run(["wmctrl", "-i", "-c", window], timeout=30)
    elif how == "sigint":
        proc.send_signal(signal.SIGINT)
    elif how == "sigterm":
        proc.send_signal(signal.SIGTERM)
    elif how == "sigkill":
        proc.send_signal(signal.SIGKILL)
    try:
        out["exit_code"] = proc.wait(timeout=25)
    except subprocess.TimeoutExpired:
        proc.kill()
        out["exit_code"] = "timeout"
    time.sleep(2.5)
    after = set(q.mpv_processes())
    out["mpv_after"] = sorted(after - before)
    out["orphan_mpv"] = len(out["mpv_after"]) > 0
    out["sockets_after"] = q.stale_ipc_sockets()
    out["verdict"] = "PASS" if not out["orphan_mpv"] else "FAIL: остался процесс mpv"
    return out


def main() -> int:
    q.ensure_dirs()
    binary = str(q.ROOT / ".venv" / "bin" / "neurowave")
    result = {
        "scenario": "QA-01 завершение приложения",
        "cases": [
            scenario("app_wm_close", [binary, "--provider", "local", "app"], "wm-close"),
            scenario("app_sigint", [binary, "--provider", "local", "app"], "sigint"),
            scenario("app_sigterm", [binary, "--provider", "local", "app"], "sigterm"),
            scenario("app_sigkill", [binary, "--provider", "local", "app"], "sigkill"),
            scenario("tray_sigterm", [binary, "--provider", "local", "tray"], "sigterm"),
        ],
    }
    path = q.write_result("qa01_termination", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"→ {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
