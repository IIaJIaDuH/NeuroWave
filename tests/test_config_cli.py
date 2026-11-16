from __future__ import annotations

import json

import pytest

from neurowave.cli import main
from neurowave.config import ConfigStore, Paths, Settings
from neurowave.errors import ConfigError


def test_paths_override_with_home(tmp_path):
    paths = Paths.resolve({"NEUROWAVE_HOME": str(tmp_path)})
    assert paths.config_dir == tmp_path / "config"
    assert paths.database_file == tmp_path / "data" / "state.db"


def test_paths_follow_xdg(tmp_path):
    env = {
        "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
        "XDG_DATA_HOME": str(tmp_path / "dat"),
        "XDG_CACHE_HOME": str(tmp_path / "cch"),
    }
    paths = Paths.resolve(env)
    assert paths.config_dir == tmp_path / "cfg" / "neurowave"
    assert paths.cache_dir == tmp_path / "cch" / "neurowave"


def test_settings_roundtrip(tmp_path):
    paths = Paths.resolve({"NEUROWAVE_HOME": str(tmp_path)})
    store = ConfigStore(paths)
    settings = Settings(volume=95, cache_quota_gb=1.5, repeat="one")
    store.save(settings)
    loaded = store.load()
    assert loaded.volume == 95
    assert loaded.cache_quota_gb == 1.5
    assert loaded.repeat == "one"
    assert loaded.cache_quota_bytes == int(1.5 * 1024**3)


def test_settings_clamp_and_ignore_unknown():
    settings = Settings.from_dict({"volume": 999, "repeat": "bogus", "unknown": True})
    assert settings.volume == 130
    assert settings.repeat == "off"


def test_broken_config_reports_error(tmp_path):
    paths = Paths.resolve({"NEUROWAVE_HOME": str(tmp_path)}).ensure()
    paths.settings_file.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError):
        ConfigStore(paths).load()


def test_status_command_json(tmp_path, capsys):
    code = main(["--home", str(tmp_path), "--json", "status"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["token"]["present"] is False
    assert data["cache"]["quota_mb"] > 0


def test_doctor_reports_checks(tmp_path, capsys):
    code = main(["--home", str(tmp_path), "--json", "doctor"])
    output = capsys.readouterr().out
    checks = json.loads(output)
    names = {item["check"] for item in checks}
    assert "Python ≥ 3.10" in names
    assert "mpv" in names
    assert "Токен" in names
    assert code in (0, 1)


def test_doctor_checks_only_selected_provider(tmp_path, capsys):
    cases = (
        ("yandex", "yandex-music", "soundcloud API"),
        ("soundcloud", "soundcloud API", "yandex-music"),
        ("local", "mutagen", "yandex-music"),
    )
    for provider, expected, absent in cases:
        code = main(
            [
                "--home",
                str(tmp_path / provider),
                "--provider",
                provider,
                "--json",
                "doctor",
            ]
        )
        names = {item["check"] for item in json.loads(capsys.readouterr().out)}
        assert expected in names, provider
        assert absent not in names, provider
        assert code in (0, 1)


def test_account_without_subscription_omits_plus_line(capsys):
    from neurowave.cli import _print_account
    from neurowave.domain import AccountInfo

    _print_account(AccountInfo(display_name="SoundCloud"), False, None)
    out = capsys.readouterr().out
    assert "Аккаунт: SoundCloud" in out
    assert "Подписка" not in out


def test_account_with_subscription_shows_plus_line(capsys):
    from neurowave.cli import _print_account
    from neurowave.domain import AccountInfo

    _print_account(AccountInfo(display_name="Яндекс Музыка", plus=True), False, "Плюс")
    assert "Подписка Плюс: активна" in capsys.readouterr().out


def test_me_for_local_points_to_local_music_dir(tmp_path, capsys):
    from neurowave.config import ConfigStore, Paths, Settings

    home = tmp_path / "home"
    paths = Paths.resolve({"NEUROWAVE_HOME": str(home)}).ensure()
    ConfigStore(paths).save(Settings(local_music_dir=str(tmp_path / "NoMusic")))

    assert main(["--home", str(home), "--provider", "local", "me"]) == 0
    out = capsys.readouterr().out
    assert "local_music_dir" in out
    assert "Сервис сообщает" not in out


def test_account_unavailable_uses_generic_hint_by_default(capsys):
    from neurowave.cli import _print_account
    from neurowave.domain import AccountInfo

    _print_account(AccountInfo(display_name="Сервис", service_available=False), False)
    assert "Сервис сообщает о недоступности" in capsys.readouterr().out


def test_me_for_local_has_no_subscription_line(tmp_path, capsys):
    assert main(["--home", str(tmp_path), "--provider", "local", "me"]) == 0
    out = capsys.readouterr().out
    assert "Локальная музыка" in out
    assert "Подписка" not in out


def test_cache_command(tmp_path, capsys):
    assert main(["--home", str(tmp_path), "cache", "clear"]) == 0
    assert "Кеш" in capsys.readouterr().out


def test_play_requires_token(tmp_path, capsys):
    code = main(["--home", str(tmp_path), "play", "jazz"])
    assert code == 2
    assert "Требуется вход" in capsys.readouterr().err


def test_search_requires_token(tmp_path, capsys):
    assert main(["--home", str(tmp_path), "search", "jazz"]) == 2
    assert "Требуется вход" in capsys.readouterr().err


def test_logout_clears_token_file(tmp_path):
    paths = Paths.resolve({"NEUROWAVE_HOME": str(tmp_path)}).ensure()
    token_file = paths.token_file
    token_file.write_text("token-value", encoding="utf-8")
    code = main(["--home", str(tmp_path), "logout"])
    assert code == 0
    assert not token_file.exists()
