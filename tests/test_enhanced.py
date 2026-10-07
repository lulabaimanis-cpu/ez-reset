import json
from pathlib import Path
import tempfile
import unittest

from ez_reset.control import ControlBackend
from ez_reset.devices import by_model, get_all_models
from ez_reset.exceptions import DeviceError, VerificationError
from ez_reset.printer import Printer


class MockControlBackend(ControlBackend):
    def __init__(self, serial: str = "VA9K012345", eeprom_map: dict[int, int] | None = None) -> None:
        self.serial = serial
        self.eeprom = eeprom_map if eeprom_map is not None else {0x18: 0x00, 0x19: 0x00, 0x20: 0x00}
        self.commands_sent: list[bytes] = []
        self.simulate_write_refusal = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def identify(self) -> str:
        return "MFG:EPSON;CMD:ESCPL2,BDC,D4,END4;MDL:L3250 Series;CLS:PRINTER;DES:EPSON L3250 Series;"

    def send(self, command: bytes) -> bytes:
        self.commands_sent.append(command)
        cmd_type = command[:2]

        if cmd_type == b"st":
            body = bytearray([0x01, 0x01, 0x04, 0x02, 0x01, 0x00])
            serial_bytes = self.serial.encode("ascii")
            body.extend([0x40, len(serial_bytes)] + list(serial_bytes))
            length_bytes = len(body).to_bytes(2, "little")
            return b"@BDC ST2\r\n" + length_bytes + bytes(body)

        elif cmd_type == b"||":
            payload_data = command[4:]
            if b"A" in payload_data[:6]:  # Read EEPROM
                addr_idx = payload_data.find(b"A") + 3
                addr = int.from_bytes(payload_data[addr_idx : addr_idx + 2], "little")
                val = self.eeprom.get(addr, 0x00)
                hex_str = f"{val:02X}".encode("ascii")
                # b"@BDC PS\r\n" is 9 bytes. We need 7 padding bytes so hex_str is at index 16:18.
                return b"@BDC PS\r\n" + b"1234567" + hex_str + b"\r\n"

            elif b"B" in payload_data[:6]:  # Write EEPROM
                addr_idx = payload_data.find(b"B") + 3
                addr = int.from_bytes(payload_data[addr_idx : addr_idx + 2], "little")
                val = payload_data[addr_idx + 2]
                if not self.simulate_write_refusal:
                    self.eeprom[addr] = val
                return b"@BDC PS\r\n:OK;\r\n"

            return b"@BDC PS\r\n:OK;\r\n"

        elif cmd_type == b"rw":
            return b"rw:01:OK;\r\n"

        return b"@BDC OK\r\n"


class TestEnhancedEzReset(unittest.TestCase):
    def test_devices_by_model(self):
        models = get_all_models()
        self.assertGreater(len(models), 100)

        dev = by_model("XP-205")
        self.assertNotEqual(dev.model, b"")
        self.assertGreater(len(dev.counters), 0)

        dev_l3250 = by_model("L3250 Series")
        self.assertNotEqual(dev_l3250.model_name, "")

    def test_printer_read_and_write_verified(self):
        backend = MockControlBackend()
        dev = by_model("XP-205")
        printer = Printer(backend, dev)

        printer.write_eeprom(0x20, 0x42, verify=True)
        self.assertEqual(backend.eeprom[0x20], 0x42)

        backend.simulate_write_refusal = True
        with self.assertRaises(VerificationError):
            printer.write_eeprom(0x20, 0x99, verify=True)

    def test_printer_dry_run(self):
        backend = MockControlBackend()
        dev = by_model("XP-205")
        printer = Printer(backend, dev)

        initial_val = backend.eeprom.get(0x20, 0x00)
        printer.write_eeprom(0x20, 0xFF, verify=True, dry_run=True)
        self.assertEqual(backend.eeprom.get(0x20), initial_val)

    def test_printer_temporary_reset(self):
        backend = MockControlBackend(serial="X123456789")
        dev = by_model("XP-205")
        printer = Printer(backend, dev)

        success = printer.temporary_reset_waste()
        self.assertTrue(success)
        self.assertTrue(any(cmd.startswith(b"rw") for cmd in backend.commands_sent))

    def test_eeprom_backup_and_restore(self):
        backend = MockControlBackend(eeprom_map={0x00: 0x10, 0x01: 0x20, 0x02: 0x30})
        dev = by_model("XP-205")
        printer = Printer(backend, dev)

        with tempfile.TemporaryDirectory() as tmpdir:
            backup_file = Path(tmpdir) / "backup.json"
            dump = printer.backup_eeprom(backup_file, start=0x00, end=0x02)
            self.assertTrue(backup_file.exists())

            content = json.loads(backup_file.read_text())
            self.assertEqual(content["eeprom"]["0x0000"], "0x10")
            self.assertEqual(content["eeprom"]["0x01"], "0x20") if "0x01" in content["eeprom"] else self.assertEqual(content["eeprom"]["0x0001"], "0x20")

            backend.eeprom[0x00] = 0x00
            backend.eeprom[0x01] = 0x00
            report = printer.restore_eeprom(backup_file, verify=True)
            self.assertEqual(len(report), 3)
            self.assertEqual(backend.eeprom[0x00], 0x10)
            self.assertEqual(backend.eeprom[0x01], 0x20)


if __name__ == "__main__":
    unittest.main()
