"""Модель DSP: строгие диапазоны, миграции и безопасная сборка графа.

Главные обещания модуля:

* бессмысленное или опасное значение (NaN, inf, строка, отрицательная
  добротность, частота за пределами слышимого) не создаёт профиль вовсе;
* в граф попадают только числа из фиксированных шаблонов, поэтому имя
  профиля или id не могут стать командой mpv;
* порядок фильтров фиксирован: preamp → полосы → лимитер последним.
"""

from __future__ import annotations

import pytest

from neurowave.audio.dsp_graph import (
    build_filter_graph,
    describe_profile,
    filter_chain,
    graph_supports_limiter,
    limiter_filter,
    summarize,
)
from neurowave.audio.dsp_profiles import (
    BUILTIN_PROFILES,
    DEFAULT_PROFILE_ID,
    SCHEMA_VERSION,
    AudioProfile,
    DspError,
    DspMode,
    EqBand,
    Limiter,
    get_profile,
    resolve_profile,
)
from neurowave.config import ConfigStore, Paths, Settings


def test_profile_roundtrip_keeps_every_field() -> None:
    profile = AudioProfile(
        id="custom",
        name="Свой",
        preamp_db=-2.5,
        bands=(EqBand("peaking", 1000.0, 3.0, 1.4),),
        limiter=Limiter(enabled=True, ceiling_dbfs=-2.0, lookahead_ms=6.0, release_ms=70.0),
    )
    restored = AudioProfile.from_dict(profile.to_dict())
    assert restored == profile


@pytest.mark.parametrize(
    "field_name,value",
    [
        ("preamp_db", float("nan")),
        ("preamp_db", float("inf")),
        ("preamp_db", "громко"),
        ("preamp_db", True),
        ("preamp_db", 40.0),
        ("mode", "loud"),
        ("id", "СВОЙ ПРОФИЛЬ"),
        ("id", ""),
        ("name", "   "),
        ("name", "x" * 65),
    ],
)
def test_invalid_profile_is_rejected(field_name: str, value: object) -> None:
    base = {"id": "ok", "name": "ok"}
    base[field_name] = value
    with pytest.raises(DspError):
        AudioProfile(**base)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": "peaking", "frequency_hz": float("nan")},
        {"kind": "peaking", "frequency_hz": 5.0},
        {"kind": "peaking", "frequency_hz": 25000.0},
        {"kind": "peaking", "frequency_hz": 1000.0, "gain_db": 40.0},
        {"kind": "peaking", "frequency_hz": 1000.0, "q": 0.0},
        {"kind": "peaking", "frequency_hz": 1000.0, "q": -1.0},
        {"kind": "peaking", "frequency_hz": 1000.0, "q": float("inf")},
        {"kind": "shelf", "frequency_hz": 1000.0},
    ],
)
def test_invalid_band_is_rejected(kwargs: dict) -> None:
    with pytest.raises(DspError):
        EqBand(**kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"enabled": True, "ceiling_dbfs": 3.0},
        {"enabled": True, "ceiling_dbfs": -80.0},
        {"enabled": True, "lookahead_ms": 0.0},
        {"enabled": True, "lookahead_ms": 100.0},
        {"enabled": True, "release_ms": 1.0},
        {"enabled": "yes"},
        {"enabled": True, "ceiling_dbfs": float("nan")},
    ],
)
def test_invalid_limiter_is_rejected(kwargs: dict) -> None:
    with pytest.raises(DspError):
        Limiter(**kwargs)


def test_duplicate_band_frequencies_are_rejected() -> None:
    with pytest.raises(DspError):
        AudioProfile(
            id="dup",
            name="Дубль",
            bands=(
                EqBand("peaking", 1000.0, 1.0, 1.0),
                EqBand("peaking", 1000.0, -1.0, 1.0),
            ),
        )


def test_too_many_bands_are_rejected() -> None:
    bands = tuple(EqBand("peaking", 100.0 + index * 100.0, 0.5, 1.0) for index in range(17))
    with pytest.raises(DspError):
        AudioProfile(id="many", name="Много", bands=bands)


def test_old_profile_without_schema_version_still_reads() -> None:
    legacy = {
        "id": "legacy",
        "name": "Старый",
        "mode": "enhanced",
        "preamp_db": -1.0,
        "bands": [{"kind": "peaking", "frequency_hz": 1000.0, "gain_db": 2.0, "q": 1.0}],
        "limiter": {"enabled": False},
    }
    profile = AudioProfile.from_dict(legacy)
    assert profile.schema_version == 1 and profile.bands[0].gain_db == 2.0


def test_newer_schema_is_refused_instead_of_guessed() -> None:
    data = AudioProfile(id="future", name="Будущее").to_dict()
    data["schema_version"] = SCHEMA_VERSION + 1
    with pytest.raises(DspError) as exc:
        AudioProfile.from_dict(data)
    assert "новее" in str(exc.value)


def test_pure_profile_produces_no_filters() -> None:
    assert build_filter_graph(BUILTIN_PROFILES["flat"]) is None
    assert filter_chain(BUILTIN_PROFILES["flat"]) == ()


def test_graph_order_is_preamp_bands_limiter() -> None:
    graph = build_filter_graph(BUILTIN_PROFILES["bluetooth"])
    assert graph is not None
    assert graph.startswith("lavfi=[")
    order = [graph.index(token) for token in ("volume=volume=", "lowshelf=", "equalizer=", "alimiter=")]
    assert order == sorted(order), "лимитер обязан быть последним"
    assert graph_supports_limiter(graph)


def test_graph_without_limiter_is_possible_for_experiments() -> None:
    graph = build_filter_graph(BUILTIN_PROFILES["bluetooth"], include_limiter=False)
    assert graph is not None and "alimiter" not in graph
    assert graph_supports_limiter(graph) is False


def test_graph_contains_only_numbers_from_profile() -> None:
    """Имя и id не попадают в граф: иначе строка стала бы командой mpv."""
    hostile = AudioProfile(
        id="hostile",
        name=",quit\nset_property volume 130",
        preamp_db=-1.0,
        bands=(EqBand("peaking", 1000.0, 1.0, 1.0),),
    )
    graph = build_filter_graph(hostile)
    assert graph is not None
    assert "quit" not in graph and "set_property" not in graph and "\n" not in graph
    assert "hostile" not in graph


def test_graph_is_deterministic() -> None:
    profile = BUILTIN_PROFILES["night"]
    assert build_filter_graph(profile) == build_filter_graph(profile)


def test_limiter_linear_conversion_and_no_auto_level() -> None:
    graph = limiter_filter(Limiter(enabled=True, ceiling_dbfs=-6.0206, lookahead_ms=5.0, release_ms=50.0))
    assert "limit=0.5" in graph  # -6.02 dBFS = 0.5 линейно (приблизительно)
    assert "level=0" in graph, "автовыравнивание уровня маскирует клиппинг"


def test_builtins_are_valid_and_buildable() -> None:
    assert DEFAULT_PROFILE_ID in BUILTIN_PROFILES
    for profile_id, profile in BUILTIN_PROFILES.items():
        assert profile.id == profile_id
        AudioProfile.from_dict(profile.to_dict())  # сериализация без потерь
        build_filter_graph(profile)  # сборка не падает


def test_unknown_profile_is_an_error_but_settings_fall_back() -> None:
    with pytest.raises(DspError):
        get_profile("missing")
    assert resolve_profile("missing").id == DEFAULT_PROFILE_ID
    assert Settings.from_dict({"dsp_profile": "missing"}).dsp_profile == DEFAULT_PROFILE_ID


def test_profile_selection_is_persisted(tmp_path) -> None:
    paths = Paths(config_dir=tmp_path / "config", data_dir=tmp_path / "data", cache_dir=tmp_path / "cache")
    settings = Settings(dsp_profile="bluetooth")
    ConfigStore(paths).save(settings)
    assert ConfigStore(paths).load().dsp_profile == "bluetooth"


def test_describe_and_summarize_are_safe_for_ui() -> None:
    text = describe_profile(BUILTIN_PROFILES["night"])
    assert "Ночь" in text and "лимитер" in text
    summary = summarize(BUILTIN_PROFILES["night"])
    assert summary["bands"] == 2 and summary["limiter"] is True
    assert summarize(BUILTIN_PROFILES["flat"])["graph"] is None
    assert DspMode.PURE.value == "pure"
