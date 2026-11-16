"""Пути XDG, настройки и их атомарное сохранение."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Mapping

from .domain import RepeatMode
from .errors import ConfigError

APP_NAME = "neurowave"
DEFAULT_VOLUME = 80
MAX_VOLUME = 130  # верхняя граница, которую принимает backend
# Предел пользовательской цифровой громкости, пока лимитер не подтверждён
# измерениями: выше 100 % возможен клиппинг без защиты.
SAFE_VOLUME = 100
DEFAULT_CACHE_QUOTA_GB = 2.0
DEFAULT_CACHE_TTL_DAYS = 7


@dataclass(frozen=True)
class Paths:
    """Каталоги приложения. ``NEUROWAVE_HOME`` подменяет всё дерево (тесты)."""

    config_dir: Path
    data_dir: Path
    cache_dir: Path

    @classmethod
    def resolve(cls, env: Mapping[str, str] | None = None) -> "Paths":
        env = os.environ if env is None else env
        override = env.get("NEUROWAVE_HOME")
        if override:
            root = Path(override).expanduser()
            return cls(root / "config", root / "data", root / "cache")

        def xdg(variable: str, default: str) -> Path:
            raw = env.get(variable)
            base = Path(raw).expanduser() if raw else Path.home() / default
            return base

        return cls(
            xdg("XDG_CONFIG_HOME", ".config") / APP_NAME,
            xdg("XDG_DATA_HOME", ".local/share") / APP_NAME,
            xdg("XDG_CACHE_HOME", ".cache") / APP_NAME,
        )

    def ensure(self) -> "Paths":
        for directory in (self.config_dir, self.data_dir, self.cache_dir):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return self

    @property
    def settings_file(self) -> Path:
        return self.config_dir / "config.json"

    @property
    def database_file(self) -> Path:
        return self.data_dir / "state.db"

    @property
    def token_file(self) -> Path:
        return self.data_dir / "token"


@dataclass
class Settings:
    volume: int = DEFAULT_VOLUME
    cache_quota_gb: float = DEFAULT_CACHE_QUOTA_GB
    cache_ttl_days: int = DEFAULT_CACHE_TTL_DAYS
    repeat: str = RepeatMode.OFF.value
    shuffle: bool = False
    audio_backend: str = "mpv"
    provider: str = "yandex"
    local_music_dir: str = ""
    reduced_motion: bool = False
    language: str = "ru"
    ai_enabled: bool = False
    autoplay_wave_feedback: bool = True
    #: id выбранного профиля DSP; сами профили живут в audio/dsp_profiles.py.
    dsp_profile: str = "flat"
    #: id аудиовыхода mpv; пусто — авто (системный выбор PipeWire).
    audio_output: str = ""
    #: Пользовательские профили звука, уже проверенные при чтении.
    custom_profiles: list[dict[str, Any]] = field(default_factory=list)
    # Ставится в True только когда в цепи есть проверенный лимитер (PR-04).
    # До этого громкость выше SAFE_VOLUME не применяется.
    boost_verified: bool = False

    def clamp_volume(self, value: int) -> int:
        """Ограничить значение рамками шкалы, сохранив желание пользователя."""
        return max(0, min(MAX_VOLUME, int(value)))

    def effective_volume(self) -> int:
        """Фактическая громкость: без проверенного лимитера — не выше 100 %."""
        limit = MAX_VOLUME if self.boost_verified else SAFE_VOLUME
        return max(0, min(limit, int(self.volume)))

    def volume_limited(self) -> bool:
        """Запрошено больше, чем безопасно применить сейчас."""
        return self.effective_volume() != int(self.volume)

    @property
    def cache_quota_bytes(self) -> int:
        return int(max(0.0, self.cache_quota_gb) * 1024**3)

    @property
    def cache_ttl_seconds(self) -> int:
        return max(0, int(self.cache_ttl_days)) * 24 * 3600

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Settings":
        known = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in known}
        settings = cls(**filtered)
        settings.volume = settings.clamp_volume(settings.volume)
        settings.repeat = RepeatMode.parse(settings.repeat).value
        # Ленивый импорт: audio импортирует config (MAX_VOLUME), поэтому
        # обычный импорт на уровне модуля дал бы цикл.
        from .audio.dsp_profiles import AudioProfile, DspError

        # Битые и устаревшие профили отбрасываются по одному: один испорченный
        # JSON не должен уносить остальные настройки.
        valid: list[dict[str, Any]] = []
        for item in settings.custom_profiles or []:
            try:
                valid.append(AudioProfile.from_dict(item).to_dict())
            except (DspError, TypeError, ValueError):
                continue
        settings.custom_profiles = valid
        from .audio.profile_store import ProfileRegistry

        settings.dsp_profile = ProfileRegistry.from_settings(settings).resolve(settings.dsp_profile).id
        return settings


class ConfigStore:
    """Чтение и запись ``config.json`` без частично записанных файлов."""

    def __init__(self, paths: Paths):
        self._paths = paths

    def load(self) -> Settings:
        file = self._paths.settings_file
        if not file.exists():
            return Settings()
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"Не удалось прочитать {file}: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"Ожидался объект JSON в {file}")
        return Settings.from_dict(data)

    def save(self, settings: Settings) -> None:
        self._paths.ensure()
        atomic_write_json(self._paths.settings_file, settings.to_dict())


def atomic_write_json(path: Path, data: Mapping[str, Any]) -> None:
    """Записать JSON атомарно, с правами только для владельца."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
