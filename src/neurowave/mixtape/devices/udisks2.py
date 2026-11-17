"""UDisks2: обнаружение съёмных томов только через D-Bus, без ``/dev``.

``Block.Device`` читается исключительно для диагностики: никаких открытий,
форматирования и записи блочных устройств здесь нет. Зависимость ``dbus-next``
импортируется лениво, а чистая функция :func:`identities_from_objects`
тестируется на словарях managed objects — без шины и реальных носителей.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Any, Callable, Mapping

from neurowave.mixtape.devices.base import (
    DeviceErrorCode,
    UsbDeviceError,
    identities_match,
)
from neurowave.mixtape.models import DeviceIdentity

UDISKS_BUS = "org.freedesktop.UDisks2"
UDISKS_PATH = "/org/freedesktop/UDisks2"
BLOCK_IFACE = "org.freedesktop.UDisks2.Block"
FILESYSTEM_IFACE = "org.freedesktop.UDisks2.Filesystem"
DRIVE_IFACE = "org.freedesktop.UDisks2.Drive"
OBJECT_MANAGER_IFACE = "org.freedesktop.DBus.ObjectManager"

SYSTEM_MOUNTPOINTS = frozenset(
    {
        "/",
        "/boot",
        "/boot/efi",
        "/home",
        "/usr",
        "/var",
        "/etc",
        "/run",
        "/run/lock",
        "/run/user",
    }
)
# ``/run/media/<user>/<label>`` — штатное место съёмных томов в Fedora, поэтому
# весь ``/run`` системным не считается: только lock/user-подкаталоги.
SYSTEM_PREFIXES = (
    "/boot/",
    "/usr/",
    "/var/",
    "/etc/",
    "/proc/",
    "/sys/",
    "/dev/",
    "/run/lock/",
    "/run/user/",
)


def identities_from_objects(
    objects: Mapping[str, Mapping[str, Mapping[str, Any]]],
    *,
    free_bytes: Callable[[Path], int] | None = None,
) -> tuple[DeviceIdentity, ...]:
    """Собрать identities из ответа ``ObjectManager.GetManagedObjects``.

    Внутренние диски (не removable и не USB), swap/шифрованные разделы, тома
    без точки монтирования отбрасываются. Системные mountpoint и ``HintSystem``
    помечаются ``system=True``; окончательно их отсекает сервис.
    """

    identities: list[DeviceIdentity] = []
    for object_path in sorted(objects):
        interfaces = objects[object_path] or {}
        raw_block = interfaces.get(BLOCK_IFACE)
        if not raw_block:
            continue
        block = {key: _plain(value) for key, value in raw_block.items()}
        if _text(block.get("IdUsage")) != "filesystem":
            continue

        raw_filesystem = interfaces.get(FILESYSTEM_IFACE) or {}
        mounts = _mountpoints(raw_filesystem.get("MountPoints"))
        if not mounts:
            continue

        drive_path = _text(block.get("Drive"))
        raw_drive = (objects.get(drive_path, {}) or {}).get(DRIVE_IFACE) or {}
        drive = {key: _plain(value) for key, value in raw_drive.items()}
        removable = bool(drive.get("Removable", False))
        connection_bus = _text(drive.get("ConnectionBus")).lower()
        if not removable and connection_bus != "usb":
            continue

        mountpoint = next((mount for mount in mounts if mount.is_absolute()), mounts[0])
        writable = not bool(block.get("ReadOnly", False))
        system = (
            bool(block.get("HintSystem", False))
            or not mountpoint.is_absolute()
            or _is_system_mount(mountpoint)
        )

        free = 0
        if free_bytes is not None and writable:
            try:
                free = max(0, int(free_bytes(mountpoint)))
            except (OSError, ValueError):
                free = 0

        identities.append(
            DeviceIdentity(
                udisks_object_path=object_path,
                block_device=_text(block.get("PreferredDevice") or block.get("Device")),
                mountpoint=mountpoint,
                filesystem_type=_text(block.get("IdType")),
                filesystem_uuid=_text(block.get("IdUUID")) or None,
                drive_id=_text(drive.get("Id")) or None,
                removable=removable,
                writable=writable,
                capacity_bytes=_int(block.get("Size")),
                free_bytes=free,
                system=system,
            )
        )
    return tuple(identities)


class UDisks2DeviceService:
    """Синхронная обёртка над UDisks2; провайдер объектов можно подменить."""

    def __init__(
        self,
        objects_provider: Callable[[], Mapping[str, Mapping[str, Mapping[str, Any]]]] | None = None,
    ) -> None:
        self._provider = objects_provider or _managed_objects

    def list_removable_writable_filesystems(self) -> tuple[DeviceIdentity, ...]:
        snapshot = self._snapshot()
        return tuple(
            device
            for _, device in sorted(snapshot.items())
            if device.removable and device.writable and not device.system
        )

    def revalidate(self, expected: DeviceIdentity) -> DeviceIdentity:
        current = self._snapshot().get(expected.udisks_object_path)
        if current is None:
            raise UsbDeviceError(DeviceErrorCode.NOT_FOUND, "USB-том больше не подключён")
        if current.system:
            raise UsbDeviceError(DeviceErrorCode.SYSTEM, "Системный том нельзя использовать")
        if not current.removable:
            raise UsbDeviceError(DeviceErrorCode.NOT_REMOVABLE, "Том не является съёмным")
        if not current.writable:
            raise UsbDeviceError(DeviceErrorCode.READ_ONLY, "Том только для чтения")
        if not identities_match(expected, current):
            raise UsbDeviceError(
                DeviceErrorCode.CHANGED,
                "Том изменился: требуется новый выбор и подтверждение",
            )
        return current

    def safely_eject(self, expected: DeviceIdentity) -> DeviceIdentity:
        current = self.revalidate(expected)
        try:
            asyncio.run(_unmount_object(current.udisks_object_path))
        except UsbDeviceError:
            raise
        except Exception as error:  # pragma: no cover - живая шина
            raise UsbDeviceError(
                DeviceErrorCode.EJECT_FAILED, f"Не удалось размонтировать том: {error}"
            ) from error
        return current

    def _snapshot(self) -> dict[str, DeviceIdentity]:
        try:
            objects = self._provider()
        except UsbDeviceError:
            raise
        except Exception as error:  # noqa: BLE001 — ошибки шины приводим к stable code
            raise UsbDeviceError(
                DeviceErrorCode.UNAVAILABLE, f"UDisks2 недоступен: {error}"
            ) from error
        return {
            identity.udisks_object_path: identity
            for identity in identities_from_objects(objects, free_bytes=_free_bytes)
        }


def _managed_objects() -> Mapping[str, Mapping[str, Mapping[str, Any]]]:
    try:
        import dbus_next  # noqa: F401
    except Exception as error:  # pragma: no cover - зависит от окружения
        raise UsbDeviceError(
            DeviceErrorCode.UNAVAILABLE,
            "Нет пакета dbus-next: обнаружение USB недоступно",
        ) from error
    try:
        return asyncio.run(_read_managed_objects())
    except UsbDeviceError:
        raise
    except Exception as error:  # pragma: no cover - живая шина
        raise UsbDeviceError(
            DeviceErrorCode.UNAVAILABLE, f"UDisks2 недоступен: {error}"
        ) from error


async def _read_managed_objects():
    from dbus_next.aio import MessageBus

    bus = await MessageBus().connect()
    try:
        introspection = await bus.introspect(UDISKS_BUS, UDISKS_PATH)
        proxy = bus.get_proxy_object(UDISKS_BUS, UDISKS_PATH, introspection)
        manager = proxy.get_interface(OBJECT_MANAGER_IFACE)
        return await manager.call_get_managed_objects()
    finally:
        bus.disconnect()


async def _unmount_object(object_path: str) -> None:
    from dbus_next.aio import MessageBus

    bus = await MessageBus().connect()
    try:
        introspection = await bus.introspect(UDISKS_BUS, object_path)
        proxy = bus.get_proxy_object(UDISKS_BUS, object_path, introspection)
        filesystem = proxy.get_interface(FILESYSTEM_IFACE)
        await filesystem.call_unmount({})
    finally:
        bus.disconnect()


def _free_bytes(mountpoint: Path) -> int:
    return int(shutil.disk_usage(mountpoint).free)


def _is_system_mount(mountpoint: Path) -> bool:
    text = mountpoint.as_posix()
    return text in SYSTEM_MOUNTPOINTS or any(text.startswith(prefix) for prefix in SYSTEM_PREFIXES)


def _mountpoints(value: Any) -> tuple[Path, ...]:
    plain = _plain(value) or ()
    if not isinstance(plain, (list, tuple)):
        return ()
    mounts: list[Path] = []
    for item in plain:
        text = _text(item)
        if text:
            mounts.append(Path(text))
    return tuple(mounts)


def _text(value: Any) -> str:
    plain = _plain(value)
    if isinstance(plain, bytes):
        return os.fsdecode(plain)
    if plain is None:
        return ""
    return str(plain)


def _int(value: Any) -> int:
    try:
        return max(0, int(_plain(value) or 0))
    except (TypeError, ValueError):
        return 0


def _plain(value: Any) -> Any:
    """Развернуть ``Variant`` из dbus-next, не трогая bytes/str."""

    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if hasattr(value, "signature") and hasattr(value, "value"):
        return _plain(value.value)
    return value
