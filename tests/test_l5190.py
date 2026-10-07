import unittest
from unittest.mock import MagicMock

from ez_reset.exceptions import VerificationError
from ez_reset.l5190.constants import (
    FACTORY_MODEL_CODE,
    GOLDEN_CHECK_SEQUENCE,
    GOLDEN_INITIALIZE_SEQUENCE,
    L5190_WIRE_KEY,
    MAIN_PAD_MAX,
    WRITE_ACK_EXPECTED,
)
from ez_reset.l5190.ctrl import EpsonCtrlL5190
from ez_reset.l5190.port_resolver import ResolvedPort
from ez_reset.l5190.workflow import L5190Workflow


class MockD4Session:
    def __init__(self, initial_eeprom: dict[int, int] | None = None) -> None:
        self.eeprom = initial_eeprom if initial_eeprom is not None else {
            0x0030: 0x70,
            0x0031: 0x15,  # 0x1570 = 5488 (86.48%)
            0x0032: 0x00,
            0x0033: 0x00,
            0x002F: 0x00,
        }
        self.sent_packets: list[bytes] = []
        self.simulate_ack_failure = False

    def send_socket_data(self, packet: bytes) -> None:
        self.sent_packets.append(packet)

    def receive_socket_data(self, timeout_sec: float = 5.0) -> bytes:
        if not self.sent_packets:
            return b""

        last_tx = self.sent_packets[-1]

        # Status prime ("st 01")
        if last_tx == b"\x73\x74\x01\x00\x01":
            return b"@BDC ST2\r\n\x00\x01\x04"

        # EEPROM Command (starts with 7C 7C)
        if last_tx.startswith(b"\x7C\x7C"):
            # Check if read or write
            if b"\x41\xBE\xA0" in last_tx:  # Read
                # Offset: 7C 7C (2) + len (2) + 97 07 (2) + 41 BE A0 (3) = 9
                addr = last_tx[9] | (last_tx[10] << 8)
                val = self.eeprom.get(addr, 0x00)
                return f"@BDC PS\r\nEE:{addr:04X}{val:02X};0C".encode("ascii")

            elif b"\x42\xBD\x21" in last_tx:  # Write
                # Offset: 7C 7C (2) + len (2) + 97 07 (2) + 42 BD 21 (3) = 9
                addr = last_tx[9] | (last_tx[10] << 8)
                val = last_tx[11]
                if self.simulate_ack_failure:
                    return b"||:42:NG;\r\n"
                self.eeprom[addr] = val
                return b"||:42:OK;\r\n"

        return b""


class TestL5190Engine(unittest.TestCase):
    def test_golden_constants_contract(self):
        # 1. Wire key is Nbsjcbzb (4E 62 73 6A 63 62 7A 62)
        self.assertEqual(L5190_WIRE_KEY, b"\x4E\x62\x73\x6A\x63\x62\x7A\x62")
        self.assertEqual(L5190_WIRE_KEY.decode("ascii"), "Nbsjcbzb")

        # 2. Model code is 97 07
        self.assertEqual(FACTORY_MODEL_CODE, b"\x97\x07")

        # 3. Exactly 11 writes and 2 reads in Golden Initialize Sequence
        writes = [step for step in GOLDEN_INITIALIZE_SEQUENCE if step[0] == "WRITE"]
        reads = [step for step in GOLDEN_INITIALIZE_SEQUENCE if step[0] == "READ"]
        self.assertEqual(len(writes), 11)
        self.assertEqual(len(reads), 2)
        self.assertEqual(len(GOLDEN_INITIALIZE_SEQUENCE), 13)

        # 4. Strictly NO W0100 in sequence
        written_addrs = [step[1] for step in writes]
        self.assertNotIn(0x0100, written_addrs)

        # 5. Last write is strictly 0x001C
        self.assertEqual(writes[-1][1], 0x001C)

    def test_port_resolver_rejection_rules(self):
        # Fake FAX interface: MI_03
        fax_port = ResolvedPort(
            device_path="\\\\?\\usb#vid_04b8&pid_114d&mi_03#...#{28d78fad...}",
            vid=0x04B8,
            pid=0x114D,
            mi="MI_03",
            port_number=39,
            logical_port="USB039",
            is_printer_interface=False,
            is_fax_interface=True,
        )
        self.assertTrue(fax_port.is_fax_interface)
        self.assertFalse(fax_port.is_printer_interface)

        # Fake PRINTER interface: MI_01
        printer_port = ResolvedPort(
            device_path="\\\\?\\usb#vid_04b8&pid_114d&mi_01#...#{28d78fad...}",
            vid=0x04B8,
            pid=0x114D,
            mi="MI_01",
            port_number=38,
            logical_port="USB038",
            is_printer_interface=True,
            is_fax_interface=False,
        )
        self.assertTrue(printer_port.is_printer_interface)
        self.assertFalse(printer_port.is_fax_interface)
        self.assertEqual(printer_port.logical_port, "USB038")

    def test_workflow_check(self):
        mock_d4 = MockD4Session()
        ctrl = EpsonCtrlL5190(mock_d4)
        mock_transport = MagicMock()
        workflow = L5190Workflow(ctrl, mock_d4, mock_transport)

        result = workflow.run_check()
        # Initial mock has 0x1570 = 5488
        self.assertEqual(result.main_pad_count, 5488)
        self.assertAlmostEqual(result.main_pad_pct, 5488 / MAIN_PAD_MAX * 100, places=2)
        self.assertEqual(result.platen_pad_count, 0)

        # Verify status-prime was executed
        self.assertEqual(mock_d4.sent_packets[0], b"\x73\x74\x01\x00\x01")

    def test_workflow_golden_initialize(self):
        mock_d4 = MockD4Session()
        ctrl = EpsonCtrlL5190(mock_d4)
        mock_transport = MagicMock()
        workflow = L5190Workflow(ctrl, mock_d4, mock_transport)

        init_res = workflow.run_initialize()
        self.assertTrue(init_res.success)
        self.assertEqual(init_res.write_count, 11)
        self.assertEqual(init_res.ack_count, 11)
        self.assertEqual(init_res.guard_read_count, 2)

        # Verify mock EEPROM values after exact initialize
        self.assertEqual(mock_d4.eeprom[0x0030], 0x00)
        self.assertEqual(mock_d4.eeprom[0x0031], 0x00)
        self.assertEqual(mock_d4.eeprom[0x0032], 0x00)
        self.assertEqual(mock_d4.eeprom[0x0033], 0x00)
        self.assertEqual(mock_d4.eeprom[0x0036], 0x5E)
        self.assertEqual(mock_d4.eeprom[0x0037], 0x5E)
        self.assertEqual(mock_d4.eeprom[0x001C], 0x00)

        # Run fresh check: Main Pad must now be 0%!
        check_after = workflow.run_check()
        self.assertEqual(check_after.main_pad_count, 0)
        self.assertEqual(check_after.main_pad_pct, 0.0)

    def test_workflow_initialize_rejection_detection(self):
        mock_d4 = MockD4Session()
        mock_d4.simulate_ack_failure = True
        ctrl = EpsonCtrlL5190(mock_d4)
        mock_transport = MagicMock()
        workflow = L5190Workflow(ctrl, mock_d4, mock_transport)

        # If any write gets :42:NG; instead of :42:OK;, VerificationError must be raised immediately
        with self.assertRaises(VerificationError):
            workflow.run_initialize()


if __name__ == "__main__":
    unittest.main()
