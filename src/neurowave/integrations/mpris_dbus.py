"""D-Bus-обвязка MPRIS на dbus-next.

Зависимость `dbus-next` импортируется лениво: без неё (или без сессионного
демона шины) модуль импортируется и :meth:`MprisService.start` просто
возвращает ``False`` с понятной причиной. Это позволяет запускать ядро, CLI и
тесты на машинах без D-Bus.

Сервис крутит собственный asyncio-цикл в фоновом потоке: ядро синхронное и
потоковое, переезд на asyncio ему не нужен.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable

from .mpris import (
    BUS_NAME,
    IDENTITY,
    OBJECT_PATH,
    MprisController,
    metadata,
    properties,
)

# Ключи Metadata → сигнатуры D-Bus (для a{sv}).
_METADATA_SIGNATURES = {
    "mpris:trackid": "o",
    "mpris:length": "x",
    "xesam:title": "s",
    "xesam:artist": "as",
    "xesam:album": "s",
    "neurowave:quality": "s",
    "neurowave:lossless": "b",
}

POLL_INTERVAL = 0.5


def dbus_available() -> bool:
    """Установлен ли `dbus-next` (без обращения к шине)."""
    try:
        import dbus_next  # noqa: F401
    except Exception:
        return False
    return True


def _variant_metadata(data: dict[str, Any]) -> dict[str, Any]:
    from dbus_next import Variant

    result: dict[str, Any] = {}
    for key, value in data.items():
        signature = _METADATA_SIGNATURES.get(key)
        if signature is None:
            continue
        result[key] = Variant(signature, value)
    return result


def _build_interfaces(controller: MprisController) -> tuple[Any, Any]:
    """Собрать два интерфейса MPRIS. Импорт dbus-next происходит здесь."""
    from dbus_next.service import (
        PropertyAccess,
        ServiceInterface,
        dbus_property,
        method,
        signal,
    )

    class RootInterface(ServiceInterface):
        def __init__(self):
            super().__init__("org.mpris.MediaPlayer2")

        @dbus_property(access=PropertyAccess.READ)
        def Identity(self) -> "s":
            return IDENTITY

        @dbus_property(access=PropertyAccess.READ)
        def CanQuit(self) -> "b":
            return controller.can_quit

        @dbus_property(access=PropertyAccess.READ)
        def CanRaise(self) -> "b":
            return False

        @dbus_property(access=PropertyAccess.READ)
        def HasTrackList(self) -> "b":
            return False

        @dbus_property(access=PropertyAccess.READ)
        def DesktopEntry(self) -> "s":
            return "neurowave"

        @dbus_property(access=PropertyAccess.READ)
        def SupportedUriSchemes(self) -> "as":
            return []

        @dbus_property(access=PropertyAccess.READ)
        def SupportedMimeTypes(self) -> "as":
            return []

        @method()
        def Raise(self):
            controller.raise_player()

        @method()
        def Quit(self):
            controller.quit()

    class PlayerInterface(ServiceInterface):
        def __init__(self):
            super().__init__("org.mpris.MediaPlayer2.Player")

        # --- свойства ---------------------------------------------
        @dbus_property(access=PropertyAccess.READ)
        def PlaybackStatus(self) -> "s":
            return properties(controller.snapshot())["PlaybackStatus"]

        @dbus_property(access=PropertyAccess.READ)
        def Metadata(self) -> "a{sv}":
            return _variant_metadata(metadata(controller.snapshot()))

        @dbus_property(access=PropertyAccess.READ)
        def Position(self) -> "x":
            return int(properties(controller.snapshot())["Position"])

        @dbus_property(access=PropertyAccess.READWRITE)
        def Volume(self) -> "d":
            return float(properties(controller.snapshot())["Volume"])

        @Volume.setter
        def Volume(self, value: "d") -> "d":
            return float(controller.set_volume(float(value)))

        @dbus_property(access=PropertyAccess.READ)
        def Rate(self) -> "d":
            return 1.0

        @dbus_property(access=PropertyAccess.READ)
        def MinimumRate(self) -> "d":
            return 1.0

        @dbus_property(access=PropertyAccess.READ)
        def MaximumRate(self) -> "d":
            return 1.0

        @dbus_property(access=PropertyAccess.READ)
        def CanPlay(self) -> "b":
            return bool(properties(controller.snapshot())["CanPlay"])

        @dbus_property(access=PropertyAccess.READ)
        def CanPause(self) -> "b":
            return bool(properties(controller.snapshot())["CanPause"])

        @dbus_property(access=PropertyAccess.READ)
        def CanGoNext(self) -> "b":
            return bool(properties(controller.snapshot())["CanGoNext"])

        @dbus_property(access=PropertyAccess.READ)
        def CanGoPrevious(self) -> "b":
            return bool(properties(controller.snapshot())["CanGoPrevious"])

        @dbus_property(access=PropertyAccess.READ)
        def CanSeek(self) -> "b":
            return bool(properties(controller.snapshot())["CanSeek"])

        @dbus_property(access=PropertyAccess.READ)
        def CanControl(self) -> "b":
            return True

        # --- методы -----------------------------------------------
        @method()
        def Play(self):
            controller.play()

        @method()
        def Pause(self):
            controller.pause()

        @method()
        def PlayPause(self):
            controller.play_pause()

        @method()
        def Stop(self):
            controller.stop()

        @method()
        def Next(self):
            controller.next()

        @method()
        def Previous(self):
            controller.previous()

        @method()
        def Seek(self, offset: "x"):
            position = controller.seek(int(offset))
            if position is not None:
                self.Seeked(int(position))

        @method()
        def SetPosition(self, track_id: "o", position: "x"):
            controller.set_position(str(track_id), int(position))

        @method()
        def OpenUri(self, uri: "s"):
            # Открытие внешних URI не поддерживается: очередь строится только
            # из реальных объектов сервиса.
            return None

        @signal()
        def Seeked(self, position_us: int) -> "x":
            return int(position_us)

    return RootInterface(), PlayerInterface()


class MprisService:
    """Сервис ``org.mpris.MediaPlayer2.neurowave`` в фоновом потоке."""

    def __init__(
        self,
        controller: MprisController,
        bus_name: str = BUS_NAME,
        object_path: str = OBJECT_PATH,
        connect_timeout: float = 5.0,
    ):
        self.controller = controller
        self.bus_name = bus_name
        self.object_path = object_path
        self.connect_timeout = connect_timeout
        self.error: str | None = None
        self.available = False
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._ready = threading.Event()
        self._root: Any = None
        self._player: Any = None
        self._last_properties: dict[str, Any] | None = None

    # --- жизненный цикл ----------------------------------------------
    def start(self, timeout: float | None = None) -> bool:
        """Поднять сервис. ``False`` — нет dbus-next или нет сессионной шины."""
        if self.available:
            return True
        if not dbus_available():
            self.error = (
                "dbus-next не установлен: pip install -e '.[mpris]' "
                "(или extras 'desktop')"
            )
            return False
        self._ready.clear()
        self.error = None
        self._thread = threading.Thread(
            target=self._run, name="neurowave-mpris", daemon=True
        )
        self._thread.start()
        self._ready.wait(self.connect_timeout if timeout is None else timeout)
        return self.available

    def stop(self, timeout: float = 2.0) -> None:
        loop, event = self._loop, self._stop_event
        if loop is not None and event is not None:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout)
        self._thread = None
        self.available = False

    # --- поток --------------------------------------------------------
    def _run(self) -> None:
        try:
            asyncio.run(self._serve())
        except Exception as exc:  # шина недоступна, имя занято и т. п.
            self.error = f"{type(exc).__name__}: {exc}"
            self.available = False
        finally:
            self._ready.set()

    async def _serve(self) -> None:
        from dbus_next.aio import MessageBus

        bus = await MessageBus().connect()
        reply = await bus.request_name(self.bus_name)
        if int(reply.value) not in (1, 4):  # PRIMARY_OWNER, ALREADY_OWNER
            raise RuntimeError(f"имя {self.bus_name} уже занято (reply={reply.name})")

        self._root, self._player = _build_interfaces(self.controller)
        bus.export(self.object_path, self._root)
        bus.export(self.object_path, self._player)

        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        self.available = True
        self._ready.set()

        poller = asyncio.ensure_future(self._poll())
        try:
            await self._stop_event.wait()
        finally:
            poller.cancel()
            try:
                bus.unexport(self.object_path, self._player)
                bus.unexport(self.object_path, self._root)
            except Exception:
                pass
            bus.disconnect()
            self.available = False

    async def _poll(self) -> None:
        """Раз в полсекунды сравнивать срез и слать PropertiesChanged."""
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            try:
                current = await asyncio.to_thread(
                    lambda: properties(self.controller.snapshot(force=True))
                )
            except Exception:
                continue
            previous = self._last_properties
            self._last_properties = current
            if previous is None or self._player is None:
                continue
            changed = {
                key: value
                for key, value in current.items()
                if key != "Position" and previous.get(key) != value
            }
            if not changed:
                continue
            payload = dict(changed)
            if "Metadata" in payload:
                payload["Metadata"] = _variant_metadata(payload["Metadata"])
            try:
                self._player.emit_properties_changed(payload)
            except Exception:
                continue


def start_mpris(player: Any, quit_callback: Callable[[], None] | None = None) -> MprisService:
    """Собрать и запустить MPRIS для сессии плеера.

    Всегда возвращает сервис: если шины нет, ``service.available`` равно
    ``False``, а причина лежит в ``service.error`` — вызывающая сторона решает,
    предупредить ли пользователя.
    """
    from .mpris import PlayerTarget

    service = MprisService(MprisController(PlayerTarget(player), quit_callback=quit_callback))
    if service.start():
        return service
    return service
