#!/usr/bin/env python3
"""PoC: захват PCM только своего потока, не всего системного звука.

Проверяет gate из плана Sound Lab (§6.4) до того, как писать спектроанализатор:

1. mpv играет в отдельный null-sink; этот sink виден только NeuroWave.
2. Второе приложение играет другую частоту в системный выход (Bluetooth).
3. Скрипт записывает monitor null-sink и проверяет, что в записи есть только
   наша частота; чужая частота должна отсутствовать (изоляция).
4. Если включён loopback в системный выход — звук остаётся слышимым, и
   измеряется добавленная задержка.

Модули PipeWire создаются с уникальными именами и выгружаются в finally.
Системный sink по умолчанию не меняется.

Запуск: `python packaging/pcm-tap-probe.py [--seconds 6] [--no-loopback]`
"""

from __future__ import annotations

import argparse
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

RATE = 48000
OURS_HZ = 1000
OTHER_HZ = 3000
SINK = f"nw_probe_{os.getpid()}"


def run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, check=False, **kwargs)


def tone(path: Path, seconds: float, freq: float, amp: float = 0.3) -> Path:
    frames = b"".join(
        struct.pack("<h", int(amp * 32767 * math.sin(2 * math.pi * freq * i / RATE)))
        for i in range(int(RATE * seconds))
    )
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(frames)
    return path


def click_track(path: Path, seconds: float, clicks: int = 6, freq: float = 2000) -> Path:
    """Редкие короткие щелчки: по ним видно задержку loopback."""
    frames = bytearray()
    step = int(RATE * seconds / clicks)
    width = int(RATE * 0.005)
    for i in range(int(RATE * seconds)):
        value = 0
        if i % step < width:
            value = int(20000 * math.sin(2 * math.pi * freq * i / RATE) * (1 - (i % step) / width))
        frames += struct.pack("<h", value)
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(bytes(frames))
    return path


def goertzel(samples: list[float], freq: float, rate: int = RATE) -> float:
    """Амплитуда одной частоты: полный FFT здесь не нужен."""
    n = len(samples)
    if not n:
        return 0.0
    coeff = 2 * math.cos(2 * math.pi * freq / rate)
    s_prev = s_prev2 = 0.0
    for sample in samples:
        s = sample + coeff * s_prev - s_prev2
        s_prev2, s_prev = s_prev, s
    power = s_prev2**2 + s_prev**2 - coeff * s_prev * s_prev2
    return 2 * math.sqrt(max(power, 0.0)) / n


def read_pcm(path: Path, channels: int = 2) -> list[float]:
    raw = path.read_bytes()
    count = len(raw) // 4
    floats = struct.unpack(f"<{count}f", raw[: count * 4])
    return [sum(floats[i : i + channels]) / channels for i in range(0, count - channels + 1, channels)]


def _record(device: str, target: Path) -> subprocess.Popen:
    """parec в файл через stdout: --file умеет только контейнеры по расширению."""
    handle = open(target, "wb")
    process = subprocess.Popen(
        ["parec", f"--device={device}.monitor", "--format=float32le",
         f"--rate={RATE}", "--channels=2"],
        stdout=handle, stderr=subprocess.DEVNULL,
    )
    process._nw_handle = handle  # type: ignore[attr-defined]
    return process


def parse_latency_us(text: str, sink: str) -> int | None:
    """Задержка sink'а из `pactl list sinks` (локале-независимо: ищем мкс)."""
    blocks = text.split("\n\n")
    for block in blocks:
        if f"node.name = \"{sink}\"" in block or f"Имя: {sink}" in block:
            match = re.search(r"(\d+)\s*(?:мкс|usec|µs)", block)
            if match:
                return int(match.group(1))
    return None


def envelope(samples: list[float], freq: float, window_s: float = 0.01, hop_s: float = 0.005) -> list[float]:
    """Уровень одной частоты по окнам: огибающая для корреляции."""
    window = int(window_s * RATE)
    hop = max(1, int(hop_s * RATE))
    return [goertzel(samples[start : start + window], freq) for start in range(0, len(samples) - window, hop)]


def envelope_lag_ms(reference: list[float], delayed: list[float], max_lag_s: float = 0.6,
                    hop_s: float = 0.005) -> float | None:
    """Лаг между огибающими двух частот в ОДНОЙ записи.

    Максимум взаимной корреляции в пределах ±max_lag_s: паразитные совпадения
    через секунду (следующий щелчок) в окно не попадают.
    """
    limit = int(max_lag_s / hop_s)
    best_lag, best_score = None, 0.0
    for lag in range(-limit, limit + 1):
        score = 0.0
        for index, value in enumerate(reference):
            other = index + lag
            if 0 <= other < len(delayed):
                score += value * delayed[other]
        if score > best_score:
            best_lag, best_score = lag, score
    if best_lag is None:
        return None
    return best_lag * hop_s * 1000


def db(value: float) -> float:
    return 20 * math.log10(value) if value > 0 else float("-inf")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=6.0)
    parser.add_argument("--no-loopback", action="store_true")
    args = parser.parse_args()

    for tool in ("pactl", "parec", "mpv"):
        if shutil.which(tool) is None:
            print(f"нет {tool}: PoC невозможен", file=sys.stderr)
            return 2

    default_sink = run(["pactl", "get-default-sink"]).stdout.strip()
    if not default_sink:
        print("не удалось определить sink по умолчанию", file=sys.stderr)
        return 2
    print(f"Системный выход: {default_sink}")
    print(f"Наш sink: {SINK}")

    modules: list[str] = []
    mpv: list[subprocess.Popen] = []
    parec: list[subprocess.Popen] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        ours = tone(tmp_path / "ours.wav", args.seconds + 2, OURS_HZ)
        other = tone(tmp_path / "other.wav", args.seconds + 2, OTHER_HZ)
        clicks = click_track(tmp_path / "clicks.wav", args.seconds)

        try:
            module = run([
                "pactl", "load-module", "module-null-sink", f"sink_name={SINK}",
                "sink_properties=device.description=NeuroWaveProbe",
                # По умолчанию null-sink держит ~200 мс; для спектра это много.
                "latency_msec=20",
            ])
            if module.returncode != 0:
                print(f"не удалось создать null-sink: {module.stderr.strip()}", file=sys.stderr)
                return 2
            modules.append(module.stdout.strip())

            if not args.no_loopback:
                loop = run([
                    "pactl", "load-module", "module-loopback",
                    f"source={SINK}.monitor", f"sink={default_sink}", "latency_msec=20",
                ])
                if loop.returncode == 0:
                    modules.append(loop.stdout.strip())
                    print("Loopback в системный выход включён (звук остаётся слышимым)")
                else:
                    print(f"loopback не создан: {loop.stderr.strip()}", file=sys.stderr)

            # 1) Изоляция: наша частота в наш sink, чужая — в системный.
            tap_file = tmp_path / "tap.raw"
            recorder = _record(SINK, tap_file)
            parec.append(recorder)
            time.sleep(0.4)
            mpv.append(subprocess.Popen([
                "mpv", "--no-video", "--no-terminal", "--really-quiet",
                f"--audio-device=pulse/{SINK}", "--volume=90", str(ours),
            ]))
            mpv.append(subprocess.Popen([
                "mpv", "--no-video", "--no-terminal", "--really-quiet",
                "--volume=20", str(other),
            ]))
            time.sleep(args.seconds)
            recorder.terminate()
            recorder.wait(timeout=5)

            samples = read_pcm(tap_file)
            ours_level = goertzel(samples, OURS_HZ)
            other_level = goertzel(samples, OTHER_HZ)
            print()
            print(f"Записано: {len(samples) / RATE:.2f} с, {len(samples)} отсчётов")
            print(f"Наша частота {OURS_HZ} Гц: {db(ours_level):+.1f} dBFS")
            print(f"Чужая частота {OTHER_HZ} Гц: {db(other_level):+.1f} dBFS")
            isolation = db(other_level) - db(ours_level) if ours_level else float("-inf")
            print(f"Изоляция: {isolation:+.1f} dB относительно нашего сигнала")

            # Перед замером задержки убираем непрерывные тоны: иначе они
            # маскируют опорный щелчок.
            for process in mpv:
                process.terminate()
                process.wait(timeout=5)
            mpv.clear()
            time.sleep(0.3)

            # 2) Задержка loopback: один recorder и две частоты щелчков.
            # Прямой щелчок (1 кГц) и loopback-щелчок (2 кГц) приходят в один
            # и тот же monitor, поэтому общий таймлайн и задержка Bluetooth
            # сокращаются.
            latency_ms = None
            if not args.no_loopback and len(modules) > 1:
                sys_tap = tmp_path / "sys_clicks.raw"
                rec_sys = _record(default_sink, sys_tap)
                parec.append(rec_sys)
                time.sleep(0.4)
                ref = click_track(tmp_path / "ref.wav", args.seconds, freq=1000)
                ours_clicks = click_track(tmp_path / "ours_clicks.wav", args.seconds, freq=2000)
                mpv.append(subprocess.Popen([
                    "mpv", "--no-video", "--no-terminal", "--really-quiet",
                    "--volume=25", str(ref),
                ]))
                mpv.append(subprocess.Popen([
                    "mpv", "--no-video", "--no-terminal", "--really-quiet",
                    f"--audio-device=pulse/{SINK}", "--volume=90", str(ours_clicks),
                ]))
                time.sleep(args.seconds)
                rec_sys.terminate()
                rec_sys.wait(timeout=5)
                getattr(rec_sys, "_nw_handle").close()
                captured = read_pcm(sys_tap)
                direct = envelope(captured, 1000)
                looped = envelope(captured, 2000)
                latency_ms = envelope_lag_ms(direct, looped)
                print(f"Добавленная задержка loopback: "
                      f"{'—' if latency_ms is None else f'{latency_ms:.0f} мс'} (±5 мс)")

            sink_list = run(["pactl", "list", "sinks"]).stdout
            reported = parse_latency_us(sink_list, SINK)
            print(f"Задержка sink'а по pactl: {'—' if reported is None else f'{reported / 1000:.1f} мс'}")

            print()
            ok = isolation < -40 and (latency_ms is None or latency_ms < 80)
            print("GATE: " + ("пройден" if ok else "не пройден"))
            print("Изоляция потока подтверждена" if isolation < -40
                  else "Изоляция не подтверждена: в tap попадает чужой звук")
            if latency_ms is not None and latency_ms >= 80:
                print("Задержка слишком велика для спектра в реальном времени")
            return 0 if ok else 1
        finally:
            for process in mpv + parec:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                handle = getattr(process, "_nw_handle", None)
                if handle is not None:
                    handle.close()
            for module in reversed(modules):
                run(["pactl", "unload-module", module])
            if modules:
                print(f"\nВыгружено модулей: {len(modules)}")


if __name__ == "__main__":
    sys.exit(main())
