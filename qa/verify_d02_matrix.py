"""QA-VERIFY-03 / D-02: повтор матрицы повреждённых настроек на стенде.

Матрица: 24 конфигурации × 3 команды (`status`, `doctor`, `app`) = 72 запуска.
Каждый запуск — в отдельном изолированном профиле NEUROWAVE_HOME; настоящий
профиль пользователя не используется и не изменяется.

Для каждого запуска фиксируются: код выхода, stdout/stderr в обезличенном
виде, наличие traceback, SHA256 и содержимое `config.json` до/после, список
файлов профиля до/после, целостность «пользовательских данных» (маркер,
токены, история), открылось ли окно GUI и восстановилось ли приложение после
ручной правки конфигурации.

Ожидаемые коды выхода не копируются из отчёта PR #36: исправление D-02
намеренно меняет реакцию на повреждённые настройки, поэтому классификация
строится по наблюдаемому поведению (наличие traceback, тип ошибки,
сохранность данных), а не по старым expectation'ам.

Запуск: python qa/verify_d02_matrix.py [--label fix|base]
"""

from __future__ import annotations

import argparse
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

MEDIA = Path(os.environ.get("QA_MEDIA") or (q.SHARED / "media"))
HOMES = q.HOMES
APP = q.ROOT / ".venv" / "bin" / "neurowave"

VALID_BASE = {
    "provider": "local",
    "local_music_dir": str(MEDIA),
    "volume": 40,
}

#: Пять исходных P1-случаев из отчёта PR #36 (D-02).
P1_CASES = ("volume_string", "volume_null", "quota_string", "quota_null",
            "ttl_string")

#: 24 конфигурации исходной матрицы PR #36 — без ожидаемых кодов выхода.
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
    """Убрать пути стенда и проверяемой машины из вывода."""
    for raw, token in ((str(HOMES), "<QA_HOMES>"), (str(MEDIA), "<MEDIA>"),
                       (str(q.SHARED), "<QA_STAND>"), (str(q.ROOT), "<REPO>"),
                       (str(Path.home()), "<HOME>")):
        text = text.replace(raw, token)
    return text


def app_env(home: Path) -> dict[str, str]:
    env = {
        "NEUROWAVE_HOME": str(home),
        "PATH": f"{q.MPV.parent}:{os.environ.get('PATH', '')}",
        "PYTHONPATH": str(q.ROOT / "src"),
        "DISPLAY": os.environ.get("DISPLAY", ":0"),
        "HOME": str(home),
        "LANG": "ru_RU.UTF-8",
        "XDG_RUNTIME_DIR": str(home),
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
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                          encoding="utf-8")


def file_state(path: Path) -> dict:
    if not path.exists():
        return {"exists": False}
    data = path.read_bytes()
    return {
        "exists": True,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "content": sanitize(data.decode("utf-8", "replace")),
    }


def tree_state(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*")
                  if path.is_file())


def token_state(home: Path) -> dict:
    """Токены и история прослушиваний тестового профиля."""
    state: dict = {}
    token_dir = home / "tokens"
    state["token_files"] = sorted(p.name for p in token_dir.glob("*")) if token_dir.exists() else []
    db = home / "data" / "history.db"
    state["db_exists"] = db.exists()
    state["db_sha256"] = (hashlib.sha256(db.read_bytes()).hexdigest()
                          if db.exists() else None)
    state["db_bytes"] = db.stat().st_size if db.exists() else 0
    history = home / "data" / "history.json"
    state["history_exists"] = history.exists()
    state["history_sha256"] = (hashlib.sha256(history.read_bytes()).hexdigest()
                                if history.exists() else None)
    return state


def parse_failure(stderr: str) -> dict:
    """Разбор stderr: traceback, тип исключения, сообщение для пользователя."""
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
        "friendly_error": bool(friendly) and not has_traceback,
    }


def classify(result: dict) -> str:
    """Классификация поведения команды без опоры на старые expectation'ы."""
    if result["has_traceback"]:
        return "UNHANDLED_TRACEBACK"
    if result["exit_code"] in (0, 1, 2):
        return "CONTROLLED"
    if result.get("window_opened"):
        return "WINDOW_OPENED"
    return "OTHER"


def run_command(command: list[str], home: Path, timeout: float = 30.0) -> dict:
    env = app_env(home)
    started = time.monotonic()
    proc = subprocess.Popen([str(APP), *command], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    killed = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        killed = True
        proc.terminate()
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
        exit_code = None
    result = {
        "command": " ".join(command),
        "exit_code": exit_code,
        "killed_by_qa": killed,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "stdout": sanitize(stdout)[-3000:],
        "stderr": sanitize(stderr)[-4000:],
    }
    result.update(parse_failure(sanitize(stderr)))
    result["classification"] = classify(result)
    return result


def run_app(home: Path) -> dict:
    """GUI на реальном X11: окно закрываем штатно, если оно открылось."""
    env = app_env(home)
    started = time.monotonic()
    proc = subprocess.Popen([str(APP), "--provider", "local", "app"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
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
        stdout, stderr = proc.communicate(timeout=15)
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
    result["classification"] = classify(result)
    return result


def run_case(case: dict) -> dict:
    home = HOMES / f"verify_d02_{case['id']}"
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    # Маркер «пользовательских данных»: их нельзя терять при восстановлении.
    marker = home / "data" / "user-state.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("пользовательские данные QA\n", encoding="utf-8")
    # Тестовый токен: не должен попасть в лог и не должен потеряться.
    tokens = home / "tokens"
    tokens.mkdir(parents=True, exist_ok=True)
    (tokens / "local.token").write_text("qa-test-token-value\n", encoding="utf-8")

    write_config(home, case.get("raw"), case.get("config"))
    config_file = home / "config" / "config.json"
    before = file_state(config_file)
    files_before = tree_state(home)
    tokens_before = token_state(home)

    outcomes = {
        "status": run_command(["--provider", "local", "status"], home),
    }
    after_status = file_state(config_file)
    outcomes["doctor"] = run_command(["--provider", "local", "doctor"], home)
    after_doctor = file_state(config_file)
    outcomes["app"] = run_app(home)
    after_app = file_state(config_file)

    after = file_state(config_file)
    files_after = tree_state(home)
    tokens_after = token_state(home)
    marker_alive = (marker.exists()
                    and marker.read_text(encoding="utf-8") == "пользовательские данные QA\n")

    # Восстановление: конфигурацию чиним вручную, профиль не удаляем.
    write_config(home, None, {"volume": 40})
    recovery = run_command(["--provider", "local", "status"], home)
    marker_after_recovery = marker.exists()
    token_after_recovery = token_state(home)

    return {
        "case_id": case["id"],
        "why": case["why"],
        "is_p1_case": case["id"] in P1_CASES,
        "outcomes": outcomes,
        "config_before": before,
        "config_after": after,
        "config_rewritten": (before.get("sha256") != after.get("sha256")),
        # Какая именно команда меняла файл: штатное сохранение при `app` —
        # это не то же самое, что перезапись повреждённого файла при ошибке.
        "config_rewritten_by": [
            name for name, state in (("status", after_status),
                                     ("doctor", after_doctor),
                                     ("app", after_app))
            if state.get("sha256") != before.get("sha256")
        ],
        "config_after_status": after_status,
        "config_after_doctor": after_doctor,
        "config_after_app": after_app,
        "files_before": files_before,
        "files_after": files_after,
        "files_added": sorted(set(files_after) - set(files_before)),
        "files_removed": sorted(set(files_before) - set(files_after)),
        "user_data_intact": marker_alive,
        "tokens_before": tokens_before,
        "tokens_after": tokens_after,
        "tokens_lost": tokens_before != tokens_after,
        "recovery": {
            "status_exit_code": recovery["exit_code"],
            "status_has_traceback": recovery["has_traceback"],
            "marker_alive": marker_after_recovery,
            "tokens_intact": (token_after_recovery["token_files"]
                              == tokens_before["token_files"]),
        },
        "token_leaked_to_output": ("qa-test-token-value" in outcomes["status"]["stdout"]
                                   + outcomes["status"]["stderr"]
                                   + outcomes["doctor"]["stdout"]
                                   + outcomes["doctor"]["stderr"]
                                   + outcomes["app"]["stdout"]
                                   + outcomes["app"]["stderr"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", default="fix", help="метка прогона (fix|base)")
    parser.add_argument("--out", default="", help="куда сохранить результат")
    args = parser.parse_args()

    q.ensure_dirs()
    results = []
    for case in CASES:
        started = time.monotonic()
        item = run_case(case)
        item["duration_s"] = round(time.monotonic() - started, 1)
        results.append(item)
        statuses = {name: out["classification"] for name, out in item["outcomes"].items()}
        print(f"  {case['id']:24} {statuses} config_rewritten={item['config_rewritten']} "
              f"user_data_intact={item['user_data_intact']}", flush=True)

    summary = {
        "label": args.label,
        "sha": q.subprocess.run(["git", "rev-parse", "HEAD"], cwd=q.ROOT,
                                capture_output=True, text=True).stdout.strip(),
        "cases": len(results),
        "runs": len(results) * 3,
        "unhandled_tracebacks": sum(
            1 for item in results for out in item["outcomes"].values()
            if out["has_traceback"]),
        "p1_cases": list(P1_CASES),
        "results": results,
    }
    out_path = Path(args.out) if args.out else (q.ARTIFACTS / f"verify_d02_matrix_{args.label}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    print(f"→ {out_path}")
    print(f"необработанных traceback: {summary['unhandled_tracebacks']} из {summary['runs']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
