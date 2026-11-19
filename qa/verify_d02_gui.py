"""QA-VERIFY-03 / D-02: реальная GUI-проверка на X11 (не offscreen).

Сценарии:

* A — повреждённый `config.json`, запуск `neurowave app`: контролируемое
  завершение с понятной диагностикой, без зависшего окна и без traceback;
* B — конфигурация исправлена вручную: окно NeuroWave появляется;
* C — воспроизведение локального трека из графического интерфейса:
  «Волна» → Play → Pause → Next → громкость → Stop → штатное закрытие.
  Управление — синтетический ввод X11 (XTEST) по координатам внутри окна;
  объективный источник состояния — IPC mpv (файл, позиция, пауза, громкость)
  и снимок окна (строка состояния);
* D — несколько последовательных запусков с повреждённой и исправленной
  конфигурацией: не накапливаются зависшие процессы, не портится профиль,
  последующие запуски не блокируются.

Звук на время проверки выводится в HDMI-выход (тестовый сигнал слышен не на
рабочем месте): значение sink'а по умолчанию фиксируется до и после и
восстанавливается.

Запуск: python qa/verify_d02_gui.py
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qaenv as q  # noqa: E402

MEDIA = Path(os.environ.get("QA_MEDIA") or (q.SHARED / "media"))
HOMES = q.HOMES
APP = q.ROOT / ".venv" / "bin" / "neurowave"
SCREENSHOTS = q.ROOT / "docs" / "qa" / "screenshots"
SYSTEM_PY = "/usr/bin/python3"

BASE = {"provider": "local", "local_music_dir": str(MEDIA), "volume": 40}
BROKEN = {"provider": "local", "local_music_dir": str(MEDIA), "volume": "громко"}

#: Кнопки определяются по снимку окна: ширина окна меняется (длинный заголовок
#: растягивает окно — U-01), поэтому фиксированные координаты ненадёжны.
BUTTON_NAMES = ("previous", "play_pause", "next", "stop", "wave")


def sanitize(text: str) -> str:
    for raw, token in ((str(HOMES), "<QA_HOMES>"), (str(MEDIA), "<MEDIA>"),
                       (str(q.SHARED), "<QA_STAND>"), (str(q.ROOT), "<REPO>"),
                       (str(Path.home()), "<HOME>")):
        text = text.replace(raw, token)
    return text


def app_env(home: Path) -> dict[str, str]:
    """Окружение приложения: профиль изолирован, остальное — как у пользователя.

    Наследование системного окружения (DBus, тема оформления) нужно, чтобы
    приложение вело себя так же, как в обычном запуске.
    """
    env = dict(os.environ)
    env.update({
        "NEUROWAVE_HOME": str(home),
        "PATH": f"{q.MPV.parent}:{os.environ.get('PATH', '')}",
        "PYTHONPATH": str(q.ROOT / "src"),
        "DISPLAY": os.environ.get("DISPLAY", ":0"),
        "HOME": str(home),
        "LANG": "ru_RU.UTF-8",
    })
    return env


def write_config(home: Path, payload: dict) -> Path:
    (home / "config").mkdir(parents=True, exist_ok=True)
    target = home / "config" / "config.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def window_id() -> str | None:
    """Окно приложения (не другие окна с похожим названием)."""
    code, out, _ = q.run(["wmctrl", "-l"], timeout=20)
    if code != 0:
        return None
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        # Заголовок окна приложения — ровно "NeuroWave"; окна с похожим
        # названием (например, заголовок вкладки браузера) не подходят.
        title = parts[3].strip().split()
        if title and title[-1] == "NeuroWave":
            return parts[0]
    return None


def detect_buttons(win: str) -> dict:
    """Координаты кнопок по фактическому снимку окна."""
    code, out, _ = q.run([str(q.ROOT / ".venv" / "bin" / "python"),
                          str(q.ROOT / "qa" / "x11_buttons.py"), win], timeout=60)
    if code != 0:
        return {"error": out.strip()[-300:], "buttons": {}}
    try:
        data = json.loads(out.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {"error": "не разобран ответ детектора", "buttons": {}}
    return data


def click_button(win: str, name: str, buttons: dict, steps: dict) -> dict:
    """Клик по кнопке с предварительным определением её координат."""
    data = detect_buttons(win)
    buttons.clear()
    buttons.update(data.get("buttons", {}))
    steps[f"detect_{name}"] = {"row_y": data.get("row_y"), "runs": data.get("runs"),
                               "error": data.get("error")}
    point = buttons.get(name)
    if not point:
        return {"exit_code": -1, "output": f"кнопка {name} не найдена"}
    return click(win, point[0], point[1])


def click(win: str, x: int, y: int) -> dict:
    code, out, _ = q.run([SYSTEM_PY, str(q.ROOT / "qa" / "x11_click.py"),
                          "click", win, str(x), str(y)], timeout=30)
    return {"exit_code": code, "output": out.strip()}


def drag(win: str, x1: int, y1: int, x2: int, y2: int) -> dict:
    code, out, _ = q.run([SYSTEM_PY, str(q.ROOT / "qa" / "x11_click.py"),
                          "drag", win, str(x1), str(y1), str(x2), str(y2)], timeout=30)
    return {"exit_code": code, "output": out.strip()}


def activate(win: str) -> dict:
    code, out, _ = q.run([SYSTEM_PY, str(q.ROOT / "qa" / "x11_click.py"),
                          "activate", win], timeout=30)
    return {"exit_code": code, "output": out.strip()}


def capture(win: str, name: str) -> dict:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    target = SCREENSHOTS / name
    code, out, _ = q.run([str(q.ROOT / ".venv" / "bin" / "python"),
                          str(q.ROOT / "qa" / "x11_capture.py"), win, str(target)],
                         timeout=60)
    return {"file": name, "saved": target.exists(), "exit_code": code,
            "output": sanitize(out.strip())}


def mpv_socket() -> str | None:
    sockets = sorted(Path("/tmp").glob("neurowave-mpv-*.sock"), key=lambda p: p.stat().st_mtime)
    return str(sockets[-1]) if sockets else None


def mpv_state() -> dict:
    """Состояние mpv через IPC — объективный источник для сценария C."""
    path = mpv_socket()
    state_all = {"sockets": sorted(item.name for item in Path("/tmp").glob("neurowave-mpv-*.sock")),
                 "mpv": [line.split()[0] for line in mpv_processes()]}
    if not path:
        state_all["socket"] = None
        return state_all
    state: dict = {**state_all, "socket": Path(path).name}
    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(3)
        conn.connect(path)
        properties = ["filename", "idle-active", "pause", "time-pos", "volume",
                      "duration", "media-title"]
        for index, name in enumerate(properties, start=1):
            conn.sendall(json.dumps({"command": ["get_property", name],
                                     "request_id": index}).encode() + b"\n")
        buffer = b""
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and len(buffer) < 4000:
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            buffer += chunk
        conn.close()
    except OSError as exc:
        state["error"] = str(exc)
        return state
    for line in buffer.decode(errors="replace").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "request_id" not in item or "data" not in item:
            continue
        name = properties[item["request_id"] - 1]
        value = item["data"]
        state[name] = sanitize(str(value)) if isinstance(value, str) else value
    return state


def mpv_processes() -> list[str]:
    code, out, _ = q.run(["pgrep", "-a", "mpv"], timeout=15)
    lines = [line for line in out.splitlines() if line.strip()]
    return lines if code == 0 else []


def ipc_sockets() -> list[str]:
    return sorted(path.name for path in Path("/tmp").glob("neurowave-mpv-*.sock"))


def launch(home: Path, wait_window: float = 12.0) -> tuple[subprocess.Popen, str | None]:
    """Запуск GUI.

    Вывод приложения пишется в файл, а не в канал: иначе большой объём
    вывода (например, список волны) переполняет буфер канала, приложение
    блокируется на записи и перестаёт обрабатывать события окна.
    """
    log = Path("/tmp") / f"neurowave_gui_{home.name}.log"
    handle = log.open("w", encoding="utf-8")
    proc = subprocess.Popen([str(APP), "--provider", "local", "app"],
                            env=app_env(home), stdout=handle, stderr=handle,
                            text=True)
    proc._qa_log = log  # type: ignore[attr-defined]
    win = None
    deadline = time.monotonic() + wait_window
    while time.monotonic() < deadline and proc.poll() is None:
        win = window_id()
        if win:
            break
        time.sleep(0.3)
    if win:
        time.sleep(1.0)  # дать окну отрисоваться до первого клика
    return proc, win


def stop_process(proc, timeout: float = 20.0) -> dict:
    log = getattr(proc, "_qa_log", None)
    if log is not None and log.exists():
        text = log.read_text(encoding="utf-8", errors="replace")
        stdout, stderr = text, text
    else:
        stdout, stderr = "", ""
    killed = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.terminate()
        killed = True
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    return {
        "exit_code": proc.returncode,
        "killed_by_qa": killed,
        "stdout": sanitize(stdout)[-1500:],
        "stderr": sanitize(stderr)[-1500:],
        "has_traceback": "Traceback (most recent call last)" in stderr,
    }


def sink_name(value: str) -> str:
    """Имя sink'а не публикуется: PCI-адрес слота и имя устройства."""
    if not value:
        return value
    if "hdmi" in value:
        return "<SINK_HDMI>"
    if "bluez" in value:
        return "<SINK_BLUETOOTH>"
    return "<SINK>"


def default_sink() -> str:
    code, out, _ = q.run(["pactl", "get-default-sink"], timeout=15)
    return sink_name(out.strip()) if code == 0 else ""


def parse_sinks() -> dict[str, str]:
    code, out, _ = q.run(["pactl", "list", "sinks", "short"], timeout=15)
    sinks: dict[str, str] = {}
    if code != 0:
        return sinks
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            sinks[parts[1]] = parts[0]
    return sinks


def scenario_a() -> dict:
    """Повреждённый config.json + app."""
    home = HOMES / "verify_d02_gui_a"
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    config = write_config(home, BROKEN)
    before = config.read_bytes()
    started = time.monotonic()
    proc, win = launch(home, wait_window=6.0)
    finished = stop_process(proc, timeout=15)
    return {
        "scenario": "A: повреждённый config.json + app",
        "window_opened": bool(win),
        "exit_code": finished["exit_code"],
        "has_traceback": finished["has_traceback"],
        "stderr": finished["stderr"][-600:],
        "duration_s": round(time.monotonic() - started, 1),
        "hung": finished["killed_by_qa"],
        "config_unmodified": config.read_bytes() == before,
        "expectation": ("контролируемое завершение (код 2) с понятной диагностикой, "
                        "без зависшего окна, без traceback, файл конфигурации не тронут"),
        "ok": (finished["exit_code"] == 2 and not finished["has_traceback"]
               and not finished["killed_by_qa"] and config.read_bytes() == before),
    }


def scenario_c(sink_switch: dict) -> dict:
    """Исправленная конфигурация: окно, воспроизведение, закрытие."""
    home = HOMES / "verify_d02_gui_c"
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    config = write_config(home, BASE)
    before = config.read_bytes()

    proc, win = launch(home, wait_window=15.0)
    steps: dict = {}
    result: dict = {
        "scenario": "C: исправленная конфигурация, воспроизведение в GUI",
        "window_opened": bool(win),
        "config_unmodified": config.read_bytes() == before,
        "steps": steps,
    }
    if win:
        activate(win)
        buttons: dict = {}
        steps["capture_start"] = capture(win, "verify_d02_gui_c_start.png")

        steps["click_wave"] = click_button(win, "wave", buttons, steps)
        time.sleep(3.0)
        steps["state_after_wave"] = mpv_state()
        steps["capture_playing"] = capture(win, "verify_d02_gui_c_playing.png")

        steps["click_pause"] = click_button(win, "play_pause", buttons, steps)
        time.sleep(1.5)
        steps["state_after_pause"] = mpv_state()
        steps["capture_paused"] = capture(win, "verify_d02_gui_c_paused.png")

        steps["click_play"] = click_button(win, "play_pause", buttons, steps)
        time.sleep(1.5)
        steps["state_after_resume"] = mpv_state()

        before_next = steps["state_after_resume"].get("filename")
        steps["click_next"] = click_button(win, "next", buttons, steps)
        time.sleep(2.5)
        steps["state_after_next"] = mpv_state()
        steps["track_changed"] = (steps["state_after_next"].get("filename") != before_next)

        # Громкость: клик по ползунку в двух положениях; положение берётся
        # из снимка окна (границы ползунка), так как ширина окна меняется.
        track = detect_buttons(win).get("volume_track") or {}
        slider_before = mpv_state().get("volume")
        steps["volume_track"] = track
        handle_x = track.get("handle_x")
        if handle_x is not None:
            # Перетаскивание бегунка: клик по дорожке сам по себе не
            # меняет значение (стиль QSlider), работает именно перетаскивание.
            steps["drag_volume_right"] = drag(win, handle_x, track["y"],
                                              handle_x + 120, track["y"])
            time.sleep(1.0)
            steps["state_after_volume_right"] = mpv_state()
            if (steps["state_after_volume_right"].get("volume") == slider_before):
                handle_x = detect_buttons(win).get("volume_track", {}).get("handle_x", handle_x)
                steps["drag_volume_left"] = drag(win, handle_x, track["y"],
                                                 handle_x - 120, track["y"])
                time.sleep(1.0)
                steps["state_after_volume_left"] = mpv_state()
        steps["state_after_volume"] = steps.get("state_after_volume_high",
                                                mpv_state())
        steps["volume_changed"] = (steps["state_after_volume"].get("volume") != slider_before)
        steps["capture_volume"] = capture(win, "verify_d02_gui_c_volume.png")

        steps["click_stop"] = click_button(win, "stop", buttons, steps)
        time.sleep(1.5)
        steps["state_after_stop"] = mpv_state()
        steps["capture_stopped"] = capture(win, "verify_d02_gui_c_stopped.png")

        code, _, _ = q.run(["wmctrl", "-i", "-c", win], timeout=20)
        steps["close_window"] = {"exit_code": code}
    finished = stop_process(proc, timeout=20)
    result["exit_code"] = finished["exit_code"]
    result["has_traceback"] = finished["has_traceback"]
    result["hung"] = finished["killed_by_qa"]
    result["stderr"] = finished["stderr"][-600:]
    result["config_unmodified_after"] = config.read_bytes() == before
    result["mpv_after"] = mpv_processes()
    result["sockets_after"] = ipc_sockets()
    result["sink_during"] = sink_switch.get("during")
    return result


def scenario_d() -> dict:
    """Последовательные запуски: повреждённый и исправленный конфиг."""
    home = HOMES / "verify_d02_gui_d"
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    runs = []
    for index in range(3):
        write_config(home, BROKEN)
        broken_proc, _ = launch(home, wait_window=5.0)
        broken = stop_process(broken_proc, timeout=15)
        write_config(home, BASE)
        fixed_proc, win = launch(home, wait_window=12.0)
        if win:
            subprocess.run(["wmctrl", "-i", "-c", win], timeout=20, check=False)
        fixed = stop_process(fixed_proc, timeout=20)
        runs.append({
            "iteration": index + 1,
            "broken": {"exit_code": broken["exit_code"],
                       "has_traceback": broken["has_traceback"],
                       "hung": broken["killed_by_qa"]},
            "fixed": {"exit_code": fixed["exit_code"],
                      "has_traceback": fixed["has_traceback"],
                      "hung": fixed["killed_by_qa"],
                      "window_opened": bool(win)},
            "profile_files": len([p for p in home.rglob("*") if p.is_file()]),
        })
        time.sleep(1.0)
    return {
        "scenario": "D: последовательные запуски (повреждённый/исправленный)",
        "runs": runs,
        "mpv_after": mpv_processes(),
        "sockets_after": ipc_sockets(),
        "ok": (all(not r["broken"]["has_traceback"] and not r["fixed"]["has_traceback"]
                   for r in runs)
               and all(r["fixed"]["window_opened"] for r in runs)),
    }


def main() -> int:
    q.ensure_dirs()
    before_mpv = mpv_processes()
    before_sockets = ipc_sockets()

    sink_before = default_sink()
    sinks = parse_sinks()
    hdmi = next((name for name in sinks if "hdmi" in name), "")
    switch: dict = {"before": sink_before, "target": "", "during": "", "restored": None}
    if hdmi:
        code, _, _ = q.run(["pactl", "set-default-sink", hdmi], timeout=15)
        time.sleep(1.0)
        switch["target"] = sink_name(hdmi)
        switch["switch_exit_code"] = code
        switch["during"] = default_sink()

    try:
        scenario_a_result = scenario_a()
        scenario_c_result = scenario_c(switch)
        scenario_d_result = scenario_d()
    finally:
        if hdmi:
            q.run(["pactl", "set-default-sink", sink_before], timeout=15)
            time.sleep(1.0)
            switch["restored"] = default_sink()

    print(f"  A: код={scenario_a_result['exit_code']} ok={scenario_a_result['ok']}", flush=True)
    steps = scenario_c_result["steps"]
    print(f"  C: окно={scenario_c_result['window_opened']} "
          f"файл={steps.get('state_after_wave', {}).get('filename')} "
          f"пауза={steps.get('state_after_pause', {}).get('pause')} "
          f"трек_сменился={steps.get('track_changed')} "
          f"громкость={steps.get('state_after_volume', {}).get('volume')}", flush=True)
    print(f"  D: ok={scenario_d_result['ok']}", flush=True)

    summary = {
        "sha": subprocess.run(["git", "rev-parse", "HEAD"], cwd=q.ROOT,
                              capture_output=True, text=True).stdout.strip(),
        "display": os.environ.get("DISPLAY", ""),
        "before": {"mpv": before_mpv, "sockets": before_sockets},
        "sink_switch": switch,
        "scenario_a": scenario_a_result,
        "scenario_c": scenario_c_result,
        "scenario_d": scenario_d_result,
        "after": {"mpv": mpv_processes(), "sockets": ipc_sockets()},
    }
    out_path = q.ARTIFACTS / "verify_d02_gui.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    print(f"→ {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
