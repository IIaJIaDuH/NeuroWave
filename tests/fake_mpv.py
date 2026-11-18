#!/usr/bin/env python3
"""Мини-двойник mpv для тестов: говорит на JSON IPC, но не играет звук.

Зачем: реальный ``MpvBackend`` нельзя проверить через ``NullBackend`` — все
баги, найденные живыми прогонами (мёртвый читатель после 3 с тишины и
дедлок в обработчике ``end-file``), живут именно в этом слое.

Поведение задаётся аргументом ``--fake-plan=…``:

* ``idle``      — просто отвечает на команды, событий не шлёт;
* ``eof``       — после ``loadfile`` через 0.1 с приходит ``end-file``/eof;
* ``error``     — то же, но с причиной ``error``;
* ``eof-late``  — ``end-file``/eof приходит через 4 с (дольше таймаута команд);
* ``eof-stale`` — ``end-file`` приходит с id *предыдущей* загрузки;
* ``deny-af``   — команда ``af`` отклоняется ошибкой;
* ``deny-loadfile`` — ``loadfile`` отклоняется ошибкой;
* ``slow-load`` — ``file-loaded`` приходит через 0.35 с, а ``seek`` до этого
  отклоняется, как у настоящего mpv (дефект D-01);
* ``no-file-loaded`` — ``loadfile`` принят, но ``file-loaded`` не приходит;
* ``load-error`` — ``loadfile`` принят, затем приходит ``end-file``/error.

``--fake-log=…`` дописывает в файл полученные команды (``loadfile``, ``seek``,
``stop``): по нему тесты видят, какие перемотки действительно дошли до mpv.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading

# plan -> (задержка end-file, причина, чей id подставить)
PLANS = {
    "idle": (None, None, None),
    "eof": (0.1, "eof", "current"),
    "error": (0.1, "error", "current"),
    "eof-late": (4.0, "eof", "current"),
    "eof-stale": (0.1, "eof", "previous"),
    "deny-af": (None, None, None),
    "deny-af-graph": (None, None, None),
    "deny-device": (None, None, None),
    "deny-loadfile": (None, None, None),
    "slow-load": (None, None, None),
    "no-file-loaded": (None, None, None),
    "load-error": (0.1, "error", "current"),
}

SLOW_LOAD_DELAY = 0.35


def _parse(argv: list[str]) -> tuple[str | None, str, str | None]:
    server: str | None = None
    plan = "idle"
    log: str | None = None
    for arg in argv[1:]:
        if arg.startswith("--input-ipc-server="):
            server = arg.split("=", 1)[1]
        elif arg.startswith("--fake-plan="):
            plan = arg.split("=", 1)[1]
        elif arg.startswith("--fake-log="):
            log = arg.split("=", 1)[1]
    return server, plan, log


def main() -> int:
    server, plan, log_path = _parse(sys.argv)
    if not server:
        return 2
    delay, reason, entry_source = PLANS.get(plan, (None, None, None))

    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        os.unlink(server)
    except OSError:
        pass
    listener.bind(server)
    listener.listen(1)
    conn, _ = listener.accept()

    state = {
        "loaded": False,
        "ready": False,
        "position": 0.0,
        "quitting": False,
        "entry": 0,
        "previous_entry": 0,
        "af": "",
        "audio-device": "auto",
        "audio-device-list": [
            {"name": "auto", "description": "Autoselect device"},
            {"name": "pulse/fake-sink", "description": "Fake Sink"},
        ],
    }
    send_lock = threading.Lock()

    def send(payload: dict) -> None:
        data = (json.dumps(payload) + "\n").encode("utf-8")
        with send_lock:
            try:
                conn.sendall(data)
            except OSError:
                pass

    def log(text: str) -> None:
        if not log_path:
            return
        with send_lock:
            with open(log_path, "a", encoding="utf-8") as handle:
                handle.write(text + "\n")

    def reply(request_id: int, data=None) -> None:
        payload: dict = {"request_id": request_id, "error": "success"}
        if data is not None:
            payload["data"] = data
        send(payload)

    def schedule(delay_s: float, fn) -> None:
        def worker() -> None:
            threading.Event().wait(delay_s)
            fn()

        threading.Thread(target=worker, daemon=True).start()

    def schedule_end() -> None:
        if reason is None:
            return
        entry = state["entry"] if entry_source == "current" else state["previous_entry"]

        def finish() -> None:
            state["ready"] = False
            state["loaded"] = False
            state["position"] = 0.0
            send({"event": "end-file", "reason": reason, "playlist_entry_id": entry})

        schedule(delay, finish)

    buffer = b""
    while not state["quitting"]:
        try:
            chunk = conn.recv(65536)
        except OSError:
            break
        if not chunk:
            break
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            if not line.strip():
                continue
            message = json.loads(line.decode("utf-8", "replace"))
            request_id = message.get("request_id")
            command = message.get("command") or []
            name = command[0] if command else ""
            if name == "get_property":
                prop = command[1]
                if prop == "pause":
                    reply(request_id, False)
                elif prop == "time-pos":
                    reply(request_id, state["position"] if state["ready"] else None)
                elif prop == "idle-active":
                    reply(request_id, not state["loaded"])
                elif prop == "af":
                    reply(request_id, [state["af"]] if state["af"] else [])
                elif prop == "audio-device":
                    reply(request_id, state["audio-device"])
                elif prop == "audio-device-list":
                    reply(request_id, state["audio-device-list"])
                elif prop == "playlist/0/id":
                    if state["entry"]:
                        reply(request_id, state["entry"])
                    else:
                        send({"request_id": request_id, "error": "property not found"})
                else:
                    reply(request_id, None)
            elif name == "loadfile":
                url = str(command[1]) if len(command) > 1 else ""
                log(f"loadfile {url}")
                if plan == "deny-loadfile":
                    send({"request_id": request_id, "error": "loading failed"})
                else:
                    state["previous_entry"] = state["entry"]
                    state["entry"] += 1
                    state["loaded"] = True
                    state["ready"] = False
                    state["position"] = 0.0
                    entry = state["entry"]
                    send(
                        {
                            "request_id": request_id,
                            "error": "success",
                            "data": {"playlist_entry_id": entry},
                        }
                    )
                    # У настоящего mpv start-file несёт id загрузки, а
                    # file-loaded — нет: двойник повторяет именно это.
                    send({"event": "start-file", "playlist_entry_id": entry})
                    if plan == "load-error":
                        schedule_end()
                    elif plan == "no-file-loaded":
                        pass
                    elif plan == "slow-load":

                        # entry — в аргументе по умолчанию: иначе замыкание
                        # увидит id следующей загрузки и пришлёт чужой
                        # file-loaded.
                        def loaded(current_entry: int = entry) -> None:
                            # Заменённая или остановленная загрузка не
                            # присылает file-loaded и у настоящего mpv.
                            if not state["loaded"] or state["entry"] != current_entry:
                                return
                            state["ready"] = True
                            state["position"] = 0.0
                            send({"event": "file-loaded"})

                        schedule(SLOW_LOAD_DELAY, loaded)
                    else:
                        state["ready"] = True
                        send({"event": "file-loaded"})
                        schedule_end()
            elif name == "af":
                raw = str(command[2]) if len(command) > 2 else ""
                denied = plan == "deny-af" or (plan == "deny-af-graph" and raw)
                if denied:
                    send({"request_id": request_id, "error": "error running command"})
                else:
                    # Настоящий mpv хранит внутреннюю часть графа без lavfi=[…].
                    if raw.startswith("lavfi=[") and raw.endswith("]"):
                        raw = raw[len("lavfi=[") : -1]
                    state["af"] = raw
                    reply(request_id)
            elif name == "stop":
                log("stop")
                state["loaded"] = False
                state["ready"] = False
                state["position"] = 0.0
                reply(request_id)
            elif name == "quit":
                state["quitting"] = True
                reply(request_id)
            elif name == "set_property":
                if len(command) >= 3 and command[1] == "audio-device":
                    if plan == "deny-device":
                        send({"request_id": request_id, "error": "error changing device"})
                    else:
                        state["audio-device"] = str(command[2])
                        reply(request_id)
                else:
                    reply(request_id)
            elif name == "seek":
                position = float(command[1]) if len(command) > 1 else 0.0
                log(f"seek {position}")
                if not state["ready"]:
                    # Настоящий mpv отвечает так, пока файл не загружен (D-01).
                    send({"request_id": request_id, "error": "error running command"})
                else:
                    state["position"] = position
                    reply(request_id)
            else:
                reply(request_id)

    try:
        conn.close()
    except OSError:
        pass
    listener.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
