"""QA-VERIFY-02 / QA-SEC-01: обезличенные копии артефактов для публикации.

Сырые прогоны лежат в qa/artifacts/ и содержат пути стенда и идентификаторы
аудиоустройств. В репозиторий попадают копии из docs/qa/artifacts/, где:

* пути стенда и репозитория заменены на плейсхолдеры <QA_STAND>, <QA_MEDIA>,
  <QA_HOMES>, <REPO>;
* идентификатор Bluetooth-устройства — на BT_DEVICE_A;
* PCI-адрес звуковой карты — на alsa_output.pci-<PCI> (смысл выхода — HDMI —
  сохраняется).

Числовые результаты измерений, PASS/FAIL и SHA проверяемого кода при этом
не меняются — контроль делает qa/check_unchanged.py.

Запуск: python qa/publish_artifacts.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qaenv as q  # noqa: E402
from sanitize_artifacts import FORBIDDEN, sanitize_text  # noqa: E402

PUBLISH = q.ROOT / "docs" / "qa" / "artifacts"


def main() -> int:
    PUBLISH.mkdir(parents=True, exist_ok=True)
    published: list[str] = []
    for source in sorted((q.ROOT / "qa" / "artifacts").glob("*.json")):
        data = json.loads(source.read_text(encoding="utf-8"))
        text, _ = sanitize_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        target = PUBLISH / source.name
        target.write_text(text, encoding="utf-8")
        published.append(source.name)

    leftovers: list[str] = []
    for path in sorted(PUBLISH.glob("*.json")):
        text = path.read_text(encoding="utf-8")
        for needle in FORBIDDEN:
            if needle in text:
                leftovers.append(f"{path.name}: {needle}")
    print(json.dumps({"published": published, "leftovers": leftovers,
                      "ok": not leftovers}, ensure_ascii=False, indent=2))
    return 0 if not leftovers else 1


if __name__ == "__main__":
    raise SystemExit(main())
