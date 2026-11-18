"""QA-VERIFY-02: растут ли ресурсы mpv при многократной смене трека.

Сценарий A повторяется 40 раз в одном процессе mpv; каждые 10 раундов
снимаются RSS, число дескрипторов и потоков. Если бы был заём ресурсов,
рост был бы линейным; у mpv кэш файлов упирается в плато.

Запуск: python qa/stress_resources.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qaenv as q  # noqa: E402

from verify_d04 import MpvProbe, StubAdapter, make_track  # noqa: E402

MEDIA = Path(os.environ.get("QA_MEDIA") or (q.SHARED / "media-d04"))
ROUNDS = 40
SAMPLE_EVERY = 10


def main() -> int:
    from neurowave.errors import TrackUnavailable

    files = {path.name: path for path in sorted(MEDIA.glob("qa_stress_*"))}
    harness = q.build_local_player("d04_res", repeat="all", volume=30, media_dir=MEDIA)
    adapter = StubAdapter(files)
    harness.player.adapter = adapter
    probe = MpvProbe()
    samples: list[dict] = []
    errors = 0
    try:
        for index in range(ROUNDS):
            track_id = sorted(files)[index % len(files)]
            broken = make_track(f"broken-r{index:02d}", "Broken")
            ok = make_track(track_id, f"OK {track_id}")
            adapter.files[track_id] = files[track_id]
            try:
                harness.player.play_tracks([broken, ok], start=0)
            except TrackUnavailable:
                errors += 1
            time.sleep(0.5)
            if (index + 1) % SAMPLE_EVERY == 0:
                pids = sorted(q.mpv_processes())
                snap = q.proc_snapshot(pids[0]) if pids else {}
                samples.append({
                    "round": index + 1,
                    "rss_kb": snap.get("rss_kb"),
                    "fds": snap.get("fds"),
                    "threads": snap.get("threads"),
                    "mpv_processes": len(pids),
                    "sockets": len(q.stale_ipc_sockets()),
                    "cpu_ticks": snap.get("cpu_ticks"),
                })
        result = {
            "scenario": "рост ресурсов mpv при 40 сменах трека",
            "rounds": ROUNDS,
            "errors": errors,
            "samples": samples,
        }
        path = q.write_result("d04_stress_resources", result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print(f"→ {path}")
    finally:
        harness.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
