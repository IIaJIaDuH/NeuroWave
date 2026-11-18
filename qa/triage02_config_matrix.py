"""QA-TRIAGE-02 / D-02: матрица негативных проверок конфигурации.

Проверяется поведение приложения при повреждённом `config.json`:

* какая команда (`status`, `doctor`, `app`) как реагирует;
* код выхода, тип исключения, наличие необработанного traceback;
* видно ли ошибку пользователю;
* остаются ли частично записанные настройки;
* можно ли восстановиться, не удаляя пользовательские данные.

Каждый случай — в отдельном изолированном профиле NEUROWAVE_HOME.
Настоящий профиль пользователя не используется и не изменяется.

Запуск: python qa/triage02_config_matrix.py
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qaenv as q  # noqa: E402

MEDIA = Path(os.environ.get("QA_MEDIA") or (q.SHARED / "media-verify"))
HOMES = q.HOMES
VALID_BASE = {
    "provider": "local",
    "local_music_dir": str(MEDIA),
    "volume": 40,
}

#: Описание негативных случаев: id — ожидаемый класс проблемы.
CASES: list[dict] = [
    {"id": "volume_string", "why": "volume = строка", "config": {"volume": "громко"}},
    {"id": "volume_null", "why": "volume = null", "config": {"volume": None}},
    {"id": "volume_negative", "why": "volume = -5", "config": {"volume": -5}},
    {"id": "volume_too_large", "why": "volume = 500", "config": {"volume": 500}},
    {"id": "volume_float", "why": "volume = 80.7 (не целое)", "config": {"volume": 80.7}},
    {"id": "volume_bool", "why": "volume = true", "config": {"volume": True}},
    {"id": "quota_string", "why": "cache_quota_gb = строка", "config": {"cache_quota_gb": "много"}},
    {"id": "quota_null", "why": "cache_quota_gb = null", "config": {"cache_quota_gb": None}},
    {"id": "quota_negative", "why": "cache_quota_gb = -1", "config": {"cache_quota_gb": -1}},
    {"id": "ttl_string", "why": "cache_ttl_days = строка", "config": {"cache_ttl_days": "неделя"}},
    {"id": "shuffle_string", "why": "shuffle = строка", "config": {"shuffle": "да"}},
    {"id": "repeat_number", "why": "repeat = 5", "config": {"repeat": 5}},
    {"id": "repeat_unknown", "why": "repeat = неизвестный режим", "config": {"repeat": "карусель"}},
    {"id": "provider_unknown", "why": "provider = неизвестный", "config": {"provider": "spotify2"}},
    {"id": "local_dir_missing", "why": "local_music_dir = несуществующий",
     "config": {"local_music_dir": "/nonexistent/music"}},
    {"id": "custom_profiles_broken", "why": "custom_profiles = мусор",
     "config": {"custom_profiles": [{"id": "x", "filters": "не список"}]}},
    {"id": "dsp_profile_unknown", "why": "dsp_profile = неизвестный", "config": {"dsp_profile": "нет-такого"}},
    {"id": "extra_fields", "why": "неизвестные дополнительные поля",
     "config": {"volume": 40, "что_то_новое": 1, "another": {"a": 2}}},
    {"id": "empty_object", "why": "пустой объект {}", "config": {}},
    {"id": "broken_json", "why": "нарушен синтаксис JSON", "raw": '{"volume": 80,,}'},
    {"id": "empty_file", "why": "пустой файл", "raw": ""},
    {"id": "json_not_object", "why": "JSON, но не объект", "raw": "[1, 2, 3]"},
    {"id": "missing_file", "why": "файла конфигурации нет", "raw": None},
    {"id": "valid_baseline", "why": "корректная конфигурация (контроль)", "config": {"volume": 40}},
]


def sanitize(text: str) -> str:
    """Убрать домашние пути и имена пользователя из вывода."""
    text = text.replace(str(HOMES), "<QA_HOME>")
    text = text.replace(str(MEDIA), "<MEDIA>")
    text = text.replace(str(q.SHARED), "<QA_STAND>")
    text = text.replace(str(q.ROOT), "<REPO>")
    return text


def app_env(home: Path) -> dict[str, str]:
    env = {
        "NEUROWAVE_HOME": str(home),
        "PATH": f"{q.MPV.parent}:{os.environ['PATH']}",
        "PYTHONPATH": str(q.ROOT / "src"),
        "DISPLAY": os.environ.get("DISPLAY", ":0"),
        "HOME": str(home),
        "LANG": "ru_RU.UTF-8",
    }
    return env


def write_config(home: Path, raw: str | None, config: dict | None) -> None:
    (home / "config").mkdir(parents=True, exist_ok=True)
    target = home / "config" / "config.json"
    if raw is not None:
        target.write_text(raw, encoding="utf-8")
    elif config is not None:
        payload = dict(VALID_BASE)
        payload.update(config)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def file_state(path: Path) -> dict:
    if not path.exists():
        return {"exists": False}
    data = path.read_bytes()
    stat = path.stat()
    return {
        "exists": True,
        "sha256": hashlib.sha256(data).hexdigest()[:16],
        "bytes": len(data),
        "mtime": round(stat.st_mtime, 3),
        "content": sanitize(data.decode("utf-8", "replace")),
    }


def tree_state(root: Path) -> list[str]:
    return sorted(
        str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()
    )


def parse_failure(stderr: str) -> dict:
    """Разобрать stderr: traceback, тип исключения, дружелюбное сообщение."""
    has_traceback = "Traceback (most recent call last)" in stderr
    exc_type = None
    exc_message = None
    if has_traceback:
        lines = [line.strip() for line in stderr.strip().splitlines() if line.strip()]
        for line in reversed(lines):
            match = re.match(r"^([A-Za-z_][\w.]*(?:Error|Exception|Warning))\b:? *(.*)$", line)
            if match:
                exc_type = match.group(1).split(".")[-1]
                exc_message = match.group(2) or None
                break
    friendly = None
    for line in stderr.splitlines():
        if line.startswith("Ошибка:") or line.startswith("Окно недоступно"):
            friendly = line.strip()
            break
    return {
        "has_traceback": has_traceback,
        "exception_type": exc_type,
        "exception_message": sanitize(exc_message) if exc_message else None,
        "user_visible_message": sanitize(friendly) if friendly else None,
        "error_visible_to_user": bool(friendly) or has_traceback,
        "friendly_error": bool(friendly) and not has_traceback,
    }


def run_command(command: list[str], home: Path, timeout: float = 25.0) -> dict:
    env = app_env(home)
    started = time.monotonic()
    proc = subprocess.Popen(
        [str(q.ROOT / ".venv" / "bin" / "neurowave"), *command],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    killed = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        # Приложение живёт (окно открыто) — закрываем штатно.
        killed = True
        proc.terminate()
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
        exit_code = None
    duration = round((time.monotonic() - started) * 1000)
    result = {
        "command": " ".join(command),
        "exit_code": exit_code,
        "killed_by_qa": killed,
        "duration_ms": duration,
        "stdout": sanitize(stdout)[-3000:],
        "stderr": sanitize(stderr)[-4000:],
    }
    result.update(parse_failure(sanitize(stderr)))
    return result


def run_app(home: Path) -> dict:
    """GUI: закрываем окно, если оно открылось."""
    env = app_env(home)
    started = time.monotonic()
    proc = subprocess.Popen(
        [str(q.ROOT / ".venv" / "bin" / "neurowave"), "--provider", "local", "app"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    window = None
    deadline = started + 8.0
    while time.monotonic() < deadline and proc.poll() is None:
        code, out, _ = q.run(["wmctrl", "-l"], timeout=20)
        for line in out.splitlines():
            if line.rstrip().endswith("NeuroWave"):
                window = line.split()[0]
                break
        if window:
            break
        time.sleep(0.4)
    closed = False
    if window:
        subprocess.run(["wmctrl", "-i", "-c", window], timeout=20)
        closed = True
    try:
        stdout, stderr = proc.communicate(timeout=12)
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        exit_code = None
    result = {
        "command": "neurowave --provider local app",
        "exit_code": exit_code,
        "window_opened": bool(window),
        "window_closed_by_qa": closed,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "stdout": sanitize(stdout)[-3000:],
        "stderr": sanitize(stderr)[-4000:],
    }
    result.update(parse_failure(sanitize(stderr)))
    return result


def run_case(case: dict) -> dict:
    home = HOMES / f"triage02_cfg_{case['id']}"
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    # Маркер «пользовательских данных»: их нельзя терять при восстановлении.
    marker = home / "data" / "user-state.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("пользовательские данные QA\n", encoding="utf-8")

    write_config(home, case.get("raw"), case.get("config"))
    config_file = home / "config" / "config.json"
    before = file_state(config_file)
    files_before = tree_state(home)

    outcomes: dict = {}
    outcomes["status"] = run_command(["--provider", "local", "status"], home)
    outcomes["doctor"] = run_command(["--provider", "local", "doctor"], home)
    outcomes["app"] = run_app(home)

    after = file_state(config_file)
    files_after = tree_state(home)
    marker_alive = marker.exists() and marker.read_text(encoding="utf-8") == "пользовательские данные QA\n"

    # Восстановление: заменяем конфигурацию корректной и повторяем запуск.
    write_config(home, None, {"volume": 40})
    recovery = run_command(["--provider", "local", "status"], home)
    marker_after_recovery = marker.exists()

    return {
        "case_id": case["id"],
        "why": case["why"],
        "config_source": "raw" if case.get("raw") is not None else "dict",
        "config_before": before,
        "outcomes": outcomes,
        "config_after": after,
        "config_changed": before.get("sha256") != after.get("sha256") or before.get("exists") != after.get("exists"),
        "config_partially_written": bool(
            after.get("exists") and not after.get("content", "").strip().endswith("}")
        ),
        "new_files": sorted(set(files_after) - set(files_before)),
        "user_data_intact_after_failure": marker_alive,
        "user_data_intact_after_recovery": marker_after_recovery,
        "recovery_exit_code": recovery["exit_code"],
        "recovery_ok": recovery["exit_code"] == 0,
        "recovery_stderr": recovery["stderr"][-600:],
    }


def main() -> int:
    q.ensure_dirs()
    HOMES.mkdir(parents=True, exist_ok=True)
    only = set(sys.argv[1:])
    cases = [c for c in CASES if not only or c["id"] in only]
    results = []
    for case in cases:
        outcome = run_case(case)
        results.append(outcome)
        flags = []
        for name, res in outcome["outcomes"].items():
            if res["has_traceback"]:
                flags.append(f"{name}:TRACEBACK/{res['exception_type']}")
        print(f"{case['id']:<26} {' | '.join(flags) if flags else 'без traceback'}")
    summary = {
        "cases": len(results),
        "case_ids": [r["case_id"] for r in results],
        "with_traceback": sum(
            1 for r in results
            if any(o["has_traceback"] for o in r["outcomes"].values())
        ),
        "config_changed": sum(1 for r in results if r["config_changed"]),
        "config_partially_written": sum(1 for r in results if r["config_partially_written"]),
        "user_data_lost": sum(
            1 for r in results
            if not r["user_data_intact_after_failure"] or not r["user_data_intact_after_recovery"]
        ),
        "recovery_failed": sum(1 for r in results if not r["recovery_ok"]),
    }
    payload = {
        "scenario": "QA-TRIAGE-02 / D-02: негативная матрица конфигурации",
        "sha": "7a82119d23775f7ed02db14e232dcefbc8ade3d9",
        "summary": summary,
        "results": results,
    }
    path = q.write_result("triage02_config_matrix", payload)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"→ {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
