"""Сравнение: что происходит с процессом mpv и IPC-сокетом после закрытия окна.

Один и тот же сценарий гоняется на исправленной ветке и на `main`, чтобы
отличить регрессию от давнего поведения.

Запуск: python qa/gui_close_socket.py <файл_результата.json>
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

MEDIA = Path("/home/zoz/.deepai-code/scratch/2026-11-18-wo03/media-verify")


def main() -> int:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else q.ARTIFACTS / "gui_close_socket.json"
    home = q.qa_home("gui_close_socket")
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
    time.sleep(7.0)
    code, stdout, _ = q.run(["wmctrl", "-l"], timeout=30)
    window = None
    for line in stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[3].strip() == "NeuroWave":
            window = parts[0]
    app_pid = proc.pid
    during = set(q.mpv_processes())
    if window:
        subprocess.run(["wmctrl", "-i", "-c", window], timeout=30)
    started = time.monotonic()
    try:
        exit_code = proc.wait(timeout=25)
    except subprocess.TimeoutExpired:
        proc.kill()
        exit_code = "timeout"
    timeline: list[dict] = []
    for moment in (1.0, 3.0, 5.0, 10.0, 20.0, 30.0):
        while time.monotonic() - started < moment:
            time.sleep(0.1)
        timeline.append({
            "at_s": moment,
            "mpv_alive": sorted(set(q.mpv_processes()) - before),
            "sockets": q.stale_ipc_sockets(),
        })
    result = {
        "app_pid": app_pid,
        "exit_code": exit_code,
        "mpv_during": sorted(during - before),
        "timeline": timeline,
        "final_mpv": q.mpv_processes(),
        "final_sockets": q.stale_ipc_sockets(),
        "sha": os.environ.get("QA_SHA", subprocess.run(
            ["git", "-C", str(q.ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True).stdout.strip()),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
