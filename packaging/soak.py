#!/usr/bin/env python3
"""Долгий прогон плеера: утечки PSS/CPU и стабильность событий.

Запускает настоящий mpv (`--ao=null`, чтобы не шуметь), играет длинный
материал с автопереходом и раз в интервал снимает PSS процесса Python и mpv,
загрузку CPU и число открытых дескрипторов. Печатает CSV и вердикт: есть ли
устойчивый рост памяти.

Запуск: `python packaging/soak.py --minutes 15 --interval 30`
"""

from __future__ import annotations

import argparse
import math
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

RATE = 48000
TONE_SECONDS = 8


def make_tracks(directory: Path, count: int) -> list[Path]:
    """Несколько тонов разной высоты: смена трека проверяется по факту."""
    paths: list[Path] = []
    for index in range(count):
        path = directory / f"track-{index}.wav"
        freq = 220 + index * 40
        frames = b"".join(
            struct.pack("<h", int(6000 * math.sin(2 * math.pi * freq * i / RATE)))
            for i in range(int(RATE * TONE_SECONDS))
        )
        with wave.open(str(path), "w") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(RATE)
            handle.writeframes(frames)
        paths.append(path)
    return paths


def read_status(pid: int) -> dict[str, float] | None:
    try:
        text = Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return None
    values: dict[str, float] = {}
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key in {"VmRSS", "VmLib", "Threads", "FDSize"}:
            values[key] = float(value.split()[0])
    try:
        smaps = Path(f"/proc/{pid}/smaps_rollup").read_text()
        for line in smaps.splitlines():
            if line.startswith("Pss:"):
                values["Pss"] = float(line.split()[1])
    except OSError:
        pass
    return values


def cpu_ticks(pid: int) -> int | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(fields[11]) + int(fields[12])
    except (OSError, IndexError, ValueError):
        return None


def open_files(pid: int) -> int:
    try:
        return len(os.listdir(f"/proc/{pid}/fd"))
    except OSError:
        return -1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=float, default=15.0)
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--tracks", type=int, default=6)
    args = parser.parse_args()

    from neurowave.audio.mpv import MpvBackend
    from neurowave.domain import Quality, StreamInfo, Track
    from neurowave.player import PlayerService

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        paths = make_tracks(tmp_path, args.tracks)

        class Adapter:
            def __init__(self) -> None:
                self.current = 0
                self.tracks = [
                    Track(
                        id=str(index),
                        title=f"Track {index}",
                        artists=("Soak",),
                        album="Soak",
                        duration_ms=TONE_SECONDS * 1000,
                    )
                    for index in range(len(paths))
                ]

            def stream_url(self, track_id: str) -> StreamInfo:
                index = int(track_id) % len(paths)
                return StreamInfo(url=str(paths[index]), quality=Quality(codec="pcm_s16le"))

            def record_play(self, *_args, **_kwargs) -> None:
                pass

            def wave_feedback(self, *_args, **_kwargs) -> bool:
                return True

        backend = MpvBackend(extra_args=["--ao=null", "--volume=40"])
        adapter = Adapter()
        player = PlayerService(adapter=adapter, backend=backend, prefetch=False)
        transitions: list[float] = []
        player.add_listener(lambda name, payload: transitions.append(time.monotonic()) if name == "track" else None)

        stop = threading.Event()
        samples: list[dict[str, float]] = []
        python_pid = os.getpid()

        def sample() -> None:
            mpv_pid = None
            process = getattr(backend, "_process", None)
            if process is not None:
                mpv_pid = process.pid
            row: dict[str, float] = {"t": time.monotonic()}
            own = read_status(python_pid) or {}
            row["pss_py"] = own.get("Pss", 0.0)
            row["rss_py"] = own.get("VmRSS", 0.0)
            row["fds_py"] = float(open_files(python_pid))
            row["threads_py"] = own.get("Threads", 0.0)
            if mpv_pid:
                child = read_status(mpv_pid) or {}
                row["pss_mpv"] = child.get("Pss", 0.0)
                row["rss_mpv"] = child.get("VmRSS", 0.0)
            ticks = cpu_ticks(python_pid)
            row["cpu_py_ticks"] = float(ticks or 0)
            samples.append(row)

        print("t,сек,pss_py,МБ,pss_mpv,МБ,rss_py,МБ,fd,потоки,позиция,сек,треков")
        try:
            player.play_tracks(adapter.tracks, start=0)
            player.queue.set_repeat("all")
            start = time.monotonic()
            last_print = 0.0
            first_cpu = cpu_ticks(python_pid) or 0
            while time.monotonic() - start < args.minutes * 60:
                sample()
                row = samples[-1]
                elapsed = row["t"] - start
                if elapsed - last_print >= args.interval:
                    last_print = elapsed
                    position = player.backend.position_ms() / 1000
                    print(
                        f"{elapsed:.0f},{elapsed:.0f},{row['pss_py'] / 1024:.1f},"
                        f"{(row.get('pss_mpv', 0.0)) / 1024:.1f},{row['rss_py'] / 1024:.1f},"
                        f"{row['fds_py']:.0f},{row['threads_py']:.0f},{position:.1f},"
                        f"{len(transitions)}",
                        flush=True,
                    )
                stop.wait(min(args.interval, 5.0))
        finally:
            player.close()

        elapsed = max(samples[-1]["t"] - samples[0]["t"], 1.0)
        ticks = cpu_ticks(python_pid) or 0
        used = (ticks - first_cpu) / os.sysconf("SC_CLK_TCK")
        first_pss = samples[0]["pss_py"] / 1024
        last_pss = samples[-1]["pss_py"] / 1024
        drift = last_pss - first_pss
        print()
        print(f"Длительность: {elapsed / 60:.1f} мин")
        print(f"Python PSS: {first_pss:.1f} → {last_pss:.1f} МБ (дрейф {drift:+.1f})")
        print(f"mpv PSS на конце: {samples[-1].get('pss_mpv', 0.0) / 1024:.1f} МБ")
        print(f"CPU Python: {used:.1f} с за {elapsed:.0f} с ({100 * used / elapsed:.2f} % ядра)")
        print(f"Дескрипторы: {samples[0]['fds_py']:.0f} → {samples[-1]['fds_py']:.0f}")
        print(f"Потоки: {samples[0]['threads_py']:.0f} → {samples[-1]['threads_py']:.0f}")
        print(f"Смен трека: {len(transitions)}")
        ok = drift < max(8.0, 0.10 * first_pss) and samples[-1]["fds_py"] <= samples[0]["fds_py"] + 4
        print("ВЕРДИКТ: " + ("утечек не видно" if ok else "есть рост — разбирать"))
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
