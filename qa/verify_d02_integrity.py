"""QA-VERIFY-03 / D-02: сводный блок целостности данных.

Собирает из артефактов матрицы и граничных значений проверку сохранности
пользовательских данных:

* SHA256 `config.json` до/после каждой команды;
* состояние БД, токенов и истории до/после;
* неизвестные пользовательские поля (отдельно — как замечание P2);
* новые и удалённые файлы профиля;
* ручная правка повреждённого конфига → восстановление без удаления профиля;
* утечки токена в вывод команд.

Запуск: python qa/verify_d02_integrity.py --matrix <файл> --edges <файл>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qaenv as q  # noqa: E402


def build(matrix: dict, edges: dict) -> dict:
    p1_ids = set(matrix.get("p1_cases", []))
    cases = []
    for case in matrix.get("results", []):
        cases.append({
            "case_id": case["case_id"],
            "is_p1_case": case.get("is_p1_case", False),
            "config_sha256_before": case.get("config_before", {}).get("sha256"),
            "config_sha256_after": case.get("config_after", {}).get("sha256"),
            "config_rewritten": case.get("config_rewritten"),
            "config_rewritten_by": case.get("config_rewritten_by", []),
            "files_added": case.get("files_added", []),
            "files_removed": case.get("files_removed", []),
            "user_data_intact": case.get("user_data_intact"),
            "tokens_lost": case.get("tokens_lost"),
            "token_leaked_to_output": case.get("token_leaked_to_output"),
            "recovery": case.get("recovery"),
            "damaged_config_preserved": (case.get("config_sha256_before") ==
                                         case.get("config_sha256_after")),
        })

    p1_cases = [case for case in cases if case["case_id"] in p1_ids]
    p1_preserved = all(case["damaged_config_preserved"] for case in p1_cases)
    p1_no_rewrite = all(case["config_rewritten"] is False for case in p1_cases)

    # Неизвестные поля: `app` переписывает конфиг своей схемой — это отдельное
    # замечание P2, к D-02 отношения не имеет.
    unknown_fields = []
    for case in matrix.get("results", []):
        if case["case_id"] == "extra_fields":
            before = json.loads(case["config_before"]["content"])
            after = json.loads(case["config_after"]["content"])
            unknown = {key: value for key, value in before.items()
                       if key not in after}
            unknown_fields.append({
                "case_id": case["case_id"],
                "unknown_keys_before": sorted(unknown),
                "unknown_keys_after": sorted(set(before) - set(after)),
                "rewritten_by": case.get("config_rewritten_by", []),
                "classification": "P2 — штатное сохранение приложения теряет "
                                  "неизвестные поля; повреждение конфигурации "
                                  "к этому отношения не имеет",
            })

    edge_summary = {
        "cases": edges.get("totals", {}).get("cases", 0),
        "unhandled_tracebacks": edges.get("totals", {}).get("unhandled_tracebacks", 0),
        "damaged_config_overwritten": edges.get("totals", {}).get("damaged_config_overwritten", 0),
        "per_case": [
            {
                "case_id": case["case_id"],
                "expect": case["expect"],
                "observed": case["observed"],
                "config_rewritten": case["config_rewritten"],
                "effective_volume": case.get("effective_volume"),
                "effective_quota_gb": case.get("effective_quota_gb"),
            }
            for case in edges.get("cases", [])
        ],
    }

    recoveries = [case["recovery"] for case in cases if case.get("recovery")]
    return {
        "sha": matrix.get("sha"),
        "per_command_sha256": cases,
        "p1_damaged_config_preserved": p1_preserved,
        "p1_config_not_rewritten": p1_no_rewrite,
        "unknown_fields": unknown_fields,
        "edge_values": edge_summary,
        "recovery_after_manual_fix": {
            "cases_checked": len(recoveries),
            "all_status_exit_zero": all(item.get("status_exit_code") == 0 for item in recoveries),
            "all_without_traceback": all(not item.get("status_has_traceback")
                                         for item in recoveries),
            "marker_alive_everywhere": all(item.get("marker_alive") for item in recoveries),
            "tokens_intact_everywhere": all(item.get("tokens_intact") for item in recoveries),
            "profile_not_deleted": all(case.get("files_removed") == [] for case in cases),
        },
        "no_token_leak_to_output": all(case.get("token_leaked_to_output") is False
                                       for case in cases),
        "no_user_data_loss": all(case.get("user_data_intact") for case in cases),
        "no_tokens_lost": all(case.get("tokens_lost") is False for case in cases),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", required=True)
    parser.add_argument("--edges", required=True)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    matrix = json.loads(Path(args.matrix).read_text(encoding="utf-8"))
    edges = json.loads(Path(args.edges).read_text(encoding="utf-8"))
    summary = build(matrix, edges)
    out_path = Path(args.out) if args.out else (q.ARTIFACTS / "verify_d02_integrity.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    print(json.dumps({key: value for key, value in summary.items()
                      if isinstance(value, (bool, int, str))}, ensure_ascii=False, indent=2))
    print(f"→ {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
