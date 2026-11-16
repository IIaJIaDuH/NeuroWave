#!/usr/bin/env python3
"""Проверка безопасности усиления: сэмпловый и inter-sample пик.

Скрипт отвечает на один вопрос: можно ли поднимать цифровую громкость выше
100 % так, чтобы лимитер действительно защищал. Он не «доверяет» фильтру, а
выгружает PCM через `mpv --ao=pcm` и измеряет результат.

Запуск: `python packaging/dsp-safety.py [--seconds 1.0]`
Требуется mpv в PATH (или .tools/bin).
"""

from __future__ import annotations

import argparse
import math
import random
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

RATE = 48000
CASES = (
    ("без обработки", None, 100),
    ("лимитер, +6 дБ", "lavfi=[volume=volume=6dB:precision=float,alimiter=limit=0.8913:attack=5:release=50:level=0]", 100),
    ("лимитер, +12 дБ", "lavfi=[volume=volume=12dB:precision=float,alimiter=limit=0.8913:attack=5:release=50:level=0]", 100),
    ("лимитер −2 dBFS, +12 дБ", "lavfi=[volume=volume=12dB:precision=float,alimiter=limit=0.7943:attack=5:release=50:level=0]", 100),
    ("+12 дБ без лимитера", "lavfi=[volume=volume=12dB:precision=float]", 100),
)


def make_signal(path: Path, seconds: float) -> Path:
    """Смесь синусов у полной шкалы + шумовые всплески: повод для клиппинга."""
    rng = random.Random(20261116)
    frames = bytearray()
    total = int(RATE * seconds)
    for i in range(total):
        t = i / RATE
        value = (
            0.55 * math.sin(2 * math.pi * 997 * t)
            + 0.30 * math.sin(2 * math.pi * 61 * t)
            + 0.12 * math.sin(2 * math.pi * 8300 * t)
        )
        # Всплеск каждые 200 мс — имитация транзиента.
        if (i % (RATE // 5)) < RATE // 100:
            value += 0.45 * (rng.random() * 2 - 1)
        frames += struct.pack("<h", max(-32768, min(32767, int(value * 32767))))
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(bytes(frames))
    return path


def decode(path: Path) -> list[float]:
    with wave.open(str(path)) as handle:
        data = handle.readframes(handle.getnframes())
    raw = struct.unpack(f"<{len(data) // 2}h", data)
    return [sample / 32768.0 for sample in raw]


def run_mpv(source: Path, graph: str | None, volume: int, out: Path) -> list[float]:
    args = [
        "mpv", "--no-video", "--no-terminal", "--really-quiet",
        "--ao=pcm", f"--ao-pcm-file={out}",
        "--audio-format=s16", f"--audio-samplerate={RATE}",
        "--audio-channels=mono", f"--volume={volume}",
    ]
    if graph:
        args.append(f"--af={graph}")
    args.append(str(source))
    subprocess.run(args, check=True, capture_output=True)
    return decode(out)


def db(value: float) -> float:
    return 20 * math.log10(value) if value > 0 else float("-inf")


def true_peak(samples: list[float], factor: int = 4, taps: int = 32) -> float:
    """Inter-sample пик через 4× передискретизацию окном Ханна.

    Это не ISO-226 true peak, но честная оценка: показывает, выходит ли
    сигнал за потолок *между* сэмплами, чего сэмпловый пик не видит.
    """
    half = taps // 2
    peak = max(abs(sample) for sample in samples) if samples else 0.0
    for index in range(len(samples)):
        for phase in range(1, factor):
            frac = phase / factor
            acc = 0.0
            for k in range(-half + 1, half + 1):
                j = index + k
                if 0 <= j < len(samples):
                    x = frac - k
                    window = 0.5 + 0.5 * math.cos(math.pi * x / half)
                    acc += samples[j] * (math.sin(math.pi * x) / (math.pi * x) if x else 1.0) * window
            peak = max(peak, abs(acc))
    return peak


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=1.0)
    parser.add_argument("--true-peak-seconds", type=float, default=0.25)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        source = make_signal(tmp_path / "source.wav", args.seconds)
        base = decode(source)
        print(f"Источник: сэмпловый пик {db(max(abs(x) for x in base)):+.2f} dBFS, "
              f"RMS {db(math.sqrt(sum(x * x for x in base) / len(base))):+.2f} dBFS")
        print()
        print(f"{'случай':26} {'пик':>9} {'true peak':>10} {'RMS':>9} {'клипов':>7}")
        print("-" * 66)
        rows = []
        for name, graph, volume in CASES:
            samples = run_mpv(source, graph, volume, tmp_path / "out.wav")
            peak = max(abs(x) for x in samples)
            rms = math.sqrt(sum(x * x for x in samples) / len(samples))
            clipped = sum(1 for x in samples if abs(x) >= 0.999)
            tp = true_peak(samples[: int(RATE * args.true_peak_seconds)])
            rows.append((name, db(peak), db(tp), db(rms), clipped))
            print(f"{name:26} {db(peak):+8.2f}  {db(tp):+9.2f} {db(rms):+8.2f} {clipped:7d}")
        over = [row for row in rows if row[1] > -0.05]
        print()
        print(f"Случаев с сэмпловым пиком выше −0.05 dBFS: {len(over)}")
        worst_tp = max(row[2] for row in rows)
        print(f"Худший inter-sample пик: {worst_tp:+.2f} dBFS")
        print("Вывод: лимитер держит сэмпловый потолок; запас по inter-sample "
              "пику надо учитывать при выборе ceiling.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
