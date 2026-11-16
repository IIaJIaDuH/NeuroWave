"""Пользовательские профили: что можно сохранить, а что отвергается.

Встроенные пресеты не редактируются, битые записи из настроек не применяются,
а один испорченный профиль не должен уносить остальные настройки.
"""

from __future__ import annotations

import pytest

from neurowave.audio.dsp_profiles import AudioProfile, DspError, EqBand, Limiter
from neurowave.audio.profile_store import ProfileRegistry, ProfileStoreError
from neurowave.config import ConfigStore, Paths, Settings


def _profile(profile_id: str = "mine", **overrides) -> AudioProfile:
    payload = {
        "id": profile_id,
        "name": "Мой профиль",
        "preamp_db": -3.0,
        "bands": (EqBand("peaking", 1000.0, 2.0, 1.0),),
        "limiter": Limiter(enabled=True, ceiling_dbfs=-3.0),
    }
    payload.update(overrides)
    return AudioProfile(**payload)


def test_settings_drop_invalid_profiles_but_keep_valid() -> None:
    settings = Settings.from_dict(
        {
            "custom_profiles": [
                _profile("good").to_dict(),
                {"id": "bad id!", "name": "плохой"},
                {"нет": "полей"},
                _profile("good2", name="Второй").to_dict(),
            ]
        }
    )
    assert [item["id"] for item in settings.custom_profiles] == ["good", "good2"]


def test_registry_reports_skipped_and_collisions() -> None:
    settings = Settings(
        custom_profiles=[
            _profile("mine").to_dict(),
            _profile("hifi").to_dict(),  # совпадает со встроенным
        ]
    )
    registry = ProfileRegistry.from_settings(settings)
    assert "mine" in registry.custom and "hifi" not in registry.custom
    assert any("совпадает" in error for error in registry.errors)


def test_builtin_profiles_are_not_editable() -> None:
    registry = ProfileRegistry()
    with pytest.raises(ProfileStoreError):
        registry.add(_profile("hifi"))
    with pytest.raises(ProfileStoreError):
        registry.update(_profile("hifi"))
    with pytest.raises(ProfileStoreError):
        registry.remove("hifi")
    assert registry.is_builtin("hifi") is True


def test_custom_profile_lifecycle() -> None:
    registry = ProfileRegistry()
    registry.add(_profile("mine"))
    with pytest.raises(ProfileStoreError):
        registry.add(_profile("mine"))
    assert registry.get("mine") is not None

    registry.update(_profile("mine", preamp_db=-6.0))
    assert registry.get("mine").preamp_db == -6.0
    assert registry.remove("mine") is True
    assert registry.get("mine") is None
    assert registry.remove("mine") is False


def test_unknown_id_resolves_to_safe_default() -> None:
    registry = ProfileRegistry()
    assert registry.resolve("nope").id == "flat"
    assert registry.get("nope") is None


def test_profiles_are_persisted_and_reloaded(tmp_path) -> None:
    paths = Paths(config_dir=tmp_path / "c", data_dir=tmp_path / "d", cache_dir=tmp_path / "k")
    settings = Settings()
    registry = ProfileRegistry.from_settings(settings)
    registry.add(_profile("mine"))
    registry.save(settings)
    ConfigStore(paths).save(settings)

    loaded = ConfigStore(paths).load()
    again = ProfileRegistry.from_settings(loaded)
    assert again.get("mine") is not None
    assert again.get("mine").limiter.ceiling_dbfs == -3.0


def test_describe_marks_builtin_and_custom() -> None:
    settings = Settings(custom_profiles=[_profile("mine").to_dict()])
    registry = ProfileRegistry.from_settings(settings)
    described = {profile_id: builtin for profile_id, _name, builtin in registry.describe()}
    assert described["flat"] is True
    assert described["mine"] is False


def test_controller_uses_registry_for_custom_profile() -> None:
    from neurowave.audio import NullBackend
    from neurowave.audio.dsp_controller import DspController

    settings = Settings(custom_profiles=[_profile("mine").to_dict()], dsp_profile="mine")
    backend = NullBackend()
    controller = DspController(
        backend, settings=settings, registry=ProfileRegistry.from_settings(settings)
    )
    controller.sync()
    assert controller.status.profile is not None
    assert controller.status.profile.id == "mine"
    assert "g=2" in (backend.audio_filter_value or "")


def test_invalid_custom_profile_cannot_be_built() -> None:
    with pytest.raises(DspError):
        _profile("mine", bands=(EqBand("peaking", 1000.0, 2.0, 1.0), EqBand("peaking", 1000.0, 1.0, 1.0)))


def test_cli_lists_and_selects_custom_profile(tmp_path, capsys) -> None:
    from neurowave.cli import main

    settings = Settings()
    registry = ProfileRegistry.from_settings(settings)
    registry.add(_profile("mine"))
    registry.save(settings)
    ConfigStore(Paths.resolve({"NEUROWAVE_HOME": str(tmp_path)})).save(settings)

    assert main(["--home", str(tmp_path), "sound", "--profiles"]) == 0
    text = capsys.readouterr().out
    assert "Мой профиль" in text and "(свой)" in text and "(встроенный)" in text

    assert main(["--home", str(tmp_path), "sound", "--profile", "mine", "--graph"]) == 0
    out = capsys.readouterr().out
    assert "lavfi=[" in out and "alimiter" in out
    assert ConfigStore(Paths.resolve({"NEUROWAVE_HOME": str(tmp_path)})).load().dsp_profile == "mine"
