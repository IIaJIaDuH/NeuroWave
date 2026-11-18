"""Снимок ТОЛЬКО окна NeuroWave на живом экране.

Полный снимок рабочего стола содержит личные данные пользователя, поэтому
в отчёт попадает только область окна приложения (по геометрии xwininfo).

Запуск: python qa/qa05_real_display.py
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

SHOTS = q.ROOT / "docs" / "qa" / "screenshots"


def window_geometry(window_id: str) -> dict | None:
    """Геометрия окна: xwininfo, иначе wmctrl -G."""
    code, out, _ = q.run(["xwininfo", "-id", window_id], timeout=30)
    if code != 0:
        code2, out2, _ = q.run(["wmctrl", "-l", "-G"], timeout=30)
        for line in out2.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0] == window_id:
                try:
                    x, y, w, h = (int(parts[2]), int(parts[3]), int(parts[4]), int(parts[5]))
                except (ValueError, IndexError):
                    return None
                return {"x": x, "y": y, "width": w, "height": h}
        return None
    geom: dict[str, int] = {}
    for line in out.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        mapping = {
            "Width": "width", "Height": "height",
            "Absolute upper-left X": "x", "Absolute upper-left Y": "y",
        }
        if key in mapping and value.isdigit():
            geom[mapping[key]] = int(value)
    if {"width", "height", "x", "y"} <= set(geom):
        return geom
    return None


def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    home = q.qa_home("qa05_real2")
    env = {
        "NEUROWAVE_HOME": str(home),
        "PATH": f"{q.MPV.parent}:{os.environ['PATH']}",
        "PYTHONPATH": str(q.ROOT / "src"),
        "DISPLAY": os.environ.get("DISPLAY", ":0"),
    }
    proc = subprocess.Popen(
        [str(q.ROOT / ".venv" / "bin" / "neurowave"), "--provider", "local", "app"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    time.sleep(6.0)
    code, out, _ = q.run(["wmctrl", "-l"], timeout=30)
    window_id = None
    for line in out.splitlines():
        if line.endswith("NeuroWave"):
            window_id = line.split()[0]
            break
    result: dict = {"window_id": window_id}
    if window_id:
        geom = window_geometry(window_id)
        result["geometry"] = geom
        if geom:
            # Поднимаем окно наверх: иначе поверх него лежит другое окно.
            subprocess.run(["wmctrl", "-i", "-a", window_id], timeout=30)
            time.sleep(1.5)
            target = SHOTS / "real_display_window.png"
            code, stdout, stderr = q.run([
                "ffmpeg", "-y", "-v", "error", "-f", "x11grab",
                "-video_size", "1920x1080", "-i", os.environ.get("DISPLAY", ":0"),
                "-vf", f"crop={geom['width']}:{geom['height']}:{geom['x']}:{geom['y']}",
                "-frames:v", "1", str(target),
            ], timeout=60)
            result["capture"] = {"code": code, "stderr": stderr.strip()[:200],
                                 "file": target.name,
                                 "bytes": target.stat().st_size if target.exists() else 0}
        subprocess.run(["wmctrl", "-i", "-c", window_id], timeout=30)
    time.sleep(3.0)
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    time.sleep(1.0)
    result["mpv_after"] = q.mpv_processes()
    path = q.write_result("qa05_real_display", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"→ {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
