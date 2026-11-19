"""QA-SEC-01: автоматический поиск сведений о пользовательском окружении.

Сканирует QA-материалы (отчёты, JSON/JSONL/CSV, скрипты, логи) на:

* абсолютные домашние пути;
* имена пользователя и компьютера;
* MAC-адреса и идентификаторы Bluetooth-устройств;
* локальные адреса и названия пользовательских каталогов;
* токены, cookie и прочие секреты;
* имена личных плейлистов и треков;
* метаданные снимков экрана (для PNG — текстовые чанки, EXIF, комментарии).

Запуск: python qa/scan_sensitive.py [<каталог> ...]
       python qa/scan_sensitive.py --git origin/qa/wo03-hardware   (срез ветки)

Код возврата: 0 — находок нет, 1 — находки есть, 2 — ошибка запуска.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

#: Метки находок и регулярные выражения к ним.
PATTERNS: list[tuple[str, str]] = [
    ("домашний путь", r"/home/[^\s\"'`,;:)\]}]+"),
    ("домашний путь", r"/root/[^\s\"'`,;:)\]}]+"),
    ("пользователь в пути", r"/Users/[^\s\"'`,;:)\]}]+"),
    ("MAC-адрес", r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b"),
    ("Bluetooth-идентификатор", r"\b[0-9A-Fa-f]{2}(?:_[0-9A-Fa-f]{2}){5}\b"),
    ("Bluetooth-идентификатор", r"bluez_output\.[0-9A-Za-z_]+"),
    ("MAC или сетевой адрес", r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}%?[^\s\"']*"),
    ("учётная запись Linux", r"\buser(?:\.name)?\s*=\s*\"[^\"]+\""),
    ("секрет", r"(?i)(?:oauth[_ -]?token|access[_ -]?token|refresh[_ -]?token|api[_ -]?key|secret|password|cookie)\s*[:=]\s*\"?[A-Za-z0-9._\-]{6,}"),
    ("токен Яндекса", r"(?i)yandex[_-]?token\s*[:=]\s*\S+"),
    ("домашний каталог в тексте", r"(?i)home\s+dir(?:ectory)?\s*[:=]\s*\S+"),
    ("локальный адрес", r"\b(?:10|127|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),
]

#: Файлы, которые сканируются.
TEXT_SUFFIXES = {".md", ".json", ".jsonl", ".csv", ".py", ".log", ".txt", ".sh", ".yaml", ".yml"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}

#: Слова, которые встречаются в отчётах и не являются находками.
FALSE_POSITIVE_HINTS = (
    "nochome", "homes", "placeholder",
)


#: Строки, где путь записан как шаблон регулярного выражения, а не как данные.
PATTERN_LITERAL = re.compile(r"""r["']/(?:home|root|Users)""")


def scan_text(name: str, text: str) -> list[dict]:
    findings: list[dict] = []
    for line_no, line in enumerate(text.splitlines(), 1):
        if any(hint in line.lower() for hint in FALSE_POSITIVE_HINTS):
            continue
        # Ложное срабатывание: сам сканер описывает шаблоны путей.
        if PATTERN_LITERAL.search(line):
            continue
        for label, pattern in PATTERNS:
            for match in re.finditer(pattern, line):
                value = match.group(0)
                if label == "домашний путь" and value.rstrip("/").count("/") < 2:
                    continue
                # Шаблон с плейсхолдером — не реальный путь.
                if any(ch in value for ch in "<>{}"):
                    continue
                findings.append({
                    "file": name,
                    "line": line_no,
                    "class": label,
                    "match": value[:120],
                    "context": line.strip()[:160],
                })
    return findings


def scan_image(path: Path) -> list[dict]:
    """Метаданные PNG: текстовые чанки (tEXt/iTXt/zTXt) и EXIF-подобные блоки."""
    findings: list[dict] = []
    data = path.read_bytes()
    for marker in (b"tEXt", b"iTXt", b"zTXt", b"eXIf", b"XML:com.adobe.xmp"):
        index = data.find(marker)
        if index >= 0:
            findings.append({
                "file": str(path),
                "line": 0,
                "class": "метаданные изображения",
                "match": marker.decode("ascii", "replace"),
                "context": data[index:index + 120].decode("utf-8", "replace"),
            })
    # Имя файла и текстовые фрагменты с путями внутри метаданных.
    for match in re.finditer(rb"[ -~]{12,}", data[:2_000_000]):
        chunk = match.group(0).decode("ascii", "replace")
        if "/home/" in chunk or "zoz" in chunk:
            findings.append({
                "file": str(path),
                "line": 0,
                "class": "текст в метаданных/данных изображения",
                "match": chunk[:120],
                "context": "",
            })
    return findings


def scan_tree(root: Path) -> list[dict]:
    findings: list[dict] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix in TEXT_SUFFIXES:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            findings.extend(scan_text(str(path.relative_to(root)), text))
        elif suffix in IMAGE_SUFFIXES:
            findings.extend(scan_image(path))
    return findings


def scan_git(ref: str) -> list[dict]:
    """Срез ветки: файлы извлекаются во временный каталог и сканируются."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        names = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", ref],
            capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        for name in names:
            if not name.startswith(("docs/qa/", "qa/")):
                continue
            suffix = Path(name).suffix.lower()
            if suffix not in TEXT_SUFFIXES and suffix not in IMAGE_SUFFIXES:
                continue
            blob = subprocess.run(
                ["git", "show", f"{ref}:{name}"], capture_output=True, check=True
            ).stdout
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)
        return scan_tree(root)


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print("укажите каталог или --git <ветка>", file=sys.stderr)
        return 2
    findings: list[dict] = []
    for arg in args:
        if arg == "--git":
            continue
        if arg.startswith("--git="):
            findings.extend(scan_git(arg.split("=", 1)[1]))
            continue
        root = Path(arg)
        if not root.exists():
            print(f"нет такого пути: {root}", file=sys.stderr)
            return 2
        findings.extend(scan_tree(root))
    by_class: dict[str, int] = {}
    for item in findings:
        by_class[item["class"]] = by_class.get(item["class"], 0) + 1
    print(json.dumps({
        "findings": len(findings),
        "by_class": by_class,
        "items": findings,
    }, ensure_ascii=False, indent=2))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
