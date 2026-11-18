"""Общая обвязка QA-стенда WO-03 (Linux Mint, 18.11.2026).

Стенд изолирован от рабочего окружения разработчика: отдельный worktree,
отдельный venv, отдельные NEUROWAVE_HOME-профили и папка с тестовыми
аудиофайлами. Никакие реальные токены и пользовательские данные не
используются: провайдер — локальный каталог с синтетическими тонами.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

ROOT = Path(__file__).resolve().parents[1]
QA_DIR = ROOT / "qa"
ARTIFACTS = QA_DIR / "artifacts"
# Каталог стенда задаётся окружением (QA_STAND), путь по умолчанию —
# относительный, чтобы в репозитории не оставалось домашних путей.
SHARED = Path(os.environ.get("QA_STAND") or (ROOT.parent / "stand")).resolve()
MEDIA = Path(os.environ.get("QA_MEDIA") or (SHARED / "media"))
MPV = Path(os.environ.get("QA_MPV") or (SHARED / "tools" / "AppDir" / "bin" / "mpv"))
HOMES = Path(os.environ.get("QA_HOMES") or (SHARED / "homes"))

#: Громкость по умолчанию для стенда: слышно, но без риска для слуха.
SAFE_TEST_VOLUME = 30


def ensure_dirs() -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    HOMES.mkdir(parents=True, exist_ok=True)


def qa_home(name: str) -> Path:
    """Изолированный профиль приложения (NEUROWAVE_HOME)."""
    path = HOMES / name
    if path.exists():
        import shutil

        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


# --- сбор данных о процессе -------------------------------------------


def _proc_status(pid: int) -> dict[str, float]:
    values: dict[str, float] = {}
    try:
        text = Path(f"/proc/{pid}/status").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return values
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key in {"VmRSS", "VmSwap", "Threads", "FDSize"}:
            values[key] = float(value.split()[0])
    return values


def _smaps_rollup(pid: int) -> dict[str, float]:
    values: dict[str, float] = {}
    try:
        text = Path(f"/proc/{pid}/smaps_rollup").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return values
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key in {"Pss", "SwapPss", "Private_Clean", "Private_Dirty"}:
            try:
                values[key] = float(value.strip().split()[0])
            except (IndexError, ValueError):
                continue
    return values


def _cpu_ticks(pid: int) -> float:
    try:
        parts = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
    except OSError:
        return 0.0
    try:
        return float(parts[13]) + float(parts[14])
    except (IndexError, ValueError):
        return 0.0


def count_fds(pid: int) -> int:
    try:
        return len(list(Path(f"/proc/{pid}/fd").iterdir()))
    except OSError:
        return -1


def proc_snapshot(pid: int) -> dict[str, Any]:
    """Снимок ресурсов процесса: CPU, память, дескрипторы, потоки."""
    if pid <= 0:
        return {}
    status = _proc_status(pid)
    smaps = _smaps_rollup(pid)
    return {
        "pid": pid,
        "rss_kb": status.get("VmRSS", 0.0),
        "pss_kb": smaps.get("Pss", 0.0),
        "swap_kb": status.get("VmSwap", 0.0),
        "swap_pss_kb": smaps.get("SwapPss", 0.0),
        "private_dirty_kb": smaps.get("Private_Dirty", 0.0),
        "threads": status.get("Threads", 0.0),
        "fds": count_fds(pid),
        "cpu_ticks": _cpu_ticks(pid),
    }


def mpv_processes() -> list[int]:
    """Все живые процессы mpv в системе (по /proc, без ps)."""
    found: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if comm == "mpv":
            found.append(int(entry.name))
    return sorted(found)


def stale_ipc_sockets() -> list[str]:
    import tempfile

    return [p.name for p in Path(tempfile.gettempdir()).glob("neurowave-mpv-*.sock")]


# --- сэмплер ресурсов ---------------------------------------------------


@dataclass
class ResourceSampler:
    """Периодический сэмплер ресурсов приложения и mpv."""

    csv_path: Path
    interval: float = 5.0
    mpv_pid: Callable[[], int] = lambda: 0
    label: str = ""
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None
    _rows: list[dict[str, Any]] = field(default_factory=list)

    def __enter__(self) -> "ResourceSampler":
        self._write_header()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def _write_header(self) -> None:
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self.csv_path.write_text(
            "t_сек,label,pid,py_pid,mpv_pid,py_rss_kb,py_pss_kb,py_swap_kb,"
            "py_swap_pss_kb,py_fds,py_threads,py_cpu_ticks,mpv_rss_kb,mpv_pss_kb,"
            "mpv_swap_kb,mpv_fds,mpv_threads,mpv_cpu_ticks,mpv_count\n",
            encoding="utf-8",
        )

    def _loop(self) -> None:
        start = time.monotonic()
        while not self._stop.is_set():
            self._sample(start)
            self._stop.wait(self.interval)
        self._sample(start)

    def _sample(self, start: float) -> None:
        py = proc_snapshot(os.getpid())
        pid = self.mpv_pid()
        mpv = proc_snapshot(pid) if pid else {}
        count = len(mpv_processes())
        row = {
            "t": round(time.monotonic() - start, 1),
            "label": self.label,
            "py_rss_kb": py.get("rss_kb", 0.0),
            "py_pss_kb": py.get("pss_kb", 0.0),
            "py_swap_kb": py.get("swap_kb", 0.0),
            "py_swap_pss_kb": py.get("swap_pss_kb", 0.0),
            "py_fds": py.get("fds", -1),
            "py_threads": py.get("threads", 0.0),
            "py_cpu_ticks": py.get("cpu_ticks", 0.0),
            "mpv_rss_kb": mpv.get("rss_kb", 0.0),
            "mpv_pss_kb": mpv.get("pss_kb", 0.0),
            "mpv_swap_kb": mpv.get("swap_kb", 0.0),
            "mpv_fds": mpv.get("fds", -1),
            "mpv_threads": mpv.get("threads", 0.0),
            "mpv_cpu_ticks": mpv.get("cpu_ticks", 0.0),
            "mpv_count": count,
        }
        self._rows.append(row)
        with self.csv_path.open("a", encoding="utf-8") as handle:
            handle.write(
                "{t},{label},{py_pid},{mpv_pid},{py_rss_kb},{py_pss_kb},{py_swap_kb},"
                "{py_swap_pss_kb},{py_fds},{py_threads},{py_cpu_ticks},{mpv_rss_kb},"
                "{mpv_pss_kb},{mpv_swap_kb},{mpv_fds},{mpv_threads},{mpv_cpu_ticks},"
                "{mpv_count}\n".format(
                    **{**row, "py_pid": os.getpid(), "mpv_pid": pid}
                )
            )

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)


# --- журнал событий ------------------------------------------------------


class EventRecorder:
    """Слушатель плеера: пишет все события с временными метками."""

    def __init__(self, jsonl_path: Path | None = None):
        self.events: list[dict[str, Any]] = []
        self._path = jsonl_path
        self._start = time.monotonic()
        if jsonl_path is not None:
            jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            jsonl_path.write_text("", encoding="utf-8")

    def __call__(self, event: str, payload: dict[str, Any]) -> None:
        record = {
            "t": round(time.monotonic() - self._start, 3),
            "event": event,
            "payload": _safe_payload(payload),
        }
        self.events.append(record)
        if self._path is not None:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def count(self, event: str) -> int:
        return sum(1 for item in self.events if item["event"] == event)

    def as_list(self) -> list[dict[str, Any]]:
        return list(self.events)


def _safe_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Убрать из payload всё, что может быть персональными данными."""
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if key in {"track"}:
            out[key] = _describe_track(value)
        elif key in {"quality"}:
            out[key] = str(value)
        elif isinstance(value, (int, float, bool, str)) or value is None:
            out[key] = value
        else:
            out[key] = type(value).__name__
    return out


def _describe_track(track: Any) -> dict[str, Any]:
    if track is None:
        return {}
    return {
        "id": getattr(track, "id", None),
        "provider": getattr(track, "provider", None),
        "duration_ms": getattr(track, "duration_ms", None),
        "title_hash": _hash_title(getattr(track, "title", "")),
    }


def _hash_title(title: str) -> str:
    import hashlib

    return hashlib.sha1(title.encode("utf-8")).hexdigest()[:10]


# --- сборка плеера -------------------------------------------------------


@dataclass
class Harness:
    player: Any
    backend: Any
    adapter: Any
    settings: Any
    paths: Any
    recorder: EventRecorder

    def close(self) -> None:
        try:
            self.player.close()
        except Exception:
            pass


def build_local_player(
    home_name: str,
    *,
    repeat: str = "off",
    shuffle: bool = False,
    volume: int = SAFE_TEST_VOLUME,
    mpv_extra_args: Iterable[str] | None = None,
    media_dir: Path | None = None,
    recorder: EventRecorder | None = None,
):
    """Собрать плеер на локальном провайдере с настоящим аудиовыходом."""
    from neurowave.audio.mpv import MpvBackend
    from neurowave.config import ConfigStore, Paths
    from neurowave.local.adapter import LocalMusicAdapter
    from neurowave.player import PlayerService
    from neurowave.queue import Queue
    from neurowave.storage.cache import MediaCache
    from neurowave.storage.db import Database

    home = qa_home(home_name)
    paths = Paths.resolve({"NEUROWAVE_HOME": str(home)}).ensure()
    settings = ConfigStore(paths).load()
    settings.volume = volume
    settings.repeat = repeat
    settings.shuffle = shuffle
    settings.local_music_dir = str(media_dir or MEDIA)
    settings.audio_backend = "mpv"

    db = Database(paths.database_file)
    cache = MediaCache(
        paths.cache_dir / "media",
        quota_bytes=settings.cache_quota_bytes,
        ttl_seconds=settings.cache_ttl_seconds,
    )
    adapter = LocalMusicAdapter(root=settings.local_music_dir)
    backend = MpvBackend(binary=str(MPV), extra_args=list(mpv_extra_args or []))
    log = recorder or EventRecorder()
    player = PlayerService(
        adapter=adapter,
        backend=backend,
        queue=Queue(repeat=settings.repeat, shuffle=settings.shuffle),
        settings=settings,
        store=db,
        cache=cache,
        listener=log,
        prefetch=False,
    )
    return Harness(player=player, backend=backend, adapter=adapter,
                   settings=settings, paths=paths, recorder=log)


# --- вспомогательное -----------------------------------------------------


def run(cmd: list[str], timeout: float = 30.0, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
        env={**os.environ, **(env or {})},
    )
    return proc.returncode, proc.stdout, proc.stderr


def write_result(name: str, data: dict[str, Any]) -> Path:
    ensure_dirs()
    path = ARTIFACTS / f"{name}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def now_ms() -> float:
    return time.monotonic() * 1000.0
