"""D-02: повреждённый config.json не должен ронять приложение.

Пять вариантов из PR #36 раньше давали необработанные ``ValueError``/``TypeError``
из ``Settings.from_dict`` или из первого обращения к свойству. Здесь закреплено
требуемое поведение: ``ConfigError`` с именем поля, код 2 для ``status``/``app``,
незавершающийся ``doctor`` и неизменный исходный файл.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from neurowave.cli import main
from neurowave.config import ConfigStore, Paths, Settings
from neurowave.errors import ConfigError

#: Ровно те значения, что тестировщик внёс в config.json (PR #36).
BROKEN_VALUES = [
    ("volume", "громко"),
    ("volume", None),
    ("cache_quota_gb", "много"),
    ("cache_quota_gb", None),
    ("cache_ttl_days", "неделя"),
]


def _store(home: Path) -> ConfigStore:
    return ConfigStore(Paths.resolve({"NEUROWAVE_HOME": str(home)}))


def _write_config(home: Path, payload: dict) -> Path:
    paths = Paths.resolve({"NEUROWAVE_HOME": str(home)}).ensure()
    file = paths.settings_file
    file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return file


def _write_raw(home: Path, text: str) -> Path:
    paths = Paths.resolve({"NEUROWAVE_HOME": str(home)}).ensure()
    file = paths.settings_file
    file.write_text(text, encoding="utf-8")
    return file


def _sha256(file: Path) -> str:
    return hashlib.sha256(file.read_bytes()).hexdigest()


# --- загрузка настроек -------------------------------------------------
@pytest.mark.parametrize("field,value", BROKEN_VALUES)
def test_load_names_the_broken_field(tmp_path, field, value):
    _write_config(tmp_path, {field: value})
    with pytest.raises(ConfigError, match=field):
        _store(tmp_path).load()


@pytest.mark.parametrize("field,value", BROKEN_VALUES)
def test_invalid_value_never_reaches_properties(tmp_path, field, value):
    """Ошибка возникает при загрузке, а не при чтении ``cache_*``."""
    _write_config(tmp_path, {field: value})
    with pytest.raises(ConfigError):
        settings = _store(tmp_path).load()
        # Если загрузка прошла, свойства обязаны работать без исключений.
        settings.effective_volume()
        settings.cache_quota_bytes
        settings.cache_ttl_seconds


@pytest.mark.parametrize(
    "field,raw",
    [
        ("cache_quota_gb", '{"cache_quota_gb": NaN}'),
        ("cache_quota_gb", '{"cache_quota_gb": Infinity}'),
        ("cache_quota_gb", '{"cache_quota_gb": -Infinity}'),
        ("cache_ttl_days", '{"cache_ttl_days": NaN}'),
        ("volume", '{"volume": NaN}'),
    ],
)
def test_non_finite_numbers_are_rejected(tmp_path, field, raw):
    _write_raw(tmp_path, raw)
    with pytest.raises(ConfigError, match=field):
        _store(tmp_path).load()


@pytest.mark.parametrize("field", ["volume", "cache_quota_gb", "cache_ttl_days"])
def test_boolean_is_not_a_number(tmp_path, field):
    _write_config(tmp_path, {field: True})
    with pytest.raises(ConfigError, match=field):
        _store(tmp_path).load()


def test_numeric_strings_still_work(tmp_path):
    """Ранее допустимая числовая строка «80» не должна сломаться."""
    settings = Settings.from_dict(
        {"volume": "80", "cache_ttl_days": "7", "cache_quota_gb": 2}
    )
    assert settings.volume == 80
    assert settings.cache_ttl_days == 7
    assert settings.cache_quota_bytes == 2 * 1024**3
    assert settings.cache_ttl_seconds == 7 * 24 * 3600


def test_clamping_of_valid_out_of_range_volume_is_preserved():
    settings = Settings.from_dict({"volume": 999})
    assert settings.volume == 130


# --- status / app ------------------------------------------------------
@pytest.mark.parametrize("command", ["status", "app"])
@pytest.mark.parametrize("field,value", BROKEN_VALUES)
def test_status_and_app_fail_cleanly(tmp_path, capsys, command, field, value):
    file = _write_config(tmp_path, {field: value})
    before = _sha256(file)

    code = main(["--home", str(tmp_path), command])

    captured = capsys.readouterr()
    assert "Traceback" not in captured.err + captured.out
    assert code == 2
    assert field in captured.err
    assert _sha256(file) == before


@pytest.mark.parametrize("command", ["status", "app"])
@pytest.mark.parametrize("text", ["{not json", "[1, 2, 3]", '"text"'])
def test_broken_json_and_non_object_keep_contract(tmp_path, capsys, command, text):
    file = _write_raw(tmp_path, text)
    before = _sha256(file)

    code = main(["--home", str(tmp_path), command])

    captured = capsys.readouterr()
    assert "Traceback" not in captured.err + captured.out
    assert code == 2
    assert "config.json" in captured.err
    assert _sha256(file) == before


def test_status_recovers_after_manual_repair(tmp_path, capsys):
    file = _write_config(tmp_path, {"volume": "громко"})
    assert main(["--home", str(tmp_path), "status"]) == 2
    capsys.readouterr()

    file.write_text(json.dumps({"volume": 55}), encoding="utf-8")
    assert main(["--home", str(tmp_path), "--json", "status"]) == 0

    data = json.loads(capsys.readouterr().out)
    assert data["volume"] == 55


def test_unknown_fields_survive_a_read_only_run(tmp_path, capsys):
    file = _write_config(tmp_path, {"volume": 42, "future_option": {"nested": 1}})
    before = file.read_text(encoding="utf-8")

    assert main(["--home", str(tmp_path), "--json", "status"]) == 0
    assert json.loads(capsys.readouterr().out)["volume"] == 42
    assert file.read_text(encoding="utf-8") == before


# --- doctor ------------------------------------------------------------
@pytest.mark.parametrize("field,value", BROKEN_VALUES)
def test_doctor_continues_and_returns_diagnostic_code(tmp_path, capsys, field, value):
    file = _write_config(tmp_path, {field: value})
    before = _sha256(file)

    code = main(["--home", str(tmp_path), "doctor"])

    captured = capsys.readouterr()
    out = captured.out
    assert "Traceback" not in captured.err + out
    assert code == 1
    assert "✗ Конфигурация" in out
    assert field in out
    # Независимые проверки выполняются дальше.
    assert "Python ≥ 3.10" in out
    assert "mpv" in out
    assert "Провайдер" in out
    assert _sha256(file) == before


def test_doctor_json_keeps_structure_and_marks_failed_check(tmp_path, capsys):
    _write_config(tmp_path, {"volume": "громко"})

    code = main(["--home", str(tmp_path), "--json", "doctor"])

    checks = json.loads(capsys.readouterr().out)
    assert isinstance(checks, list)
    assert all({"check", "ok", "detail"} <= set(item) for item in checks)
    names = {item["check"] for item in checks}
    assert {"Python ≥ 3.10", "mpv", "Каталоги XDG", "Конфигурация", "Провайдер"} <= names

    failed = [item for item in checks if not item["ok"]]
    assert code == 1
    config_failed = [item for item in failed if item["check"] == "Конфигурация"]
    assert len(config_failed) == 1
    assert "volume" in config_failed[0]["detail"]


@pytest.mark.parametrize("text", ["{not json", "[1, 2, 3]"])
def test_doctor_survives_broken_json(tmp_path, capsys, text):
    file = _write_raw(tmp_path, text)
    before = _sha256(file)

    code = main(["--home", str(tmp_path), "--json", "doctor"])

    checks = json.loads(capsys.readouterr().out)
    assert code == 1
    assert [item for item in checks if item["check"] == "Конфигурация"][0]["ok"] is False
    assert _sha256(file) == before


def test_doctor_with_valid_config_still_passes_config_check(tmp_path, capsys):
    _write_config(tmp_path, {"volume": 70, "provider": "local"})

    code = main(["--home", str(tmp_path), "--json", "doctor"])

    checks = json.loads(capsys.readouterr().out)
    config = [item for item in checks if item["check"] == "Конфигурация"]
    assert config[0]["ok"] is True
    assert code in (0, 1)  # остальные проверки зависят от окружения
