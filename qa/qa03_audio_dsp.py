"""QA-03: реальное аудиооборудование и DSP.

Что есть в системе (PipeWire 1.0.5), что mpv о ней думает, и как ведёт себя
плеер при смене выхода, отключении устройства и работе DSP.

Отсутствующие устройства помечаются NOT AVAILABLE и не подменяются
виртуальными реализациями.

Запуск: python qa/qa03_audio_dsp.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qaenv as q  # noqa: E402


def pactl(args: list[str]) -> str:
    return q.run(["pactl", *args], timeout=20)[1].strip()


def device_inventory() -> dict:
    sinks = []
    for line in pactl(["list", "sinks", "short"]).splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            sinks.append({"index": parts[0], "name": parts[1], "description": parts[2]})
    sources = []
    for line in pactl(["list", "sources", "short"]).splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            sources.append({"index": parts[0], "name": parts[1], "description": parts[2]})
    cards = []
    for line in pactl(["list", "cards", "short"]).splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            cards.append({"index": parts[0], "name": parts[1]})
    default_sink = pactl(["get-default-sink"])
    formats = pactl(["list", "sink-inputs"])[:1500]
    return {
        "sinks": sinks, "sources": sources, "cards": cards,
        "default_sink": default_sink,
        "sink_formats": formats,
        "bluetooth_present": any("bluez" in (s["name"] + s["description"]) for s in sinks),
        "usb_audio_present": any("usb" in (s["name"] + s["description"]).lower() for s in sinks),
        "hdmi_present": any("hdmi" in (s["name"] + s["description"]).lower() for s in sinks),
        "builtin_analog_present": any(
            "analog" in (s["name"] + s["description"]).lower() and "alsa_output" in s["name"]
            for s in sinks
        ),
    }


def mpv_device_list() -> list[dict]:
    """Список устройств, который видит mpv (реальная конфигурация)."""
    code, out, _ = q.run([str(q.MPV), "--audio-device=help"], timeout=30)
    devices = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("'"):
            parts = line.split("'", 2)
            if len(parts) >= 3:
                devices.append({"id": parts[1], "description": parts[2].strip("() ")})
    return devices


def section_outputs(harness) -> dict:
    """Что плеер считает выходами и что реально выбрано."""
    outputs = [o.to_dict() for o in harness.player.outputs()]
    return {
        "player_outputs": outputs,
        "mpv_devices": mpv_device_list(),
        "current": harness.backend.audio_device(),
    }


def section_switch_during_playback(harness) -> dict:
    """Смена выхода во время воспроизведения."""
    out: dict = {"steps": []}
    outputs = harness.player.outputs()
    for target in outputs:
        try:
            t0 = time.monotonic()
            chosen = harness.player.set_output(target.id)
            time.sleep(1.0)
            out["steps"].append({
                "requested": target.id,
                "chosen": chosen.to_dict(),
                "latency_ms": round((time.monotonic() - t0) * 1000),
                "still_playing": harness.player.state.status.value,
                "mpv_audio_device": harness.backend.audio_device(),
                "position_ms": harness.backend.position_ms(),
            })
        except Exception as exc:  # noqa: BLE001
            out["steps"].append({"requested": target["id"], "error": f"{type(exc).__name__}: {exc}"})
    return out


def section_device_unplug(harness) -> dict:
    """Имитация отключения устройства: pactl suspend-sink.

    Физическое отключение HDMI-кабеля запрещено без разрешения владельца,
    поэтому устройство «отключается» системным флагом PipeWire. Проверяем
    объективно: сигнал на мониторе sink'а должен исчезнуть, а плеер — не
    упасть и не остановить воспроизведение.
    """
    from probe_audio import monitor_source, peak_dbfs

    out: dict = {}
    sink = pactl(["get-default-sink"])
    out["sink"] = sink
    raw_path = q.ARTIFACTS / "qa03_sink.raw"
    monitor = monitor_source()
    record = subprocess.Popen(
        ["parec", "--device", monitor, "--format=s16le", "--rate", "48000",
         "--channels", "2", "--raw"],
        stdout=raw_path.open("wb"), stderr=subprocess.DEVNULL,
    )
    time.sleep(0.8)
    harness.player.play_tracks(harness.adapter.tracks(), start=0)
    time.sleep(2.5)

    def capture(tag: str, seconds: float = 2.5, offset: int = 0) -> tuple[dict, int]:
        time.sleep(seconds)
        raw = raw_path.read_bytes() if raw_path.exists() else b""
        chunk = raw[offset:]
        peak, rms, frames = peak_dbfs(chunk)
        return {"phase": tag, "peak_dbfs": round(peak, 2), "rms_dbfs": round(rms, 2),
                "frames": frames, "silent": peak < -80}, len(raw)

    out["before"], offset = capture("до suspend")
    code, stdout, stderr = q.run(["pactl", "suspend-sink", sink, "true"], timeout=20)
    out["suspend"] = {"code": code, "stdout": stdout.strip(), "stderr": stderr.strip()}
    out["during_suspend"], offset = capture("sink suspended", offset=offset)
    out["during_suspend_state"] = {
        "position_ms": harness.backend.position_ms(),
        "status": harness.player.state.status.value,
        "mpv_running": harness.backend.is_running,
        "mpv_device": harness.backend.audio_device(),
        "overlay": str(harness.player.state.overlay),
    }
    code, stdout, stderr = q.run(["pactl", "suspend-sink", sink, "false"], timeout=20)
    out["resume"] = {"code": code, "stdout": stdout.strip(), "stderr": stderr.strip()}
    out["after_resume"], offset = capture("после resume", offset=offset)
    out["after_resume_state"] = {
        "position_ms": harness.backend.position_ms(),
        "status": harness.player.state.status.value,
        "overlay": str(harness.player.state.overlay),
    }
    record.terminate()
    try:
        record.wait(timeout=5)
    except subprocess.TimeoutExpired:
        record.kill()
    raw_path.unlink(missing_ok=True)
    return out


def section_volume_policy(harness) -> dict:
    """Запрос громкости выше 100 % и безопасная громкость."""
    out: dict = {}
    before = harness.player.applied_volume
    requested = harness.player.set_volume(120)
    time.sleep(0.5)
    mpv_state = harness.backend.snapshot_state()
    out["above_100"] = {
        "requested": 120,
        "applied_returned": requested,
        "player_volume": harness.player.volume,
        "applied_volume": harness.player.applied_volume,
        "mpv_volume": mpv_state.get("volume"),
        "limited_event": harness.recorder.count("volume_limited"),
        "events": [e for e in harness.recorder.as_list() if e["event"] == "volume_limited"][-2:],
    }
    out["before_applied"] = before
    harness.player.set_volume(30)
    time.sleep(0.3)
    out["after_restore"] = {"applied": harness.player.applied_volume,
                            "mpv_volume": harness.backend.snapshot_state().get("volume")}
    return out


def section_dsp_consistency(harness) -> dict:
    """Соответствие запрошенного DSP тому, что реально стоит в mpv."""
    out: dict = {"cases": []}
    from neurowave.audio.dsp_profiles import AudioProfile, DspMode, EqBand, Limiter

    custom = AudioProfile(
        id="qa-eq",
        name="QA custom EQ",
        mode=DspMode.ENHANCED,
        preamp_db=-3.0,
        bands=(EqBand(kind="peaking", frequency_hz=1000.0, gain_db=4.0, q=1.0),),
        limiter=Limiter(enabled=True, ceiling_dbfs=-1.5, lookahead_ms=5.0, release_ms=50.0),
    )
    saved = harness.player.save_profile(custom)
    for label, action in [
        ("pure", lambda: harness.player.set_dsp_mode("pure")),
        ("enhanced", lambda: harness.player.set_dsp_mode("enhanced")),
        ("flat", lambda: harness.player.set_dsp_profile("flat")),
        ("qa-eq", lambda: harness.player.set_dsp_profile("qa-eq")),
        ("несуществующий", lambda: harness.player.set_dsp_profile("no-such-profile")),
    ]:
        try:
            status = action()
            time.sleep(0.4)
            mpv_af = harness.backend.audio_filter()
            out["cases"].append({
                "requested": label,
                "mode": status.mode.value,
                "profile": status.profile.id if status.profile else None,
                "confirmed": status.confirmed,
                "fallback": status.fallback,
                "reason": status.reason,
                "graph": status.graph,
                "mpv_af": mpv_af,
                "matches": _normalize(status.graph) == _normalize(mpv_af),
                "position_ms": harness.backend.position_ms(),
                "status": harness.player.state.status.value,
            })
        except Exception as exc:  # noqa: BLE001
            out["cases"].append({"requested": label, "error": f"{type(exc).__name__}: {exc}"})
    out["saved_profile_id"] = saved.id
    return out


def _normalize(text: str | None) -> str | None:
    if text is None:
        return None
    return "".join(text.split()).lower().replace("lavfi=[", "").replace("]", "")


def main() -> int:
    q.ensure_dirs()
    result: dict = {"scenario": "QA-03 аудиоустройства и DSP"}
    result["system_devices"] = device_inventory()
    harness = q.build_local_player("qa03_audio", repeat="all", volume=30)
    try:
        result["outputs"] = section_outputs(harness)
        harness.player.play_tracks(harness.adapter.tracks(), start=0)
        time.sleep(1.5)
        result["switch_during_playback"] = section_switch_during_playback(harness)
        result["device_unplug"] = section_device_unplug(harness)
        result["volume_policy"] = section_volume_policy(harness)
        result["dsp_consistency"] = section_dsp_consistency(harness)
        result["final"] = {
            "status": harness.player.state.status.value,
            "mpv_device": harness.backend.audio_device(),
            "audio_params": harness.backend.snapshot_state().get("audio_params"),
        }
    finally:
        harness.close()
    path = q.write_result("qa03_audio_dsp", result)
    print(json.dumps(result, ensure_ascii=False, indent=2)[:16000])
    print(f"→ {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
