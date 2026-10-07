from collections.abc import Iterable
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import struct
from typing import Any

from .control import ControlBackend
from .devices import Device
from .exceptions import ProtocolError, VerificationError
from .status import Status

logger = logging.getLogger(__name__)


class Printer:
    def __init__(self, control_backend: ControlBackend, device: Device) -> None:
        self.device = device
        self._control = control_backend

    def send_command(self, command: bytes, payload: bytes) -> bytes:
        packet = command + len(payload).to_bytes(2, "little") + payload
        return self._control.send(packet)

    def send_factory_command(self, model: bytes, action: int, payload: bytes = b"") -> bytes:
        command = b"||"
        action_code = bytes(
            (action, action ^ 0xFF, ((action >> 1) & 0x7F) | ((action << 7) & 0x80)),
        )
        return self.send_command(command, model + action_code + payload)

    def get_status(self) -> Status:
        command = b"st"
        expected = b"@BDC ST2\r\n"
        response = self.send_command(command, b"\x01")

        if not response.startswith(expected):
            msg = f"Unknown response {response!r} for command {expected!r}"
            raise ProtocolError(msg)

        payload = response[len(expected) :]
        return Status.from_bytes(payload)

    def read_eeprom(self, address: int) -> int:
        action = 0x41
        expected = b"@BDC PS\r\n"
        response = self.send_factory_command(
            self.device.model,
            action,
            address.to_bytes(2, "little"),
        )

        if not response.startswith(expected):
            msg = f"Unknown response {response!r} for command {expected!r}"
            raise ProtocolError(msg)

        return int(response[16:18], base=16)

    def write_eeprom(
        self,
        address: int,
        value: int,
        verify: bool = True,
        dry_run: bool = False,
    ) -> bytes:
        """Write a single byte to EEPROM with optional read-back verification and dry-run guard."""
        if dry_run:
            logger.info("[DRY-RUN] Would write EEPROM 0x%04X = 0x%02X (%d)", address, value, value)
            return b"@BDC PS\r\n:DRY_RUN:OK;"

        action = 0x42
        payload = address.to_bytes(2, "little") + value.to_bytes(1, "little") + self.device.key
        response = self.send_factory_command(self.device.model, action, payload)

        if verify:
            actual_value = self.read_eeprom(address)
            if actual_value != value:
                msg = (
                    f"EEPROM write verification failed at address 0x{address:04X} ({address}): "
                    f"wrote 0x{value:02X} ({value}), but read back 0x{actual_value:02X} ({actual_value}). "
                    "Firmware may have silently rejected the write or write-key is mismatched."
                )
                logger.error(msg)
                raise VerificationError(msg)

        return response

    def read_eeprom_multiple(self, addresses: Iterable[int]) -> bytes:
        return bytes([self.read_eeprom(address) for address in addresses])

    def read_eeprom_range(self, address: int, size: int) -> bytes:
        action = 0x51
        expected = b"@BDC PS\r\n"
        response = self.send_factory_command(
            self.device.model,
            action,
            address.to_bytes(2, "little") + size.to_bytes(1, "little"),
        )

        if not response.startswith(expected):
            msg = f"Unknown response {response!r} for command {expected!r}"
            raise ProtocolError(msg)

        return bytes.fromhex(response[16 : 16 + size * 2].decode("ascii"))

    def dump_eeprom(self, start: int = 0x00, end: int = 0xFF) -> dict[int, int]:
        """Dump EEPROM contents over the specified address range."""
        dump: dict[int, int] = {}
        for addr in range(start, end + 1):
            try:
                dump[addr] = self.read_eeprom(addr)
            except Exception as e:
                logger.warning("Failed to read EEPROM address 0x%04X: %s", addr, e)
        return dump

    def backup_eeprom(self, filepath: str | Path, start: int = 0x00, end: int = 0xFF) -> dict[int, int]:
        """Dump EEPROM and save as safe JSON or raw .bin backup file."""
        path = Path(filepath)
        dump = self.dump_eeprom(start=start, end=end)

        if path.suffix.lower() == ".bin":
            raw_bytes = bytes([dump.get(addr, 0x00) for addr in range(start, end + 1)])
            path.write_bytes(raw_bytes)
        else:
            model_info = self.identify().get("MDL", "Unknown")
            serial_info = self.get_serial()
            backup_data: dict[str, Any] = {
                "format": "ez_reset_eeprom_backup",
                "version": "1.0",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "printer_model": model_info,
                "serial_number": serial_info,
                "range": [start, end],
                "eeprom": {f"0x{k:04X}": f"0x{v:02X}" for k, v in dump.items()},
            }
            path.write_text(json.dumps(backup_data, indent=2), encoding="utf-8")

        logger.info("EEPROM backup successfully saved to %s", path)
        return dump

    def restore_eeprom(
        self,
        filepath: str | Path,
        verify: bool = True,
        dry_run: bool = False,
    ) -> list[tuple[int, int, int, bool]]:
        """Restore EEPROM from backup file with safety verification."""
        path = Path(filepath)
        entries: dict[int, int] = {}

        if path.suffix.lower() == ".bin":
            data = path.read_bytes()
            for addr, byte_val in enumerate(data):
                entries[addr] = byte_val
        else:
            raw_data = json.loads(path.read_text(encoding="utf-8"))
            eeprom_map = raw_data.get("eeprom", {})
            for k, v in eeprom_map.items():
                addr = int(k, 16) if isinstance(k, str) and k.startswith("0x") else int(k)
                val = int(v, 16) if isinstance(v, str) and v.startswith("0x") else int(v)
                entries[addr] = val

        results: list[tuple[int, int, int, bool]] = []
        for addr, target_val in sorted(entries.items()):
            old_val = -1
            if not dry_run:
                try:
                    old_val = self.read_eeprom(addr)
                except Exception:
                    old_val = -1

            self.write_eeprom(addr, target_val, verify=verify, dry_run=dry_run)
            results.append((addr, old_val, target_val, True))

        return results

    def identify(self) -> dict[str, str]:
        fields = (self._control.identify()).split(";")
        return dict(e.split(":") for e in fields if e and ":" in e)

    def get_serial(self) -> str:
        return (self.get_status()).serial

    def get_waste(self) -> list[tuple[int, int]]:
        return [
            (
                int.from_bytes(self.read_eeprom_multiple(counter.addresses), "little"),
                counter.max,
            )
            for counter in self.device.counters
        ]

    def reset_waste(
        self,
        verify: bool = True,
        dry_run: bool = False,
    ) -> list[tuple[int, int, int, bool]]:
        """Reset waste ink counters defined for this model with verification and report."""
        report: list[tuple[int, int, int, bool]] = []
        for addr, target_value in self.device.reset.items():
            old_value = -1
            if not dry_run:
                try:
                    old_value = self.read_eeprom(addr)
                except Exception:
                    old_value = -1

            self.write_eeprom(addr, target_value, verify=verify, dry_run=dry_run)
            report.append((addr, old_value, target_value, True))

        return report

    def temporary_reset_waste(self, mode: int = 1, dry_run: bool = False) -> bool:
        """
        Execute temporary waste ink reset via the 'rw' service command.
        Compatible with newer firmwares where EEPROM write keys are locked.
        """
        serial = self.get_serial().strip()
        if not serial:
            raise ProtocolError("Cannot perform temporary reset: serial number unavailable.")

        if dry_run:
            logger.info("[DRY-RUN] Would execute temporary reset ('rw') for serial: %s", serial)
            return True

        sha1_hash = hashlib.sha1(serial.encode("ascii")).digest()
        payload = struct.pack("<H", mode) + sha1_hash
        response = self.send_command(b"rw", payload)

        return b":OK;" in response or response.startswith(b"rw:")

    def nozzle_check(self) -> bytes:
        """Trigger printhead nozzle check pattern."""
        action = 0x80
        return self.send_factory_command(self.device.model, action)

    def clean(self, level: int = 1) -> bytes:
        """
        Trigger printhead cleaning routine.
        level=1: Standard Clean
        level=2: Medium Clean
        level=3: Power Clean / Deep Clean
        """
        action = 0x84
        payload = level.to_bytes(1, "little")
        return self.send_factory_command(self.device.model, action, payload)

    def power_off(self) -> None:
        action = 0x20
        self.send_factory_command(self.device.model, action)

    def restart(self) -> None:
        action = 0x21
        self.send_factory_command(self.device.model, action)
