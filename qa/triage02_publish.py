"""QA-TRIAGE-02: подготовка обезличенных артефактов для публикации.

Сырые прогоны лежат в qa/artifacts/ и содержат пути стенда. В репозиторий
попадают только копии из docs/qa/artifacts/, где домашние пути, имена
пользователя и имена карт заменены на плейсхолдеры.

Запуск: python qa/triage02_publish.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qaenv as q  # noqa: E402

MEDIA = Path(os.environ.get("QA_MEDIA") or (q.SHARED / "media-verify"))
HOMES = q.HOMES
STAND = q.SHARED
HOME_PATTERNS = [str(STAND), str(MEDIA), str(HOMES)]
PUBLISH = q.ROOT / "docs" / "qa" / "artifacts"
LOGS = q.ROOT / "docs" / "qa" / "logs"

REPLACEMENTS = [
    (str(MEDIA), "<МЕДИАТЕКА>"),
    (str(HOMES), "<ПРОФИЛЬ_QA>"),
    (str(STAND), "<СТЕНД>"),
    (str(q.ROOT), "<РЕПОЗИТОРИЙ>"),
    ("CARD=Generic", "CARD=<КАРТА>"),
    ("CARD=NVidia", "CARD=<КАРТА>"),
]


def sanitize(text: str) -> str:
    for old, new in REPLACEMENTS:
        text = text.replace(old, new)
    # Имена процессов/pid не персональные, но pid привязывают к сессии —
    # оставляем, они не раскрывают личных данных.
    return text


def main() -> int:
    PUBLISH.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    published = []
    for source in sorted((q.ROOT / "qa" / "artifacts").glob("triage02_*.json")):
        data = json.loads(source.read_text(encoding="utf-8"))
        text = json.dumps(data, ensure_ascii=False, indent=2)
        clean = sanitize(text)
        target = PUBLISH / source.name
        target.write_text(clean + "\n", encoding="utf-8")
        published.append(source.name)
    # Логи исключений: пути внутри traceback.
    for source in sorted((q.ROOT / "docs" / "qa" / "logs").glob("*.log")):
        clean = sanitize(source.read_text(encoding="utf-8"))
        (LOGS / source.name).write_text(clean, encoding="utf-8")
    # Контроль: в публикуемых файлах не должно остаться домашних путей.
    leftovers: list[str] = []
    for path in list(PUBLISH.glob("*.json")) + list(LOGS.glob("*.log")):
        text = path.read_text(encoding="utf-8")
        for needle in ["/home/", str(STAND), str(MEDIA), str(HOMES), "zoz"]:
            if needle in text:
                leftovers.append(f"{path.name}: {needle}")
    print(json.dumps({
        "published": published,
        "leftovers": leftovers,
        "ok": not leftovers,
    }, ensure_ascii=False, indent=2))
    return 0 if not leftovers else 1


if __name__ == "__main__":
    raise SystemExit(main())
