"""Аудиовыход через mpv (JSON IPC).

mpv декодирует поток и отдаёт звук в PipeWire/PulseAudio, приложение лишь
управляет воспроизведением. Аудиопоток не зависит от сети, UI и ИИ.
"""

from __future__ import annotations

import json
import math
import os
import queue
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..config import MAX_VOLUME
from ..errors import PlayerError
from .backend import EndHandler

CONNECT_TIMEOUT = 5.0
COMMAND_TIMEOUT = 3.0


def _volume_value(volume: Any) -> int:
    """Проверить громкость до отправки в mpv.

    NaN/inf или строка вместо числа вешают/ломают mpv: на проверке `volume
    NaN` ответ не пришёл вообще. Поэтому валидация обязательна и до IPC.
    """
    if isinstance(volume, bool) or not isinstance(volume, (int, float)):
        raise PlayerError(f"Некорректная громкость: {volume!r}")
    if not math.isfinite(float(volume)):
        raise PlayerError(f"Некорректная громкость: {volume!r}")
    return max(0, min(MAX_VOLUME, int(round(float(volume)))))


def _entry_id_of(reply: dict[str, Any] | None) -> int | None:
    """Идентификатор загрузки, который mpv вернул на `loadfile`."""
    data = (reply or {}).get("data")
    if isinstance(data, dict):
        value = data.get("playlist_entry_id")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _filter_text(value: Any) -> str | None:
    """Свойство `af` → строка графа.

    Форма ответа mpv не фиксирована: в 0.42 при одном фильтре приходит объект
    ``{"name": "lavfi", "params": {"graph": "…"}}``, при нескольких — массив.
    Пустое значение — ``[]``.
    """
    if not value:
        return None
    items = value if isinstance(value, list) else [value]
    parts: list[str] = []
    for item in items:
        if isinstance(item, dict):
            params = item.get("params")
            graph = params.get("graph") if isinstance(params, dict) else None
            text = graph if graph is not None else item.get("name")
            if text:
                parts.append(str(text))
        elif str(item).strip():
            parts.append(str(item))
    return ",".join(parts) or None


class MpvBackend:
    def __init__(
        self,
        on_end: EndHandler | None = None,
        binary: str = "mpv",
        socket_path: Path | str | None = None,
        extra_args: list[str] | None = None,
        start_timeout: float = CONNECT_TIMEOUT,
    ):
        self.on_end = on_end
        self._binary = binary
        self._socket_path = Path(
            socket_path
            or Path(tempfile.gettempdir()) / f"neurowave-mpv-{os.getpid()}.sock"
        )
        self._extra_args = list(extra_args or [])
        self._start_timeout = start_timeout
        self._process: subprocess.Popen[bytes] | None = None
        self._socket: socket.socket | None = None
        self._reader: threading.Thread | None = None
        self._event_thread: threading.Thread | None = None
        # Обработчики завершения трека не должны исполняться в потоке
        # чтения IPC: изнутри них посылаются новые команды и нужен ответ.
        self._events: queue.Queue[tuple[Callable[[str], None], str] | None] = queue.Queue()
        self._write_lock = threading.Lock()
        self._pending: dict[int, tuple[threading.Event, dict[str, Any]]] = {}
        self._request_id = 0
        self._closing = False
        # Идентификатор текущей загрузки: по нему отсеиваем устаревшие end-file.
        self._entry_id: int | None = None
        self.last_error: str | None = None

    # --- жизненный цикл ----------------------------------------------
    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self) -> None:
        if self.is_running:
            return
        if shutil.which(self._binary) is None:
            raise PlayerError(
                f"mpv не найден ({self._binary}). Установите mpv: sudo apt install mpv"
            )
        try:
            self._socket_path.unlink()
        except OSError:
            pass
        args = [
            self._binary,
            "--no-video",
            "--idle=yes",
            "--no-terminal",
            "--really-quiet",
            "--cache=yes",
            f"--input-ipc-server={self._socket_path}",
            *self._extra_args,
        ]
        self._process = subprocess.Popen(  # noqa: S603
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + self._start_timeout
        while time.monotonic() < deadline:
            if self._socket_path.exists():
                try:
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    sock.settimeout(COMMAND_TIMEOUT)
                    sock.connect(str(self._socket_path))
                    # Чтение — блокирующее: mpv молчит между треками, и тишина
                    # в сокете не означает обрыв. Таймаут команд считает
                    # _command через Event.wait.
                    sock.settimeout(None)
                    self._socket = sock
                    break
                except OSError:
                    sock.close()
            if self._process.poll() is not None:
                raise PlayerError("mpv завершился сразу после запуска")
            time.sleep(0.05)
        if self._socket is None:
            self.shutdown()
            raise PlayerError("Не удалось подключиться к mpv IPC")
        self._closing = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._event_thread = threading.Thread(target=self._dispatch_loop, daemon=True)
        self._event_thread.start()

    def shutdown(self) -> None:
        self._closing = True
        try:
            if self._socket is not None:
                self._send({"command": ["quit"]})
        except Exception:
            pass
        for sock in (self._socket,):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        self._socket = None
        if self._process is not None:
            try:
                self._process.terminate()
                self._process.wait(timeout=3)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
        self._process = None
        if self._reader is not None and self._reader.is_alive():
            self._reader.join(timeout=1)
        self._reader = None
        self._events.put(None)
        if self._event_thread is not None and self._event_thread.is_alive():
            self._event_thread.join(timeout=1)
        self._event_thread = None
        try:
            self._socket_path.unlink()
        except OSError:
            pass

    # --- команды ------------------------------------------------------
    def load(self, url: str, start_ms: int = 0) -> None:
        self.start()
        reply = self._command("loadfile", url, "replace")
        # mpv возвращает id загрузки: без него нельзя отличить свой end-file
        # от конца предыдущего трека после ручного next.
        self._entry_id = _entry_id_of(reply)
        if start_ms > 0:
            self.seek(start_ms)

    def play(self) -> None:
        self._set_property("pause", False)

    def pause(self) -> None:
        self._set_property("pause", True)

    def stop(self) -> None:
        if self.is_running:
            self._command("stop")
        self._entry_id = None

    def set_volume(self, volume: int) -> None:
        self._set_property("volume", _volume_value(volume))

    def seek(self, position_ms: int) -> None:
        if isinstance(position_ms, bool) or not isinstance(position_ms, (int, float)):
            raise PlayerError(f"Некорректная позиция: {position_ms!r}")
        if not math.isfinite(float(position_ms)):
            raise PlayerError(f"Некорректная позиция: {position_ms!r}")
        self._command("seek", max(0.0, float(position_ms) / 1000.0), "absolute")

    def audio_devices(self) -> list[dict[str, Any]]:
        """Все выходы, которые видит mpv (`audio-device-list`)."""
        value = self._get_property("audio-device-list")
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)]

    def audio_device(self) -> str | None:
        value = self._get_property("audio-device")
        return str(value) if value else None

    def set_audio_device(self, device_id: str) -> None:
        """Переключить выход.

        mpv принимает даже несуществующий id без ошибки (проверено на 0.42),
        поэтому вызывающий обязан сверить id со `audio-devices()` до вызова.
        """
        if not isinstance(device_id, str) or not device_id.strip():
            raise PlayerError(f"Некорректный аудиовыход: {device_id!r}")
        self._set_property("audio-device", device_id.strip())

    def set_audio_filter(self, graph: str) -> None:
        """Применить граф фильтров. Отказ mpv — это ошибка, а не тихий no-op."""
        if not isinstance(graph, str):
            raise PlayerError("Граф аудиофильтров должен быть строкой")
        self._command("af", "set", graph.strip())

    def audio_filter(self) -> str | None:
        """Фактический граф фильтров по данным mpv (None — пусто)."""
        return _filter_text(self._get_property("af"))

    def snapshot_state(self) -> dict[str, Any]:
        """Срез фактического состояния mpv: для диагностики и логов."""
        if not self.is_running:
            return {"running": False, "entry_id": self._entry_id, "last_error": self.last_error}
        return {
            "running": True,
            "idle": self._get_property("idle-active"),
            "time_pos": self._get_property("time-pos"),
            "pause": self._get_property("pause"),
            "volume": self._get_property("volume"),
            "audio_device": self._get_property("audio-device"),
            "audio_params": self._get_property("audio-out-params"),
            "af": self.audio_filter(),
            "entry_id": self._entry_id,
            "last_error": self.last_error,
        }

    def position_ms(self) -> int:
        value = self._get_property("time-pos")
        try:
            return int(float(value) * 1000)
        except (TypeError, ValueError):
            return 0

    def is_playing(self) -> bool:
        if not self.is_running:
            return False
        if self._get_property("pause") is not False:
            return False
        # В idle time-pos равен None: файла нет, и играть нечего.
        return self._get_property("time-pos") is not None

    # --- IPC ----------------------------------------------------------
    def _is_current_entry(self, message: dict[str, Any]) -> bool:
        """Относится ли событие к текущей загрузке."""
        incoming = message.get("playlist_entry_id")
        if incoming is None or self._entry_id is None:
            return True  # нет данных для сравнения — событие не отбрасываем
        return incoming == self._entry_id

    def _set_property(self, name: str, value: Any, optional: bool = False) -> None:
        self._command("set_property", name, value, optional=optional)

    def _get_property(self, name: str) -> Any:
        # Отсутствующее свойство — штатная ситуация (например time-pos в idle).
        reply = self._command("get_property", name, optional=True)
        if not reply or reply.get("error") != "success":
            return None
        return reply.get("data")

    def _command(self, *args: Any, optional: bool = False) -> dict[str, Any] | None:
        self.start() if self._socket is None and not self._closing else None
        if self._socket is None:
            raise PlayerError("mpv IPC недоступен")
        request_id = self._send({"command": list(args)})
        event, box = self._pending[request_id]
        if not event.wait(COMMAND_TIMEOUT):
            self._pending.pop(request_id, None)
            self.last_error = f"timeout: {args[0]}"
            raise PlayerError(f"mpv не ответил на команду {args[0]}")
        self._pending.pop(request_id, None)
        reply = box.get("reply")
        if not optional:
            self._check_reply(args, reply)
        return reply

    def _check_reply(self, args: tuple[Any, ...], reply: dict[str, Any] | None) -> None:
        """Проверить ответ mpv: «успешно отправлено» ≠ «применено»."""
        if reply is None:
            self.last_error = f"empty reply: {args[0]}"
            raise PlayerError(f"mpv не ответил на команду {args[0]}")
        error = reply.get("error")
        if error == "success":
            self.last_error = None
            return
        self.last_error = str(error)
        raise PlayerError(f"mpv отклонил {args[0]}: {error}")

    def _send(self, payload: dict[str, Any]) -> int:
        with self._write_lock:
            self._request_id += 1
            request_id = self._request_id
            payload = dict(payload, request_id=request_id)
            if payload.get("command") != ["quit"]:
                self._pending[request_id] = (threading.Event(), {})
            assert self._socket is not None
            data = (json.dumps(payload) + "\n").encode("utf-8")
            try:
                self._socket.sendall(data)
            except OSError as exc:
                self._pending.pop(request_id, None)
                raise PlayerError(f"Связь с mpv потеряна: {exc}") from exc
        return request_id

    def _read_loop(self) -> None:
        buffer = b""
        sock = self._socket
        if sock is None:
            return
        while not self._closing:
            try:
                chunk = sock.recv(65536)
            except (OSError, socket.timeout):
                break
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    message = json.loads(line.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    continue
                self._handle_message(message)

    def _dispatch_loop(self) -> None:
        """Отдавать события трека отдельному потоку, а не читателю IPC."""
        while True:
            item = self._events.get()
            if item is None:
                return
            handler, reason = item
            try:
                handler(reason)
            except Exception:
                pass

    def _handle_message(self, message: dict[str, Any]) -> None:
        if "event" in message:
            if message["event"] == "end-file":
                reason = str(message.get("reason", "eof"))
                if not self._is_current_entry(message):
                    return  # конец предыдущего файла: очередь двигать нельзя
                handler: Callable[[str], None] | None = self.on_end
                if handler is not None and reason in {"eof", "error"}:
                    self._events.put((handler, reason))
            return
        request_id = message.get("request_id")
        if request_id is None:
            return
        pending = self._pending.get(request_id)
        if pending is None:
            return
        event, box = pending
        box["reply"] = message
        event.set()
