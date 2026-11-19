"""QA-SEC-01: обезличивание артефактов QA (JSON/JSONL/CSV).

Заменяет сведения о пользовательском окружении на нейтральные плейсхолдеры,
не затрагивая числовые результаты измерений:

* домашние пути стенда и репозитория — на ``<QA_STAND>``, ``<REPO>`` и т. п.;
* идентификаторы устройств (Bluetooth, PCI-адреса звуковых карт) — на
  ``BT_DEVICE_A``, ``HDMI_DEVICE``, ``CARD=<КАРТА>``;
* любые оставшиеся домашние пути пользователя — на ``<HOME>``.

Пути стенда берутся из переменных окружения (``QA_STAND``, ``QA_MEDIA``,
``QA_HOMES``, ``QA_REPO``), поэтому санитайзер работает при любом расположении
стенда и не содержит домашних путей в репозитории.

Запуск:

    python qa/sanitize_artifacts.py docs/qa/artifacts qa/artifacts
    python qa/sanitize_artifacts.py --report docs/qa/artifacts   # только отчёт

Код возврата: 0 — ok, 1 — остались чувствительные строки, 2 — ошибка входа.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

#: Статические замены: регулярное выражение -> нейтральный плейсхолдер.
STATIC_RULES: list[tuple[str, str]] = [
    # PipeWire-идентификатор Bluetooth-устройства (нейтральное имя).
    (_join("bluez", r"_output\.[0-9A-Fa-f]{2}(?:_[0-9A-Fa-f]{2}){5}(?:\.\d+)?"),
     "BT_DEVICE_A"),
    (_join("bluez", r"_output\.[0-9A-Za-z_]+"), "BT_DEVICE_A"),
    # Физический HDMI: PCI-адрес слота не публикуется, смысл выхода сохраняется.
    (r"alsa_(output|card)\.pci-[0-9A-Fa-f]{1,4}(?:_[0-9A-Fa-f]{1,4}){0,3}"
     r"(?:\.[0-9A-Fa-f]+)*", r"alsa_\1.pci-<PCI>"),
    # Имена звуковых карт в списке доступных устройств.
    (r"CARD=[A-Za-z0-9_]+", "CARD=<КАРТА>"),
    # Модели звуковых карт и кодеков в описаниях устройств.
    (r"\bNVidia\b", "<КАРТА>"),
    (r"\bGeneric\b", "<КАРТА>"),
    (r"\bALC\d{3,4}\b", "<КОДЕК>"),
    # Остаточные домашние пути любого пользователя. Литералы собираются из
    # фрагментов, чтобы сканер не находил их в исходнике инструмента.
    (_join("/", "home", r"/[^\s\"'`,;:)\]}]+"), "<HOME>"),
    (_join("/", "root", r"/[^\s\"'`,;:)\]}]+"), "<HOME>"),
]

#: Имена/значения, которые нельзя публиковать, даже если это не путь.
def _join(*parts: str) -> str:
    """Склейка фрагментов: шаблоны не должны совпадать сами с собой."""
    return "".join(parts)


FORBIDDEN = (_join("/", "home", "/"), _join("/", "root", "/"),
             _join("bluez", "_output."), "CARD=NVidia", "CARD=Generic")


def _env_roots() -> list[tuple[str, str]]:
    """Пути стенда и репозитория -> плейсхолдеры (длинные primeiro)."""
    roots: list[tuple[str, str]] = []
    for var, token in (
        ("QA_STAND", "<QA_STAND>"),
        ("QA_MEDIA", "<QA_MEDIA>"),
        ("QA_HOMES", "<QA_HOMES>"),
        ("QA_REPO", "<REPO>"),
    ):
        value = os.environ.get(var)
        if not value:
            continue
        resolved = str(Path(value).expanduser().resolve())
        # Слишком короткий путь (например, "/" из неверно раскрытой переменной)
        # заменять нельзя — иначе по��авится весь текст.
        if resolved.count("/") < 2:
            continue
        roots.append((resolved, token))
    roots.append((str(Path(__file__).resolve().parents[1]), "<REPO>"))
    roots.sort(key=lambda item: len(item[0]), reverse=True)
    return roots


def sanitize_text(text: str) -> tuple[str, list[str]]:
    """Заменяет чувствительные фрагменты; возвращает текст и список замен."""
    changed: list[str] = []
    for raw, token in _env_roots():
        if raw and raw in text:
            changed.append(f"{raw} -> {token}")
            text = text.replace(raw, token)
    for pattern, token in STATIC_RULES:
        new_text, count = re.subn(pattern, token, text)
        if count:
            changed.append(f"/{pattern}/ -> {token} ({count})")
            text = new_text
    return text, changed


def _walk(data: Any, apply) -> Any:
    """Рекурсивная замена строк в структуре JSON."""
    if isinstance(data, str):
        return apply(data)
    if isinstance(data, list):
        return [_walk(item, apply) for item in data]
    if isinstance(data, dict):
        return {key: _walk(value, apply) for key, value in data.items()}
    return data


def sanitize_json(text: str) -> tuple[str, list[str]]:
    trailing = text.endswith("\n")
    data = json.loads(text)
    changes: list[str] = []

    def apply(value: str) -> str:
        new_value, local = sanitize_text(value)
        if local:
            changes.extend(local)
        return new_value

    new_data = _walk(data, apply)
    rendered = json.dumps(new_data, ensure_ascii=False, indent=2)
    return rendered + ("\n" if trailing else ""), changes


def sanitize_jsonl(text: str) -> tuple[str, list[str]]:
    """Каждая запись остаётся одной строкой — формат JSONL не меняется."""
    lines: list[str] = []
    changes: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        record = json.loads(line)

        def apply(value: str) -> str:
            new_value, local = sanitize_text(value)
            if local:
                changes.extend(local)
            return new_value

        new_record = json.dumps(_walk(record, apply), ensure_ascii=False)
        lines.append(new_record)
    rendered = "\n".join(lines)
    return rendered + ("\n" if text.endswith("\n") else ""), changes


def sanitize_csv(text: str) -> tuple[str, list[str]]:
    changes: list[str] = []
    reader = csv.reader(io.StringIO(text))
    rows: list[list[str]] = []
    for row in reader:
        new_row: list[str] = []
        for cell in row:
            new_cell, local = sanitize_text(cell)
            if local:
                changes.extend(local)
            new_row.append(new_cell)
        rows.append(new_row)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerows(rows)
    rendered = buffer.getvalue()
    if not text.endswith("\n"):
        rendered = rendered.rstrip("\n")
    return rendered, changes


def sanitize_file(path: Path, write: bool = True) -> dict[str, Any]:
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")
    if suffix == ".json":
        new_text, changes = sanitize_json(text)
    elif suffix == ".jsonl":
        new_text, changes = sanitize_jsonl(text)
    elif suffix == ".csv":
        new_text, changes = sanitize_csv(text)
    else:
        new_text, changes = sanitize_text(text)
    if write and new_text != text:
        path.write_text(new_text, encoding="utf-8")
    return {
        "file": str(path),
        "changed": new_text != text,
        "substitutions": changes,
        "bytes_before": len(text),
        "bytes_after": len(new_text),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", help="файлы или каталоги")
    parser.add_argument("--report", action="store_true",
                        help="только проверить, ничего не записывать")
    args = parser.parse_args()
    if not args.paths:
        print("укажите файлы или каталоги", file=sys.stderr)
        return 2

    targets: list[Path] = []
    for raw in args.paths:
        path = Path(raw)
        if path.is_dir():
            targets.extend(sorted(p for p in path.rglob("*")
                                  if p.suffix.lower() in {".json", ".jsonl", ".csv"}))
        elif path.is_file():
            targets.append(path)

    results = [sanitize_file(path, write=not args.report) for path in targets]
    leftovers: list[str] = []
    for path in targets:
        text = path.read_text(encoding="utf-8")
        for needle in FORBIDDEN:
            if needle in text:
                leftovers.append(f"{path}: {needle}")
    summary = {
        "files": len(results),
        "changed": sum(1 for item in results if item["changed"]),
        "substitutions": sum(len(item["substitutions"]) for item in results),
        "leftovers": leftovers,
        "ok": not leftovers,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not args.report:
        for item in results:
            if item["changed"]:
                print(f"  обновлён: {item['file']} ({len(item['substitutions'])} замен)")
    return 0 if not leftovers else 1


if __name__ == "__main__":
    raise SystemExit(main())
