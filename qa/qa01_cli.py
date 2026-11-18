"""QA-01: запуск из CLI, из установленного пакета и из ярлыка.

Проверяем то, чем пользователь реально запускает плеер:
  * `neurowave --version`, `doctor`, `status`, `providers` в изолированном профиле;
  * установка собранного wheel в чистый venv (сценарий «пользователь поставил пакет»);
  * ярлык из packaging/install-desktop.sh: содержимое .desktop и запуск его Exec.

Никаких токенов: профиль пустой, провайдер по умолчанию — yandex, ожидаем
понятную ошибку входа вместо краша.

Запуск: python qa/qa01_cli.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qaenv as q  # noqa: E402

QA_HOME = q.HOMES / "qa01_cli"
SAX = q.SHARED / "qa01_sandbox"
VENV = q.SHARED / "qa01_installed_venv"


def cli(args: list[str], home: Path, timeout: float = 60.0, extra_env: dict[str, str] | None = None):
    env = {
        "NEUROWAVE_HOME": str(home),
        "PATH": f"{q.MPV.parent}:{os.environ['PATH']}",
        "PYTHONPATH": str(q.ROOT / "src"),
    }
    env.update(extra_env or {})
    return q.run([str(q.ROOT / ".venv" / "bin" / "neurowave"), *args], timeout=timeout, env=env)


def section_cli(home: Path) -> dict:
    out: dict = {}
    for name, args, timeout in [
        ("version", ["--version"], 30),
        ("doctor", ["doctor"], 60),
        ("status", ["status"], 60),
        ("providers", ["providers"], 60),
        ("wave_no_auth", ["wave"], 60),
        ("me_no_auth", ["me"], 60),
        ("library_no_auth", ["library"], 60),
        ("output", ["output"], 60),
        ("sound_profiles", ["sound", "--profiles"], 60),
    ]:
        started = time.monotonic()
        code, stdout, stderr = cli(args, home, timeout=timeout)
        out[name] = {
            "argv": args,
            "code": code,
            "stdout": stdout.strip()[:2000],
            "stderr": stderr.strip()[:2000],
            "seconds": round(time.monotonic() - started, 2),
        }
    return out


def section_wheel_install() -> dict:
    """Сборка wheel и установка в чистый venv: сценарий реальной установки."""
    out: dict = {}
    shutil.rmtree(VENV, ignore_errors=True)
    build_dir = q.SHARED / "qa01_build"
    shutil.rmtree(build_dir, ignore_errors=True)
    code, stdout, stderr = q.run(
        [str(q.ROOT / ".venv" / "bin" / "python"), "-m", "build", "--outdir", str(build_dir),
         str(q.ROOT)], timeout=600,
    )
    out["build"] = {"code": code, "stdout": stdout.strip()[-1500:], "stderr": stderr.strip()[-1500:]}
    wheels = sorted(build_dir.glob("*.whl"))
    if not wheels:
        out["install"] = {"code": -1, "stderr": "wheel не собран"}
        return out
    code, stdout, stderr = q.run([sys.executable, "-m", "venv", str(VENV)], timeout=180)
    pip = VENV / "bin" / "pip"
    code2, stdout2, stderr2 = q.run(
        [str(pip), "install", "--quiet", str(wheels[0])], timeout=900,
    )
    out["install"] = {"code": code2, "stdout": stdout2.strip()[-800:], "stderr": stderr2.strip()[-800:],
                      "wheel": wheels[0].name}
    for label, args in [("version", ["--version"]), ("doctor", ["doctor"]), ("providers", ["providers"])]:
        c, so, se = q.run([str(VENV / "bin" / "neurowave"), *args], timeout=120,
                           env={"NEUROWAVE_HOME": str(QA_HOME / "wheel")})
        out[f"installed_{label}"] = {"code": c, "stdout": so.strip()[:1200], "stderr": se.strip()[:1200]}
    # Проверка метаданных пакета: лицензия и содержимое
    c, so, se = q.run([str(pip), "show", wheels[0].name], timeout=120)
    out["pip_show"] = {"code": c, "stdout": so.strip()[:2500]}
    c, so, se = q.run([str(VENV / "bin" / "python"), "-c",
                        "import importlib.metadata as m; d=m.metadata('neurowave');"
                        "print(d.get('Name'), d.get('Version'), d.get('License'), d.get('License-Expression'))"],
                       timeout=60)
    out["metadata"] = {"code": c, "stdout": so.strip(), "stderr": se.strip()[:500]}
    return out


def section_desktop_entry() -> dict:
    """Ярлык: генерация в изолированном XDG и проверка его Exec-команды."""
    out: dict = {}
    shutil.rmtree(SAX, ignore_errors=True)
    (SAX / "data" / "applications").mkdir(parents=True, exist_ok=True)
    (SAX / "home").mkdir(parents=True, exist_ok=True)
    env = {
        "XDG_DATA_HOME": str(SAX / "data"),
        "HOME": str(SAX / "home"),
        "PATH": f"{q.MPV.parent}:{os.environ['PATH']}",
    }
    code, stdout, stderr = q.run([str(q.ROOT / "packaging" / "install-desktop.sh")],
                                  timeout=60, env=env)
    out["install_code"] = code
    out["install_stdout"] = stdout.strip()
    out["install_stderr"] = stderr.strip()
    entry = SAX / "data" / "applications" / "neurowave.desktop"
    out["entry_exists"] = entry.exists()
    if entry.exists():
        out["entry_text"] = entry.read_text(encoding="utf-8")
        out["entry_mode"] = oct(entry.stat().st_mode & 0o777)
    desktop_dir = SAX / "home" / "Рабочий стол"
    alt = SAX / "home" / "Desktop"
    for candidate in (desktop_dir, alt):
        target = candidate / "neurowave.desktop"
        if target.exists():
            out["desktop_copy"] = str(target)
    # Exec-команда ярлыка: wave без токена
    code, stdout, stderr = cli(["wave"], QA_HOME / "desktop", timeout=90)
    out["exec_wave_without_token"] = {"code": code, "stdout": stdout.strip()[:1200],
                                      "stderr": stderr.strip()[:1200]}
    # mpv доступен ли в PATH пользователя (как требует подсказка в ошибке)
    code, stdout, stderr = q.run(["sh", "-lc", "command -v mpv || echo 'mpv-not-in-path'"], timeout=30)
    out["system_mpv_in_path"] = stdout.strip()
    return out


def main() -> int:
    q.ensure_dirs()
    QA_HOME.mkdir(parents=True, exist_ok=True)
    result = {
        "scenario": "QA-01 CLI / установка / ярлык",
        "base_sha": os.environ.get("QA_BASE_SHA", ""),
        "cli": section_cli(QA_HOME / "profile"),
        "wheel_install": section_wheel_install(),
        "desktop_entry": section_desktop_entry(),
    }
    path = q.write_result("qa01_cli", result)
    print(json.dumps(result, ensure_ascii=False, indent=2)[:12000])
    print(f"→ {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
