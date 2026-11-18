"""QA-VERIFY-02: обезличенные копии артефактов для публикации.

Сырые прогоны лежат в qa/artifacts/ и содержат пути стенда. В репозиторий
попадают копии из docs/qa/artifacts/, где домашние пути и имена пользователя
заменены плейсхолдерами.

Запуск: python qa/publish_artifacts.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qaenv as q  # noqa: E402

PUBLISH = q.ROOT / "docs" / "qa" / "artifacts"


def main() -> int:
    PUBLISH.mkdir(parents=True, exist_ok=True)
    replacements = [
        (str(q.SHARED), "<СТЕНД>"),
        (str(q.MEDIA), "<МЕДИАТЕКА>"),
        (str(q.HOMES), "<ПРОФИЛЬ_QA>"),
        (str(q.ROOT), "<РЕПОЗИТОРИЙ>"),
    ]

    def sanitize(text: str) -> str:
        for old, new in replacements:
            text = text.replace(old, new)
        return text

    published = []
    for source in sorted((q.ROOT / "qa" / "artifacts").glob("*.json")):
        data = json.loads(source.read_text(encoding="utf-8"))
        target = PUBLISH / source.name
        target.write_text(sanitize(json.dumps(data, ensure_ascii=False, indent=2)) + "\n",
                          encoding="utf-8")
        published.append(source.name)
    leftovers: list[str] = []
    for path in PUBLISH.glob("*.json"):
        text = path.read_text(encoding="utf-8")
        for needle in ["/home/", str(q.SHARED), str(q.MEDIA), str(q.HOMES), "zoz"]:
            if needle in text:
                leftovers.append(f"{path.name}: {needle}")
    print(json.dumps({"published": published, "leftovers": leftovers, "ok": not leftovers},
                     ensure_ascii=False, indent=2))
    return 0 if not leftovers else 1


if __name__ == "__main__":
    raise SystemExit(main())
