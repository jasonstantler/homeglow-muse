"""Bluetooth setup without BlueZ, for pairing Muse Link with the Muse app.

Why: the Muse Android app writes (MTU - 3)-byte packets and fails when they
pass 512 bytes. Home Assistant OS's BlueZ negotiates an ATT MTU of 517, and
an add-on can't change BlueZ's settings. So while pairing is open, Muse Link
borrows the Bluetooth adapter from BlueZ (an HCI user channel) and runs its
own stack (Google's Bumble), offering at most MAX_MTU, the size Meta's own
installer and ESP32 firmware use. Closing the channel hands the adapter back
to BlueZ, which turns it on again.

DirectBleServer implements the same transport as musegadget.ble_server's
BleServer, so the SDK's SetupController and pair() drive it unchanged.
"""

from __future__ import annotations

import asyncio
import errno
import fcntl
import logging
import os
import socket
import struct
import threading
import time
from typing import Awaitable, Callable

from bumble import data_types
from bumble.core import AdvertisingData
from bumble.device import Device, DeviceConfiguration
from bumble.gatt import Characteristic, CharacteristicValue, Service
from bumble.hci import Address
from bumble.host import Host
from bumble.pairing import PairingConfig, PairingDelegate

from musegadget.ble_framing import CHUNK_STAGGER_S

log = logging.getLogger("muse-link.bluetooth")

# The setup service, as musegadget.ble_server advertises it.
SERVICE_UUID = "7fdd3d1c-38ea-46cf-8b46-314ecf5f240c"
RX_UUID = "4d593029-28a2-4a6e-a1f0-3c2d5e8f9b01"
TX_UUID = "d75dc4ca-7b2b-4e9c-8f0a-1d2e3f4a5b6c"
PAIRED_FLAG_COMPANY_ID = 0xFFFF

MAX_MTU = 256
HCIDEVDOWN = 0x400448CA  # _IOW('H', 202, int)
NOTIFY_TIMEOUT_S = 10.0


class AdapterUnavailable(RuntimeError):
    """The Bluetooth adapter couldn't be borrowed; ``reason`` says why, for the setup page."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason


def adapter_indexes() -> list[int]:
    try:
        names = os.listdir("/sys/class/bluetooth")
    except OSError:
        return []
    return sorted(int(n[3:]) for n in names if n.startswith("hci") and n[3:].isdigit())


def _bring_down(index: int) -> None:
    sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW | socket.SOCK_CLOEXEC, socket.BTPROTO_HCI)
    try:
        fcntl.ioctl(sock.fileno(), HCIDEVDOWN, index)
    finally:
        sock.close()


async def borrow_adapter(index: int):
    """Take the adapter away from BlueZ; returns a Bumble HCI transport."""
    from bumble.transport.hci_socket import open_hci_socket_transport

    last: OSError | None = None
    for _ in range(6):
        try:
            _bring_down(index)
            return await open_hci_socket_transport(str(index))
        except OSError as exc:
            last = exc
            if exc.errno in (errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT):
                raise AdapterUnavailable("no_bluetooth_sockets", str(exc))
            if exc.errno in (errno.EPERM, errno.EACCES):
                raise AdapterUnavailable("no_permission", str(exc))
            if exc.errno == errno.ENODEV:
                raise AdapterUnavailable("no_adapter", str(exc))
            if exc.errno != errno.EBUSY:
                raise AdapterUnavailable("failed", str(exc))
            await asyncio.sleep(0.4)  # BlueZ was using it; try again
    raise AdapterUnavailable("busy", str(last))


def random_static_address(mac: str) -> Address:
    """A stable random static address made from the gadget's identity."""
    octets = bytearray(bytes.fromhex(mac.replace(":", "")))
    octets[0] |= 0xC0
    return Address(":".join(f"{b:02X}" for b in octets))


class _NoBonding(PairingDelegate):
    """Setup is encrypted at the application layer; refuse Bluetooth pairing (BlueZ: Pairable off)."""

    async def accept(self) -> bool:
        return False


class DirectBleServer:
    """Advertises the setup service on a borrowed adapter until :meth:`stop`."""

    def __init__(
        self,
        local_name: str,
        on_write: Callable[[bytes], None],
        on_disconnect: Callable[[], None],
        *,
        address: Address | None = None,
        adapter: int | None = None,
        open_host: Callable[[], Awaitable[tuple[Host, Callable[[], Awaitable[None]]]]] | None = None,
    ) -> None:
        self._local_name = local_name
        self._on_write = on_write
        self._on_disconnect = on_disconnect
        self._address = address or Address("C0:4D:55:53:45:00")
        self._adapter = adapter
        self._open_host = open_host or self._borrow
        self._lock = threading.Lock()
        self._mtu = 23
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopped: asyncio.Event | None = None
        self._stop_requested = False
        self._device: Device | None = None
        self._tx: Characteristic | None = None
        self._connection = None
        self.ready = threading.Event()   # set once advertising

    # -- Transport (called from the setup worker thread) -------------------------

    def mtu(self) -> int:
        with self._lock:
            return self._mtu

    def send_packets(self, packets: list[bytes]) -> None:
        loop = self._loop
        if loop is None:
            return
        for i, packet in enumerate(packets):
            if i:
                time.sleep(CHUNK_STAGGER_S)
            future = asyncio.run_coroutine_threadsafe(self._notify(packet), loop)
            try:
                future.result(NOTIFY_TIMEOUT_S)
            except Exception as exc:  # disconnected mid-message
                log.info("notification not sent: %s", exc)
                return

    def disconnect(self, delay: float) -> None:
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(loop.call_later, delay, self._disconnect_now)

    # -- Lifecycle --------------------------------------------------------------

    def run(self) -> None:
        """Borrow the adapter, advertise, and serve until :meth:`stop`. Blocks."""
        asyncio.run(self.serve())

    def stop(self) -> None:
        self._stop_requested = True
        loop, stopped = self._loop, self._stopped
        if loop is not None and stopped is not None:
            loop.call_soon_threadsafe(stopped.set)

    async def serve(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stopped = asyncio.Event()
        if self._stop_requested:
            return
        host, close = await self._open_host()
        try:
            await self._start(host)
            self.ready.set()
            await self._stopped.wait()
        finally:
            try:
                if self._connection is not None:
                    await asyncio.wait_for(self._connection.disconnect(), 3)
            except Exception:
                pass
            await close()
            self._loop = None
            log.info("gave the Bluetooth adapter back to Home Assistant")

    # -- Internals (event loop) -------------------------------------------------

    async def _borrow(self):
        indexes = adapter_indexes()
        index = self._adapter if self._adapter is not None else (indexes[0] if indexes else 0)
        transport = await borrow_adapter(index)
        log.info("borrowed Bluetooth adapter hci%d from Home Assistant for pairing", index)

        async def close() -> None:
            await transport.close()

        return Host(transport.source, transport.sink), close

    async def _start(self, host: Host) -> None:
        config = DeviceConfiguration(name=self._local_name, address=self._address)
        config.classic_enabled = False
        device = self._device = Device(config=config, host=host)
        device.gatt_server.max_mtu = MAX_MTU
        device.pairing_config_factory = lambda _connection: PairingConfig(
            sc=True, mitm=False, bonding=False, delegate=_NoBonding())

        rx = Characteristic(
            RX_UUID,
            Characteristic.Properties.WRITE | Characteristic.Properties.WRITE_WITHOUT_RESPONSE,
            Characteristic.WRITEABLE,
            CharacteristicValue(write=self._handle_write),
        )
        self._tx = Characteristic(
            TX_UUID,
            Characteristic.Properties.READ | Characteristic.Properties.NOTIFY,
            Characteristic.READABLE,
            b"",
        )
        device.add_service(Service(SERVICE_UUID, [rx, self._tx]))
        device.on(device.EVENT_CONNECTION, self._handle_connection)
        device.on("characteristic_subscription", self._handle_subscription)

        await device.power_on()
        advertising = AdvertisingData([
            data_types.Flags(AdvertisingData.LE_GENERAL_DISCOVERABLE_MODE_FLAG
                             | AdvertisingData.BR_EDR_NOT_SUPPORTED_FLAG),
            data_types.CompleteListOf128BitServiceUUIDs([_uuid(SERVICE_UUID)]),
            data_types.ManufacturerSpecificData(PAIRED_FLAG_COMPANY_ID, bytes([0])),
        ])
        scan_response = AdvertisingData([data_types.CompleteLocalName(self._local_name)])
        await device.start_advertising(
            auto_restart=True,
            advertising_data=bytes(advertising),
            scan_response_data=bytes(scan_response),
            advertising_interval_min=100,
            advertising_interval_max=150,
        )
        log.info("advertising as %s (ATT MTU up to %d)", self._local_name, MAX_MTU)

    def _handle_connection(self, connection) -> None:
        log.info("device %s connected", connection.peer_address)
        with self._lock:
            self._connection = connection
            self._mtu = connection.att_mtu

        def mtu_changed() -> None:
            with self._lock:
                self._mtu = connection.att_mtu
            log.info("ATT MTU %d", connection.att_mtu)

        def disconnected(reason) -> None:
            log.info("device %s disconnected", connection.peer_address)
            with self._lock:
                ours = self._connection is connection
                if ours:
                    self._connection = None
                    self._mtu = 23
            if ours:
                self._on_disconnect()

        connection.on(connection.EVENT_CONNECTION_ATT_MTU_UPDATE, mtu_changed)
        connection.on(connection.EVENT_DISCONNECTION, disconnected)

    def _handle_subscription(self, connection, characteristic, notify_enabled, indicate_enabled) -> None:
        if notify_enabled or indicate_enabled:
            log.info("client subscribed to %s", TX_UUID)

    def _handle_write(self, connection, value) -> None:
        with self._lock:
            if connection is not self._connection:
                return
        self._on_write(bytes(value))

    async def _notify(self, packet: bytes) -> None:
        connection, device, tx = self._connection, self._device, self._tx
        if connection is None or device is None or tx is None:
            raise ConnectionError("no phone connected")
        tx.value = packet
        await device.notify_subscriber(connection, tx, packet)

    def _disconnect_now(self) -> None:
        connection = self._connection
        if connection is not None:
            asyncio.ensure_future(connection.disconnect())


def _uuid(text: str):
    from bumble.core import UUID
    return UUID(text)
