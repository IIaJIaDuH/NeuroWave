"""QA-VERIFY-03 / D-02: контракт `doctor` (обычный и `--json`).

Проверяется:

* для пяти исходных повреждённых значений и для нарушенного синтаксиса
  JSON: код выхода, наличие неуспешной проверки «Конфигурация», сохранение
  остальных диагностик;
* `doctor --json`: stdout — корректный JSON-массив объектов с ключами
  `check`, `ok`, `detail`; в stdout нет постороннего текста;
* `provider: "local"` + `volume: "громко"` — запуск с явным `--provider local`
  и без него (известное замечание P2 про провайдера по умолчанию при
  повреждённой конфигурации фиксируется отдельно, регрессией D-02 не
  считается).

Запуск: python qa/verify_d02_doctor.py [--label fix|base]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qaenv as q  # noqa: E402

MEDIA = Path(os.environ.get("QA_MEDIA") or (q.SHARED / "media"))
HOMES = q.HOMES
APP = q.ROOT / ".venv" / "bin" / "neurowave"

BASE = {"provider": "local", "local_music_dir": str(MEDIA), "volume": 40}

#: Повреждённые значения: пять P1-случаев + нарушенный синтаксис JSON.
CASES: list[dict] = [
    {"id": "volume_string", "config": {"volume": "громко"}},
    {"id": "volume_null", "config": {"volume": None}},
    {"id": "quota_string", "config": {"cache_quota_gb": "много"}},
    {"id": "quota_null", "config": {"cache_quota_gb": None}},
    {"id": "ttl_string", "config": {"cache_ttl_days": "неделя"}},
    {"id": "broken_json", "raw": '{"volume": 80,,}'},
]


def sanitize(text: str) -> str:
    for raw, token in ((str(HOMES), "<QA_HOMES>"), (str(MEDIA), "<MEDIA>"),
                       (str(q.SHARED), "<QA_STAND>"), (str(q.ROOT), "<REPO>"),
                       (str(Path.home()), "<HOME>")):
        text = text.replace(raw, token)
    return text


def app_env(home: Path) -> dict[str, str]:
    return {
        "NEUROWAVE_HOME": str(home),
        "PATH": f"{q.MPV.parent}:{os.environ.get('PATH', '')}",
        "PYTHONPATH": str(q.ROOT / "src"),
        "DISPLAY": os.environ.get("DISPLAY", ":0"),
        "HOME": str(home),
        "LANG": "ru_RU.UTF-8",
    }


def write_config(home: Path, case: dict) -> None:
    (home / "config").mkdir(parents=True, exist_ok=True)
    target = home / "config" / "config.json"
    if case.get("raw") is not None:
        target.write_text(case["raw"], encoding="utf-8")
        return
    payload = dict(BASE)
    payload.update(case["config"])
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run(command: list[str], home: Path) -> dict:
    proc = subprocess.run([str(APP), *command], env=app_env(home),
                          capture_output=True, text=True, timeout=60)
    return {
        "command": " ".join(command),
        "exit_code": proc.returncode,
        "stdout": sanitize(proc.stdout),
        "stderr": sanitize(proc.stderr),
        "has_traceback": "Traceback (most recent call last)" in proc.stderr,
    }


def analyze_json_output(result: dict) -> dict:
    """Разбор stdout как JSON-массива объектов {check, ok, detail}."""
    analysis: dict = {
        "json_parsed": False,
        "is_array": False,
        "items": [],
        "entry_keys_ok": True,
        "config_check_present": False,
        "config_check_ok": None,
        "failed_checks": [],
        "checks_count": 0,
        "stdout_clean": True,
        "parse_error": None,
    }
    text = result["stdout"]
    try:
        data = json.loads(text)
        analysis["json_parsed"] = True
    except json.JSONDecodeError as exc:
        analysis["parse_error"] = str(exc)
        analysis["stdout_clean"] = False
        return analysis
    if not isinstance(data, list):
        analysis["stdout_clean"] = False
        return analysis
    analysis["is_array"] = True
    analysis["checks_count"] = len(data)
    for item in data:
        if not isinstance(item, dict) or not {"check", "ok", "detail"} <= set(item):
            analysis["entry_keys_ok"] = False
            continue
        name = str(item["check"])
        ok = bool(item["ok"])
        analysis["items"].append({"check": name, "ok": ok,
                                   "detail": sanitize(str(item["detail"]))[:400]})
        if name == "Конфигурация":
            analysis["config_check_present"] = True
            analysis["config_check_ok"] = ok
        if not ok:
            analysis["failed_checks"].append(name)
    return analysis


def run_case(case: dict) -> dict:
    home = HOMES / f"verify_d02_doctor_{case['id']}"
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    write_config(home, case)

    plain = run(["--provider", "local", "doctor"], home)
    # Флаг --json глобальный (до подкоманды) — контракт CLI из PR #33.
    as_json = run(["--json", "--provider", "local", "doctor"], home)
    # Форма после подкоманды проверяется отдельно: её поведение должно
    # совпадать с main и не является частью D-02.
    after_subcommand = run(["--provider", "local", "doctor", "--json"], home)
    json_analysis = analyze_json_output(as_json)

    plain_lines = [line for line in plain["stdout"].splitlines() if line.strip()]
    failed_in_text = [line for line in plain_lines if line.startswith("✗")]

    return {
        "case_id": case["id"],
        "plain": {
            "exit_code": plain["exit_code"],
            "has_traceback": plain["has_traceback"],
            "checks_total": len(plain_lines),
            "failed_checks": failed_in_text,
            "mentions_config": any("Конфигурация" in line for line in plain_lines),
            "mentions_field": case["id"].split("_")[0] in plain["stdout"],
            "stderr": plain["stderr"][-1500:],
        },
        "json_flag_after_subcommand": {
            "exit_code": after_subcommand["exit_code"],
            "stderr_head": after_subcommand["stderr"].strip().splitlines()[-1:] or [""],
            "note": ("--json — глобальный флаг (до подкоманды); форма после "
                     "подкоманды отклоняется парсером одинаково на main и на "
                     "проверяемой ветке."),
        },
        "json": {
            "exit_code": as_json["exit_code"],
            "has_traceback": as_json["has_traceback"],
            "stderr": as_json["stderr"][-1500:],
            **json_analysis,
        },
    }


def run_provider_variant() -> dict:
    """`provider: local` + `volume: громко`: с явным --provider local и без."""
    home = HOMES / "verify_d02_doctor_provider"
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    payload = dict(BASE)
    payload.update({"volume": "громко"})
    (home / "config").mkdir(parents=True, exist_ok=True)
    (home / "config" / "config.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    with_flag = run(["--provider", "local", "status"], home)
    without_flag = run(["status"], home)
    return {
        "with_provider_flag": {
            "exit_code": with_flag["exit_code"],
            "has_traceback": with_flag["has_traceback"],
            "stdout": with_flag["stdout"][-800:],
            "stderr": with_flag["stderr"][-800:],
        },
        "without_provider_flag": {
            "exit_code": without_flag["exit_code"],
            "has_traceback": without_flag["has_traceback"],
            "stdout": without_flag["stdout"][-800:],
            "stderr": without_flag["stderr"][-800:],
        },
        "note": ("Известное замечание P2: без явного --provider команда status "
                 "обращается к провайдеру по умолчанию (yandex) и может завершиться "
                 "с ошибкой авторизации раньше, чем сообщит о повреждённой "
                 "конфигурации; регрессией D-02 это не является."),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", default="fix")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    q.ensure_dirs()
    cases = [run_case(case) for case in CASES]
    provider = run_provider_variant()

    summary = {
        "label": args.label,
        "sha": subprocess.run(["git", "rev-parse", "HEAD"], cwd=q.ROOT,
                              capture_output=True, text=True).stdout.strip(),
        "cases": cases,
        "provider_variant": provider,
        "summary": {
            "cases": len(cases),
            "unhandled_tracebacks": sum(
                1 for c in cases for key in ("plain", "json")
                if c[key]["has_traceback"]),
            "doctor_json_valid": sum(1 for c in cases if c["json"]["json_parsed"]),
            "doctor_json_array": sum(1 for c in cases if c["json"]["is_array"]),
            "doctor_json_keys_ok": sum(1 for c in cases if c["json"]["entry_keys_ok"]),
            "config_check_failed": sum(
                1 for c in cases if c["json"]["config_check_present"]
                and c["json"]["config_check_ok"] is False),
            "doctor_exit_code_1": sum(1 for c in cases if c["plain"]["exit_code"] == 1),
        },
    }
    out_path = Path(args.out) if args.out else (q.ARTIFACTS / f"verify_d02_doctor_{args.label}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    print(json.dumps(summary["summary"], ensure_ascii=False, indent=2))
    print(f"→ {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
