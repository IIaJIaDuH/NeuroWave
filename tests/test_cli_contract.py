"""Контракт CLI NeuroWave: дерево argparse, справка, коды выхода, JSON.

Baseline (`tests/cli_snapshot.json`) снят с `main` 7a82119 **до** выделения
парсера в отдельный модуль. Тесты фиксируют пользовательское поведение, а не
внутреннее расположение кода: перенос `build_parser()` не должен их менять.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from neurowave import __version__
from neurowave.cli import build_parser, main

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_FILE = Path(__file__).with_name("cli_snapshot.json")


def _norm(text: object) -> str:
    """Схлопнуть пробелы: переносы строк зависят от ширины терминала."""
    return " ".join(str(text or "").split())


def _load_snapshot() -> dict:
    return json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))


def _option_info(action: argparse.Action) -> dict:
    choices = action.choices
    if isinstance(choices, (list, tuple)):
        choices = list(choices)
    elif choices:
        choices = sorted(str(item) for item in choices)
    default = action.default
    if default is argparse.SUPPRESS:
        default = "<suppressed>"
    return {
        "dest": action.dest,
        "option_strings": list(action.option_strings),
        "required": bool(action.required),
        "nargs": action.nargs,
        "choices": choices,
        "default": default,
        "type": getattr(action.type, "__name__", None),
        "action": type(action).__name__,
        "help": _norm(action.help),
    }


def _subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
    return next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )


def _describe_parser(parser: argparse.ArgumentParser) -> dict:
    subs = _subparsers(parser)
    return {
        "prog": parser.prog,
        "description": _norm(parser.description),
        "help": _norm(parser.format_help()),
        "global_options": [
            _option_info(action)
            for action in parser._actions
            if not isinstance(action, argparse._SubParsersAction)
        ],
        "commands": {
            name: {
                "help": _norm(sub.description),
                "option_help": _norm(sub.format_help()),
                "options": [_option_info(action) for action in sub._actions],
            }
            for name, sub in subs.choices.items()
        },
    }


def _parse(*args: str) -> argparse.Namespace:
    return build_parser().parse_args(list(args))


def _run_module(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": str(PROJECT_ROOT / "src"),
            "COLUMNS": "80",
            "NEUROWAVE_KEYRING": "off",
        }
    )
    return subprocess.run(
        [sys.executable, "-m", "neurowave", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd or PROJECT_ROOT),
        timeout=60,
    )


SNAPSHOT = _load_snapshot()
COMMANDS = list(SNAPSHOT["commands"])


# --- дерево argparse ---------------------------------------------------
def test_global_arguments_match_baseline() -> None:
    parser = build_parser()
    described = _describe_parser(parser)
    assert described["prog"] == SNAPSHOT["prog"]
    assert described["description"] == SNAPSHOT["description"]
    assert described["global_options"] == SNAPSHOT["global_options"]


def test_command_tree_matches_baseline() -> None:
    subs = _subparsers(build_parser())
    assert list(subs.choices) == COMMANDS
    for name in COMMANDS:
        sub = subs.choices[name]
        assert _norm(sub.description) == SNAPSHOT["commands"][name]["help"], name


@pytest.mark.parametrize("command", COMMANDS)
def test_command_options_match_baseline(command: str) -> None:
    sub = _subparsers(build_parser()).choices[command]
    options = [_option_info(action) for action in sub._actions]
    assert options == SNAPSHOT["commands"][command]["options"]


@pytest.mark.parametrize("command", ["", *COMMANDS], ids=lambda value: value or "<root>")
def test_help_text_matches_baseline(command: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "80")
    parser = build_parser()
    if command:
        parser = _subparsers(parser).choices[command]
        expected = SNAPSHOT["commands"][command]["option_help"]
    else:
        expected = SNAPSHOT["help"]
    assert _norm(parser.format_help()) == expected


def test_minimal_invocation_defaults() -> None:
    args = _parse("status")
    assert vars(args) == {
        "home": None,
        "backend": None,
        "provider": None,
        "json": False,
        "command": "status",
    }


def test_playback_options_are_shared() -> None:
    invocations = {
        "wave": ("wave",),
        "play": ("play", "jazz"),
        "playlist": ("playlist", "123"),
    }
    for command, prefix in invocations.items():
        args = _parse(*prefix, "--seconds", "2.5", "--no-interactive", "--mpris")
        assert args.command == command
        assert args.seconds == 2.5
        assert args.no_interactive is True
        assert args.mpris is True


def test_choice_arguments_reject_unknown_values() -> None:
    with pytest.raises(SystemExit) as exc:
        _parse("--backend", "pulseaudio", "status")
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        _parse("cache", "flush")
    assert exc.value.code == 2


# --- точка входа и коды выхода ----------------------------------------
def test_help_exits_zero(capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.startswith("usage: neurowave ")


@pytest.mark.parametrize("command", COMMANDS)
def test_command_help_exits_zero(
    command: str, capsys: pytest.CaptureFixture
) -> None:
    with pytest.raises(SystemExit) as exc:
        main([command, "--help"])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert captured.out.startswith(f"usage: neurowave {command} ")
    assert captured.err == ""


def test_help_does_not_touch_network(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("--help не должен открывать сокеты")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0


def test_version_flag(capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"NeuroWave {__version__}"


def test_missing_command_is_usage_error(capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2
    assert "usage: neurowave" in capsys.readouterr().err


def test_unknown_command_is_usage_error(capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["frobnicate"])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_json_is_global_only(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--home", str(tmp_path), "status", "--json"])
    assert exc.value.code == 2
    assert "unrecognized arguments: --json" in capsys.readouterr().err


def test_unknown_provider_exit_code(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    code = main(["--home", str(tmp_path), "--provider", "nope", "status"])
    assert code == 2
    assert "Неизвестный провайдер" in capsys.readouterr().err


def test_missing_token_exit_code(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    code = main(["--home", str(tmp_path), "search", "jazz"])
    assert code == 2
    assert "Требуется вход" in capsys.readouterr().err


# --- запуск как модуль -------------------------------------------------
def test_module_help_runs() -> None:
    result = _run_module("--help")
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("usage: neurowave ")


def test_module_version_runs() -> None:
    result = _run_module("--version")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"NeuroWave {__version__}"


def test_module_help_stays_offline_and_light() -> None:
    probe = (
        "import sys\n"
        "from neurowave.cli import build_parser\n"
        "build_parser().format_help()\n"
        "heavy = [name for name in ('PySide6', 'yandex_music', 'dbus_next', "
        "'keyring', 'tinytag') if name in sys.modules]\n"
        "print(','.join(heavy))\n"
    )
    env = dict(os.environ)
    env.update(
        {"PYTHONPATH": str(PROJECT_ROOT / "src"), "NEUROWAVE_KEYRING": "off"}
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(PROJECT_ROOT),
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


# --- JSON-контракты ----------------------------------------------------
def test_status_json_contract(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    assert main(["--home", str(tmp_path), "--json", "status"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {
        "version",
        "home",
        "provider",
        "token",
        "backend",
        "audio_output",
        "volume",
        "volume_effective",
        "cache",
        "recent",
    }
    assert data["version"] == __version__
    assert data["home"] == str(tmp_path)
    assert set(data["provider"]) == {"id", "title", "auth"}
    assert set(data["token"]) == {"present", "required", "backend"}
    assert set(data["cache"]) == {"files", "mb", "quota_mb"}
    assert data["token"]["present"] is False
    assert data["token"]["required"] is True
    assert data["recent"] == []


def test_doctor_json_contract(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    code = main(["--home", str(tmp_path), "--json", "doctor"])
    checks = json.loads(capsys.readouterr().out)
    assert code in (0, 1)
    assert [item["check"] for item in checks] == [
        "Python ≥ 3.10",
        "mpv",
        "keyring",
        "Каталоги XDG",
        "yandex-music",
        "Провайдер",
        "Токен",
        "Сеть до API",
    ]
    for item in checks:
        assert set(item) == {"check", "ok", "detail"}


def test_providers_json_contract(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    assert main(["--home", str(tmp_path), "--json", "providers"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"current", "connected", "not_connected", "plugin_errors"}
    assert data["current"] == "yandex"
    assert data["plugin_errors"] == []
    for row in data["connected"]:
        assert set(row) == {
            "id",
            "title",
            "summary",
            "capabilities",
            "capabilities_text",
            "auth",
            "plays_in_mpv",
            "cache_allowed",
            "notes",
            "token_present",
            "current",
        }


# --- публичные импорты -------------------------------------------------
def test_public_cli_imports_stay_available() -> None:
    import neurowave.cli as cli

    for name in (
        "main",
        "build_parser",
        "_add_playback_options",
        "Context",
        "_print_account",
        "_handle_key",
        "DEFAULT_SEARCH_LIMIT",
        "API_HOSTS",
    ):
        assert hasattr(cli, name), name
    assert cli.DEFAULT_SEARCH_LIMIT == 20
    assert set(cli.API_HOSTS) == {"yandex", "soundcloud", "spotify"}
