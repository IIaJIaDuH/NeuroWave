"""Реестр провайдеров и команда `providers`: без сети, без токенов."""

from __future__ import annotations

import json

import pytest

from neurowave.cli import main
from neurowave.config import ConfigStore, Paths, Settings
from neurowave.providers import (
    AuthKind,
    Capability,
    ProviderContext,
    ProviderInfo,
    ProviderNotImplemented,
    ProviderSpec,
    UnknownProvider,
    all_specs,
    create,
    get_spec,
    planned,
    plugin_errors,
    register,
)
from neurowave.providers import registry as registry_module
from neurowave.storage import TokenStore


@pytest.fixture(autouse=True)
def clean_registry():
    """Тесты не должны оставлять своих провайдеров в глобальном реестре."""
    before = set(registry_module._REGISTRY)
    yield
    for provider_id in set(registry_module._REGISTRY) - before:
        registry_module._REGISTRY.pop(provider_id, None)
    registry_module._PLUGIN_ERRORS.clear()


def test_yandex_is_registered_with_stream_capability() -> None:
    spec = get_spec("yandex")
    assert spec.info.implemented is True
    assert spec.info.auth is AuthKind.OAUTH_DEVICE
    assert spec.info.plays_in_mpv is True
    for capability in (
        Capability.CATALOG,
        Capability.LIBRARY,
        Capability.PLAYLISTS,
        Capability.PERSONAL_RADIO,
        Capability.DIRECT_STREAM,
    ):
        assert spec.info.supports(capability), capability
    assert spec.factory is not None and spec.login is not None


def test_local_provider_is_implemented_without_account() -> None:
    spec = get_spec("local")
    assert spec.info.implemented is True
    assert spec.info.auth is AuthKind.NONE
    assert spec.info.plays_in_mpv is True
    assert spec.info.cache_allowed is False
    assert spec.info.supports(Capability.SHUFFLE_LIBRARY)
    assert not spec.info.supports(Capability.LIBRARY)
    assert not spec.info.supports(Capability.PERSONAL_RADIO)
    assert spec.login is None  # входить некуда


def test_spotify_provider_is_delegated_playback_only() -> None:
    spec = get_spec("spotify")
    assert spec.info.implemented is True
    assert spec.info.auth is AuthKind.OAUTH_BROWSER
    assert spec.info.supports(Capability.CATALOG)
    assert spec.info.supports(Capability.DELEGATED_PLAYBACK)
    assert not spec.info.supports(Capability.DIRECT_STREAM)
    assert spec.info.plays_in_mpv is False
    assert spec.info.cache_allowed is False
    assert spec.login is not None  # вход по PKCE в браузере


def test_soundcloud_provider_requires_app_key() -> None:
    spec = get_spec("soundcloud")
    assert spec.info.auth is AuthKind.API_TOKEN
    assert spec.info.cache_allowed is False
    assert spec.info.supports(Capability.DIRECT_STREAM)
    assert spec.info.supports(Capability.CATALOG)
    assert spec.login is None  # ключ вводится через login --token


def test_yandex_keeps_cache_allowed() -> None:
    assert get_spec("yandex").info.cache_allowed is True


def test_yandex_factory_uses_token_from_context() -> None:
    adapter = create(get_spec("yandex"), ProviderContext(token="token-for-test"))
    assert adapter.token == "token-for-test"


def test_unknown_provider_lists_available() -> None:
    with pytest.raises(UnknownProvider) as exc:
        get_spec("no-such-service")
    assert "yandex" in str(exc.value)


def test_planned_provider_explains_why_not_connected() -> None:
    with pytest.raises(ProviderNotImplemented) as exc:
        get_spec("vk")
    assert "VK" in str(exc.value)


def test_planned_catalogue_is_honest_about_playback() -> None:
    by_id = {info.id: info for info in planned()}
    assert "yandex" not in by_id  # реализованные не попадают в «не подключённые»
    assert "local" not in by_id  # локальные файлы уже подключены
    assert "soundcloud" not in by_id  # поток уже реализован
    assert "spotify" not in by_id  # подключён как «пульт»
    assert {"vk", "deezer", "tidal", "youtube-music"} <= set(by_id)

    assert by_id["vk"].capabilities == frozenset()
    for info in by_id.values():
        assert info.implemented is False
        assert info.summary, f"у {info.id} нет описания"


def test_register_rejects_duplicates_and_supports_replace() -> None:
    info = ProviderInfo(id="custom", title="Custom", capabilities=frozenset({Capability.CATALOG}))
    spec = ProviderSpec(info=info, factory=lambda context: {"token": context.token})
    register(spec)
    try:
        with pytest.raises(ValueError):
            register(spec)
        register(spec, replace=True)
        assert create(get_spec("custom"), ProviderContext(token="x")) == {"token": "x"}
    finally:
        registry_module._REGISTRY.pop("custom", None)


def test_all_specs_contains_yandex_and_is_sorted() -> None:
    ids = [spec.id for spec in all_specs()]
    assert ids == sorted(ids)
    assert "yandex" in ids


def test_discover_loads_entry_points_and_reports_broken(monkeypatch) -> None:
    good = ProviderSpec(
        info=ProviderInfo(id="plugin-good", title="Good", capabilities=frozenset({Capability.CATALOG}))
    )

    class FakeEntryPoint:
        def __init__(self, name, value=None, error=None):
            self.name = name
            self._value = value
            self._error = error

        def load(self):
            if self._error:
                raise ImportError(self._error)
            return self._value

    entries = [
        FakeEntryPoint("good", value=good),
        FakeEntryPoint("factory", value=lambda: good),
        FakeEntryPoint("broken", error="нет модуля"),
    ]
    monkeypatch.setattr(
        "importlib.metadata.entry_points", lambda **kwargs: entries
    )

    from neurowave.providers import discover

    discovered = discover()
    try:
        assert "plugin-good" in discovered
        assert get_spec("plugin-good").info.title == "Good"
        assert any("broken" in error for error in plugin_errors())
    finally:
        registry_module._REGISTRY.pop("plugin-good", None)


# --- хранение токенов по провайдерам ----------------------------------
def test_tokens_are_stored_per_provider(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(TokenStore, "_import_keyring", staticmethod(lambda: None))
    paths = Paths.resolve({"NEUROWAVE_HOME": str(tmp_path)}).ensure()

    yandex = TokenStore(paths)
    other = TokenStore(paths, provider="soundcloud")
    yandex.save("yandex-token")
    other.save("sc-token")

    assert yandex.load() == "yandex-token"
    assert other.load() == "sc-token"
    assert other.provider == "soundcloud"
    assert (paths.data_dir / "token").read_text(encoding="utf-8") == "yandex-token"
    assert (paths.data_dir / "token.soundcloud").read_text(encoding="utf-8") == "sc-token"
    for file in (paths.data_dir / "token", paths.data_dir / "token.soundcloud"):
        assert file.stat().st_mode & 0o777 == 0o600


# --- CLI ---------------------------------------------------------------
def test_providers_command_lists_connected_and_planned(tmp_path, capsys) -> None:
    code = main(["--home", str(tmp_path), "--json", "providers"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["current"] == "yandex"
    ids = {row["id"] for row in data["connected"]}
    assert ids == {"yandex", "local", "soundcloud", "spotify"}
    planned_ids = {row["id"] for row in data["not_connected"]}
    assert {"vk", "deezer"} <= planned_ids
    assert "spotify" not in planned_ids
    yandex = next(row for row in data["connected"] if row["id"] == "yandex")
    assert yandex["plays_in_mpv"] is True
    assert "direct_stream" in yandex["capabilities"]
    local = next(row for row in data["connected"] if row["id"] == "local")
    assert local["auth"] == "none"
    assert local["cache_allowed"] is False
    soundcloud = next(row for row in data["connected"] if row["id"] == "soundcloud")
    assert soundcloud["auth"] == "api_token"
    assert soundcloud["cache_allowed"] is False
    assert soundcloud["plays_in_mpv"] is True
    spotify = next(row for row in data["connected"] if row["id"] == "spotify")
    assert spotify["auth"] == "oauth_browser"
    assert spotify["plays_in_mpv"] is False
    assert spotify["cache_allowed"] is False
    assert "delegated_playback" in spotify["capabilities"]
    assert "direct_stream" not in spotify["capabilities"]


def test_providers_command_human_output(tmp_path, capsys) -> None:
    assert main(["--home", str(tmp_path), "providers"]) == 0
    out = capsys.readouterr().out
    assert "yandex" in out
    assert "поток в mpv" in out
    assert "Известны, но не подключены" in out
    assert "VK Музыка" in out


def test_provider_flag_selects_provider_and_reports_unimplemented(tmp_path, capsys) -> None:
    code = main(["--home", str(tmp_path), "--provider", "vk", "status"])
    assert code == 2
    assert "не подключён" in capsys.readouterr().err


def test_unknown_provider_flag_is_named_error(tmp_path, capsys) -> None:
    code = main(["--home", str(tmp_path), "--provider", "nope", "status"])
    assert code == 2
    assert "Неизвестный провайдер" in capsys.readouterr().err


def test_settings_store_default_provider(tmp_path) -> None:
    paths = Paths.resolve({"NEUROWAVE_HOME": str(tmp_path)}).ensure()
    store = ConfigStore(paths)
    settings = store.load()
    assert settings.provider == "yandex"
    settings.provider = "soundcloud"
    store.save(settings)
    assert store.load().provider == "soundcloud"
    assert Settings.from_dict({"provider": "vk"}).provider == "vk"
