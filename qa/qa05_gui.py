"""QA-05: визуальный аудит Qt-интерфейса.

Два прохода:
  1. Реальный запуск на живом экране (DISPLAY=:0, 1920x1080) — окно
     приложения и снимок экрана, чтобы проверить поведение в настоящей
     сессии рабочего стола.
  2. Детерминированный проход по состояниям (offscreen): пустой плеер,
     воспроизведение, длинное название, ошибка, Sound Lab, минимальный размер,
     масштабирование 150 %.

Снимки сохраняются в docs/qa/screenshots/ и содержат только синтетические
тестовые данные ( названия файлов QA, громкость, имя HDMI-выхода).

Запуск: python qa/qa05_gui.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qaenv as q  # noqa: E402

SHOTS = q.ROOT / "docs" / "qa" / "screenshots"


def screen_capture(path: Path) -> dict:
    """Снимок экрана X11 через ffmpeg x11grab."""
    path.parent.mkdir(parents=True, exist_ok=True)
    code, stdout, stderr = q.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "x11grab", "-video_size", "1920x1080",
         "-i", os.environ.get("DISPLAY", ":0"), "-frames:v", "1", str(path)],
        timeout=60,
    )
    return {"code": code, "stderr": stderr.strip()[:300], "file": path.name,
            "exists": path.exists(), "size": path.stat().st_size if path.exists() else 0}


def real_display_launch() -> dict:
    """Запуск окна на живом экране рабочего стола."""
    out: dict = {}
    home = q.qa_home("qa05_real")
    env = {
        "NEUROWAVE_HOME": str(home),
        "PATH": f"{q.MPV.parent}:{os.environ['PATH']}",
        "PYTHONPATH": str(q.ROOT / "src"),
        "DISPLAY": os.environ.get("DISPLAY", ":0"),
    }
    proc = subprocess.Popen(
        [str(q.ROOT / ".venv" / "bin" / "neurowave"), "--provider", "local", "app"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    time.sleep(6.0)
    code, stdout, _ = q.run(["wmctrl", "-l"], timeout=30)
    windows = [line for line in stdout.splitlines() if "NeuroWave" in line]
    out["windows"] = windows
    out["window_found"] = bool(windows)
    out["captures"] = [screen_capture(SHOTS / f"real_display_{index}.png")
                       for index in range(1)]
    out["process_alive"] = proc.poll() is None
    mpv_before = q.mpv_processes()
    out["mpv_started_with_app"] = len(mpv_before)
    # Закрытие окна: сначала мягко (WM_DELETE), как это делает пользователь.
    if windows:
        parts = windows[0].split()
        window_id = parts[0] if parts else ""
        subprocess.run(["wmctrl", "-i", "-c", window_id], timeout=30)
        time.sleep(4.0)
        out["after_close_alive"] = proc.poll() is None
        out["mpv_after_close"] = q.mpv_processes()
        out["sockets_after_close"] = q.stale_ipc_sockets()
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=10)
    time.sleep(1.0)
    out["mpv_final"] = q.mpv_processes()
    out["sockets_final"] = q.stale_ipc_sockets()
    return out


def widget_audit() -> dict:
    """Состояния окна в offscreen-режиме: снимки и измерения."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QFontMetrics
    from PySide6.QtWidgets import QApplication

    from neurowave.domain import Track
    from neurowave.ui.app import _build_window
    from neurowave.ui.viewmodel import PlayerViewModel

    out: dict = {"shots": [], "states": []}
    app = QApplication.instance() or QApplication([])
    harness = q.build_local_player("qa05_widgets", repeat="off", volume=30)
    vm = PlayerViewModel(harness.player)
    window = _build_window(vm)
    window.show()
    app.processEvents()

    refresh_errors: list[str] = []

    def safe_refresh() -> None:
        """Ошибки обновления окна фиксируем, аудит не прерываем."""
        try:
            window._refresh()
        except Exception as exc:  # noqa: BLE001
            refresh_errors.append(f"{type(exc).__name__}: {exc}")
        app.processEvents()

    def shot(name: str) -> dict:
        path = SHOTS / name
        path.parent.mkdir(parents=True, exist_ok=True)
        pixmap = window.grab()
        pixmap.save(str(path))
        return {"file": name, "size": [pixmap.width(), pixmap.height()],
                "bytes": path.stat().st_size if path.exists() else 0}

    # 1. Пустое состояние
    safe_refresh()
    out["states"].append({"state": "пустой плеер", "shot": shot("01_empty.png")})
    out["shots"].append("01_empty.png")

    # 2. Воспроизведение
    harness.player.play_tracks(harness.adapter.tracks(), start=0)
    time.sleep(2.0)
    safe_refresh()
    out["states"].append({"state": "воспроизведение",
                          "shot": shot("02_playing.png"),
                          "snapshot": _safe_snapshot(vm)})
    out["shots"].append("02_playing.png")

    # 3. Длинное название
    long_track = next(
        (t for t in harness.adapter.tracks() if "длинное" in t.title or "длинное" in t.id),
        None,
    )
    if long_track is None:
        long_track = Track(id="long.mp3", title="Очень длинное название трека",
                           provider="local", duration_ms=245000)
    try:
        harness.player.play_track(long_track)
    except Exception as exc:  # noqa: BLE001
        out["long_title_error"] = f"{type(exc).__name__}: {exc}"
    time.sleep(1.0)
    safe_refresh()
    out["states"].append({"state": "длинное название",
                          "shot": shot("03_long_title.png"),
                          "snapshot": _safe_snapshot(vm),
                          "overflow": _overflow_report(window)})
    out["shots"].append("03_long_title.png")

    # 4. Состояние ошибки: недоступный трек
    broken = Track(id="broken.mp3", title="Недоступный трек", provider="local")
    try:
        harness.player.play_track(broken)
    except Exception as exc:  # noqa: BLE001
        out["error_state_reason"] = f"{type(exc).__name__}: {exc}"
    time.sleep(0.5)
    safe_refresh()
    out["states"].append({"state": "ошибка", "shot": shot("04_error.png"),
                          "snapshot": _safe_snapshot(vm)})
    out["shots"].append("04_error.png")

    # 5. Sound Lab
    lab = _open_lab_snapshot(app, vm)
    if lab:
        out["states"].append({"state": "Sound Lab", "shot": lab["shot"],
                              "info": lab["info"]})
        out["shots"].append(lab["shot"]["file"])

    # 6. Минимальный размер окна
    window.resize(320, 240)
    app.processEvents()
    safe_refresh()
    out["states"].append({"state": "минимальное окно 320x240",
                          "shot": shot("05_min_size.png"),
                          "overflow": _overflow_report(window)})
    out["shots"].append("05_min_size.png")

    # 7. Максимизация на реальном разрешении
    window.resize(1920, 1040)
    app.processEvents()
    safe_refresh()
    out["states"].append({"state": "1920x1040", "shot": shot("06_wide.png")})
    out["shots"].append("06_wide.png")

    # 8. Клавиатурная навигация и доступность
    out["keyboard"] = _keyboard_audit(window, app)

    # 9. Контраст текста
    out["contrast"] = _contrast_audit(window)

    out["refresh_errors"] = refresh_errors
    vm.close()
    harness.close()
    return out


def _safe_snapshot(vm) -> dict:
    snap = vm.snapshot()
    return {
        "title": snap.title, "subtitle": snap.subtitle, "quality": snap.quality,
        "status_text": getattr(vm, "last_error", None),
        "volume": snap.volume, "applied_volume": snap.applied_volume,
        "volume_limited": snap.volume_limited,
        "profile_id": snap.profile_id, "profile_mode": snap.profile_mode,
        "dsp_confirmed": snap.dsp_confirmed, "output_id": snap.output_id,
        "is_playing": snap.is_playing, "position_ms": snap.position_ms,
        "duration_ms": snap.duration_ms,
    }


def _overflow_report(window) -> dict:
    """Есть ли обрезанный/переполненный текст в метках окна."""
    from PySide6.QtGui import QFontMetrics
    from PySide6.QtWidgets import QLabel, QLineEdit

    report: dict = {"labels": []}
    for label in window.findChildren(QLabel):
        text = label.text()
        if not text:
            continue
        metrics = QFontMetrics(label.font())
        width = metrics.boundingRect(text).width()
        available = label.width()
        item = {"text": text[:70], "text_px": width, "label_px": available,
                "clipped": width > available + 1}
        report["labels"].append(item)
    for edit in window.findChildren(QLineEdit):
        report.setdefault("line_edits", []).append({"text": edit.text()[:60]})
    return report


def _open_lab_snapshot(app, vm) -> dict | None:
    """Открыть Sound Lab и снять снимок окна."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QDialog

    from neurowave.ui import soundlab

    holder: dict = {}
    original = soundlab._build_dialog

    def patched(vm_):
        dialog = original(vm_)
        holder["dialog"] = dialog
        return dialog

    soundlab._build_dialog = patched
    try:
        dialog = soundlab._build_dialog(vm)
        dialog.show()
        app.processEvents()
        time.sleep(0.5)
        app.processEvents()
        path = SHOTS / "04b_sound_lab.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        pixmap = dialog.grab()
        pixmap.save(str(path))
        info = {
            "title": dialog.windowTitle(),
            "size": [pixmap.width(), pixmap.height()],
            "widgets": len(dialog.findChildren(QDialog)) ,
        }
        dialog.close()
        return {"shot": {"file": "04b_sound_lab.png", "size": [pixmap.width(), pixmap.height()],
                         "bytes": path.stat().st_size if path.exists() else 0},
                "info": info}
    except Exception as exc:  # noqa: BLE001
        return {"shot": {"file": "04b_sound_lab.png", "error": f"{type(exc).__name__}: {exc}"},
                "info": {}}
    finally:
        soundlab._build_dialog = original


def _keyboard_audit(window, app) -> dict:
    """Клавиатурная навигация: достижимость кнопок, фокус."""
    from PySide6.QtWidgets import QPushButton

    buttons = []
    for button in window.findChildren(QPushButton):
        if not button.isVisible():
            continue
        buttons.append({
            "text": button.text(),
            "focus_policy": int(button.focusPolicy()),
            "enabled": button.isEnabled(),
            "shortcut": button.shortcut().toString() if button.shortcut() else "",
            "accessible_name": button.accessibleName(),
            "tooltip": button.toolTip(),
            "minimum_size": [button.minimumWidth(), button.minimumHeight()],
        })
    focus = window.focusWidget()
    return {
        "buttons": buttons,
        "buttons_without_text": [b["text"] for b in buttons if not b["text"].strip()],
        "buttons_without_tooltip_or_name": [
            b["text"] for b in buttons if not b["tooltip"] and not b["accessible_name"]
        ],
        "focus_chain": _focus_chain(window),
        "initial_focus": focus.text() if focus is not None and hasattr(focus, "text") else None,
    }


def _focus_chain(window) -> list[str]:
    names: list[str] = []
    widget = window.focusWidget()
    if widget is None:
        widget = window
    for _ in range(30):
        widget = widget.focusProxy() or widget
        try:
            widget = widget.nextInFocusChain()
        except Exception:  # noqa: BLE001
            break
        if widget is None or widget is window:
            break
        text = getattr(widget, "text", None)
        names.append(str(text()) if callable(text) else widget.objectName() or type(widget).__name__)
    return names


def _contrast_audit(window) -> dict:
    """Контраст подписей (стиль #8b97a8) к фону окна."""
    from PySide6.QtGui import QColor, QPalette
    from PySide6.QtWidgets import QLabel

    def luminance(color: QColor) -> float:
        def channel(value: int) -> float:
            v = value / 255
            return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
        return 0.2126 * channel(color.red()) + 0.7152 * channel(color.green()) + 0.0722 * channel(color.blue())

    background = window.palette().color(QPalette.ColorRole.Window)
    out: dict = {"background": background.name(), "labels": []}
    for label in window.findChildren(QLabel):
        if not label.isVisible() or not label.text().strip():
            continue
        color = label.palette().color(label.foregroundRole())
        try:
            l1 = max(luminance(color), luminance(background))
            l2 = min(luminance(color), luminance(background))
            ratio = (l1 + 0.05) / (l2 + 0.05)
        except Exception:  # noqa: BLE001
            continue
        out["labels"].append({
            "text": label.text()[:40], "color": color.name(),
            "contrast_ratio": round(ratio, 2),
            "passes_aa_normal": ratio >= 4.5,
            "passes_aa_large": ratio >= 3.0,
        })
    return out


def main() -> int:
    q.ensure_dirs()
    SHOTS.mkdir(parents=True, exist_ok=True)
    result: dict = {"scenario": "QA-05 GUI"}
    result["real_display"] = real_display_launch()
    result["widget_audit"] = widget_audit()
    path = q.write_result("qa05_gui", result)
    print(json.dumps(result, ensure_ascii=False, indent=2)[:12000])
    print(f"→ {path}")
    print(f"Снимки: {SHOTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
