"""
Layer D: EpsonCtrl for Epson L5190 (Family: L5JX).

Features:
- status_prime ("st 01" -> "@BDC ST2").
- read_eeprom with format "7C 7C 07 00 97 07 41 BE A0 ADDR_LO ADDR_HI".
- write_eeprom with golden wire key "Nbsjcbzb" (4E 62 73 6A 63 62 7A 62).
- Verification of "||:42:OK;" ACK on every write.
"""

import logging
import re
from typing import Final

from .constants import (
    ACTION_READ,
    ACTION_WRITE,
    FACTORY_MODEL_CODE,
    L5190_WIRE_KEY,
    STATUS_PRIME_CMD,
    STATUS_PRIME_EXPECTED,
    WRITE_ACK_EXPECTED,
)
from .transport import D4Session
from ..exceptions import ProtocolError, VerificationError

logger = logging.getLogger("ez_reset.l5190.ctrl")


class EpsonCtrlL5190:
    """EPSON-CTRL protocol handler specifically tuned for L5190 golden trace."""

    def __init__(self, d4_session: D4Session) -> None:
        self.d4 = d4_session

    def status_prime(self) -> bytes:
        """
        Execute status-prime command ("st 01").
        MANDATORY APDA TRACE STEP: must run before Check or Initialize!
        Expected response: @BDC ST2\r\n...
        """
        logger.info("[CTRL] Sending status-prime ('st 01')...")
        self.d4.send_socket_data(STATUS_PRIME_CMD)

        rx = self.d4.receive_socket_data(timeout_sec=5.0)
        logger.debug("[CTRL] Prime response: %s", rx[:32])

        if not rx.startswith(STATUS_PRIME_EXPECTED) and b"@BDC ST2" not in rx:
            raise ProtocolError(f"Status-prime failed: expected '@BDC ST2', got {rx[:32]!r}")

        logger.info("[CTRL] Status-prime successful (@BDC ST2 received).")
        return rx

    def read_eeprom(self, address: int) -> int:
        """
        Read a single byte from EEPROM address.
        Frame: 7C 7C 07 00 97 07 41 BE A0 ADDR_LO ADDR_HI
        """
        addr_lo = address & 0xFF
        addr_hi = (address >> 8) & 0xFF
        payload_len = 7

        packet = (
            b"\x7C\x7C"
            + bytes([payload_len, 0x00])
            + FACTORY_MODEL_CODE
            + ACTION_READ
            + bytes([addr_lo, addr_hi])
        )

        logger.debug("[CTRL] Read EEPROM 0x%04X: TX %s", address, packet.hex())
        self.d4.send_socket_data(packet)

        rx = self.d4.receive_socket_data(timeout_sec=5.0)
        logger.debug("[CTRL] Read EEPROM 0x%04X: RX %s", address, rx)

        # Expected format: @BDC PS\r\n... or EE:003070;0C or hex at offset 16:18
        if b"EE:" in rx:
            # Pattern EE:<addr_4hex><val_2hex>;
            match = re.search(rb"EE:[0-9A-Fa-f]{4}([0-9A-Fa-f]{2});", rx)
            if match:
                val = int(match.group(1), 16)
                logger.info("[EEPROM] Read 0x%04X = 0x%02X (%d)", address, val, val)
                return val

        if rx.startswith(b"@BDC PS\r\n") and len(rx) >= 18:
            try:
                val = int(rx[16:18], 16)
                logger.info("[EEPROM] Read 0x%04X = 0x%02X (%d)", address, val, val)
                return val
            except ValueError:
                pass

        raise ProtocolError(f"Failed to parse EEPROM read response for 0x{address:04X}: {rx!r}")

    def write_eeprom(self, address: int, value: int, verify_ack: bool = True) -> bytes:
        """
        Write a single byte to EEPROM address with golden write key 'Nbsjcbzb'.
        Frame: 7C 7C 10 00 97 07 42 BD 21 ADDR_LO ADDR_HI VALUE <KEY[8]>
        Expected ACK: '||:42:OK;'
        """
        addr_lo = address & 0xFF
        addr_hi = (address >> 8) & 0xFF
        payload_len = 0x10  # 16 bytes

        packet = (
            b"\x7C\x7C"
            + bytes([payload_len, 0x00])
            + FACTORY_MODEL_CODE
            + ACTION_WRITE
            + bytes([addr_lo, addr_hi, value & 0xFF])
            + L5190_WIRE_KEY
        )

        logger.info("[CTRL] Write EEPROM 0x%04X = 0x%02X (%d)", address, value, value)
        logger.debug("[CTRL] TX Packet: %s", packet.hex())
        self.d4.send_socket_data(packet)

        rx = self.d4.receive_socket_data(timeout_sec=5.0)
        logger.debug("[CTRL] Write RX: %s", rx)

        if verify_ack:
            if WRITE_ACK_EXPECTED not in rx:
                msg = f"EEPROM Write to 0x{address:04X} refused: expected '||:42:OK;', received {rx!r}"
                logger.error("[CTRL] %s", msg)
                raise VerificationError(msg)

        logger.info("[CTRL] ACK received for 0x%04X: ||:42:OK;", address)
        return rx
