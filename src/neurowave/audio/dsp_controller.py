"""Транзакционное применение профиля DSP к работающему mpv.

Смысл модуля — не дать интерфейсу соврать. «Команда отправлена» здесь не
значит «фильтр работает»: после каждого применения граф читается обратно из
mpv и сравнивается с ожидаемым. Если mpv отказал или применил другое, прежний
подтверждённый профиль возвращается, а если не удаётся и это — снимаются все
фильтры, чтобы вместо «Enhanced» не осталось неизвестное состояние.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..config import Settings
from ..errors import PlayerError
from .profile_store import ProfileRegistry
from .backend import AudioBackend
from .dsp_graph import build_filter_graph
from .dsp_profiles import (
    BUILTIN_PROFILES,
    DEFAULT_PROFILE_ID,
    AudioProfile,
    DspError,
    DspMode,
    get_profile,
    resolve_profile,
)

Listener = Callable[[str, dict[str, Any]], None]

_LAVFI_PREFIX = "lavfi=["
_LAVFI_SUFFIX = "]"


def _graph_body(graph: str | None) -> str | None:
    """Внутренняя часть графа без обёртки `lavfi=[…]`."""
    if not graph:
        return None
    text = graph.strip()
    if text.startswith(_LAVFI_PREFIX) and text.endswith(_LAVFI_SUFFIX):
        return text[len(_LAVFI_PREFIX) : -len(_LAVFI_SUFFIX)]
    return text


def _normalized(text: str | None) -> str | None:
    """Сравнение без пробелов и регистра: mpv переформатирует числа по-своему."""
    return None if text is None else "".join(text.split()).lower()


@dataclass(frozen=True)
class DspStatus:
    """Что реально стоит в цепи, а не что мы попросили."""

    profile: AudioProfile | None = None
    mode: DspMode = DspMode.PURE
    graph: str | None = None
    confirmed: bool = False
    fallback: bool = False
    requested: str | None = None
    reason: str | None = None

    @property
    def name(self) -> str:
        return self.profile.name if self.profile else "нет профиля"

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile.id if self.profile else None,
            "name": self.name,
            "mode": self.mode.value,
            "graph": self.graph,
            "confirmed": self.confirmed,
            "fallback": self.fallback,
            "requested": self.requested,
            "reason": self.reason,
        }


class DspController:
    """Единственное место, где профиль превращается в состояние mpv."""

    def __init__(
        self,
        backend: AudioBackend,
        settings: Settings | None = None,
        listener: Listener | None = None,
        registry: ProfileRegistry | None = None,
    ):
        self._backend = backend
        self.settings = settings
        self.registry = registry or ProfileRegistry.from_settings(settings or Settings())
        self._listener = listener
        self._profile: AudioProfile | None = None
        self._graph: str | None = None
        self._confirmed = False
        self._verified = False  # граф сверен с фактическим ответом mpv
        self._last_enhanced: str = DEFAULT_PROFILE_ID
        self._last_attempt: tuple[str | None, str | None] = (None, None)
        self.last_error: str | None = None
        self.status = DspStatus()

    # --- состояние ----------------------------------------------------
    @property
    def profile(self) -> AudioProfile | None:
        return self._profile

    @property
    def verified(self) -> bool:
        return self._verified

    def invalidate(self) -> None:
        """Забыть подтверждение: mpv перезапущен и фильтров в нём нет."""
        self._verified = False
        self._last_attempt = (None, None)
        self.status = DspStatus()
        self._graph = None

    # --- применение ---------------------------------------------------
    def apply(self, profile: AudioProfile | str, *, persist: bool = True) -> DspStatus:
        """Применить профиль; при отказе — откат, а не ложный успех."""
        if isinstance(profile, str):
            profile = self.registry.get(profile) or get_profile(profile)
        previous, previous_graph = self._profile, self._graph
        try:
            graph = self._push(profile)
        except (PlayerError, DspError) as exc:
            self.last_error = str(exc)
            restored, restored_ok = self._restore(previous, previous_graph)
            status = DspStatus(
                profile=restored,
                mode=restored.mode if restored else DspMode.PURE,
                graph=None if restored is None else self._graph,
                confirmed=restored_ok,
                fallback=True,
                requested=profile.id,
                # При неудачном откате причина должна включать и его: иначе
                # пользователь видит только первую ошибку и не понимает, что
                # цепь осталась в неизвестном состоянии.
                reason=str(exc) if restored_ok else (self.last_error or str(exc)),
            )
            self.status = status
            self._notify("dsp_failed", status)
            return status

        self._profile, self._graph, self._confirmed, self._verified = profile, graph, True, True
        if profile.mode is DspMode.ENHANCED and not profile.is_bypass():
            self._last_enhanced = profile.id
        if persist and self.settings is not None:
            self.settings.dsp_profile = profile.id
        self.status = DspStatus(
            profile=profile,
            mode=profile.mode,
            graph=graph,
            confirmed=True,
        )
        self._notify("dsp_applied", self.status)
        return self.status

    def set_mode(self, mode: DspMode | str) -> DspStatus:
        """Pure снимает наши фильтры; Enhanced возвращает последний непустой."""
        mode = DspMode.parse(mode)
        if mode is DspMode.PURE:
            return self.apply(BUILTIN_PROFILES[DEFAULT_PROFILE_ID], persist=False)
        profile_id = self.settings.dsp_profile if self.settings is not None else DEFAULT_PROFILE_ID
        candidate = self.registry.get(profile_id) or resolve_profile(profile_id)
        if candidate.mode is DspMode.PURE:
            candidate = self.registry.get(self._last_enhanced) or resolve_profile(self._last_enhanced)
        if candidate.mode is DspMode.PURE:
            candidate = BUILTIN_PROFILES["hifi"]
        return self.apply(candidate)

    def sync(self) -> DspStatus:
        """Привести mpv к сохранённому профилю. Повторный вызов не шлёт IPC."""
        wanted = self.settings.dsp_profile if self.settings is not None else DEFAULT_PROFILE_ID
        profile = self.registry.get(wanted) or resolve_profile(wanted)
        desired = None if profile.mode is DspMode.PURE else build_filter_graph(profile)
        signature = (profile.id, _normalized(desired))
        if self._verified and _normalized(self._graph) == _normalized(desired):
            return self.status
        # Неудачную попытку не повторяем на каждом треке: иначе сломанный
        # граф будет дёргать IPC перед каждым воспроизведением.
        if not self._verified and signature == self._last_attempt:
            return self.status
        self._last_attempt = signature
        return self.apply(profile, persist=False)

    # --- внутреннее ---------------------------------------------------
    def _push(self, profile: AudioProfile) -> str | None:
        """Отправить граф и сверить с фактическим состоянием."""
        graph = None if profile.mode is DspMode.PURE else build_filter_graph(profile)
        if graph is None:
            self._backend.set_audio_filter("")
            actual = self._backend.audio_filter()
            if actual is not None:
                raise DspError(f"mpv не очистил фильтры: {actual!r}")
            return None
        self._backend.set_audio_filter(graph)
        actual = self._backend.audio_filter()
        # Сравниваем без обёртки: один backend вернёт `lavfi=[…]`, другой —
        # только внутреннюю часть, как делает mpv.
        if _normalized(_graph_body(actual)) != _normalized(_graph_body(graph)):
            raise DspError(f"mpv применил другой граф: {actual!r} вместо {_graph_body(graph)!r}")
        return graph

    def _restore(
        self,
        profile: AudioProfile | None,
        graph: str | None,
    ) -> tuple[AudioProfile | None, bool]:
        """Вернуть прежний профиль; в крайнем случае — снять фильтры."""
        candidates: list[AudioProfile] = []
        if profile is not None:
            candidates.append(profile)
        candidates.append(BUILTIN_PROFILES[DEFAULT_PROFILE_ID])
        for candidate in candidates:
            try:
                self._push(candidate)
            except (PlayerError, DspError) as exc:
                self.last_error = f"{self.last_error}; откат не удался: {exc}"
                continue
            self._profile = candidate
            self._graph = None if candidate.mode is DspMode.PURE else build_filter_graph(candidate)
            self._confirmed = True
            self._verified = True
            return candidate, True
        # Ничего не удалось: честно помечаем состояние неподтверждённым.
        self._confirmed = False
        self._verified = False
        self._graph = None
        return None, False

    def _notify(self, event: str, status: DspStatus) -> None:
        if self._listener is None:
            return
        try:
            self._listener(event, {"status": status.to_dict()})
        except Exception:  # слушатель не должен ломать аудиотракт
            pass
