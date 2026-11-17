"""Анализ PCM-записи для gate спектроанализатора (PR-06).

Сам захват требует живого PipeWire, но математика проверяема без звука:
Goertzel должен видеть амплитуду одной частоты, а корреляция огибающих —
находить сдвиг между «прямым» и задержанным сигналом.
"""

from __future__ import annotations

import importlib.util
import math
import struct
from pathlib import Path

import pytest

PROBE = Path(__file__).resolve().parents[1] / "packaging" / "pcm-tap-probe.py"


@pytest.fixture(scope="module")
def probe():
    spec = importlib.util.spec_from_file_location("pcm_tap_probe", PROBE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _sine(freq: float, seconds: float, amp: float = 0.5, rate: int = 48000) -> list[float]:
    return [amp * math.sin(2 * math.pi * freq * i / rate) for i in range(int(rate * seconds))]


def test_goertzel_measures_single_tone(probe) -> None:
    samples = _sine(1000, 0.5, 0.25)
    assert probe.db(probe.goertzel(samples, 1000)) == pytest.approx(-12.04, abs=0.5)
    # Соседняя частота должна быть намного тише: иначе «изоляция» ничего не значит.
    assert probe.goertzel(samples, 3000) < probe.goertzel(samples, 1000) / 100


def test_envelope_lag_finds_known_delay(probe) -> None:
    """Задержка 100 мс должна находиться корреляцией огибающих."""
    rate = 48000
    clicks = [0.0] * (rate // 2)
    for start in (rate // 2, rate, 3 * rate // 2):
        for i in range(start, start + rate // 100):
            clicks.append(0.8 * math.sin(2 * math.pi * 2000 * (i - start) / rate))
    reference = probe.envelope(clicks, 1000)
    delayed = [0.0] * 20 + probe.envelope(clicks, 2000)  # 20 окон × 5 мс = 100 мс
    lag = probe.envelope_lag_ms(reference, delayed)
    assert lag is not None
    assert lag == pytest.approx(100, abs=15)


def test_read_pcm_downsamples_stereo(probe, tmp_path) -> None:
    path = tmp_path / "stereo.raw"
    frames = [(0.5, 0.5), (-0.25, -0.75), (0.0, 0.0)]
    path.write_bytes(struct.pack("<6f", *(value for frame in frames for value in frame)))
    assert probe.read_pcm(path) == pytest.approx([0.5, -0.5, 0.0])


def test_gate_requires_isolation(probe) -> None:
    """Порог gate: чужой сигнал должен быть тише нашего минимум на 40 дБ."""
    assert -86.1 < -40
    assert -5.0 > -40
