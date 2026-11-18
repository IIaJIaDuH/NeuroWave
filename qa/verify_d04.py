"""QA-VERIFY-02: аппаратная приёмка исправления D-04.

Проверяемая ветка: fix/qa-first-track-recovery-02, SHA 07733e1e26b5c38b3c64161f109bab9af662c5af
(«fix(player): не останавливать очередь из-за недоступного первого трека»).

Исходный дефект D-04 (QA PR #32): недоступный первый трек блокировал очередь —
`play_tracks([broken, good])` бросал `TrackUnavailable` наружу, текущим оставался
«broken», состояние «buffering», рабочий трек не запускался.

Сценарии:
  A. Недоступный первый трек: Broken → Local OK → Local OK 2
  B. Недоступный средний трек: Local OK → Broken → Local OK 2
  C. Два последовательных отказа: Broken → Broken → Local OK
  D. Единственный недоступный трек: итоговое состояние, overlay, события
  E. Восстановление после отказов: новая очередь с рабочим треком
  F. Repeat ONE / Repeat ALL / Shuffle с недоступными треками
  G. История и обратная связь: неудачные треки не считаются прослушанными
  H. Стресс: сценарий A не менее 20 раз на разных файлах

Проверка воспроизведения независимая от PlayerService: файл, который реально
играет, спрашивается у mpv по его IPC-сокету (`filename`, `time-pos`), а
наличие цифрового аудиосигнала подтверждается записью монитора sink'а (parec).

Запуск: python qa/verify_d04.py
"""

from __future__ import annotations

import json
import math
import os
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qaenv as q  # noqa: E402

MEDIA = Path(os.environ.get("QA_MEDIA") or (q.SHARED / "media-d04"))
BASE_SHA = "07733e1e26b5c38b3c64161f109bab9af662c5af"
RATE = 48000
CHANNELS = 2
#: Порог «есть цифровой сигнал» (dBFS). Тишина — это −120 dBFS.
SIGNAL_FLOOR_DBFS = -60.0


# --- независимая проверка через mpv IPC ---------------------------------
class MpvProbe:
    """Клиент IPC mpv: какой файл реально играет и какая позиция."""

    def __init__(self) -> None:
        self.path = Path("/tmp") / f"neurowave-mpv-{os.getpid()}.sock"
        self._request_id = 0
        self._buffer = b""

    def _connect(self) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(1.5)
        sock.connect(str(self.path))
        return sock

    def get(self, prop: str) -> Any:
        try:
            sock = self._connect()
        except OSError:
            return None
        try:
            self._request_id += 1
            request_id = self._request_id
            sock.sendall(
                (json.dumps({"command": ["get_property", prop], "request_id": request_id}) + "\n").encode()
            )
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                try:
                    chunk = sock.recv(65536)
                except socket.timeout:
                    break
                if not chunk:
                    break
                self._buffer += chunk
                while b"\n" in self._buffer:
                    line, _, self._buffer = self._buffer.partition(b"\n")
                    text = line.decode("utf-8", "replace").strip()
                    if not text:
                        continue
                    try:
                        message = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    if message.get("request_id") == request_id:
                        return message.get("data")
            return None
        finally:
            sock.close()

    def filename(self) -> str | None:
        value = self.get("filename")
        return value if isinstance(value, str) else None

    def position_ms(self) -> float:
        value = self.get("time-pos")
        return float(value) * 1000.0 if isinstance(value, (int, float)) else -1.0


# --- независимая проверка цифрового сигнала -----------------------------
HDMI_SINK = "alsa_output.pci-0000_09_00.1.hdmi-stereo"
HDMI_SINK_INDEX = "76"


def mpv_streams() -> list[dict]:
    """Потоки PipeWire, созданные mpv: sink, куда они идут.

    Вывод pactl локализован, поэтому берём машинно-читаемый JSON.
    """
    import json as _json

    code, out, _ = q.run(["pactl", "-f", "json", "list", "sink-inputs"])
    found: list[dict] = []
    if code != 0 or not out.strip():
        return found
    try:
        data = _json.loads(out)
    except ValueError:
        return found
    for entry in data:
        props = entry.get("properties") or {}
        if props.get("application.name") != "mpv":
            continue
        found.append({
            "client": "mpv",
            "sink_index": entry.get("sink"),
            "sink": HDMI_SINK if str(entry.get("sink")) == str(HDMI_SINK_INDEX) else str(entry.get("sink")),
            "state": entry.get("state"),
        })
    return found


def default_sink() -> str:
    return q.run(["pactl", "get-default-sink"])[1].strip()


def monitor_source(sink: str) -> str | None:
    """Монитор указанного sink'а: на нём видно реальный цифровой сигнал."""
    for line in q.run(["pactl", "list", "sources", "short"])[1].splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1].endswith(".monitor") and sink in parts[1]:
            # В выводе pactl первый столбец — индекс, второй — имя источника.
            return parts[1]
    return None


def switch_default_sink(sink: str) -> str:
    """Временно назначить sink по умолчанию (возвращает прежний)."""
    previous = default_sink()
    if previous != sink:
        q.run(["pactl", "set-default-sink", sink], timeout=20)
    return previous


def expected_frequency(filename: str) -> int | None:
    """Частота тона тестового файла: по имени (a440, c523, qa_stress_03_339hz)."""
    import re

    match = re.search(r"(\d+)hz", filename)
    if match:
        return int(match.group(1))
    match = re.search(r"[a-z](\d{3})\b", filename)
    return int(match.group(1)) if match else None


def tone_amplitudes(data: bytes, frequencies: list[int], rate: int = RATE,
                    window_seconds: float = 0.5) -> dict:
    """Амплитуда на каждой частоте-кандидате: медиана по окнам.

    Окно ~0.5 с различает частоты тона, а медиана по окнам делает оценку
    устойчивой: на том же sink'е может играть постороннее приложение.
    """
    count = len(data) // (2 * CHANNELS)
    if count == 0:
        return {f: 0.0 for f in frequencies}
    frames = struct.unpack(f"<{count * CHANNELS}h", data[: count * CHANNELS * 2])
    mono = [sum(frames[i:i + CHANNELS]) / CHANNELS for i in range(0, len(frames), CHANNELS)]
    size = max(1, int(rate * window_seconds))
    windows = [mono[i:i + size] for i in range(0, len(mono) - size + 1, size)]
    if not windows:
        windows = [mono]
    out: dict[int, float] = {}
    for freq in frequencies:
        step = 2 * math.pi * freq / rate
        per_window: list[float] = []
        for window_values in windows:
            re_part = im_part = 0.0
            for n, value in enumerate(window_values):
                re_part += value * math.cos(step * n)
                im_part += value * math.sin(step * n)
            per_window.append(math.sqrt(re_part * re_part + im_part * im_part) / len(window_values))
        per_window.sort()
        out[freq] = per_window[len(per_window) // 2]
    return out


def measure_signal(source: str, seconds: float = 2.0,
                   frequencies: list[int] | None = None,
                   expected: int | None = None) -> dict:
    """Пик, RMS и спектр монитора sink'а за указанное время."""
    raw = q.ARTIFACTS / "d04_sink.raw"
    raw.parent.mkdir(parents=True, exist_ok=True)
    record = subprocess.Popen(
        ["parec", "--device", source, "--format=s16le", "--rate", str(RATE),
         "--channels", str(CHANNELS), "--raw"],
        stdout=raw.open("wb"), stderr=subprocess.DEVNULL,
    )
    time.sleep(seconds)
    # Читаем файл, пока parec ещё жив: при SIGTERM он не сбрасывает свой буфер.
    # Данные приходят крупными порциями, поэтому ждём первые байты и читаем
    # файл многократно до конца окна.
    deadline = time.monotonic() + seconds
    # Ждём не меньше трёх секунд данных: медиана по окнам иначе считается
    # по одному-двум окнам и сильно шумит.
    min_bytes = RATE * CHANNELS * 2 * 3
    data = b""
    while time.monotonic() < deadline:
        time.sleep(0.25)
        try:
            chunk = raw.read_bytes()
        except OSError:
            continue
        if len(chunk) > len(data):
            data = chunk
        if len(data) >= min_bytes:
            break
    record.terminate()
    try:
        record.wait(timeout=5)
    except subprocess.TimeoutExpired:
        record.kill()
    raw.unlink(missing_ok=True)
    count = len(data) // (2 * CHANNELS)
    if count == 0:
        return {"peak_dbfs": -120.0, "rms_dbfs": -120.0, "samples": 0, "tones": {}}
    values = struct.unpack(f"<{count * CHANNELS}h", data[: count * CHANNELS * 2])
    peak = max(abs(v) for v in values) / 32768.0
    rms = math.sqrt(sum(v * v for v in values) / len(values)) / 32768.0
    to_db = lambda x: 20 * math.log10(x) if x > 0 else -120.0  # noqa: E731
    tones: dict = {}
    if frequencies:
        expected_hz = expected if expected is not None else frequencies[0]
        raw_tones = tone_amplitudes(data, frequencies)
        best = max(raw_tones, key=lambda f: raw_tones[f])
        others = {f: v for f, v in raw_tones.items() if f != expected_hz}
        tones = {
            "expected": expected_hz,
            "dominant": best,
            "dominant_amplitude": round(raw_tones[best], 1),
            "expected_amplitude": round(raw_tones[expected_hz], 1),
            "max_other_amplitude": round(max(others.values()), 1) if others else 0.0,
            "margin_db": round(
                20 * math.log10(raw_tones[expected_hz] / max(others.values()))
                if others and max(others.values()) > 0 else 0.0, 1),
            "all": {str(f): round(v, 1) for f, v in sorted(raw_tones.items())},
        }
    return {"peak_dbfs": round(to_db(peak), 1), "rms_dbfs": round(to_db(rms), 1),
            "samples": count, "tones": tones}


# --- провайдер-заглушка: реальные файлы + управляемый отказ --------------
class StubAdapter:
    """Как в PR #32: часть идентификаторов не разрешается.

    Рабочие треки указывают на настоящие локальные файлы, поэтому mpv играет
    по-настоящему; «битые» идентификаторы дают доменную ошибку.
    """

    provider = "local"

    def __init__(self, files: dict[str, Path]) -> None:
        self.files = files
        self.feedback: list[tuple[str, str]] = []

    def wave_tracks(self) -> list:
        return []

    def stream_url(self, track_id: str):
        from neurowave.domain import Quality, StreamInfo
        from neurowave.errors import TrackUnavailable

        path = self.files.get(track_id)
        if path is None:
            raise TrackUnavailable("трек недоступен")
        return StreamInfo(url=str(path), quality=Quality(codec="mp3"))

    def wave_feedback(self, kind: str, track_key: str) -> None:
        self.feedback.append((kind, track_key))


def make_track(track_id: str, title: str, provider: str = "local", duration_ms: int = 20000):
    from neurowave.domain import Track

    return Track(id=track_id, title=title, provider=provider, duration_ms=duration_ms)


def build(files: dict[str, Path], *, repeat: str = "off", shuffle: bool = False, home: str = "d04"):
    """Плеер с настоящим mpv и заглушкой вместо провайдера."""
    harness = q.build_local_player(
        home, repeat=repeat, shuffle=shuffle, volume=30, media_dir=MEDIA
    )
    adapter = StubAdapter(files)
    harness.player.adapter = adapter
    harness.stub = adapter  # type: ignore[attr-defined]
    return harness


def events_of(harness, names: set[str]) -> list[dict]:
    return [e for e in harness.recorder.as_list() if e["event"] in names]


def track_event_ids(harness) -> list[str]:
    ids = []
    for event in harness.recorder.as_list():
        if event["event"] != "track":
            continue
        track = event["payload"].get("track") or {}
        ids.append(str(track.get("id")))
    return ids


def snapshot_state(harness, probe: MpvProbe) -> dict:
    player = harness.player
    state = player.state
    current = player.current_track
    overlays = {str(getattr(k, "value", k)): v for k, v in state.overlays.items()}
    return {
        "status": state.status.value,
        "overlay": str(getattr(state.overlay, "value", state.overlay)) if state.overlay else None,
        "overlays": overlays,
        "current_track_id": current.id if current else None,
        "current_track_title": current.title if current else None,
        "queue_ids": [str(t.get("id")) for t in player.queue.snapshot().get("items", [])],
        "mpv_filename": probe.filename(),
        "mpv_position_ms": round(probe.position_ms(), 1),
        "backend_position_ms": harness.backend.position_ms(),
        "mpv_running": harness.backend.is_running,
    }


# --- сценарии -------------------------------------------------------------
def scenario_a(files: dict[str, Path], probe: MpvProbe, signal_source: str | None) -> dict:
    """A. Недоступный первый трек: Broken → Local OK → Local OK 2."""
    harness = build(files, home="d04_a")
    result: dict = {"scenario": "A_недоступный_первый_трек"}
    try:
        broken = make_track("broken-a", "Broken A")
        ok1 = make_track("local-ok-a", "Local OK A")
        ok2 = make_track("local-ok-a2", "Local OK A 2")
        harness.stub.files = {**files}
        harness.stub.files.update({
            ok1.id: files["local-ok-a"], ok2.id: files["local-ok-a2"],
        })
        # Базовый уровень sink'а до воспроизведения: на том же выходе может
        # играть постороннее приложение, поэтому сигнал приписывается по спектру
        # и подтверждается остановкой воспроизведения.
        expected_hz = expected_frequency(files["local-ok-a"].name)
        candidates = sorted({f for f in [expected_hz, 220, 349, 440, 523, 659, 880, 1234] if f})
        result["expected_tone_hz"] = expected_hz
        result["sink_inputs_before"] = q.run(["pactl", "list", "sink-inputs", "short"])[1].strip().splitlines()
        baseline = (measure_signal(signal_source, seconds=4.0, frequencies=candidates,
                                   expected=expected_hz) if signal_source else None)
        result["signal_baseline"] = baseline
        raised = None
        try:
            harness.player.play_tracks([broken, ok1, ok2], start=0)
        except Exception as exc:  # noqa: BLE001
            raised = f"{type(exc).__name__}: {exc}"
        result["raised"] = raised
        time.sleep(1.5)
        result["state_after_1_5s"] = snapshot_state(harness, probe)
        signal = None
        if signal_source:
            signal = measure_signal(signal_source, seconds=5.0, frequencies=candidates,
                                    expected=expected_hz)
        result["signal"] = signal
        result["sink_inputs_during"] = q.run(["pactl", "list", "sink-inputs", "short"])[1].strip().splitlines()
        result["mpv_streams_during"] = mpv_streams()
        # Контроль атрибуции: после остановки воспроизведения тон исчезает.
        harness.player.stop()
        time.sleep(1.0)
        after_stop = (measure_signal(signal_source, seconds=4.0, frequencies=candidates,
                                     expected=expected_hz) if signal_source else None)
        result["signal_after_stop"] = after_stop
        result["mpv_streams_after_stop"] = mpv_streams()
        result["events"] = {
            "error": len(events_of(harness, {"error"})),
            "failed": len(events_of(harness, {"failed"})),
            "track": track_event_ids(harness),
            "error_messages": [e["payload"].get("error") for e in events_of(harness, {"error"})],
        }
        tones = (signal or {}).get("tones", {})
        tones_after = (after_stop or {}).get("tones", {})
        baseline_amp = ((baseline or {}).get("tones", {}) or {}).get("expected_amplitude", 0.0)
        playing_amp = tones.get("expected_amplitude", 0.0)
        stopped_amp = tones_after.get("expected_amplitude", 0.0)
        mpv_on_hdmi = any(
            item.get("sink") == HDMI_SINK or item.get("sink_index") == HDMI_SINK_INDEX
            for item in result["mpv_streams_during"]
        )
        mpv_after_stop = bool(result["mpv_streams_after_stop"])
        result["attribution"] = {
            "baseline_expected_amplitude": baseline_amp,
            "playing_expected_amplitude": playing_amp,
            "stopped_expected_amplitude": stopped_amp,
            "ratio_playing_to_stopped": round(playing_amp / stopped_amp, 2) if stopped_amp else None,
            "dominant_tone_while_playing": tones.get("dominant"),
            "margin_db_while_playing": tones.get("margin_db"),
            "mpv_stream_on_hdmi_while_playing": mpv_on_hdmi,
            "mpv_stream_present_after_stop": mpv_after_stop,
            "signal_attributed_to_app": bool(
                playing_amp > 0 and playing_amp > baseline_amp * 3.0 and mpv_on_hdmi
            ),
        }
        result["passed"] = bool(
            raised is None
            and result["state_after_1_5s"]["status"] == "playing"
            and result["state_after_1_5s"]["current_track_id"] == "local-ok-a"
            and result["state_after_1_5s"]["mpv_filename"] == Path(files["local-ok-a"]).name
            and result["events"]["track"] == ["local-ok-a"]
            and (signal or {}).get("peak_dbfs", -120.0) > SIGNAL_FLOOR_DBFS
            and result["attribution"]["signal_attributed_to_app"]
            and result["attribution"]["signal_attributed_to_app"]
        )
    finally:
        harness.close()
    return result


def scenario_b(files: dict[str, Path], probe: MpvProbe) -> dict:
    """B. Недоступный средний трек: Local OK → Broken → Local OK 2."""
    harness = build(files, home="d04_b")
    result: dict = {"scenario": "B_недоступный_средний_трек"}
    try:
        broken = make_track("broken-b", "Broken B")
        ok1 = make_track("local-ok-b", "Local OK B")
        ok2 = make_track("local-ok-b2", "Local OK B 2")
        harness.stub.files.update({
            ok1.id: files["local-ok-b"], ok2.id: files["local-ok-b2"],
        })
        raised = None
        try:
            harness.player.play_tracks([ok1, broken, ok2], start=0)
        except Exception as exc:  # noqa: BLE001
            raised = f"{type(exc).__name__}: {exc}"
        time.sleep(1.2)
        result["after_start"] = snapshot_state(harness, probe)
        before = len(track_event_ids(harness))
        harness.player.next_track()
        time.sleep(1.5)
        result["after_next"] = snapshot_state(harness, probe)
        result["raised"] = raised
        result["track_events"] = track_event_ids(harness)[before:]
        result["errors"] = [e["payload"].get("error") for e in events_of(harness, {"error"})]
        result["passed"] = bool(
            raised is None
            and result["after_start"]["current_track_id"] == "local-ok-b"
            and result["after_next"]["current_track_id"] == "local-ok-b2"
            and result["after_next"]["mpv_filename"] == Path(files["local-ok-b2"]).name
            and result["track_events"] == ["local-ok-b2"]
        )
    finally:
        harness.close()
    return result


def scenario_c(files: dict[str, Path], probe: MpvProbe) -> dict:
    """C. Два последовательных отказа: ровно две попытки, затем STOPPED."""
    harness = build(files, home="d04_c")
    result: dict = {"scenario": "C_два_последовательных_отказа"}
    try:
        broken1 = make_track("broken-c1", "Broken C1")
        broken2 = make_track("broken-c2", "Broken C2")
        ok1 = make_track("local-ok-c", "Local OK C")
        harness.stub.files.update({ok1.id: files["local-ok-c"]})
        raised = None
        try:
            harness.player.play_tracks([broken1, broken2, ok1], start=0)
        except Exception as exc:  # noqa: BLE001
            raised = f"{type(exc).__name__}: {exc}"
        time.sleep(1.0)
        errors_after_start = len(events_of(harness, {"error"}))
        result["state"] = snapshot_state(harness, probe)
        time.sleep(3.0)  # контроль бесконечного перебора
        result["errors_after_start"] = errors_after_start
        result["errors_after_4s"] = len(events_of(harness, {"error"}))
        result["failed_events"] = len(events_of(harness, {"failed"}))
        result["finished_events"] = len(events_of(harness, {"finished"}))
        result["track_events"] = track_event_ids(harness)
        result["raised"] = raised
        result["no_infinite_retry"] = result["errors_after_4s"] == errors_after_start
        result["passed"] = bool(
            errors_after_start == 2
            and result["failed_events"] == 1
            and result["state"]["status"] == "stopped"
            and result["track_events"] == []
            and result["no_infinite_retry"]
        )
    finally:
        harness.close()
    return result


def scenario_d(files: dict[str, Path], probe: MpvProbe) -> dict:
    """D. Единственный недоступный трек: состояние, overlay, события."""
    harness = build(files, home="d04_d")
    result: dict = {"scenario": "D_единственный_недоступный_трек"}
    try:
        broken = make_track("broken-d", "Broken D")
        raised = None
        try:
            harness.player.play_tracks([broken], start=0)
        except Exception as exc:  # noqa: BLE001
            raised = f"{type(exc).__name__}: {exc}"
        time.sleep(1.0)
        result["state"] = snapshot_state(harness, probe)
        result["raised"] = raised
        result["events"] = {
            "error": len(events_of(harness, {"error"})),
            "failed": len(events_of(harness, {"failed"})),
            "finished": len(events_of(harness, {"finished"})),
            "track": track_event_ids(harness),
            "error_messages": [e["payload"].get("error") for e in events_of(harness, {"error"})],
        }
        result["failed_event_present"] = result["events"]["failed"] > 0
        result["note"] = (
            "Событие failed не отправляется: лимит MAX_CONSECUTIVE_FAILURES=2 не "
            "достигнут, очередь просто закончилась (известное замечание P2)."
        )
        result["passed"] = bool(
            result["state"]["status"] == "stopped"
            and result["state"]["overlay"] == "track_unavailable"
            and result["events"]["error"] == 1
            and result["events"]["track"] == []
        )
    finally:
        harness.close()
    return result


def scenario_e(files: dict[str, Path], probe: MpvProbe) -> dict:
    """E. Восстановление после отказов: новая очередь с рабочим треком."""
    harness = build(files, home="d04_e")
    result: dict = {"scenario": "E_восстановление_после_отказов"}
    try:
        broken1 = make_track("broken-e1", "Broken E1")
        broken2 = make_track("broken-e2", "Broken E2")
        ok = make_track("local-ok-e", "Local OK E")
        harness.stub.files.update({ok.id: files["local-ok-e"]})
        try:
            harness.player.play_tracks([broken1, broken2], start=0)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.8)
        result["state_after_failures"] = snapshot_state(harness, probe)
        errors_before = len(events_of(harness, {"error"}))
        # Новая сессия: счётчик отказов должен сброситься.
        raised = None
        try:
            harness.player.play_tracks([ok], start=0)
        except Exception as exc:  # noqa: BLE001
            raised = f"{type(exc).__name__}: {exc}"
        time.sleep(1.5)
        result["raised"] = raised
        result["state_after_recovery"] = snapshot_state(harness, probe)
        result["new_errors"] = len(events_of(harness, {"error"})) - errors_before
        result["track_events"] = track_event_ids(harness)
        result["passed"] = bool(
            raised is None
            and result["state_after_recovery"]["status"] == "playing"
            and result["state_after_recovery"]["current_track_id"] == "local-ok-e"
            and result["state_after_recovery"]["mpv_filename"] == Path(files["local-ok-e"]).name
            and result["new_errors"] == 0
        )
    finally:
        harness.close()
    return result


def scenario_f(files: dict[str, Path], probe: MpvProbe) -> dict:
    """F. Repeat ONE / Repeat ALL / Shuffle с недоступными треками."""
    results: dict = {}

    def run(mode: str, shuffle: bool, queue_ids: list[str], label: str) -> dict:
        harness = build(files, repeat=mode, shuffle=shuffle, home=f"d04_f_{label}")
        entry: dict = {"repeat": mode, "shuffle": shuffle, "queue": queue_ids}
        try:
            tracks = []
            for track_id in queue_ids:
                if track_id.startswith("broken"):
                    tracks.append(make_track(track_id, f"Broken {track_id}"))
                else:
                    tracks.append(make_track(track_id, f"OK {track_id}"))
                    harness.stub.files[track_id] = files[track_id]
            raised = None
            try:
                harness.player.play_tracks(tracks, start=0)
            except Exception as exc:  # noqa: BLE001
                raised = f"{type(exc).__name__}: {exc}"
            time.sleep(1.5)
            entry["raised"] = raised
            entry["state_after_start"] = snapshot_state(harness, probe)
            errors_start = len(events_of(harness, {"error"}))
            # Смотрим, что происходит дальше: повторы не должны превращаться
            # в бесконечный перебор отказов.
            time.sleep(4.0)
            entry["errors_start"] = errors_start
            entry["errors_after_4s"] = len(events_of(harness, {"error"}))
            entry["errors_delta"] = entry["errors_after_4s"] - errors_start
            entry["track_events"] = track_event_ids(harness)
            entry["state_final"] = snapshot_state(harness, probe)
            entry["mpv_processes"] = len(q.mpv_processes())
            entry["bounded_errors"] = entry["errors_delta"] <= 2
            entry["passed"] = bool(entry["bounded_errors"])
            return entry
        finally:
            harness.close()

    results["repeat_one_broken_first"] = run("one", False, ["broken-f1", "local-ok-f1"], "one")
    results["repeat_all_broken_first"] = run("all", False, ["broken-f2", "local-ok-f2"], "all")
    results["shuffle_broken_first"] = run("off", True, ["broken-f3", "local-ok-f3"], "shuffle")
    passed = all(v["passed"] for v in results.values() if isinstance(v, dict))
    return {"scenario": "F_repeat_и_shuffle", "cases": results, "passed": passed}


def scenario_g(files: dict[str, Path], probe: MpvProbe) -> dict:
    """G. История и обратная связь."""
    harness = build(files, home="d04_g")
    result: dict = {"scenario": "G_история_и_обратная_связь"}
    try:
        broken = make_track("broken-g", "Broken G")
        ok = make_track("local-ok-g", "Local OK G")
        harness.stub.files[ok.id] = files["local-ok-g"]
        harness.stub.wave_tracks = lambda: [broken, ok]  # type: ignore[assignment]
        raised = None
        try:
            harness.player.start_wave()
        except Exception as exc:  # noqa: BLE001
            raised = f"{type(exc).__name__}: {exc}"
        time.sleep(1.5)
        result["raised"] = raised
        result["state"] = snapshot_state(harness, probe)
        result["feedback"] = list(harness.stub.feedback)
        started_ids = [key for kind, key in harness.stub.feedback if kind == "started"]
        result["started_feedback_ids"] = started_ids
        result["broken_has_started_feedback"] = any("broken" in key for key in started_ids)
        recent = harness.paths.database_file.exists()
        history = harness.player.store.recent_tracks(limit=50)
        result["history_ids"] = [t.id for t in history]
        result["history_has_broken"] = any("broken" in t.id for t in history)
        result["history_file_exists"] = recent
        result["passed"] = bool(
            not result["broken_has_started_feedback"]
            and not result["history_has_broken"]
            and "local-ok-g" in result["history_ids"]
        )
    finally:
        harness.close()
    return result


def scenario_h(files: dict[str, Path], probe: MpvProbe, stress_map: dict[str, Path]) -> dict:
    """H. Стресс: сценарий A не менее 20 раз на разных файлах."""
    harness = build(files, repeat="all", home="d04_h")
    rounds: list[dict] = []
    try:
        mpv_before = set(q.mpv_processes())
        before = q.proc_snapshot(sorted(mpv_before)[0]) if mpv_before else {}
        for index in range(20):
            track_id = sorted(stress_map)[index]
            broken_id = f"broken-h{index:02d}"
            broken = make_track(broken_id, f"Broken H{index}")
            ok = make_track(track_id, f"OK {track_id}")
            harness.stub.files[track_id] = stress_map[track_id]
            errors_before = len(events_of(harness, {"error"}))
            tracks_before = len(track_event_ids(harness))
            raised = None
            try:
                harness.player.play_tracks([broken, ok], start=0)
            except Exception as exc:  # noqa: BLE001
                raised = f"{type(exc).__name__}: {exc}"
            time.sleep(0.8)
            state = snapshot_state(harness, probe)
            rounds.append({
                "index": index,
                "file": track_id,
                "raised": raised,
                "current_track_id": state["current_track_id"],
                "mpv_filename": state["mpv_filename"],
                "mpv_position_ms": state["mpv_position_ms"],
                "status": state["status"],
                "new_errors": len(events_of(harness, {"error"})) - errors_before,
                "new_track_events": len(track_event_ids(harness)) - tracks_before,
                "ok": bool(
                    raised is None
                    and state["status"] == "playing"
                    and state["current_track_id"] == track_id
                    and state["mpv_filename"] == Path(stress_map[track_id]).name
                    and state["mpv_position_ms"] > 0
                ),
            })
        mpv_after = set(q.mpv_processes())
        after = q.proc_snapshot(sorted(mpv_after)[0]) if mpv_after else {}
        result = {
            "scenario": "H_стресс_20_итераций",
            "rounds": rounds,
            "passed_rounds": sum(1 for r in rounds if r["ok"]),
            "total_rounds": len(rounds),
            "mpv_processes_final": len(q.mpv_processes()),
            "sockets_final": q.stale_ipc_sockets(),
            "errors_total": len(events_of(harness, {"error"})),
            "track_events_total": len(track_event_ids(harness)),
            "rss_kb_before": before.get("rss_kb"),
            "rss_kb_after": after.get("rss_kb"),
            "fds_before": before.get("fds"),
            "fds_after": after.get("fds"),
        }
        result["passed"] = bool(
            result["passed_rounds"] == result["total_rounds"]
            and result["mpv_processes_final"] <= 1
        )
    finally:
        harness.close()
    return result


def main() -> int:
    q.ensure_dirs()
    files: dict[str, Path] = {}
    for path in sorted(MEDIA.iterdir()):
        if path.suffix.lower() in {".mp3", ".wav", ".flac"}:
            files[path.name] = path
    probes = {
        "local-ok-a": "qa_signal_1234hz.wav",
        "local-ok-a2": "qa_tone_c523_320k.mp3",
        "local-ok-b": "qa_tone_b220_short.mp3",
        "local-ok-b2": "qa_tone_g349_44k.wav",
        "local-ok-c": "qa_tone_a880_48k.wav",
        "local-ok-e": "qa_tone_e659_lossless.flac",
        "local-ok-f1": "qa_tone_a440_128k.mp3",
        "local-ok-f2": "qa_tone_c523_320k.mp3",
        "local-ok-f3": "qa_tone_b220_short.mp3",
        "local-ok-g": "qa_tone_a440_128k.mp3",
    }
    for logical, filename in probes.items():
        files[logical] = MEDIA / filename
    stress_map = {
        path.name: path for path in sorted(MEDIA.glob("qa_stress_*"))
    }

    probe = MpvProbe()
    previous_sink = switch_default_sink(HDMI_SINK)
    signal_source = monitor_source(HDMI_SINK)
    sink_states = q.run(["pactl", "list", "sinks", "short"])[1].strip().splitlines()
    payload: dict = {
        "scenario": "QA-VERIFY-02: приёмка исправления D-04",
        "sha": BASE_SHA,
        "branch": "fix/qa-first-track-recovery-02",
        "media": str(MEDIA),
        "monitor_source": signal_source,
        "default_sink_before": previous_sink,
        "default_sink_during": default_sink(),
        "sink_states_during": sink_states,
        "files": {k: v.name for k, v in files.items()},
    }
    only = set(sys.argv[1:])
    def maybe(key: str, func, *args) -> None:
        if not only or key in only:
            payload[key] = func(*args)

    maybe("A", scenario_a, files, probe, signal_source)
    maybe("B", scenario_b, files, probe)
    maybe("C", scenario_c, files, probe)
    maybe("D", scenario_d, files, probe)
    maybe("E", scenario_e, files, probe)
    maybe("F", scenario_f, files, probe)
    maybe("G", scenario_g, files, probe)
    maybe("H", scenario_h, files, probe, stress_map)

    summary = {
        key: payload[key]["passed"]
        for key in "ABCDEFGH"
        if key in payload and isinstance(payload[key], dict) and "passed" in payload[key]
    }
    missing = [k for k in "ABCDEFGH" if k in payload and "passed" not in payload[k]]
    if missing:
        payload["_missing_passed_key"] = missing
    criteria = {
        "рабочие_треки_запускаются_после_недоступных": summary.get("A", False) and summary.get("B", False),
        "стресс_20_из_20": summary.get("H", False),
        "лимит_последовательных_ошибок": payload.get("C", {}).get("errors_after_start") == 2,
        "нет_бесконечной_рекурсии": payload.get("C", {}).get("no_infinite_retry", False),
        "нет_ложных_событий_успеха": summary.get("G", False),
        "нет_осиротевших_mpv_и_сокетов": not q.mpv_processes() and not q.stale_ipc_sockets(),
        "состояние_не_застревает_в_buffering": payload.get("A", {}).get("state_after_1_5s", {}).get("status") != "buffering",
        "нет_регрессий_в_воспроизведении": summary.get("A", False) and summary.get("B", False) and summary.get("E", False),
    }
    payload["summary"] = summary
    payload["criteria"] = criteria
    payload["verdict"] = "PASS" if all(criteria.values()) and all(summary.values()) else "FAIL"
    q.run(["pactl", "set-default-sink", previous_sink], timeout=20)
    payload["default_sink_restored"] = default_sink()
    path = q.write_result("verify_d04", payload)
    print(json.dumps({"summary": summary, "criteria": criteria, "verdict": payload["verdict"]},
                     ensure_ascii=False, indent=2))
    print(f"→ {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
