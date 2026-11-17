"""Опциональный транскодер через внешний ffmpeg/ffprobe.

Никакого shell, URL и автозагрузки: только локальный файл, argv-список,
таймаут и проверка результата. Без ffmpeg режим Original продолжает работать,
а запрос MP3 честно отключается на уровне writer.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

TARGET_FORMATS = frozenset({"mp3", "flac"})
CODECS = {"mp3": "libmp3lame", "flac": "flac"}


class TranscoderError(Exception):
    pass


class TranscoderUnavailable(TranscoderError):
    pass


@dataclass(frozen=True)
class MediaInfo:
    media_format: str
    duration_ms: int = 0


class FfmpegTranscoder:
    def __init__(
        self,
        ffmpeg: str | None = None,
        ffprobe: str | None = None,
        *,
        timeout: float = 180.0,
        runner: Callable[..., Any] | None = None,
    ) -> None:
        self._ffmpeg = ffmpeg if ffmpeg is not None else shutil.which("ffmpeg")
        self._ffprobe = ffprobe if ffprobe is not None else shutil.which("ffprobe")
        self._timeout = float(timeout)
        self._runner = runner or subprocess.run

    @property
    def available(self) -> bool:
        return bool(self._ffmpeg)

    def transcode(
        self,
        source: Path,
        destination: Path,
        *,
        target_format: str = "mp3",
        bitrate_kbps: int = 320,
    ) -> None:
        if not self._ffmpeg:
            raise TranscoderUnavailable("ffmpeg не установлен")
        if target_format not in TARGET_FORMATS:
            raise TranscoderError(f"Неподдерживаемый формат: {target_format}")
        command = [
            str(self._ffmpeg),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-n",
            "-i",
            str(source),
            "-vn",
            "-map_metadata",
            "-1",
            "-codec:a",
            CODECS[target_format],
        ]
        if target_format == "mp3":
            command += ["-b:a", f"{max(1, int(bitrate_kbps))}k"]
        # Временный файл без расширения: формат задаём явно, без догадок ffmpeg.
        command += ["-f", target_format, str(destination)]
        try:
            result = self._runner(
                command,
                shell=False,
                timeout=self._timeout,
                capture_output=True,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise TranscoderError(f"ffmpeg не запустился: {error}") from error
        if getattr(result, "returncode", 1) != 0 or not destination.is_file():
            message = getattr(result, "stderr", b"") or b""
            if isinstance(message, bytes):
                message = message.decode("utf-8", errors="replace")
            raise TranscoderError(f"ffmpeg завершился с ошибкой: {message[:300]}")

    def probe(self, path: Path) -> MediaInfo:
        if not self._ffprobe:
            raise TranscoderUnavailable("ffprobe не установлен")
        command = [
            str(self._ffprobe),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            str(path),
        ]
        try:
            result = self._runner(
                command,
                shell=False,
                timeout=self._timeout,
                capture_output=True,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise TranscoderError(f"ffprobe не запустился: {error}") from error
        if getattr(result, "returncode", 1) != 0:
            raise TranscoderError("ffprobe не смог прочитать файл")
        raw = getattr(result, "stdout", b"") or b""
        try:
            payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
            fmt = payload.get("format") or {}
            names = str(fmt.get("format_name") or "")
            duration = float(fmt.get("duration") or 0)
        except (ValueError, TypeError, AttributeError) as error:
            raise TranscoderError("ffprobe вернул некорректный JSON") from error
        media_format = names.split(",")[0].strip().lower()
        return MediaInfo(media_format=media_format, duration_ms=max(0, int(duration * 1000)))


def command_preview(command: Sequence[str]) -> str:
    """Диагностика без секретов: только имена аргументов, не значения путей."""

    return " ".join(part if not part.startswith("/") else "<path>" for part in command)
