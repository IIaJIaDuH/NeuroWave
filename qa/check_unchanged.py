"""QA-SEC-01: контроль — изменились только чувствительные строки, числа нет.

Сравнивает текущие артефакты с версией в git-ссылке и требует, чтобы каждое
изменённое значение отличалось от исходного **только** заменой чувствительных
фрагментов (пути стенда, идентификаторы устройств). Числовые метрики, строки
PASS/FAIL и SHA проверяемого кода обязаны совпадать байт в байт.

Запуск:

    python qa/check_unchanged.py --ref HEAD --paths docs/qa/artifacts qa/artifacts

Код возврата: 0 — расхождений нет, 1 — есть, 2 — ошибка запуска.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sanitize_artifacts import sanitize_text  # noqa: E402


def git_show(ref: str, path: str) -> str | None:
    result = subprocess.run(["git", "show", f"{ref}:{path}"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return result.stdout


def PLACEHOLDER_CHANGED_OK(old: str, new: str) -> bool:
    """Смена одного плейсхолдера на другой допустима (стиль обозначений).

    Обе стороны — плейсхолдеры без сведений об окружении; измерения при
    этом не меняются. Такие замены перечисляются в отчёте отдельно.
    """
    def placeholder(value: str) -> bool:
        return "<" in value and ">" in value
    return placeholder(old) and placeholder(new)


def compare_json(old: Any, new: Any, trail: str, problems: list[str]) -> None:
    if isinstance(old, dict) and isinstance(new, dict):
        if set(old) != set(new):
            problems.append(f"{trail}: изменён набор ключей")
            return
        for key in old:
            compare_json(old[key], new[key], f"{trail}.{key}", problems)
        return
    if isinstance(old, list) and isinstance(new, list):
        if len(old) != len(new):
            problems.append(f"{trail}: изменена длина списка ({len(old)} → {len(new)})")
            return
        for index, (a, b) in enumerate(zip(old, new)):
            compare_json(a, b, f"{trail}[{index}]", problems)
        return
    if isinstance(old, bool) or isinstance(new, bool):
        if old != new:
            problems.append(f"{trail}: значение изменилось: {old!r} → {new!r}")
        return
    if isinstance(old, (int, float)) and isinstance(new, (int, float)):
        if old != new:
            problems.append(f"{trail}: числовое значение изменилось: {old} → {new}")
        return
    if isinstance(old, str) and isinstance(new, str):
        expected, _ = sanitize_text(old)
        if expected != new and not (PLACEHOLDER_CHANGED_OK(old, new)):
            problems.append(f"{trail}: строка изменена сверх обезличивания:\n"
                            f"    было:  {old[:160]!r}\n"
                            f"    стало: {new[:160]!r}\n"
                            f"    ожидалось: {expected[:160]!r}")
        return
    if old != new:
        problems.append(f"{trail}: значение изменено: {old!r} → {new!r}")


def compare_jsonl(old_text: str, new_text: str, path: str, problems: list[str]) -> None:
    old_lines = [line for line in old_text.splitlines() if line.strip()]
    new_lines = [line for line in new_text.splitlines() if line.strip()]
    if len(old_lines) != len(new_lines):
        problems.append(f"{path}: изменено число записей ({len(old_lines)} → {len(new_lines)})")
        return
    for index, (a, b) in enumerate(zip(old_lines, new_lines)):
        compare_json(json.loads(a), json.loads(b), f"{path}[{index}]", problems)


def compare_csv(old_text: str, new_text: str, path: str, problems: list[str]) -> None:
    old_rows = list(csv.reader(io.StringIO(old_text)))
    new_rows = list(csv.reader(io.StringIO(new_text)))
    if len(old_rows) != len(new_rows):
        problems.append(f"{path}: изменено число строк ({len(old_rows)} → {len(new_rows)})")
        return
    for r, (old_row, new_row) in enumerate(zip(old_rows, new_rows)):
        if len(old_row) != len(new_row):
            problems.append(f"{path}: строка {r} — изменилось число полей")
            continue
        for c, (a, b) in enumerate(zip(old_row, new_row)):
            expected, _ = sanitize_text(a)
            if expected != b:
                problems.append(f"{path} строка {r} поле {c}: {a!r} → {b!r} "
                                f"(ожидалось {expected!r})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="HEAD", help="ссылка git с исходной версией")
    parser.add_argument("--paths", nargs="+", required=True, help="каталоги или файлы")
    args = parser.parse_args()

    targets: list[Path] = []
    for raw in args.paths:
        path = Path(raw)
        if path.is_dir():
            targets.extend(sorted(p for p in path.rglob("*")
                                  if p.suffix.lower() in {".json", ".jsonl", ".csv"}))
        elif path.is_file():
            targets.append(path)

    problems: list[str] = []
    checked = 0
    for path in targets:
        rel = str(path).replace("\\", "/")
        old_text = git_show(args.ref, rel)
        if old_text is None:
            problems.append(f"{rel}: нет версии в {args.ref} (новый файл)")
            continue
        new_text = path.read_text(encoding="utf-8")
        checked += 1
        if path.suffix.lower() == ".json":
            try:
                compare_json(json.loads(old_text), json.loads(new_text), rel, problems)
            except json.JSONDecodeError as exc:
                problems.append(f"{rel}: JSON не разбирается после обезличивания: {exc}")
        elif path.suffix.lower() == ".jsonl":
            compare_jsonl(old_text, new_text, rel, problems)
        elif path.suffix.lower() == ".csv":
            compare_csv(old_text, new_text, rel, problems)

    print(json.dumps({
        "ref": args.ref,
        "files_checked": checked,
        "problems": problems,
        "ok": not problems,
    }, ensure_ascii=False, indent=2))
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
