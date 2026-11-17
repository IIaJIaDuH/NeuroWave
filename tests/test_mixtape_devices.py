"""USB-03: обнаружение съёмных томов без D-Bus, /dev и реальных носителей.

Чистый маппер UDisks2 проверяется на словарях managed objects, сервис — на
подменённом провайдере, а fake-сервис воспроизводит T-001…T-005: системные
диски не предлагаются, read-only отклоняется, подмена/перемонтирование тома
ломает подтверждение.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from neurowave.mixtape import DeviceIdentity
from neurowave.mixtape.devices import (
    DeviceErrorCode,
    FakeUsbDeviceService,
    UDisks2DeviceService,
    UsbDeviceError,
    identities_from_objects,
    identities_match,
)
from neurowave.mixtape.devices.udisks2 import BLOCK_IFACE, DRIVE_IFACE, FILESYSTEM_IFACE

OBJECT = "/org/freedesktop/UDisks2/block_devices/sdb1"


class Variant:
    """Минимальная замена dbus_next.Variant для проверки разворачивания."""

    def __init__(self, signature: str, value: object) -> None:
        self.signature = signature
        self.value = value


def managed(
    *,
    object_path: str = OBJECT,
    mount: str = "/media/user/USB",
    bus: str = "usb",
    removable: bool = True,
    read_only: bool = False,
    hint_system: bool = False,
    uuid: str | None = "1234-ABCD",
    fs: str = "vfat",
    size: int = 16_000_000_000,
    usage: str = "filesystem",
    device: bytes = b"/dev/sdb1",
    drive_id: str = "USB_Stick",
    drives: bool = True,
    mounted: bool = True,
) -> dict:
    drive_path = f"/org/freedesktop/UDisks2/drives/{object_path.rsplit('/', 1)[-1]}_drive"
    block = {
        "IdUsage": usage,
        "IdType": fs,
        "IdUUID": uuid,
        "Size": size,
        "ReadOnly": read_only,
        "HintSystem": hint_system,
        "PreferredDevice": device,
        "Drive": drive_path if drives else "",
    }
    interfaces: dict = {BLOCK_IFACE: block}
    if mounted:
        interfaces[FILESYSTEM_IFACE] = {"MountPoints": [os.fsencode(mount)]}
    objects = {object_path: interfaces}
    if drives:
        objects[drive_path] = {
            DRIVE_IFACE: {
                "Removable": removable,
                "ConnectionBus": bus,
                "Id": drive_id,
            }
        }
    return objects


def identity(**overrides) -> DeviceIdentity:
    defaults = dict(
        udisks_object_path=OBJECT,
        block_device="/dev/sdb1",
        mountpoint=Path("/media/user/USB"),
        filesystem_type="vfat",
        filesystem_uuid="1234-ABCD",
        drive_id="USB_Stick",
        removable=True,
        writable=True,
        capacity_bytes=16_000_000_000,
        free_bytes=8_000_000_000,
    )
    defaults.update(overrides)
    return DeviceIdentity(**defaults)


def service_for(objects: dict) -> UDisks2DeviceService:
    return UDisks2DeviceService(lambda: objects)


# --- маппер UDisks2 -------------------------------------------------------


def test_usb_volume_is_detected_with_fields():
    [device] = identities_from_objects(
        managed(device=b"/dev/sdb1"), free_bytes=lambda mount: 7_500_000_000
    )

    assert device.udisks_object_path == OBJECT
    assert device.block_device == "/dev/sdb1"
    assert device.mountpoint == Path("/media/user/USB")
    assert device.filesystem_type == "vfat"
    assert device.filesystem_uuid == "1234-ABCD"
    assert device.drive_id == "USB_Stick"
    assert device.removable is True
    assert device.writable is True
    assert device.capacity_bytes == 16_000_000_000
    assert device.free_bytes == 7_500_000_000
    assert device.system is False


def test_variant_values_are_unwrapped():
    objects = managed()
    objects[OBJECT][BLOCK_IFACE]["IdType"] = Variant("s", "exfat")
    objects[OBJECT][FILESYSTEM_IFACE]["MountPoints"] = Variant(
        "aay", [Variant("ay", os.fsencode("/media/user/ExFAT"))]
    )

    [device] = identities_from_objects(objects)

    assert device.filesystem_type == "exfat"
    assert device.mountpoint == Path("/media/user/ExFAT")


def test_internal_drive_is_not_detected():
    objects = managed(bus="ata", removable=False, mount="/")

    assert identities_from_objects(objects) == ()


def test_usb_bus_without_removable_flag_is_accepted():
    [device] = identities_from_objects(managed(removable=False, bus="usb"))

    assert device.removable is False
    assert device.system is False


@pytest.mark.parametrize("mount", ["/", "/home", "/boot", "/boot/efi", "/usr"])
def test_system_mountpoints_are_marked(mount):
    [device] = identities_from_objects(managed(mount=mount))

    assert device.system is True


def test_hint_system_is_marked():
    [device] = identities_from_objects(managed(hint_system=True))

    assert device.system is True


def test_read_only_volume_is_not_writable():
    [device] = identities_from_objects(managed(read_only=True), free_bytes=lambda mount: 1)

    assert device.writable is False
    assert device.free_bytes == 0


def test_swap_and_unmounted_volumes_are_skipped():
    assert identities_from_objects(managed(usage="swap")) == ()
    assert identities_from_objects(managed(mounted=False)) == ()


def test_free_bytes_errors_do_not_break_detection():
    def broken(mount):
        raise OSError("statvfs failed")

    [device] = identities_from_objects(managed(), free_bytes=broken)

    assert device.free_bytes == 0


def test_snapshot_is_sorted_by_object_path():
    second = "/org/freedesktop/UDisks2/block_devices/sdc1"
    objects = {
        **managed(),
        **managed(object_path=second, drive_id="OTHER"),
    }

    paths = [device.udisks_object_path for device in identities_from_objects(objects)]

    assert paths == [OBJECT, second]


# --- UDisks2DeviceService -------------------------------------------------


def test_service_lists_only_writable_removable_non_system_volumes():
    objects = {
        **managed(),
        **managed(
            object_path="/org/freedesktop/UDisks2/block_devices/sdc1",
            mount="/media/user/READONLY",
            read_only=True,
            drive_id="RO",
        ),
        **managed(
            object_path="/org/freedesktop/UDisks2/block_devices/sdd1",
            mount="/media/user/SYSTEM",
            hint_system=True,
            drive_id="SYS",
        ),
        **managed(
            object_path="/org/freedesktop/UDisks2/block_devices/sde1",
            mount="/media/user/INTERNAL",
            bus="ata",
            removable=False,
            drive_id="INT",
        ),
    }
    service = UDisks2DeviceService(lambda: objects)

    devices = service.list_removable_writable_filesystems()

    assert [device.udisks_object_path for device in devices] == [OBJECT]


def test_two_devices_are_not_auto_selected_t001_t002():
    objects = {
        **managed(),
        **managed(
            object_path="/org/freedesktop/UDisks2/block_devices/sdc1",
            mount="/media/user/SECOND",
            drive_id="SECOND",
        ),
    }

    devices = service_for(objects).list_removable_writable_filesystems()

    assert len(devices) == 2
    assert {device.mountpoint for device in devices} == {
        Path("/media/user/USB"),
        Path("/media/user/SECOND"),
    }


def test_t001_system_only_environment_has_no_eligible_device():
    objects = {
        **managed(mount="/", bus="ata", removable=False),
        **managed(
            object_path="/org/freedesktop/UDisks2/block_devices/nvme0n1p2",
            mount="/home",
            bus="nvme",
            removable=False,
            drive_id="NVME",
        ),
    }

    assert service_for(objects).list_removable_writable_filesystems() == ()


def test_revalidate_returns_fresh_identity():
    service = service_for(managed())

    current = service.revalidate(identity())

    assert current.udisks_object_path == OBJECT
    assert current.mountpoint == Path("/media/user/USB")


def test_t005_unplugged_device_is_rejected():
    service = service_for(managed())
    expected = service.list_removable_writable_filesystems()[0]

    service = service_for({})

    with pytest.raises(UsbDeviceError) as error:
        service.revalidate(expected)
    assert error.value.code is DeviceErrorCode.NOT_FOUND


def test_t004_changed_uuid_is_rejected_even_with_same_path():
    expected = service_for(managed()).list_removable_writable_filesystems()[0]
    swapped = service_for(managed(uuid="FFFF-0000"))

    with pytest.raises(UsbDeviceError) as error:
        swapped.revalidate(expected)
    assert error.value.code is DeviceErrorCode.CHANGED


def test_t004_same_uuid_but_changed_mount_and_block_is_rejected():
    expected = service_for(managed()).list_removable_writable_filesystems()[0]
    remounted = service_for(
        managed(mount="/media/user/OTHER", device=b"/dev/sdc1", drive_id="USB_Stick")
    )

    with pytest.raises(UsbDeviceError) as error:
        remounted.revalidate(expected)
    assert error.value.code is DeviceErrorCode.CHANGED


def test_t003_remounted_read_only_is_rejected():
    expected = service_for(managed()).list_removable_writable_filesystems()[0]
    read_only = service_for(managed(read_only=True))

    with pytest.raises(UsbDeviceError) as error:
        read_only.revalidate(expected)
    assert error.value.code is DeviceErrorCode.READ_ONLY
    assert read_only.list_removable_writable_filesystems() == ()


def test_system_and_non_removable_are_rejected_on_revalidate():
    expected = identity()

    with pytest.raises(UsbDeviceError) as system_error:
        service_for(managed(hint_system=True)).revalidate(expected)
    assert system_error.value.code is DeviceErrorCode.SYSTEM

    with pytest.raises(UsbDeviceError) as internal_error:
        service_for(managed(bus="usb", removable=False)).revalidate(expected)
    assert internal_error.value.code is DeviceErrorCode.NOT_REMOVABLE


def test_bus_failure_becomes_stable_unavailable_code():
    def broken():
        raise RuntimeError("system bus is not available")

    service = UDisks2DeviceService(broken)

    with pytest.raises(UsbDeviceError) as error:
        service.list_removable_writable_filesystems()
    assert error.value.code is DeviceErrorCode.UNAVAILABLE
    assert "system bus" in str(error.value)


# --- FakeUsbDeviceService -------------------------------------------------


def test_fake_lists_only_usable_devices():
    fake = FakeUsbDeviceService(
        [
            identity(),
            identity(
                udisks_object_path="/org/freedesktop/UDisks2/block_devices/sdc1",
                mountpoint=Path("/media/user/RO"),
                writable=False,
            ),
            identity(
                udisks_object_path="/org/freedesktop/UDisks2/block_devices/sdd1",
                mountpoint=Path("/media/user/SYS"),
                system=True,
            ),
            identity(
                udisks_object_path="/org/freedesktop/UDisks2/block_devices/sde1",
                mountpoint=Path("/media/user/INT"),
                removable=False,
            ),
        ]
    )

    assert [device.mountpoint for device in fake.list_removable_writable_filesystems()] == [
        Path("/media/user/USB")
    ]


def test_fake_unplugged_device_is_rejected():
    fake = FakeUsbDeviceService([identity()])
    expected = fake.list_removable_writable_filesystems()[0]
    fake.remove(OBJECT)

    with pytest.raises(UsbDeviceError) as error:
        fake.revalidate(expected)
    assert error.value.code is DeviceErrorCode.NOT_FOUND


def test_fake_changed_device_is_rejected():
    fake = FakeUsbDeviceService([identity()])
    expected = fake.list_removable_writable_filesystems()[0]
    fake.replace(identity(filesystem_uuid="NEW-UUID"))

    with pytest.raises(UsbDeviceError) as error:
        fake.revalidate(expected)
    assert error.value.code is DeviceErrorCode.CHANGED


def test_fake_read_only_device_is_rejected():
    fake = FakeUsbDeviceService([identity()])
    expected = fake.list_removable_writable_filesystems()[0]
    fake.replace(identity(writable=False))

    with pytest.raises(UsbDeviceError) as error:
        fake.revalidate(expected)
    assert error.value.code is DeviceErrorCode.READ_ONLY


def test_fake_second_device_requires_explicit_choice():
    fake = FakeUsbDeviceService([identity()])
    second = identity(
        udisks_object_path="/org/freedesktop/UDisks2/block_devices/sdc1",
        block_device="/dev/sdc1",
        mountpoint=Path("/media/user/SECOND"),
        filesystem_uuid="SECOND-UUID",
    )
    fake.add(second)

    devices = fake.list_removable_writable_filesystems()

    assert len(devices) == 2
    assert fake.revalidate(second) == second


# --- сравнение identity ---------------------------------------------------


def test_identity_match_compares_available_fields():
    current = identity()

    assert identities_match(current, current) is True
    assert identities_match(current, identity(block_device="/dev/sdc1")) is False
    assert identities_match(current, identity(mountpoint=Path("/media/user/OTHER"))) is False
    assert identities_match(current, identity(filesystem_uuid="OTHER")) is False
    assert identities_match(current, identity(capacity_bytes=1)) is False


def test_missing_uuid_does_not_prevent_match():
    expected = identity(filesystem_uuid=None, drive_id=None)
    current = identity(filesystem_uuid=None, drive_id=None)

    assert identities_match(expected, current) is True
