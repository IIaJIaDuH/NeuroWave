"""Протокол доступа к съёмным томам.

Сервис только перечисляет и перепроверяет устройства. Он не форматирует,
не монтирует и не пишет в ``/dev``: запись появится в USB-04 и только после
повторной проверки identity.
"""

from __future__ import annotations

from enum import Enum
from typing import Protocol, Sequence

from neurowave.mixtape.models import DeviceIdentity


class DeviceErrorCode(str, Enum):
    UNAVAILABLE = "device_service_unavailable"
    NOT_FOUND = "device_not_found"
    CHANGED = "device_changed"
    READ_ONLY = "device_read_only"
    NOT_REMOVABLE = "device_not_removable"
    SYSTEM = "device_system"
    UNVERIFIED = "device_unverified"
    EJECT_FAILED = "eject_failed"


class UsbDeviceError(Exception):
    """Ошибка выбора/перепроверки USB-тома со стабильным кодом для CLI."""

    def __init__(self, code: DeviceErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class UsbDeviceService(Protocol):
    def list_removable_writable_filesystems(self) -> Sequence[DeviceIdentity]: ...

    def revalidate(self, expected: DeviceIdentity) -> DeviceIdentity: ...

    def safely_eject(self, expected: DeviceIdentity) -> DeviceIdentity: ...


def identities_match(expected: DeviceIdentity, current: DeviceIdentity) -> bool:
    """Совпадает ли том по устойчивым признакам.

    UUID или серийный номер могут отсутствовать/дублироваться, поэтому
    сравниваются все доступные поля, а не один идентификатор.
    """

    if expected.udisks_object_path != current.udisks_object_path:
        return False
    if expected.block_device != current.block_device:
        return False
    if expected.mountpoint != current.mountpoint:
        return False
    if expected.filesystem_type != current.filesystem_type:
        return False
    if int(expected.capacity_bytes) != int(current.capacity_bytes):
        return False
    if (
        expected.filesystem_uuid
        and current.filesystem_uuid
        and expected.filesystem_uuid != current.filesystem_uuid
    ):
        return False
    if expected.drive_id and current.drive_id and expected.drive_id != current.drive_id:
        return False
    return True
