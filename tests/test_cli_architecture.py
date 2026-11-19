"""Архитектура первого этапа WO-02: парсер вынесен в один модуль.

Проверки страхуют от копии парсера в двух местах, циклического импорта и
побочных эффектов при импорте.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE = PROJECT_ROOT / "src" / "neurowave"


def _run(code: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update({"PYTHONPATH": str(PROJECT_ROOT / "src"), "NEUROWAVE_KEYRING": "off"})
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(PROJECT_ROOT),
        timeout=60,
    )


def test_parser_module_is_the_single_definition() -> None:
    import neurowave.cli as cli
    from neurowave.cli_commands import parser as parser_module

    assert cli.build_parser is parser_module.build_parser
    assert cli._add_playback_options is parser_module._add_playback_options
    assert cli.DEFAULT_SEARCH_LIMIT is parser_module.DEFAULT_SEARCH_LIMIT


def test_cli_module_keeps_no_private_parser_copy() -> None:
    source = (SOURCE / "cli.py").read_text(encoding="utf-8")
    assert "def build_parser(" not in source
    assert "def _add_playback_options(" not in source
    assert "add_subparsers(" not in source


def test_parser_module_uses_only_stdlib_and_package_imports() -> None:
    tree = ast.parse((SOURCE / "cli_commands" / "parser.py").read_text(encoding="utf-8"))
    external: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            external.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            external.add(node.module.split(".")[0])
    assert external <= {"argparse", "__future__"}


def test_parser_module_does_not_pull_in_cli_or_heavy_dependencies() -> None:
    result = _run(
        "import sys\n"
        "from neurowave.cli_commands import parser\n"
        "parser.build_parser()\n"
        "heavy = ('neurowave.cli', 'PySide6', 'yandex_music', 'dbus_next', 'keyring', "
        "'tinytag')\n"
        "print(','.join(name for name in heavy if name in sys.modules))\n"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_importing_cli_creates_no_state(tmp_path: Path) -> None:
    home = tmp_path / "state"
    result = _run(
        "import neurowave.cli\n"
        "from neurowave.cli_commands import parser\n"
        "parser.build_parser().parse_args(['status'])\n"
        "print('ok')\n",
        {"NEUROWAVE_HOME": str(home)},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
    assert result.stderr == ""
    assert not home.exists()
