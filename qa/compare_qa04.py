"""QA-VERIFY-02: сравнение D-04-сценария из PR #32 на текущей ревизии и на main.

Запускает ровно тот сценарий `section_local_after_failure`, которым в PR #32
был зафиксирован дефект D-04 (недоступный первый трек блокирует очередь),
и печатает результат для сравнения «до/после».

Запуск: python qa/compare_qa04.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qaenv as q  # noqa: E402

import qa04_network as qa04  # noqa: E402


def main() -> int:
    result = qa04.section_local_after_failure()
    payload = {
        "sha": os.environ.get("QA_SHA", "не указан"),
        "section": "qa04_network.section_local_after_failure (сценарий D-04 из PR #32)",
        "result": result,
    }
    path = q.write_result("compare_qa04_d04", payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"→ {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
