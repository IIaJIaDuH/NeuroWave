"""QA-SEC-02: автоматический поиск сведений о пользовательском окружении.

Сканирует QA-материалы (отчёты, JSON/JSONL/CSV, скрипты, логи, снимки
экрана) на:

* абсолютные домашние пути;
* MAC-адреса и идентификаторы Bluetooth-устройств;
* локальные адреса и PCI-слоты звуковых карт;
* токены, ключи, пароли и cookie;
* метаданные изображений (текстовые чанки PNG, EXIF, XMP).

Контракт:

* ``0`` — чувствительных строк не найдено;
* ``1`` — найдены чувствительные строки;
* ``2`` — некорректный запуск или ошибка сканирования.

Запуск:

    python qa/scan_sensitive.py --git HEAD          # содержимое git-ссылки
    python qa/scan_sensitive.py --git=HEAD          # та же форма
    python qa/scan_sensitive.py docs/qa qa          # обычные каталоги

Особенности (QA-SEC-02):

* обработка аргументов через argparse: поддержаны обе формы ``--git REF``
  и ``--git=REF``;
* никаких пропусков целых строк: безопасные значения распознаются
  **по совпадению**, а не по общим подстрокам в строке; реальный домашний
  путь не маскируется безопасным плейсхолдером в той же строке;
* в отчёте не публикуется исходное чувствительное значение — только класс
  находки, файл, номер строки и редактированное значение.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2

#: Каталоги и файлы, которые сканируются по git-ссылке.
SCAN_PREFIXES = ("docs/qa/", "qa/")

#: Расширения файлов с текстовым содержимым.
TEXT_SUFFIXES = {".md", ".json", ".jsonl", ".csv", ".py", ".log", ".txt", ".sh",
                 ".yaml", ".yml", ".toml", ".cfg", ".ini"}

#: Расширения файлов с изображениями.
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


class ScannerError(Exception):
    """Ошибка запуска или сканирования — без трассировки, код возврата 2."""


def _join(*parts: str) -> str:
    """Склейка фрагментов.

    Нужна, чтобы шаблоны в исходнике сканера не совпадали сами с собой:
    иначе сканер находил бы собственные регулярные выражения.
    """
    return "".join(parts)


#: Классы находок и их шаблоны.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("домашний путь", re.compile(_join("/", "home", r"/[^\s\"'`,;:)\]}]+"))),
    ("домашний путь", re.compile(_join("/", "root", r"/[^\s\"'`,;:)\]}]+"))),
    ("каталог пользователя", re.compile(_join("/", "Users", r"/[^\s\"'`,;:)\]}]+"))),
    ("MAC-адрес", re.compile(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b")),
    ("Bluetooth-идентификатор", re.compile(r"\b[0-9A-Fa-f]{2}(?:_[0-9A-Fa-f]{2}){5}\b")),
    ("Bluetooth-идентификатор",
     re.compile(_join("bluez", r"_output\.[0-9A-Za-z_]+"))),
    ("локальный адрес",
     re.compile(r"\b(?:10|127)\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"
                r"|\b192\.168\.\d{1,3}\.\d{1,3}\b"
                r"|\b172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b")),
    ("PCI-адрес слота",
     re.compile(r"\bpci-[0-9A-Fa-f]{4}(?:_[0-9A-Fa-f]{2,4}){0,3}(?:\.[0-9A-Fa-f]+)?")),
    ("секрет",
     re.compile(r"(?i)\b(?:oauth[_ -]?token|access[_ -]?token|refresh[_ -]?token"
                r"|api[_ -]?key|secret|password|passwd|cookie|token|bearer)\b"
                r"[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9._\-]{8,})")),
)

#: Явные маркеры синтетических значений: такие значения в QA-материалах не
#: являются секретами. Проверка — по самому значению, а не по строке.
SYNTHETIC_PREFIXES = ("qa-", "test-", "fake-", "dummy-", "example-", "sample-",
                      "xxxx", "notasecret")

#: Значения, которые заведомо безопасны (нейтральные обозначения в
#: обезличенных материалах). Сравнение точное, по всему совпадению.
SAFE_VALUES = frozenset({
    "BT_DEVICE_A", "ALSA_DEVICE", "HDMI_DEVICE",
})

#: Плейсхолдер вида <HOME>, <QA_STAND>, <PCI> и т. п. — безопасное значение
#: целиком (не в сочетании с другим текстом).
PLACEHOLDER_RE = re.compile(r"^<[^<>\s]{1,64}>$")

#: Фрагменты, которые нельзя публиковать в отчёте.
REDACT_RE = re.compile(r"[A-Za-z0-9А-Яа-яЁё]")


def is_safe_value(value: str) -> bool:
    """Совпадение целиком безопасно (плейсхолдер или нейтральное имя)."""
    return value in SAFE_VALUES or bool(PLACEHOLDER_RE.match(value))


def is_synthetic_secret(value: str) -> bool:
    """Значение помечено как синтетическое (qa-…, fake-…, test-…)."""
    return value.lower().startswith(SYNTHETIC_PREFIXES)


def redact(value: str) -> str:
    """Редактированное значение: видны разделители, символы заменены."""
    return REDACT_RE.sub("*", value)


def scan_text(name: str, text: str) -> list[dict[str, Any]]:
    """Поиск чувствительных совпадений в тексте.

    Пропускаются только безопасные совпадения целиком; строка целиком
    никогда не пропускается.
    """
    findings: list[dict[str, Any]] = []
    for line_no, line in enumerate(text.splitlines(), 1):
        for label, pattern in PATTERNS:
            for match in pattern.finditer(line):
                value = match.group(0)
                if is_safe_value(value):
                    continue
                if label == "секрет":
                    candidate = match.group(1)
                    if is_synthetic_secret(candidate):
                        continue
                findings.append({
                    "file": name,
                    "line": line_no,
                    "class": label,
                    "match": redact(value),
                })
    return findings


def scan_image(path: Path) -> list[dict[str, Any]]:
    """Метаданные изображений: текстовые чанки, EXIF/XMP и текстовые следы."""
    findings: list[dict[str, Any]] = []
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ScannerError(f"не удалось прочитать {path}: {exc}") from None
    for marker in (b"tEXt", b"iTXt", b"zTXt", b"eXIf", b"XML:com.adobe.xmp"):
        index = data.find(marker)
        if index >= 0:
            findings.append({
                "file": str(path),
                "line": 0,
                "class": "метаданные изображения",
                "match": redact(marker.decode("ascii", "replace")),
            })
    for match in re.finditer(rb"[ -~]{12,}", data[:2_000_000]):
        chunk = match.group(0).decode("ascii", "replace")
        if "/" + "home" + "/" in chunk or "bluez" + "_output." in chunk:
            findings.append({
                "file": str(path),
                "line": 0,
                "class": "текст в метаданных изображения",
                "match": redact(chunk),
            })
    return findings


def scan_tree(root: Path) -> list[dict[str, Any]]:
    """Сканирование каталога."""
    findings: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        try:
            if suffix in TEXT_SUFFIXES:
                text = path.read_text(encoding="utf-8", errors="replace")
                findings.extend(scan_text(str(path), text))
            elif suffix in IMAGE_SUFFIXES:
                findings.extend(scan_image(path))
        except OSError as exc:
            raise ScannerError(f"не удалось прочитать {path}: {exc}") from None
    return findings


def _git(args: list[str], cwd: str | None = None) -> str:
    """Запуск git без трассировки: ошибка — ScannerError (код 2)."""
    try:
        result = subprocess.run(["git", *args], capture_output=True, text=True,
                                cwd=cwd)
    except OSError as exc:
        raise ScannerError(f"не удалось запустить git: {exc}") from None
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "").strip().splitlines()
        detail = message[0] if message else f"код {result.returncode}"
        raise ScannerError(f"git {' '.join(args)}: {detail}")
    return result.stdout


def scan_git(ref: str) -> list[dict[str, Any]]:
    """Сканирование содержимого git-ссылки (только docs/qa/ и qa/)."""
    _git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
    names = _git(["ls-tree", "-r", "--name-only", ref]).splitlines()
    findings: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        root_prefix = str(root) + "/"
        for name in names:
            if not name.startswith(SCAN_PREFIXES):
                continue
            suffix = Path(name).suffix.lower()
            if suffix not in TEXT_SUFFIXES and suffix not in IMAGE_SUFFIXES:
                continue
            blob = subprocess.run(["git", "show", f"{ref}:{name}"],
                                  capture_output=True)
            if blob.returncode != 0:
                raise ScannerError(f"не удалось получить {name} из {ref}")
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob.stdout)
        findings.extend(scan_tree(root))
    for item in findings:
        # В отчёте — путь относительно корня репозитория, а не временный каталог.
        item["file"] = item["file"].replace(root_prefix, "")
    return findings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scan_sensitive.py",
        description="Поиск сведений о пользовательском окружении в QA-материалах.",
    )
    parser.add_argument("--git", metavar="REF", nargs="?", const="",
                        default=None,
                        help="сканировать содержимое git-ссылки (--git HEAD)")
    parser.add_argument("paths", nargs="*",
                        help="каталоги или файлы для сканирования")
    return parser


def run(argv: Iterable[str] | None = None) -> tuple[int, dict[str, Any]]:
    """Основная логика; возвращает (код возврата, отчёт)."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.git == "":
        raise ScannerError("для --git нужна ссылка, например: --git HEAD")
    if args.git is None and not args.paths:
        raise ScannerError("укажите каталог для сканирования или --git <ссылка>")

    if args.git is not None:
        findings = scan_git(args.git)
        scope = f"git:{args.git}"
    else:
        findings = []
        for raw in args.paths:
            path = Path(raw)
            if not path.exists():
                raise ScannerError(f"путь не найден: {raw}")
            if path.is_dir():
                findings.extend(scan_tree(path))
            else:
                if path.suffix.lower() in IMAGE_SUFFIXES:
                    findings.extend(scan_image(path))
                else:
                    findings.extend(scan_text(str(path), path.read_text(
                        encoding="utf-8", errors="replace")))
        scope = ",".join(args.paths)

    by_class: dict[str, int] = {}
    for item in findings:
        by_class[item["class"]] = by_class.get(item["class"], 0) + 1
    report = {
        "scope": scope,
        "findings": len(findings),
        "by_class": by_class,
        "items": findings,
    }
    return (EXIT_FINDINGS if findings else EXIT_OK), report


def main(argv: Iterable[str] | None = None) -> int:
    try:
        code, report = run(argv)
    except ScannerError as exc:
        print(json.dumps({"error": str(exc), "findings": 0}, ensure_ascii=False))
        return EXIT_ERROR
    except KeyboardInterrupt:  # pragma: no cover - интерактивное прерывание
        print(json.dumps({"error": "прервано пользователем", "findings": 0},
                         ensure_ascii=False))
        return EXIT_ERROR
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
