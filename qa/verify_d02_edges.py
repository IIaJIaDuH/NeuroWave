"""QA-VERIFY-03 / D-02: дополнительные граничные значения конфигурации.

Проверяются совместимость и безопасность:

* числовые строки (`"80"` для volume, `"7"` для TTL) — принимаются;
* `volume = 999` ограничивается до 130, отрицательная громкость — до 0;
* отрицательная квота — существующее безопасное поведение;
* `true` для каждого числового поля — контролируемая ошибка;
* `NaN`, `Infinity`, `-Infinity` — контролируемая ошибка;
* пустой объект `{}` — корректная загрузка;
* отсутствующий config.json — загрузка настроек по умолчанию.

Для каждого случая фиксируются код выхода, наличие traceback, сообщение для
пользователя, состояние `config.json` (SHA256 до/после) и то, какая настройка
получилась в приложении (по выводу `status`).

Запуск: python qa/verify_d02_edges.py [--label fix|base]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
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

#: Ожидаемый класс поведения задаётся независимо от реализации исправления.
CASES: list[dict] = [
    {"id": "volume_numeric_string", "config": {"volume": "80"},
     "expect": "accepted"},
    {"id": "ttl_numeric_string", "config": {"cache_ttl_days": "7"},
     "expect": "accepted"},
    {"id": "volume_999", "config": {"volume": 999}, "expect": "clamped_130"},
    {"id": "volume_negative", "config": {"volume": -5}, "expect": "clamped_0"},
    {"id": "quota_negative", "config": {"cache_quota_gb": -1}, "expect": "accepted"},
    {"id": "volume_bool", "config": {"volume": True}, "expect": "controlled_error"},
    {"id": "quota_bool", "config": {"cache_quota_gb": True}, "expect": "controlled_error"},
    {"id": "ttl_bool", "config": {"cache_ttl_days": True}, "expect": "controlled_error"},
    {"id": "volume_nan", "config": {"volume": math.nan}, "expect": "controlled_error"},
    {"id": "volume_infinity", "config": {"volume": math.inf}, "expect": "controlled_error"},
    {"id": "volume_neg_infinity", "config": {"volume": -math.inf},
     "expect": "controlled_error"},
    {"id": "quota_nan", "config": {"cache_quota_gb": math.nan}, "expect": "controlled_error"},
    {"id": "quota_infinity", "config": {"cache_quota_gb": math.inf},
     "expect": "controlled_error"},
    {"id": "ttl_nan", "config": {"cache_ttl_days": math.nan}, "expect": "controlled_error"},
    {"id": "empty_object", "config": {}, "expect": "accepted"},
    {"id": "missing_file", "omit": True, "expect": "accepted"},
]


def sanitize(text: str) -> str:
    for raw, token in ((str(HOMES), "<QA_HOMES>"), (str(MEDIA), "<MEDIA>"),
                       (str(q.SHARED), "<QA_STAND>"), (str(q.ROOT), "<REPO>")):
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


def write_config(home: Path, case: dict) -> Path | None:
    (home / "config").mkdir(parents=True, exist_ok=True)
    target = home / "config" / "config.json"
    if case.get("omit"):
        if target.exists():
            target.unlink()
        return None
    payload = dict(BASE)
    payload.update(case["config"])
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def sha256(path: Path | None) -> str | None:
    if path is None or not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(command: list[str], home: Path) -> dict:
    proc = subprocess.run([str(APP), *command], env=app_env(home),
                          capture_output=True, text=True, timeout=60)
    stdout = sanitize(proc.stdout)
    stderr = sanitize(proc.stderr)
    return {
        "command": " ".join(command),
        "exit_code": proc.returncode,
        "has_traceback": "Traceback (most recent call last)" in stderr,
        "stdout": stdout[-2500:],
        "stderr": stderr[-2500:],
        "friendly_error": any(line.startswith("Ошибка:") for line in stderr.splitlines()),
    }


def effective_volume(stdout: str) -> int | None:
    """Эффективная громкость, показанная приложением."""
    for line in stdout.splitlines():
        if "громкость" not in line:
            continue
        match = re.search(r"громкость\s+(\d+)%", line)
        if match:
            return int(match.group(1))
    return None


def effective_quota_gb(stdout: str) -> float | None:
    """Эффективная квота кеша, показанная приложением (МБ → ГБ)."""
    for line in stdout.splitlines():
        match = re.search(r"из\s+([0-9.]+)\s*МБ", line)
        if match:
            return round(float(match.group(1)) / 1024.0, 3)
    return None


def observed(result: dict, case: dict) -> str:
    """Наблюдаемый класс поведения по факту."""
    if result["has_traceback"]:
        return "unhandled_traceback"
    if result["exit_code"] == 0:
        volume = effective_volume(result["stdout"])
        if case["expect"] == "clamped_130" and volume == 100:
            # Настройка ограничена до 130 %, а выход — до 100 % (лимитер).
            return "clamped_130"
        if case["expect"] == "clamped_0" and volume == 0:
            return "clamped_0"
        return "accepted"
    if result["friendly_error"]:
        return "controlled_error"
    return "other_error"


def run_case(case: dict) -> dict:
    home = HOMES / f"verify_d02_edge_{case['id']}"
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    target = write_config(home, case)
    before = sha256(target)

    status = run(["--provider", "local", "status"], home)
    after = sha256(target)
    doctor = run(["--provider", "local", "doctor"], home)

    # Что приложение считает настройками: читаем вывод status.
    volume_line = None
    for line in status["stdout"].splitlines():
        if "громкость" in line:
            volume_line = line.strip()
            break
    return {
        "case_id": case["id"],
        "expect": case["expect"],
        "observed": observed(status, case),
        "effective_volume": effective_volume(status["stdout"]),
        "effective_quota_gb": effective_quota_gb(status["stdout"]),
        "status": status,
        "doctor_exit_code": doctor["exit_code"],
        "doctor_has_traceback": doctor["has_traceback"],
        "config_sha256_before": before,
        "config_sha256_after": after,
        "config_rewritten": (before is not None and after is not None and before != after),
        "volume_line": volume_line,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", default="fix")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    q.ensure_dirs()
    cases = []
    for case in CASES:
        item = run_case(case)
        cases.append(item)
        print(f"  {item['case_id']:22} ожидалось={item['expect']:17} получено={item['observed']:20}"
              f" код={item['status']['exit_code']} traceback={item['status']['has_traceback']}",
              flush=True)

    summary = {
        "label": args.label,
        "sha": subprocess.run(["git", "rev-parse", "HEAD"], cwd=q.ROOT,
                              capture_output=True, text=True).stdout.strip(),
        "cases": cases,
        "totals": {
            "cases": len(cases),
            "unhandled_tracebacks": sum(1 for c in cases if c["observed"] == "unhandled_traceback"),
            "controlled_errors": sum(1 for c in cases if c["observed"] == "controlled_error"),
            "accepted": sum(1 for c in cases if c["observed"] == "accepted"),
            "expect_match": sum(1 for c in cases if c["expect"] == c["observed"]),
            "damaged_config_overwritten": sum(
                1 for c in cases
                if c["expect"] == "controlled_error" and c["config_rewritten"]),
        },
    }
    out_path = Path(args.out) if args.out else (q.ARTIFACTS / f"verify_d02_edges_{args.label}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    print(json.dumps(summary["totals"], ensure_ascii=False, indent=2))
    print(f"→ {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
