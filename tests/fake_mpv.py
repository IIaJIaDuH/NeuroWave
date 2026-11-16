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
* ``deny-loadfile`` — ``loadfile`` отклоняется ошибкой.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading

# plan -> (задержка, причина end-file, чей id подставить)
PLANS = {
    "idle": (None, None, None),
    "eof": (0.1, "eof", "current"),
    "error": (0.1, "error", "current"),
    "eof-late": (4.0, "eof", "current"),
    "eof-stale": (0.1, "eof", "previous"),
    "deny-af": (None, None, None),
    "deny-loadfile": (None, None, None),
}


def _parse(argv: list[str]) -> tuple[str | None, str]:
    server: str | None = None
    plan = "idle"
    for arg in argv[1:]:
        if arg.startswith("--input-ipc-server="):
            server = arg.split("=", 1)[1]
        elif arg.startswith("--fake-plan="):
            plan = arg.split("=", 1)[1]
    return server, plan


def main() -> int:
    server, plan = _parse(sys.argv)
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
        "quitting": False,
        "entry": 0,
        "previous_entry": 0,
        "af": "",
    }
    send_lock = threading.Lock()

    def send(payload: dict) -> None:
        data = (json.dumps(payload) + "\n").encode("utf-8")
        with send_lock:
            try:
                conn.sendall(data)
            except OSError:
                pass

    def reply(request_id: int, data=None) -> None:
        payload: dict = {"request_id": request_id, "error": "success"}
        if data is not None:
            payload["data"] = data
        send(payload)

    def schedule_end() -> None:
        if reason is None:
            return

        def worker() -> None:
            threading.Event().wait(delay)
            entry = (
                state["entry"]
                if entry_source == "current"
                else state["previous_entry"]
            )
            send({"event": "end-file", "reason": reason, "playlist_entry_id": entry})

        threading.Thread(target=worker, daemon=True).start()

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
                    reply(request_id, 1.0 if state["loaded"] else None)
                elif prop == "idle-active":
                    reply(request_id, not state["loaded"])
                elif prop == "af":
                    reply(request_id, [state["af"]] if state["af"] else [])
                elif prop == "playlist/0/id":
                    if state["entry"]:
                        reply(request_id, state["entry"])
                    else:
                        send({"request_id": request_id, "error": "property not found"})
                else:
                    reply(request_id, None)
            elif name == "loadfile":
                if plan == "deny-loadfile":
                    send({"request_id": request_id, "error": "loading failed"})
                else:
                    state["previous_entry"] = state["entry"]
                    state["entry"] += 1
                    state["loaded"] = True
                    send(
                        {
                            "request_id": request_id,
                            "error": "success",
                            "data": {"playlist_entry_id": state["entry"]},
                        }
                    )
                    schedule_end()
            elif name == "af":
                if plan == "deny-af":
                    send({"request_id": request_id, "error": "error running command"})
                else:
                    state["af"] = str(command[2]) if len(command) > 2 else ""
                    reply(request_id)
            elif name == "stop":
                state["loaded"] = False
                reply(request_id)
            elif name == "quit":
                state["quitting"] = True
                reply(request_id)
            elif name in {"set_property", "seek"}:
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
