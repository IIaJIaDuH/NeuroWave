"""Проба стенда: реальный звук доходит до sink'а, mpv играет в HDMI.

Проверяем не «процесс жив», а сигнал: записываем монитор системного sink'а
через `parec` и считаем пик/rms в dBFS. Молчание вместо звука означало бы,
что mpv играет в никуда.

Запуск: python qa/probe_audio.py
"""

from __future__ import annotations

import json
import struct
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qaenv as q  # noqa: E402

RATE = 48000
CHANNELS = 2


def monitor_source() -> str:
    code, out, _ = q.run(["pactl", "get-default-sink"])
    sink = out.strip()
    code, out, _ = q.run(["pactl", "list", "sources", "short"])
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1].endswith(".monitor") and sink in parts[1]:
            return parts[0]
    raise SystemExit("��Е найден монитор системного sink'а")


def peak_dbfs(raw: bytes) -> tuple[float, float, int]:
    """Пик и RMS в dBFS по сырым s16le-кадрам."""
    count = len(raw) // (2 * CHANNELS)
    if count == 0:
        return -120.0, -120.0, 0
    values = struct.unpack(f"<{count * CHANNELS}h", raw[: count * CHANNELS * 2])
    peak = max(abs(v) for v in values) / 32768.0
    total = sum(v * v for v in values) / len(values)
    rms = (total ** 0.5) / 32768.0
    db = lambda x: 20 * __import__("math").log10(x) if x > 0 else -120.0
    return db(peak), db(rms), count


def main() -> int:
    q.ensure_dirs()
    sink = q.run(["pactl", "get-default-sink"])[1].strip()
    source = monitor_source()
    raw_path = q.ARTIFACTS / "probe_sink.raw"
    record = subprocess.Popen(
        ["parec", "--device", source, "--format=s16le", "--rate", str(RATE),
         "--channels", str(CHANNELS), "--raw"],
        stdout=raw_path.open("wb"), stderr=subprocess.DEVNULL,
    )
    time.sleep(1.0)

    harness = q.build_local_player("probe", repeat="all", volume=30)
    tracks = harness.adapter.tracks()
    result: dict = {"sink": sink, "monitor_source": source, "tracks": [t.id for t in tracks]}
    try:
        harness.player.play_tracks(tracks, start=0)
        time.sleep(6.0)
        state = harness.backend.snapshot_state()
        result["mpv_state"] = state
        result["position_ms"] = harness.backend.position_ms()
        result["is_playing"] = harness.backend.is_playing()
        result["events"] = harness.recorder.as_list()
    finally:
        harness.close()
        time.sleep(0.5)
        record.terminate()
        try:
            record.wait(timeout=5)
        except subprocess.TimeoutExpired:
            record.kill()

    raw = raw_path.read_bytes()
    db_peak, db_rms, frames = peak_dbfs(raw)
    result["sink_capture"] = {
        "bytes": len(raw), "frames": frames,
        "peak_dbfs": round(db_peak, 2), "rms_dbfs": round(db_rms, 2),
    }
    raw_path.unlink(missing_ok=True)
    path = q.write_result("probe_audio", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"→ {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
