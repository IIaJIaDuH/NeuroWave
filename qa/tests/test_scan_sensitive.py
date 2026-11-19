"""QA-SEC-02: негативные тесты сканера чувствительных строк.

Тесты проверяют **поведение CLI** (через подпроцесс), а не внутренние
функции: так проверяется и разбор аргументов, и коды возврата, и то, что
исходные чувствительные значения не попадают в вывод.

Все значения синтетические. Чтобы сканер не находил собственные тестовые
значения в своём репозитории, они собираются из фрагментов (frag).
"""

from __future__ import annotations

import base64
import json
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

SCANNER = Path(__file__).resolve().parents[1] / "scan_sensitive.py"

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2


def frag(*parts: str) -> str:
    """Склейка фрагментов: тестовые значения не хранятся в исходнике целиком."""
    return "".join(parts)


#: Синтетические значения (правдоподобные по форме, но выдуманные).
HOME_PATH = frag("/", "home", "/testuser", "/private-data")
HOME_DIR = frag("/", "home", "/testuser")
MAC = frag("02", ":", "11", ":", "22", ":", "33", ":", "44", ":", "55")
BT_ID = frag("59", "_", "19", "_", "70", "_", "A9", "_", "CF", "_", "F8")
FAKE_KEY = frag("sk-", "live-", "0123456789abcdef")
SAFE_PLACEHOLDER = "<" + "HOME" + ">"
NEUTRAL_BT = "BT" + "_DEVICE_A"
#: Имена полей собираются из фрагментов, чтобы исходник теста сам не
#: содержал строк, которые сканер считает секретами.
KEY_FIELD = "api" + "_key"
TOKEN_FIELD = "to" + "ken"

#: Обычный PNG 1×1 без текстовых чанков.
CLEAN_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)


def run_scanner(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Запуск сканера как внешней программы."""
    return subprocess.run([sys.executable, str(SCANNER), *args],
                          capture_output=True, text=True, cwd=cwd)


def report_of(proc: subprocess.CompletedProcess) -> dict:
    """Разбор JSON-отчёта сканера."""
    return json.loads(proc.stdout)


def make_png_with_text(text: str) -> bytes:
    """PNG с текстовым чанком (метаданные) — синтетический."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    text_chunk = chunk(b"tEXt", b"Comment\x00" + text.encode("utf-8"))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + text_chunk
            + chunk(b"IEND", b""))


def make_git_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    """Временный git-репозиторий с заданными файлами."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {"GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
           "GIT_CONFIG_SYSTEM": str(tmp_path / "gitconfig"),
           "PATH": "/usr/bin:/bin:/usr/local/bin"}
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
    for name, content in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "-c", "user.name=QA", "-c", "user.email=qa@example.invalid",
                    "commit", "-q", "-m", "synthetic"], cwd=repo, check=True, env=env)
    return repo


# --- 1–4: обработка --git ----------------------------------------------


def test_git_ref_separate_argument(tmp_path: Path) -> None:
    """SEC-02-A: `--git HEAD` (с пробелом) работает."""
    repo = make_git_repo(tmp_path, {"qa/a.json": json.dumps(
        {"path": HOME_PATH}) + "\n"})
    proc = run_scanner("--git", "HEAD", cwd=repo)
    assert proc.returncode == EXIT_FINDINGS, proc.stdout + proc.stderr
    report = report_of(proc)
    assert report["findings"] >= 1
    assert any(item["class"] == "домашний путь" for item in report["items"])


def test_git_ref_equals_form_matches(tmp_path: Path) -> None:
    """SEC-02-A: `--git=HEAD` даёт тот же результат, что и `--git HEAD`."""
    repo = make_git_repo(tmp_path, {"qa/a.json": json.dumps(
        {"path": HOME_PATH}) + "\n"})
    separate = run_scanner("--git", "HEAD", cwd=repo)
    equals = run_scanner("--git=HEAD", cwd=repo)
    assert separate.returncode == equals.returncode == EXIT_FINDINGS
    assert report_of(separate) == report_of(equals)


def test_git_invalid_ref(tmp_path: Path) -> None:
    """SEC-02-A: неверная ссылка — код 2 и понятная ошибка без трассировки."""
    repo = make_git_repo(tmp_path, {"qa/a.json": "{}\n"})
    proc = run_scanner("--git", "no-such-ref", cwd=repo)
    assert proc.returncode == EXIT_ERROR
    assert "error" in report_of(proc)
    assert "Traceback" not in proc.stdout + proc.stderr


def test_git_without_value(tmp_path: Path) -> None:
    """SEC-02-A: `--git` без значения — код 2."""
    repo = make_git_repo(tmp_path, {"qa/a.json": "{}\n"})
    proc = run_scanner("--git", cwd=repo)
    assert proc.returncode == EXIT_ERROR
    assert "error" in report_of(proc)
    assert "Traceback" not in proc.stdout + proc.stderr


# --- 5–6: пропуск целых строк больше не допускается ---------------------


def test_home_path_with_homes_marker(tmp_path: Path) -> None:
    """SEC-02-B: строка с `QA_HOMES=` и реальным путём не пропускается."""
    target = tmp_path / "note.txt"
    target.write_text(f"QA_HOMES={HOME_PATH}\n", encoding="utf-8")
    proc = run_scanner(str(tmp_path))
    assert proc.returncode == EXIT_FINDINGS
    report = report_of(proc)
    assert any(item["class"] == "домашний путь" for item in report["items"])
    assert any(item["line"] == 1 for item in report["items"])


def test_placeholder_word_next_to_real_path(tmp_path: Path) -> None:
    """SEC-02-B: слово `placeholder` рядом с реальным путём не отключает поиск."""
    target = tmp_path / "note.txt"
    target.write_text(f"placeholder {HOME_PATH}\n", encoding="utf-8")
    proc = run_scanner(str(tmp_path))
    assert proc.returncode == EXIT_FINDINGS
    assert report_of(proc)["findings"] >= 1


def test_safe_placeholder_no_false_positive(tmp_path: Path) -> None:
    """Плейсхолдер <HOME> ложной тревоги не вызывает."""
    target = tmp_path / "note.txt"
    target.write_text(f"path = {SAFE_PLACEHOLDER}/{SAFE_PLACEHOLDER}\n",
                      encoding="utf-8")
    proc = run_scanner(str(tmp_path))
    assert proc.returncode == EXIT_OK, proc.stdout
    assert report_of(proc)["findings"] == 0


# --- 8–11: MAC/BT, ключи и секреты --------------------------------------


def test_mac_and_bluetooth_detected(tmp_path: Path) -> None:
    """MAC-адрес и Bluetooth-идентификатор обнаруживаются."""
    target = tmp_path / "devices.txt"
    target.write_text(f"mac={MAC}\nbt={BT_ID}\n", encoding="utf-8")
    proc = run_scanner(str(tmp_path))
    assert proc.returncode == EXIT_FINDINGS
    classes = {item["class"] for item in report_of(proc)["items"]}
    assert "MAC-адрес" in classes
    assert "Bluetooth-идентификатор" in classes


def test_neutral_bluetooth_name_no_false_positive(tmp_path: Path) -> None:
    """Нейтральное имя BT_DEVICE_A ложной тревоги не вызывает."""
    target = tmp_path / "devices.txt"
    target.write_text(f"default_sink={NEUTRAL_BT}\n", encoding="utf-8")
    proc = run_scanner(str(tmp_path))
    assert proc.returncode == EXIT_OK, proc.stdout
    assert report_of(proc)["findings"] == 0


def test_fake_api_key_detected(tmp_path: Path) -> None:
    """Фиктивный API-ключ обнаруживается."""
    target = tmp_path / "config.json"
    target.write_text(json.dumps({KEY_FIELD: FAKE_KEY}) + "\n", encoding="utf-8")
    proc = run_scanner(str(tmp_path))
    assert proc.returncode == EXIT_FINDINGS
    assert any(item["class"] == "секрет" for item in report_of(proc)["items"])


def test_placeholder_and_secret_on_same_line(tmp_path: Path) -> None:
    """Плейсхолдер в той же строке не маскирует секрет."""
    target = tmp_path / "config.json"
    target.write_text(json.dumps(
        {"path": SAFE_PLACEHOLDER, TOKEN_FIELD: FAKE_KEY}) + "\n",
        encoding="utf-8")
    proc = run_scanner(str(tmp_path))
    assert proc.returncode == EXIT_FINDINGS
    assert any(item["class"] == "секрет" for item in report_of(proc)["items"])


# --- 12–13: JSON, JSONL, CSV --------------------------------------------


def test_clean_json_jsonl_csv(tmp_path: Path) -> None:
    """Обычные JSON/JSONL/CSV без чувствительных строк — код 0."""
    (tmp_path / "a.json").write_text(json.dumps(
        {"scenario": "QA-01", "verdict": "PASS", "seconds": 0.5}) + "\n",
        encoding="utf-8")
    (tmp_path / "a.jsonl").write_text(
        json.dumps({"t": 0.1, "event": "track"}) + "\n", encoding="utf-8")
    (tmp_path / "a.csv").write_text("round,rss_kb\n10,69284\n", encoding="utf-8")
    proc = run_scanner(str(tmp_path))
    assert proc.returncode == EXIT_OK, proc.stdout
    assert report_of(proc)["findings"] == 0


def test_sensitive_line_inside_jsonl(tmp_path: Path) -> None:
    """Чувствительная строка внутри JSONL — код 1."""
    (tmp_path / "events.jsonl").write_text(
        json.dumps({"t": 0.1, "event": "ok"}) + "\n"
        + json.dumps({"t": 0.2, "path": HOME_PATH}) + "\n", encoding="utf-8")
    proc = run_scanner(str(tmp_path))
    assert proc.returncode == EXIT_FINDINGS
    assert report_of(proc)["findings"] >= 1


# --- 14: некорректный каталог -------------------------------------------


def test_unavailable_directory(tmp_path: Path) -> None:
    """Несуществующий каталог — код 2 и понятная ошибка без трассировки."""
    proc = run_scanner(str(tmp_path / "нет-такого"))
    assert proc.returncode == EXIT_ERROR
    assert "error" in report_of(proc)
    assert "Traceback" not in proc.stdout + proc.stderr


def test_no_arguments() -> None:
    """Без аргументов — код 2 и понятная ошибка."""
    proc = run_scanner()
    assert proc.returncode == EXIT_ERROR
    assert "error" in report_of(proc)
    assert "Traceback" not in proc.stdout + proc.stderr


# --- 15–16: метаданные изображений --------------------------------------


def test_png_metadata_with_path(tmp_path: Path) -> None:
    """PNG с путём в метаданных — находка."""
    (tmp_path / "shot.png").write_bytes(make_png_with_text(HOME_PATH))
    proc = run_scanner(str(tmp_path))
    assert proc.returncode == EXIT_FINDINGS
    assert any(item["class"] == "метаданные изображения"
               for item in report_of(proc)["items"])


def test_clean_png_no_false_positive(tmp_path: Path) -> None:
    """Обычный PNG без чувствительных метаданных — ложной тревоги нет."""
    (tmp_path / "shot.png").write_bytes(CLEAN_PNG)
    proc = run_scanner(str(tmp_path))
    assert proc.returncode == EXIT_OK, proc.stdout
    assert report_of(proc)["findings"] == 0


# --- безопасность вывода и самопроверка ----------------------------------


def test_output_does_not_leak_values(tmp_path: Path) -> None:
    """Исходные чувствительные значения не публикуются в отчёте."""
    (tmp_path / "leak.txt").write_text(
        f"{HOME_PATH}\n{MAC}\n{FAKE_KEY}\n{BT_ID}\n", encoding="utf-8")
    proc = run_scanner(str(tmp_path))
    assert proc.returncode == EXIT_FINDINGS
    output = proc.stdout + proc.stderr
    for value in (HOME_PATH, HOME_DIR, MAC, FAKE_KEY, BT_ID):
        assert value not in output, f"значение {value!r} попало в вывод"
    report = report_of(proc)
    for item in report["items"]:
        assert set(item) == {"file", "line", "class", "match"}
        assert "*" in item["match"]


def test_published_qa_material_is_clean() -> None:
    """Опубликованное содержимое QA (docs/qa и qa) не содержит находок."""
    root = Path(__file__).resolve().parents[2]
    proc = run_scanner("--git", "HEAD", cwd=root)
    assert proc.returncode == EXIT_OK, proc.stdout
    assert report_of(proc)["findings"] == 0
