"""Fake USB-сервис для CI: без UDisks2, D-Bus, ``/dev`` и реальных носителей."""

from __future__ import annotations

from typing import Iterable

from neurowave.mixtape.devices.base import (
    DeviceErrorCode,
    UsbDeviceError,
    identities_match,
)
from neurowave.mixtape.models import DeviceIdentity


class FakeUsbDeviceService:
    """Управляемый список томов; тесты могут подменять и выдёргивать их."""

    def __init__(self, devices: Iterable[DeviceIdentity] = ()) -> None:
        self._devices: dict[str, DeviceIdentity] = {
            device.udisks_object_path: device for device in devices
        }

    @property
    def devices(self) -> tuple[DeviceIdentity, ...]:
        return tuple(self._devices[key] for key in sorted(self._devices))

    def add(self, device: DeviceIdentity) -> None:
        self._devices[device.udisks_object_path] = device

    def remove(self, object_path: str) -> None:
        self._devices.pop(object_path, None)

    def replace(self, device: DeviceIdentity) -> None:
        self._devices[device.udisks_object_path] = device

    def list_removable_writable_filesystems(self) -> tuple[DeviceIdentity, ...]:
        return tuple(
            device
            for device in self.devices
            if device.removable and device.writable and not device.system
        )

    def revalidate(self, expected: DeviceIdentity) -> DeviceIdentity:
        current = self._devices.get(expected.udisks_object_path)
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
