"""Превращение проверенного профиля в граф lavfi.

Никакой пользовательской строки здесь нет: только числа из
:mod:`neurowave.audio.dsp_profiles` подставляются в фиксированные шаблоны.
Порядок жёсткий — preamp, полосы, лимитер последним, иначе лимитер не
защищает от усиления, которое идёт после него.
"""

from __future__ import annotations

from typing import Any

from .dsp_profiles import AudioProfile, DspMode, EqBand, Limiter

#: Соответствие типа полосы фильтру FFmpeg.
_BAND_FILTERS = {
    "peaking": "equalizer",
    "lowshelf": "lowshelf",
    "highshelf": "highshelf",
}


def _num(value: float) -> str:
    """Число в граф: без хвостовых нулей, всегда с точкой для дробей."""
    text = f"{float(value):.4f}".rstrip("0").rstrip(".")
    return text if text not in {"", "-0"} else "0"


def _dbfs_to_linear(dbfs: float) -> float:
    return 10.0 ** (dbfs / 20.0)


def band_filter(band: EqBand) -> str:
    name = _BAND_FILTERS[band.kind]
    return (
        f"{name}=f={_num(band.frequency_hz)}"
        f":t=q:w={_num(band.q)}:g={_num(band.gain_db)}"
    )


def limiter_filter(limiter: Limiter) -> str:
    """`alimiter` без автовыравнивания уровня: `level=0` обязателен.

    Иначе FFmpeg сам поднимает выход до потолка, и «−4 дБ preamp» перестаёт
    означать запас — именно так незаметно появляется клиппинг.
    """
    return (
        f"alimiter=limit={_num(_dbfs_to_linear(limiter.ceiling_dbfs))}"
        f":attack={_num(limiter.lookahead_ms)}"
        f":release={_num(limiter.release_ms)}"
        ":level=0"
    )


def filter_chain(profile: AudioProfile, *, include_limiter: bool = True) -> tuple[str, ...]:
    """Список фильтров по порядку; пустой кортеж — обрабатывать нечего."""
    if profile.mode is DspMode.PURE:
        return ()
    filters: list[str] = []
    if profile.preamp_db != 0.0:
        filters.append(f"volume=volume={_num(profile.preamp_db)}dB:precision=float")
    filters.extend(band_filter(band) for band in profile.bands)
    if include_limiter and profile.limiter.enabled:
        filters.append(limiter_filter(profile.limiter))
    return tuple(filters)


def build_filter_graph(profile: AudioProfile, *, include_limiter: bool = True) -> str | None:
    """Строка для `af set` или None, если профиль ничего не делает."""
    filters = filter_chain(profile, include_limiter=include_limiter)
    if not filters:
        return None
    return "lavfi=[" + ",".join(filters) + "]"


def describe_profile(profile: AudioProfile) -> str:
    """Короткое человеческое описание для CLI/UI и логов."""
    if profile.mode is DspMode.PURE:
        return f"{profile.name} · без обработки"
    parts: list[str] = []
    if profile.preamp_db:
        parts.append(f"preamp {profile.preamp_db:+g} дБ")
    if profile.bands:
        parts.append(f"{len(profile.bands)} полос")
    if profile.limiter.enabled:
        parts.append(f"лимитер {profile.limiter.ceiling_dbfs:g} dBFS")
    return f"{profile.name} · " + (" · ".join(parts) if parts else "пусто")


def graph_supports_limiter(graph: str | None) -> bool:
    """Есть ли в готовом графе лимитер (для статуса «безопасно/нет»)."""
    return bool(graph and "alimiter=" in graph)


def summarize(profile: AudioProfile) -> dict[str, Any]:
    """Данные для UI без раскрытия внутреннего графа."""
    return {
        "id": profile.id,
        "name": profile.name,
        "mode": profile.mode.value,
        "preamp_db": profile.preamp_db,
        "bands": len(profile.bands),
        "limiter": profile.limiter.enabled,
        "ceiling_dbfs": profile.limiter.ceiling_dbfs if profile.limiter.enabled else None,
        "graph": build_filter_graph(profile),
        "description": describe_profile(profile),
    }
