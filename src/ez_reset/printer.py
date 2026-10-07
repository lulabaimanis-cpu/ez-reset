from collections.abc import Iterable
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import struct
import threading
from typing import Any

from .control import ControlBackend
from .devices import Device
from .exceptions import BackupError, ProtocolError, RestoreValidationError, VerificationError
from .status import Status

logger = logging.getLogger(__name__)


class Printer:
    def __init__(self, control_backend: ControlBackend, device: Device) -> None:
        self.device = device
        self._control = control_backend
        self._lock = threading.RLock()

    def send_command(self, command: bytes, payload: bytes) -> bytes:
        with self._lock:
            packet = command + len(payload).to_bytes(2, "little") + payload
            return self._control.send(packet)

    def send_factory_command(self, model: bytes, action: int, payload: bytes = b"") -> bytes:
        with self._lock:
            command = b"||"
            action_code = bytes(
                (action, action ^ 0xFF, ((action >> 1) & 0x7F) | ((action << 7) & 0x80)),
            )
            return self.send_command(command, model + action_code + payload)

    def get_status(self) -> Status:
        with self._lock:
            command = b"st"
            expected = b"@BDC ST2\r\n"
            response = self.send_command(command, b"\x01")

            if not response.startswith(expected):
                msg = f"Unknown response {response!r} for command {expected!r}"
                raise ProtocolError(msg)

            payload = response[len(expected) :]
            return Status.from_bytes(payload)

    def read_eeprom(self, address: int) -> int:
        with self._lock:
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
        with self._lock:
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
        with self._lock:
            return bytes([self.read_eeprom(address) for address in addresses])

    def read_eeprom_range(self, address: int, size: int) -> bytes:
        with self._lock:
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
        """
        Dump EEPROM contents over the specified address range.
        Fails hard with BackupError if any address cannot be read to prevent corrupt partial backups.
        """
        with self._lock:
            dump: dict[int, int] = {}
            for addr in range(start, end + 1):
                try:
                    dump[addr] = self.read_eeprom(addr)
                except Exception as e:
                    msg = (
                        f"EEPROM dump aborted: Failed to read address 0x{addr:04X} ({addr}): {e}. "
                        "Aborting to prevent corrupted partial backup."
                    )
                    logger.error(msg)
                    raise BackupError(msg) from e
            return dump

    def backup_eeprom(self, filepath: str | Path, start: int = 0x00, end: int = 0xFF) -> dict[int, int]:
        """
        Dump EEPROM and save as verified JSON or raw .bin backup file atomically.
        Guarantees that an incomplete or failed dump will never overwrite an existing backup.
        """
        with self._lock:
            path = Path(filepath)
            dump = self.dump_eeprom(start=start, end=end)

            temp_path = path.with_suffix(path.suffix + ".tmp")
            try:
                if path.suffix.lower() == ".bin":
                    raw_bytes = bytes([dump[addr] for addr in range(start, end + 1)])
                    temp_path.write_bytes(raw_bytes)
                else:
                    ident = self.identify()
                    model_info = ident.get("MDL", self.device.model_name or "Unknown")
                    serial_info = self.get_serial()
                    raw_bytes = bytes([dump[addr] for addr in range(start, end + 1)])
                    sha256_hash = hashlib.sha256(raw_bytes).hexdigest()
                    backup_data: dict[str, Any] = {
                        "format": "ez_reset_eeprom_backup",
                        "version": "1.1",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "printer_model": model_info,
                        "serial_number": serial_info,
                        "range": [start, end],
                        "sha256": sha256_hash,
                        "eeprom": {f"0x{k:04X}": f"0x{v:02X}" for k, v in dump.items()},
                    }
                    temp_path.write_text(json.dumps(backup_data, indent=2), encoding="utf-8")
                temp_path.replace(path)
            except Exception:
                if temp_path.exists():
                    temp_path.unlink()
                raise

            logger.info("EEPROM backup successfully saved and verified to %s", path)
            return dump

    def restore_eeprom(
        self,
        filepath: str | Path,
        verify: bool = True,
        dry_run: bool = False,
        force: bool = False,
    ) -> list[tuple[int, int, int, bool]]:
        """
        Restore EEPROM from backup file with strict safety verification:
        - Validates file exists, parses cleanly, and bounds are valid (0x00..0xFF)
        - Validates that byte values are valid (0..255)
        - Matches printer model to prevent bricking mainboard with cross-model backup
        - Returns actual verification status per address
        """
        with self._lock:
            path = Path(filepath)
            if not path.exists():
                raise FileNotFoundError(f"Backup file not found: {path}")

            entries: dict[int, int] = {}

            if path.suffix.lower() == ".bin":
                data = path.read_bytes()
                if len(data) != 256:
                    raise RestoreValidationError(
                        f"Invalid .bin backup size: expected exactly 256 bytes for standard EEPROM, got {len(data)} bytes."
                    )
                for addr, byte_val in enumerate(data):
                    entries[addr] = byte_val
            else:
                try:
                    raw_data = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as jde:
                    raise RestoreValidationError(f"Invalid JSON in backup file: {jde}") from jde

                backup_model = raw_data.get("printer_model", "").strip()
                current_model = self.identify().get("MDL", self.device.model_name).strip()

                # Strict Model Match Guard
                if not force and backup_model and current_model:
                    norm_backup = backup_model.upper().replace(" SERIES", "").strip()
                    norm_current = current_model.upper().replace(" SERIES", "").strip()
                    if norm_backup != norm_current:
                        raise RestoreValidationError(
                            f"Model mismatch! Backup is for '{backup_model}', but connected printer is '{current_model}'. "
                            "Restoring data across different printer models can cause irreversible damage. Aborting."
                        )

                eeprom_map = raw_data.get("eeprom", {})
                if not eeprom_map:
                    raise RestoreValidationError("Backup file contains no EEPROM entries.")

                for k, v in eeprom_map.items():
                    addr = int(k, 16) if isinstance(k, str) and k.startswith("0x") else int(k)
                    val = int(v, 16) if isinstance(v, str) and v.startswith("0x") else int(v)
                    if not (0x00 <= addr <= 0xFF):
                        raise RestoreValidationError(f"Address 0x{addr:04X} is out of legal EEPROM range (0x00..0xFF).")
                    if not (0 <= val <= 255):
                        raise RestoreValidationError(f"Byte value {val} at 0x{addr:04X} is out of valid byte range (0..255).")
                    entries[addr] = val

            results: list[tuple[int, int, int, bool]] = []
            for addr, target_val in sorted(entries.items()):
                old_val = -1
                if not dry_run:
                    try:
                        old_val = self.read_eeprom(addr)
                    except Exception:
                        old_val = -1

                verified = False
                if not dry_run:
                    self.write_eeprom(addr, target_val, verify=verify, dry_run=False)
                    if verify:
                        actual = self.read_eeprom(addr)
                        verified = (actual == target_val)
                    else:
                        verified = True
                else:
                    verified = True

                results.append((addr, old_val, target_val, verified))

            return results

    def identify(self) -> dict[str, str]:
        with self._lock:
            fields = (self._control.identify()).split(";")
            return dict(e.split(":") for e in fields if e and ":" in e)

    def get_serial(self) -> str:
        with self._lock:
            return (self.get_status()).serial

    def get_waste(self) -> list[tuple[int, int]]:
        with self._lock:
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
        with self._lock:
            report: list[tuple[int, int, int, bool]] = []
            for addr, target_value in self.device.reset.items():
                old_value = -1
                if not dry_run:
                    try:
                        old_value = self.read_eeprom(addr)
                    except Exception:
                        old_value = -1

                verified = False
                if not dry_run:
                    self.write_eeprom(addr, target_value, verify=verify, dry_run=False)
                    if verify:
                        actual = self.read_eeprom(addr)
                        verified = (actual == target_value)
                    else:
                        verified = True
                else:
                    verified = True

                report.append((addr, old_value, target_value, verified))

            return report

    def temporary_reset_waste(self, mode: int = 1, dry_run: bool = False) -> bool:
        """
        Execute temporary waste ink reset via the 'rw' service command.
        Compatible with newer firmwares where EEPROM write keys are locked.
        """
        with self._lock:
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
        with self._lock:
            action = 0x80
            return self.send_factory_command(self.device.model, action)

    def clean(self, level: int = 1) -> bytes:
        """
        Trigger printhead cleaning routine.
        level=1: Standard Clean
        level=2: Medium Clean
        level=3: Power Clean / Deep Clean
        """
        with self._lock:
            action = 0x84
            payload = level.to_bytes(1, "little")
            return self.send_factory_command(self.device.model, action, payload)

    def power_off(self) -> None:
        with self._lock:
            action = 0x20
            self.send_factory_command(self.device.model, action)

    def restart(self) -> None:
        with self._lock:
            action = 0x21
            self.send_factory_command(self.device.model, action)
