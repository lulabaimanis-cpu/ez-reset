"""
Layer B & C: ApdaTransport and D4Session for Epson L5190 (Family: L5JX).

Features:
- Synchronous, exclusive file handle semantics (share_mode=0, flags=0).
- ADInitDevice IOCTL 0x220040 with input byte 0x01.
- D4 C5 Handshake + D4 Init.
- Fixed Socket 02/02.
- Stash/Pending Queue for out-of-order DATA packets.
"""

from collections import deque
import logging
import time
from typing import Self

from win32file import (
    GENERIC_READ,
    GENERIC_WRITE,
    OPEN_EXISTING,
    CreateFileW,
    DeviceIoControl,
    ReadFile,
    WriteFile,
)

from .constants import (
    D4_ENTER_ACK,
    D4_ENTER_PACKET,
    D4_INIT_ACK_PREFIX,
    D4_INIT_PACKET,
    IOCTL_AD_GET_DEVICE_ID,
    IOCTL_AD_INIT_DEVICE,
    PACKET_SIZE,
    SOCKET_PSID,
    SOCKET_SSID,
)
from ..exceptions import BackendError, ProtocolError

logger = logging.getLogger("ez_reset.l5190.transport")

MAX_TRANSFER_SIZE = 0x400000


class ApdaTransport:
    """Windows USBPRINT transport adhering to APDA exclusive/synchronous semantics."""

    def __init__(self, device_path: str) -> None:
        self.device_path = device_path
        self.handle = None
        self.closed = True

    def __enter__(self) -> Self:
        logger.info("[APDA] Opening synchronous exclusive handle to: %s", self.device_path)
        # CRITICAL APDA SEMANTICS:
        # dwShareMode = 0 (EXCLUSIVE)
        # dwFlagsAndAttributes = 0 (SYNCHRONOUS, NO FILE_FLAG_OVERLAPPED)
        self.handle = CreateFileW(
            self.device_path,
            GENERIC_READ | GENERIC_WRITE,
            0,  # EXCLUSIVE
            None,
            OPEN_EXISTING,
            0,  # SYNCHRONOUS
            None,
        )
        self.closed = False
        logger.info("[APDA] Opened successfully on handle: %s", self.handle.handle)

        # ADInitDevice (IOCTL 0x220040 with input [0x01], len 1)
        self.init_device()

        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.close()
        return False

    def close(self) -> None:
        if not self.closed and self.handle is not None:
            logger.info("[APDA] Closing handle %s", self.handle.handle)
            try:
                self.handle.close()
            except Exception as e:
                logger.debug("[APDA] Exception closing handle: %s", e)
            self.closed = True

    def init_device(self) -> None:
        """
        Execute ADInitDevice.
        CRITICAL APDA TRACE: IOCTL 0x220040 with input byte 0x01 (len=1).
        """
        if self.closed:
            raise OSError("Handle is closed.")

        logger.debug("[APDA] Issuing ADInitDevice (IOCTL 0x220040, input=0x01)")
        input_data = b"\x01"
        try:
            DeviceIoControl(self.handle, IOCTL_AD_INIT_DEVICE, input_data, 1024)
            logger.info("[APDA] ADInitDevice successful.")
        except Exception as e:
            logger.error("[APDA] ADInitDevice failed: %s", e)
            raise BackendError(f"ADInitDevice IOCTL 0x220040 failed: {e}") from e

    def get_device_id(self) -> str:
        """Query IEEE-1284 Device ID via IOCTL 0x220034."""
        if self.closed:
            raise OSError("Handle is closed.")

        logger.debug("[APDA] Issuing ADGetDeviceID (IOCTL 0x220034)")
        try:
            raw_id = DeviceIoControl(self.handle, IOCTL_AD_GET_DEVICE_ID, None, 1024)
            # Skip first 2 length bytes
            device_id_str = raw_id[2:].decode("ascii", errors="replace")
            logger.info("[APDA] Device ID: %s", device_id_str)
            return device_id_str
        except Exception as e:
            logger.error("[APDA] ADGetDeviceID failed: %s", e)
            raise BackendError(f"ADGetDeviceID IOCTL 0x220034 failed: {e}") from e

    def write_all(self, data: bytes) -> None:
        if self.closed:
            raise OSError("Handle is closed.")

        _status, bytes_written = WriteFile(self.handle, data)
        if bytes_written != len(data):
            raise OSError(f"Incomplete write: wrote {bytes_written} of {len(data)} bytes.")

    def read_exact(self, size: int, timeout_sec: float = 5.0) -> bytes:
        if self.closed:
            raise OSError("Handle is closed.")

        buffer = bytearray()
        deadline = time.time() + timeout_sec
        while len(buffer) < size:
            if time.time() > deadline:
                raise TimeoutError(f"Read timeout waiting for {size} bytes (got {len(buffer)}).")

            _status, data = ReadFile(self.handle, min(size - len(buffer), MAX_TRANSFER_SIZE))
            if data:
                buffer.extend(data)
            else:
                time.sleep(0.01)

        return bytes(buffer)

    def read_available(self, timeout_sec: float = 1.0) -> bytes:
        if self.closed:
            raise OSError("Handle is closed.")

        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            _status, data = ReadFile(self.handle, 1024)
            if data:
                return data
            time.sleep(0.01)

        return b""


class D4Session:
    """
    IEEE 1284.4 session management on fixed socket 02/02 with Stash Queue
    to handle out-of-order DATA arrival before control 0x83 ACK.
    """

    def __init__(self, transport: ApdaTransport) -> None:
        self.transport = transport
        self.pending_data_queue: deque[bytes] = deque()
        self.session_active = False

    def enter(self) -> None:
        """Send EJL 1284.4 enter packet and verify C5 00 ACK."""
        logger.info("[D4] Sending D4 Enter (EJL 1284.4)...")
        self.transport.write_all(D4_ENTER_PACKET)

        # Expected ACK: 00 00 00 08 01 00 C5 00
        rx = self.transport.read_exact(len(D4_ENTER_ACK))
        logger.debug("[D4] Enter RX: %s", rx.hex())

        if D4_ENTER_ACK not in rx and b"\xC5" not in rx:
            raise ProtocolError(f"D4 Enter rejected: expected C5 ACK, got {rx.hex()}")

        logger.info("[D4] D4 Enter ACK (C5) received.")

    def init(self) -> None:
        """Send D4 Init packet and verify response."""
        logger.info("[D4] Sending D4 Init packet...")
        self.transport.write_all(D4_INIT_PACKET)

        rx = self.transport.read_exact(9)
        logger.debug("[D4] Init RX: %s", rx.hex())

        if not rx.startswith(D4_INIT_ACK_PREFIX[:7]):
            raise ProtocolError(f"D4 Init rejected: unexpected response {rx.hex()}")

        logger.info("[D4] D4 Init acknowledged.")
        self.session_active = True

    def open_socket_02_02(self) -> None:
        """L5190 uses fixed socket 02/02 with packet size 0x0100."""
        logger.info("[D4] Using fixed socket PSID=%02X, SSID=%02X (Packet Size: %04X)",
                    SOCKET_PSID, SOCKET_SSID, PACKET_SIZE)

    def send_socket_data(self, payload: bytes) -> None:
        """
        Send DATA packet on socket 02/02.
        Header: 02 02 <len_lo> <len_hi> 01 <payload>
        """
        length = len(payload)
        header = bytes([SOCKET_PSID, SOCKET_SSID, length & 0xFF, (length >> 8) & 0xFF, 0x01])
        packet = header + payload
        self.transport.write_all(packet)

    def receive_socket_data(self, timeout_sec: float = 5.0) -> bytes:
        """
        Receive DATA packet from socket 02/02.
        Handles out-of-order packets via pending_data_queue.
        """
        # First check stash queue
        if self.pending_data_queue:
            data = self.pending_data_queue.popleft()
            logger.debug("[D4] Retrieved DATA from stash queue (%d bytes)", len(data))
            return data

        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            rx = self.transport.read_available(timeout_sec=0.5)
            if not rx:
                continue

            # Check if this is DATA packet on socket 02/02
            if len(rx) >= 5 and rx[0] == SOCKET_PSID and rx[1] == SOCKET_SSID:
                payload_len = rx[2] | (rx[3] << 8)
                payload = rx[5 : 5 + payload_len]

                # If there are remaining trailing bytes in buffer, stash them
                if len(rx) > 5 + payload_len:
                    trailing = rx[5 + payload_len :]
                    if len(trailing) >= 5 and trailing[0] == SOCKET_PSID and trailing[1] == SOCKET_SSID:
                        t_len = trailing[2] | (trailing[3] << 8)
                        self.pending_data_queue.append(trailing[5 : 5 + t_len])

                return payload

            # If this is control reply 0x83 or other non-data, stash or ignore
            if len(rx) > 0 and rx[0] == 0x83:
                logger.debug("[D4] Received control reply 0x83")
                continue

        raise TimeoutError("Timed out waiting for DATA on socket 02/02.")
