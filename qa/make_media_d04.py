"""QA-VERIFY-02: синтетическая библиотека для приёмки D-04.

Кроме стандартной библиотеки PR #32 генерируется 24 файла с уникальными
именами и частотами: стенду нужен «разный тестовый файл» на каждой итерации
стресс-прогона (сценарий H). Файлы синтетические (синусоиды), без чужих
прав и персональных данных.

Запуск: python qa/make_media_d04.py [<каталог>]
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

#: (суффикс имени, частота, длительность, формат, кодек, частота дискретизации)
STRESS_FILES = [
    (f"qa_stress_{i:02d}_{300 + 13 * i}hz", 300 + 13 * i, 8 + (i % 5) * 2,
     ("mp3", "wav", "flac")[i % 3],
     ("libmp3lame", "pcm_s16le", "flac")[i % 3],
     (44100, 48000)[i % 2])
    for i in range(24)
]


def build(target: Path) -> list[Path]:
    target.mkdir(parents=True, exist_ok=True)
    made: list[Path] = []
    for name, freq, dur, fmt, codec, rate in STRESS_FILES:
        path = target / f"{name}.{fmt}"
        command = [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={dur}:sample_rate={rate}",
            "-af", "volume=0.25",
            "-metadata", f"title=QA stress {freq} Hz",
            "-metadata", "artist=NeuroWave QA",
            "-metadata", "album=QA-VERIFY-02 library",
            str(path),
        ]
        if codec == "libmp3lame":
            command.insert(len(command) - 1, "-b:a")
            command.insert(len(command) - 1, "128k")
        subprocess.run(command, check=True)
        made.append(path)
    return made


def main() -> int:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        os.environ.get("QA_MEDIA_DIR") or (Path(__file__).resolve().parents[2] / "stand" / "media-d04"))
    made = build(target)
    print(f"создано файлов: {len(made)} в {target}")
    for path in made[:5]:
        print(" ", path.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
