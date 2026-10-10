"""
Layer E: L5190Workflow for Epson L5190 (Family: L5JX).

Features:
- Exact Check Sequence: [R002F, R0030, R0031, R002F, R0032, R0033].
- Golden Initialize Sequence: 11 Writes + 2 Guard Reads (002F), NO W0100, NO write skipping.
- Power-Cycle Boundary State Machine: Keeps session alive until device power-off,
  cleans up on disappearance, awaits power-on, opens fresh session, verifies target state.
"""

from dataclasses import dataclass
import logging
import time
from typing import Callable

from .constants import (
    ADDR_MAIN_HIGH,
    ADDR_MAIN_LOW,
    ADDR_PLATEN_HIGH,
    ADDR_PLATEN_LOW,
    GOLDEN_CHECK_SEQUENCE,
    GOLDEN_INITIALIZE_SEQUENCE,
    MAIN_PAD_MAX,
)
from .ctrl import EpsonCtrlL5190
from .port_resolver import resolve_l5190_printer
from .transport import ApdaTransport, D4Session
from ..exceptions import DeviceError, VerificationError

logger = logging.getLogger("ez_reset.l5190.workflow")


@dataclass
class CheckResult:
    main_pad_count: int
    main_pad_max: int
    main_pad_pct: float
    platen_pad_count: int
    guard_2f: int
    raw_readings: dict[int, int]


@dataclass
class InitializeResult:
    write_count: int
    ack_count: int
    guard_read_count: int
    steps_log: list[str]
    success: bool


class L5190Workflow:
    def __init__(self, ctrl: EpsonCtrlL5190, d4: D4Session, transport: ApdaTransport) -> None:
        self.ctrl = ctrl
        self.d4 = d4
        self.transport = transport

    def run_check(self) -> CheckResult:
        """
        Execute exact Check sequence:
        D4 READY -> socket 02/02 -> st 01 -> R002F, R0030, R0031, R002F, R0032, R0033
        """
        logger.info("[WORKFLOW] Starting L5190 Check sequence...")
        self.ctrl.status_prime()

        readings: dict[int, int] = {}
        for addr in GOLDEN_CHECK_SEQUENCE:
            val = self.ctrl.read_eeprom(addr)
            readings[addr] = val

        main_count = readings[ADDR_MAIN_LOW] | (readings[ADDR_MAIN_HIGH] << 8)
        platen_count = readings[ADDR_PLATEN_LOW] | (readings[ADDR_PLATEN_HIGH] << 8)
        main_pct = (main_count / MAIN_PAD_MAX) * 100.0 if MAIN_PAD_MAX > 0 else 0.0

        logger.info(
            "[WORKFLOW] Main Pad: %d / %d (%0.2f%%), Platen: %d",
            main_count,
            MAIN_PAD_MAX,
            main_pct,
            platen_count,
        )

        return CheckResult(
            main_pad_count=main_count,
            main_pad_max=MAIN_PAD_MAX,
            main_pad_pct=main_pct,
            platen_pad_count=platen_count,
            guard_2f=readings.get(0x002F, 0x00),
            raw_readings=readings,
        )

    def run_initialize(self) -> InitializeResult:
        """
        Execute Golden Initialize Sequence (11 Writes + 2 Guard Reads).
        Strict contract:
        - 11 Writes
        - 11 ACKs (||:42:OK;)
        - 2 Guard Reads of 002F
        - 2 Writes of 002F
        - 0 W0100 (NO COMMIT BYTE)
        - NO write skipping if before == target.
        """
        logger.info("[WORKFLOW] Starting L5190 Golden Initialize sequence...")
        self.ctrl.status_prime()

        write_count = 0
        ack_count = 0
        guard_reads = 0
        steps: list[str] = []

        for action, addr, val, desc in GOLDEN_INITIALIZE_SEQUENCE:
            if action == "WRITE":
                write_count += 1
                self.ctrl.write_eeprom(addr, val, verify_ack=True)
                ack_count += 1
                msg = f"ACK {ack_count:02d}/11 : {desc} -> ||:42:OK;"
                logger.info("[INITIALIZE] %s", msg)
                steps.append(msg)
            elif action == "READ":
                guard_reads += 1
                r_val = self.ctrl.read_eeprom(addr)
                msg = f"GUARD {guard_reads}/2 : R{addr:04X} = 0x{r_val:02X}"
                logger.info("[INITIALIZE] %s", msg)
                steps.append(msg)

        if write_count != 11 or ack_count != 11 or guard_reads != 2:
            raise VerificationError(
                f"Protocol conformance failure: expected 11 writes/ACKs and 2 guard reads, "
                f"got {write_count} writes, {ack_count} ACKs, {guard_reads} reads."
            )

        logger.info("[WORKFLOW] INITIALIZE ACKNOWLEDGED (11/11 ACKs). Waiting for power-cycle.")
        return InitializeResult(
            write_count=write_count,
            ack_count=ack_count,
            guard_read_count=guard_reads,
            steps_log=steps,
            success=True,
        )

    def execute_power_cycle_and_verify(
        self,
        prompt_power_off_cb: Callable[[], None],
        prompt_power_on_cb: Callable[[], None],
        poll_interval: float = 1.0,
        timeout_sec: float = 60.0,
    ) -> CheckResult:
        """
        Manage Level C power-cycle boundary:
        1. Keep session alive.
        2. Prompt user to turn off printer.
        3. Detect device disappearance.
        4. Release session.
        5. Prompt user to turn on printer.
        6. Detect device reappearance.
        7. Open fresh session -> fresh st 01 -> fresh Check.
        8. Verify Main Pad = 0%.
        """
        logger.info("[POWER-CYCLE] Prompting user to POWER OFF printer...")
        prompt_power_off_cb()

        # Step 1: Wait for device to go offline
        logger.info("[POWER-CYCLE] Waiting for printer to turn OFF (device disappearance)...")
        start = time.monotonic()
        device_gone = False
        while time.monotonic() - start < timeout_sec:
            try:
                # ADGetDeviceID should fail once power is cut
                self.transport.get_device_id()
                time.sleep(poll_interval)
            except Exception:
                logger.info("[POWER-CYCLE] Device offline detected.")
                device_gone = True
                break

        if not device_gone:
            self.transport.close()
            raise DeviceError("Timeout waiting for printer to turn OFF. Device remained online.")

        # Step 2: Release old session
        self.transport.close()
        logger.info("[POWER-CYCLE] Old session released.")

        # Step 3: Prompt user to power back on
        prompt_power_on_cb()

        # Step 4: Wait for device reappearance
        logger.info("[POWER-CYCLE] Waiting for printer to turn ON (device reappearance)...")
        start = time.monotonic()
        reappeared_port = None
        while time.monotonic() - start < timeout_sec:
            resolved = resolve_l5190_printer()
            if resolved is not None:
                reappeared_port = resolved
                logger.info("[POWER-CYCLE] Device reappeared at %s", resolved.device_path)
                break
            time.sleep(poll_interval)

        if reappeared_port is None:
            raise DeviceError("Timeout waiting for printer to turn back ON.")

        # Small settling delay
        time.sleep(2.0)

        # Step 5: Open fresh session
        logger.info("[POWER-CYCLE] Opening fresh session on %s...", reappeared_port.device_path)
        with ApdaTransport(reappeared_port.device_path) as fresh_transport:
            fresh_d4 = D4Session(fresh_transport)
            fresh_d4.enter()
            fresh_d4.init()
            fresh_d4.open_socket_02_02()

            fresh_ctrl = EpsonCtrlL5190(fresh_d4)
            fresh_workflow = L5190Workflow(fresh_ctrl, fresh_d4, fresh_transport)

            # Step 6: Fresh check
            check_res = fresh_workflow.run_check()
            if check_res.main_pad_count == 0:
                logger.info("[VERIFY] SUCCESS: Main Pad reset verified at 0%!")
            else:
                logger.warning(
                    "[VERIFY] Post-cycle main pad reading is %d (%0.2f%%)",
                    check_res.main_pad_count,
                    check_res.main_pad_pct,
                )

            return check_res
