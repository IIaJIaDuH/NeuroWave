"""Интеграции необязательны: импорт без D-Bus и без Qt, деградация без шины.

Тесты специально запускаются в отдельных процессах: проверяется именно то, что
видит пользователь без установленного `dbus-next`/`PySide6` и без сессионного
демона шины.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"


def run_python(code: str, *, timeout: float = 30.0, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"DBUS_SESSION_BUS_ADDRESS", "DBUS_SYSTEM_BUS_ADDRESS"}
    }
    env["PYTHONPATH"] = str(SRC)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


def test_mpris_logic_imports_without_dbus_or_qt() -> None:
    result = run_python(
        "import sys\n"
        "from neurowave.integrations import mpris\n"
        "assert 'dbus_next' not in sys.modules, 'mpris не должен тянуть dbus-next'\n"
        "assert 'PySide6' not in sys.modules, 'mpris не должен тянуть PySide6'\n"
        "print(mpris.BUS_NAME)\n"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "org.mpris.MediaPlayer2.neurowave"


def test_dbus_and_tray_modules_import_without_their_dependencies() -> None:
    result = run_python(
        "import sys\n"
        "from neurowave.integrations import mpris_dbus, tray\n"
        "assert 'dbus_next' not in sys.modules, 'mpris_dbus должен импортировать dbus-next лениво'\n"
        "assert 'PySide6' not in sys.modules, 'tray должен импортировать PySide6 лениво'\n"
        "print(mpris_dbus.POLL_INTERVAL, tray.TITLE)\n"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["0.5", "NeuroWave"]


def test_service_reports_missing_dbus_next() -> None:
    """Без установленного dbus-next start() возвращает False с подсказкой."""
    result = run_python(
        "import sys\n"
        "sys.modules['dbus_next'] = None\n"
        "from neurowave.integrations.mpris_dbus import MprisService\n"
        "from neurowave.integrations.mpris import MprisController\n"
        "from neurowave.integrations.mpris import MprisSnapshot\n"
        "class Target:\n"
        "    def snapshot(self): return MprisSnapshot()\n"
        "service = MprisService(MprisController(Target()))\n"
        "assert service.start(timeout=1.0) is False\n"
        "assert 'dbus-next' in (service.error or '')\n"
        "print('ok')\n"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_service_without_session_bus_does_not_raise() -> None:
    """Без сессионного демона сервис просто не поднимается."""
    result = run_python(
        "from neurowave.integrations.mpris_dbus import MprisService\n"
        "from neurowave.integrations.mpris import MprisController, MprisSnapshot\n"
        "class Target:\n"
        "    def snapshot(self): return MprisSnapshot()\n"
        "service = MprisService(MprisController(Target()))\n"
        "started = service.start(timeout=2.0)\n"
        "service.stop()\n"
        "print('started' if started else 'no-bus')\n",
        timeout=40.0,
        extra_env={"DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent/bus"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "no-bus"


def test_tray_reports_missing_pyside6() -> None:
    result = run_python(
        "import sys\n"
        "sys.modules['PySide6'] = None\n"
        "from neurowave.integrations.tray import is_available\n"
        "ok, reason = is_available()\n"
        "assert ok is False and 'PySide6' in reason\n"
        "print('ok')\n"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
