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
import re
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
# Предел ожидания готовности файла для отложенной перемотки. Нужен только как
# страховка: обычно готовность приходит событием file-loaded за миллисекунды.
SEEK_READY_TIMEOUT = 5.0

SOCKET_NAME = "neurowave-mpv-{pid}.sock"
_SOCKET_RE = re.compile(r"^neurowave-mpv-(?P<pid>\d+)\.sock$")


def cleanup_stale_sockets(directory: Path | None = None) -> int:
    """Удалить сокеты завершённых процессов NeuroWave.

    mpv не убирает сокет, если его убили (SIGKILL, падение сессии), поэтому
    /tmp постепенно заполняется `neurowave-mpv-<pid>.sock`. Чужие живые
    сокеты не трогаем: проверяем, существует ли процесс с таким pid.
    """
    root = Path(directory or tempfile.gettempdir())
    removed = 0
    try:
        entries = list(root.glob("neurowave-mpv-*.sock"))
    except OSError:
        return 0
    for path in entries:
        match = _SOCKET_RE.match(path.name)
        if match is None:
            continue
        pid = int(match.group("pid"))
        if pid == os.getpid():
            continue  # свой сокет: им управляет текущая сессия
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        except PermissionError:
            continue  # процесс существует, но чужой
    return removed


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
        self._events: queue.Queue[Callable[[], None] | None] = queue.Queue()
        self._write_lock = threading.Lock()
        self._pending: dict[int, tuple[threading.Event, dict[str, Any]]] = {}
        self._request_id = 0
        self._closing = False
        # Идентификатор текущей загрузки: по нему отсеиваем устаревшие end-file.
        self._entry_id: int | None = None
        # Готовность файла. mpv отклоняет `seek`, пока файл не загружен, поэтому
        # перемотка в этот промежуток откладывается до события file-loaded.
        self._state_lock = threading.Lock()
        self._started_entry: int | None = None  # id из события start-file
        self._loading = False  # loadfile отправлен, file-loaded ещё не пришёл
        self._has_file = False  # в mpv есть загруженный файл
        # Поколение загрузки: отложенная перемотка и таймер привязаны к нему,
        # поэтому запоздавший callback не достанет seek уже другого трека.
        self._load_gen = 0
        self._pending_seek: tuple[int, float] | None = None  # (поколение, мс)
        self._seek_timer: threading.Timer | None = None
        # Порядок «проверка поколения → отправка seek» и переходы load/stop.
        # Блокировка не нужна читателю IPC: он берёт только _state_lock.
        self._order_lock = threading.RLock()
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
        cleanup_stale_sockets(self._socket_path.parent)
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
        # Без _order_lock: иначе обработчик end-file, ждавший замка, после
        # завершения мог бы заново запустить mpv. Отложенная перемотка и так
        # отменяется сменой поколения в _cancel_load().
        self._closing = True
        self._cancel_load()
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
        cleanup_stale_sockets(self._socket_path.parent)

    # --- команды ------------------------------------------------------
    def load(self, url: str, start_ms: int = 0) -> None:
        self.start()
        with self._order_lock:
            self._begin_load()
            try:
                reply = self._command("loadfile", url, "replace")
            except PlayerError:
                # Файл не загрузился: ждать больше нечего, отложенная перемотка
                # не должна переехать на следующий трек.
                self._cancel_load()
                raise
            # mpv возвращает id загрузки: без него нельзя отличить свой end-file
            # от конца предыдущего трека после ручного next.
            with self._state_lock:
                self._entry_id = _entry_id_of(reply)
                self._has_file = True
            if start_ms > 0:
                self.seek(start_ms)

    def play(self) -> None:
        self._set_property("pause", False)

    def pause(self) -> None:
        self._set_property("pause", True)

    def stop(self) -> None:
        with self._order_lock:
            if self.is_running:
                self._command("stop")
            with self._state_lock:
                self._entry_id = None
            self._cancel_load()

    def set_volume(self, volume: int) -> None:
        self._set_property("volume", _volume_value(volume))

    def seek(self, position_ms: int) -> None:
        """Перемотать текущий файл.

        mpv отклоняет `seek`, пока файл ещё загружается («error running
        command» в QA D-01). Поэтому во время загрузки команда запоминается и
        применяется после file-loaded: последняя перемотка важнее предыдущих,
        а stop/shutdown/новая загрузка отменяют её. Если файла нет вовсе
        (stop), перематывать нечего и запрос игнорируется.
        """
        if isinstance(position_ms, bool) or not isinstance(position_ms, (int, float)):
            raise PlayerError(f"Некорректная позиция: {position_ms!r}")
        if not math.isfinite(float(position_ms)):
            raise PlayerError(f"Некорректная позиция: {position_ms!r}")
        position = max(0.0, float(position_ms))
        # Проверка и отправка — под одним замком с load()/stop(): иначе между
        # ними успевает начаться другая загрузка, и seek уходит в чужой файл.
        with self._order_lock:
            if self._defer_seek(position):
                return
            with self._state_lock:
                has_file = self._has_file
            if not has_file:
                return  # после stop или неудачной загрузки перематывать нечего
            self._command("seek", position / 1000.0, "absolute")

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

    # --- готовность файла ---------------------------------------------
    def _begin_load(self) -> None:
        """Новая загрузка: перемотка прошлого трека не должна достаться новому."""
        with self._state_lock:
            self._load_gen += 1
            self._loading = True
            self._started_entry = None
            self._has_file = False
            self._pending_seek = None
            self._cancel_seek_timer_locked()

    def _cancel_load(self) -> None:
        """Забыть ожидание загрузки и отложенную перемотку."""
        with self._state_lock:
            self._load_gen += 1
            self._loading = False
            self._started_entry = None
            self._has_file = False
            self._pending_seek = None
            self._cancel_seek_timer_locked()

    def _defer_seek(self, position_ms: float) -> bool:
        """Запомнить перемотку до готовности файла, если он ещё грузится."""
        with self._state_lock:
            if not self._loading:
                return False
            generation = self._load_gen
            self._pending_seek = (generation, position_ms)  # выигрывает последний
            if self._seek_timer is None:
                timer = threading.Timer(
                    SEEK_READY_TIMEOUT, self._expire_pending_seek, args=(generation,)
                )
                timer.daemon = True
                self._seek_timer = timer
                timer.start()
            return True

    def _expire_pending_seek(self, generation: int) -> None:
        """Страховка: не ждать file-loaded бесконечно.

        Callback помнит своё поколение: если успела начаться другая загрузка,
        он ничего не делает и не трогает чужую отложенную перемотку.
        """
        self._apply_pending_seek(None, generation, force=True)

    def _apply_pending_seek(
        self, token: int | None, generation: int, *, force: bool = False
    ) -> None:
        """Отправить отложенную перемотку, если она всё ещё про текущий файл.

        Проверка поколения и отправка идут под одним замком с load()/stop(),
        поэтому seek не может уехать в файл, загруженный после проверки.
        """
        with self._order_lock:
            with self._state_lock:
                if generation != self._load_gen:
                    return  # загрузка уже другая: callback устарел
                pending = self._pending_seek
                if pending is None or pending[0] != generation:
                    return
                if not force:
                    if self._loading:
                        return
                    if token is not None and self._started_entry != token:
                        return  # файл уже сменился: чужой seek не отправляем
                position = pending[1]
                self._pending_seek = None
                timer, self._seek_timer = self._seek_timer, None
            if timer is not None:
                timer.cancel()
            try:
                self._command("seek", position / 1000.0, "absolute")
            except PlayerError:
                # Файл сменился или mpv закрыт: отложенная перемотка не должна
                # ронять поток событий.
                pass

    def _cancel_seek_timer_locked(self) -> None:
        timer, self._seek_timer = self._seek_timer, None
        if timer is not None:
            timer.cancel()

    def _handle_start_file(self, message: dict[str, Any]) -> None:
        """Запомнить, какая загрузка идёт: по ней проверяем file-loaded.

        Событие file-loaded не содержит playlist_entry_id (в отличие от
        start-file и end-file), поэтому единственный признак «свой/чужой» —
        id последнего start-file. mpv присылает file-loaded только для
        текущей загрузки; прерванная загрузка его не получает.
        """
        incoming = message.get("playlist_entry_id")
        if not isinstance(incoming, int) or isinstance(incoming, bool):
            return
        with self._state_lock:
            if not self._loading:
                return
            self._started_entry = incoming

    def _handle_file_loaded(self, _message: dict[str, Any]) -> None:
        """Файл готов принимать seek — применить последнюю отложенную перемотку."""
        with self._state_lock:
            if not self._loading:
                return
            if self._started_entry is None and self._entry_id is not None:
                # Новая загрузка ещё не прислала start-file, а id reply уже
                # обновлён — значит это file-loaded предыдущего файла.
                return
            self._loading = False
            token = self._started_entry
            generation = self._load_gen
        self._events.put(lambda: self._apply_pending_seek(token, generation))

    def _handle_end_file(self, message: dict[str, Any]) -> None:
        reason = str(message.get("reason", "eof"))
        if not self._is_current_entry(message):
            return  # конец предыдущего файла: очередь двигать нельзя
        # Файла больше нет: отложенная перемотка относилась к ушедшему треку.
        with self._state_lock:
            self._loading = False
            self._started_entry = None
            self._has_file = False
            self._pending_seek = None
            self._cancel_seek_timer_locked()
        handler: Callable[[str], None] | None = self.on_end
        if handler is not None and reason in {"eof", "error"}:
            self._events.put(lambda: handler(reason))

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
            try:
                item()
            except Exception:
                pass

    def _handle_message(self, message: dict[str, Any]) -> None:
        if "event" in message:
            event = message["event"]
            if event == "start-file":
                self._handle_start_file(message)
            elif event == "file-loaded":
                self._handle_file_loaded(message)
            elif event == "end-file":
                self._handle_end_file(message)
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
