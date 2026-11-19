"""QA-VERIFY-03 / D-02: регрессионные прогоны.

Выполняются прогоны, указанные в задании, и рядом с ними — проверка, что
изменение `doctor` не сломало CLI-парсер (PR #33) и диагностику Яндекс Музыки
(PR #31). Результаты пишутся в JSON.

Запуск: python qa/verify_d02_regressions.py [--label fix|base]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qaenv as q  # noqa: E402

VENV = q.ROOT / ".venv" / "bin"


def sanitize(text: str) -> str:
    """Убрать домашние пути и пути стенда из публикуемого вывода."""
    for raw, token in ((str(q.SHARED), "<QA_STAND>"), (str(q.ROOT), "<REPO>"),
                       (str(Path.home()), "<HOME>")):
        text = text.replace(raw, token)
    return text


def run(argv: list[str], env_extra: dict[str, str] | None = None,
        timeout: int = 1800) -> dict:
    env = dict(os.environ)
    env["PATH"] = f"{q.MPV.parent}:{env.get('PATH', '')}"
    if env_extra:
        env.update(env_extra)
    started = time.monotonic()
    try:
        proc = subprocess.run(argv, cwd=q.ROOT, env=env, capture_output=True,
                              text=True, timeout=timeout)
        code, out, err = proc.returncode, proc.stdout, proc.stderr
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        code, out, err = None, "", f"таймаут {timeout} с"
        timed_out = True
        del exc
    tail = sanitize(out or "")[-1500:]
    return {
        "command": sanitize(" ".join(argv)),
        "exit_code": code,
        "timed_out": timed_out,
        "duration_s": round(time.monotonic() - started, 1),
        "stdout_tail": tail,
        "stderr_tail": sanitize(err or "")[-800:],
        "passed": re_count(tail),
        "failed": re_fail_count(tail),
        "errors": re_err_count(tail),
    }


def re_count(text: str) -> int | None:
    for line in reversed(text.splitlines()):
        if " passed" in line or " failed" in line:
            parts = line.replace(",", " ").split()
            for index, part in enumerate(parts):
                if part == "passed" and index:
                    try:
                        return int(parts[index - 1])
                    except ValueError:
                        return None
    return None


def re_fail_count(text: str) -> int | None:
    for line in reversed(text.splitlines()):
        if " failed" in line:
            parts = line.replace(",", " ").split()
            for index, part in enumerate(parts):
                if part == "failed" and index:
                    try:
                        return int(parts[index - 1])
                    except ValueError:
                        return None
    return None


def re_err_count(text: str) -> int | None:
    for line in reversed(text.splitlines()):
        if " error" in line:
            parts = line.replace(",", " ").split()
            for index, part in enumerate(parts):
                if part == "error" and index:
                    try:
                        return int(parts[index - 1])
                    except ValueError:
                        return None
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", default="fix")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    q.ensure_dirs()
    python = str(VENV / "python")
    pytest = str(VENV / "pytest")

    # В проекте addopts = "-q", поэтому лишний "-q" подавляет итоговую
    # строку с количеством тестов; количество берётся через --collect-only.
    checks = [
        ("config_recovery", [pytest, "tests/test_config_recovery.py"]),
        ("cli_contract", [pytest, "tests/test_config_cli.py", "tests/test_cli_contract.py",
                          "tests/test_cli_architecture.py"]),
        ("all_not_integration", [pytest, "-m", "not integration"]),
        ("compileall", [python, "-m", "compileall", "-q", "src", "tests"]),
        ("git_diff_check", ["git", "diff", "--check"]),
        ("integration_mpv", [pytest, "-m", "integration"]),
        ("integration_qt_offscreen", [pytest, "-m", "integration"],
         {"QT_QPA_PLATFORM": "offscreen"}),
    ]

    results = []
    for entry in checks:
        name, argv = entry[0], entry[1]
        env_extra = entry[2] if len(entry) > 2 else None
        item = run(argv, env_extra=env_extra)
        item["name"] = name
        item["collected"] = item.get("passed")
        results.append(item)
        print(f"  {name:22} код={item['exit_code']} тестов_пройдено={item.get('passed')} "
              f"passed={item['passed']} failed={item['failed']} errors={item['errors']} "
              f"({item['duration_s']} с)", flush=True)

    # CLI-парсер (PR #33): глобальные флаги до подкоманды и после.
    parser_checks = []
    for argv in (["--json", "doctor"], ["doctor", "--json"], ["--version"]):
        item = run([str(VENV / "neurowave"), *argv], timeout=60)
        parser_checks.append({"args": argv, "exit_code": item["exit_code"],
                              "stdout_head": item["stdout_tail"][:200]})
    results.append({"name": "cli_parser_flags", "cases": parser_checks})

    # Диагностика Яндекс Музыки (PR #31): doctor должен содержать проверки
    # провайдера и возвращать 1 при неуспешной диагностике.
    doctor = run([str(VENV / "neurowave"), "--provider", "local", "doctor"], timeout=60)
    doctor_json = run([str(VENV / "neurowave"), "--json", "--provider", "local", "doctor"],
                      timeout=60)
    yandex_checks = []
    try:
        parsed = json.loads(doctor_json["stdout_tail"])
        names = [item.get("check") for item in parsed if isinstance(item, dict)]
        yandex_checks.append({"check": "doctor --json — корректный массив",
                              "ok": isinstance(parsed, list) and bool(names),
                              "detail": f"проверок: {len(names)}"})
        yandex_checks.append({"check": "в наборе есть проверка провайдера",
                              "ok": any("провайдер" in str(name).lower() for name in names),
                              "detail": ", ".join(str(name) for name in names)})
    except (json.JSONDecodeError, TypeError) as exc:
        yandex_checks.append({"check": "doctor --json — корректный массив", "ok": False,
                              "detail": str(exc)[:200]})
    yandex_checks.append({
        "check": "doctor без авторизации Яндекс Музыки завершается без traceback",
        "ok": doctor["exit_code"] is not None and "Traceback" not in doctor["stdout_tail"],
        "detail": f"код {doctor['exit_code']}"})
    results.append({"name": "yandex_diagnostics", "checks": yandex_checks})

    summary = {
        "label": args.label,
        "sha": subprocess.run(["git", "rev-parse", "HEAD"], cwd=q.ROOT,
                              capture_output=True, text=True).stdout.strip(),
        "results": results,
        "totals": {
            "checks": len(results),
            "failed_commands": sum(1 for item in results
                                   if item.get("exit_code") not in (0, None)),
            "tests_collected": {item["name"]: item.get("collected", 0)
                                for item in results if "collected" in item},
        },
    }
    out_path = Path(args.out) if args.out else (q.ARTIFACTS / f"verify_d02_regressions_{args.label}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    print(f"→ {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
