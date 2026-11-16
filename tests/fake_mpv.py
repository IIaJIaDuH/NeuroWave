#!/usr/bin/env python3
"""Мини-двойник mpv для тестов: говорит на JSON IPC, но не играет звук.

Зачем: реальный ``MpvBackend`` нельзя проверить через ``NullBackend`` — все
баги, найденные живыми прогонами (мёртвый читатель после 3 с тишины и
дедлок в обработчике ``end-file``), живут именно в этом слое.

Поведение задаётся аргументом ``--fake-plan=…``:

* ``idle``    — просто отвечает на команды, событий не шлёт;
* ``eof``     — после ``loadfile`` через 0.1 с приходит ``end-file``/eof;
* ``error``   — то же, но с причиной ``error``;
* ``eof-late``— ``end-file``/eof приходит через 4 с (дольше таймаута команд).
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading

PLANS = {
    "idle": (None, None),
    "eof": (0.1, "eof"),
    "error": (0.1, "error"),
    "eof-late": (4.0, "eof"),
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
    delay, reason = PLANS.get(plan, (None, None))

    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        os.unlink(server)
    except OSError:
        pass
    listener.bind(server)
    listener.listen(1)
    conn, _ = listener.accept()

    state = {"loaded": False, "quitting": False}
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
            send({"event": "end-file", "reason": reason})

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
                else:
                    reply(request_id, None)
            elif name == "loadfile":
                state["loaded"] = True
                reply(request_id)
                schedule_end()
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
