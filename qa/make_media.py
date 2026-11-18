"""Сгенерировать разрешённую тестовую аудиобиблиотеку для WO-03.

Файлы синтетические (синусоиды разных частот), без чужих прав и без
персональных данных. Каждая частота уникальна — по спектру вывода можно
понять, какой файл реально играет.

Запуск: python qa/make_media.py [<каталог>]
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

#: Каталог с синтетической библиотекой: задаётся окружением (QA_MEDIA_DIR),
#: путь по умолчанию — относительный, чтобы в репозитории не было домашних путей.
DEFAULT_DIR = Path(os.environ.get("QA_MEDIA_DIR") or (Path(__file__).resolve().parents[2] / "stand" / "media"))

# (имя, частота, длительность, формат, кодек, частота дискретизации, теги)
FILES = [
    ("qa_tone_a440_128k.mp3", 440, 30, "mp3", "libmp3lame", 44100, "128k"),
    ("qa_tone_c523_320k.mp3", 523, 45, "mp3", "libmp3lame", 44100, "320k"),
    ("qa_tone_e659_lossless.flac", 659, 3, "flac", "flac", 44100, "0"),
    ("qa_tone_g349_44k.wav", 349, 20, "wav", "pcm_s16le", 44100, "0"),
    ("qa_tone_a880_48k.wav", 880, 25, "wav", "pcm_s16le", 48000, "0"),
    ("qa_tone_b220_short.mp3", 220, 8, "mp3", "libmp3lame", 44100, "64k"),
]


def build(target: Path) -> list[Path]:
    target.mkdir(parents=True, exist_ok=True)
    made: list[Path] = []
    for name, freq, dur, fmt, codec, rate, bitrate in FILES:
        path = target / name
        command = [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={dur}:sample_rate={rate}",
            "-af", "volume=0.25",
            "-metadata", f"title=QA tone {freq} Hz",
            "-metadata", f"artist=NeuroWave QA",
            "-metadata", f"album=WO-03 test library",
            "-c:a", codec,
        ]
        if fmt == "mp3":
            command += ["-b:a", bitrate, "-ac", "1"]
        else:
            command += ["-ar", str(rate)]
        command.append(str(path))
        subprocess.run(command, check=True)
        made.append(path)
    made.append(_long_title_file(target))
    return made


def _long_title_file(target: Path) -> Path:
    """Файл с очень длинным именем и без тегов: заголовок в UI будет длинным."""
    long_name = (
        "QA экран длинное название трека для проверки переполнения строки заголовка "
        "окна NeuroWave без переносов и обрезания символов"
    )
    path = target / f"{long_name}.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "sine=frequency=300:duration=6:sample_rate=44100",
         "-af", "volume=0.25", "-map_metadata", "-1", "-c:a", "pcm_s16le", str(path)],
        check=True,
    )
    return path


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DIR
    for item in build(out):
        print(f"{item.name}\t{item.stat().st_size} Б")
