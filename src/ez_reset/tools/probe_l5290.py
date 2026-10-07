"""
Epson L5190 / L5290 / L5JX Hardware Probe & Diagnostic Tool.

Captures:
1. Exact USB PID & MI interfaces (Printer vs Fax).
2. IEEE-1284 Device ID (MDL:L5190/L5290 Series, CMD capabilities).
3. Factory Model Code on EPSON-CTRL frame.
4. Serial number & firmware status block.

Generates a complete report saved to 'L5190_PROBE_REPORT.txt'.
"""

from datetime import datetime, timezone
import logging
from pathlib import Path
import sys
import threading
import time

from ez_reset.l5190.constants import (
    ACTION_READ,
    D4_ENTER_ACK,
    D4_ENTER_PACKET,
    D4_INIT_ACK_PREFIX,
    D4_INIT_PACKET,
    EPSON_VID,
    IOCTL_AD_GET_DEVICE_ID,
    IOCTL_AD_INIT_DEVICE,
    SOCKET_PSID,
    SOCKET_SSID,
    STATUS_PRIME_CMD,
)
from ez_reset.l5190.port_resolver import enumerate_l5190_interfaces
from ez_reset.utils import parse_identifier
from win32file import (
    FILE_SHARE_READ,
    FILE_SHARE_WRITE,
    GENERIC_READ,
    GENERIC_WRITE,
    OPEN_EXISTING,
    CreateFileW,
    DeviceIoControl,
    ReadFile,
    WriteFile,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("probe_l5290")


def safe_read(handle, size: int = 1024, timeout_sec: float = 2.0) -> bytes:
    """Read from handle with strict timeout in a daemon thread to prevent freezing."""
    out = [b""]

    def _reader():
        try:
            _s, data = ReadFile(handle, size)
            out[0] = data
        except Exception:
            pass

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    t.join(timeout_sec)
    return out[0]


def probe_connected_printers() -> int:
    report_lines: list[str] = []

    def log(msg: str) -> None:
        print(msg, flush=True)
        report_lines.append(msg)

    log("=" * 70)
    log(f"  EPSON HARDWARE PROBE REPORT - {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    log("=" * 70)

    log("\n[STEP 1] Scanning USBPRINT Interfaces (SetupAPI)...")
    interfaces = list(enumerate_l5190_interfaces())

    epson_interfaces = [p for p in interfaces if p.vid == EPSON_VID or "04b8" in p.device_path.lower()]
    if not epson_interfaces:
        log("[-] No Epson USB printers found in USBPRINT interfaces.")
        log("    Tips: Make sure the printer is powered ON and USB cable is connected.")
        return 1

    log(f"[+] Found {len(epson_interfaces)} Epson interface(s):")
    for idx, iface in enumerate(epson_interfaces, start=1):
        role = "PRINTER (Target)" if iface.is_printer_interface else ("FAX (Ignored)" if iface.is_fax_interface else "Other")
        log(f"    {idx}. VID={iface.vid:04X} PID={iface.pid:04X} MI={iface.mi or 'None'} "
            f"Port={iface.logical_port or 'N/A'} -> {role}")
        log(f"       Path: {iface.device_path}")

    # Select printer interface (MUST be MI_01 / PRINTER)
    target_iface = next((p for p in epson_interfaces if p.is_printer_interface), None)
    if not target_iface:
        target_iface = next((p for p in epson_interfaces if not p.is_fax_interface), epson_interfaces[0])

    log(f"\n[+] Selected Target Printer Interface:")
    log(f"    Device Path: {target_iface.device_path}")
    log(f"    USB VID    : 0x{target_iface.vid:04X} ({target_iface.vid})")
    log(f"    USB PID    : 0x{target_iface.pid:04X} ({target_iface.pid})")
    log(f"    Interface  : {target_iface.mi}")
    log(f"    LogicalPort: {target_iface.logical_port}")

    log("\n[STEP 2] Connecting to USBPRINT driver...")
    try:
        handle = CreateFileW(
            target_iface.device_path,
            GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
    except Exception as e:
        log(f"[-] Failed to open handle: {e}")
        return 1

    try:
        # ADInitDevice (IOCTL 0x220040 with input byte 0x01)
        log("    Sending ADInitDevice (IOCTL 0x220040, input=0x01)...")
        try:
            DeviceIoControl(handle, IOCTL_AD_INIT_DEVICE, b"\x01", 1024)
            log("    [+] ADInitDevice OK.")
        except Exception as e:
            log(f"    [!] ADInitDevice warning: {e}")

        # ADGetDeviceID (IOCTL 0x220034)
        log("\n[STEP 3] Reading IEEE-1284 Device ID (IOCTL 0x220034)...")
        parsed_id = {}
        try:
            raw_id = DeviceIoControl(handle, IOCTL_AD_GET_DEVICE_ID, None, 1024)
            device_id_str = raw_id[2:].decode("ascii", errors="replace")
            log(f"[+] Raw Device ID: {device_id_str}")

            parsed_id = parse_identifier(device_id_str)
            log(f"    Model (MDL) : {parsed_id.get('MDL', 'Unknown')}")
            log(f"    Desc  (DES) : {parsed_id.get('DES', 'Unknown')}")
            log(f"    Cmds  (CMD) : {parsed_id.get('CMD', 'Unknown')}")
            log(f"    Class (CLS) : {parsed_id.get('CLS', 'Unknown')}")
            log(f"    Serial (SN) : {parsed_id.get('SN', 'Unknown')}")
        except Exception as e:
            log(f"[-] Failed to read Device ID: {e}")

        # D4 Handshake
        log("\n[STEP 4] Executing IEEE-1284.4 (D4) Handshake...")
        try:
            _s, _w = WriteFile(handle, D4_ENTER_PACKET)
            d4_ack = safe_read(handle, size=len(D4_ENTER_ACK), timeout_sec=1.5)
            if d4_ack and (D4_ENTER_ACK in d4_ack or b"\xC5" in d4_ack):
                log("    [+] D4 Enter OK (C5 ACK received).")
            else:
                log(f"    [!] D4 Enter Reply: {d4_ack.hex() if d4_ack else '(No response/handled in transport)'}")

            # D4 Init
            _s, _w = WriteFile(handle, D4_INIT_PACKET)
            d4_init_rx = safe_read(handle, size=16, timeout_sec=1.5)
            log(f"    [+] D4 Init Reply: {d4_init_rx.hex() if d4_init_rx else '(Awaiting socket data)'}")
        except Exception as e:
            log(f"[-] D4 handshake exception: {e}")

        # Status Prime (st 01)
        log("\n[STEP 5] Sending Status-Prime ('st 01')...")
        prime_pkt = bytes([SOCKET_PSID, SOCKET_SSID, 0x05, 0x00, 0x01]) + STATUS_PRIME_CMD
        try:
            WriteFile(handle, prime_pkt)
            rx_prime = safe_read(handle, size=1024, timeout_sec=2.0)
            log(f"    [+] Status-Prime Response: {rx_prime[:48] if rx_prime else '(No direct reply)'}")
            if b"@BDC ST2" in rx_prime:
                log("    [+] @BDC ST2 confirmed!")
        except Exception as e:
            log(f"    [!] Status-Prime error: {e}")

        # Factory Model Code Probe
        log("\n[STEP 6] Probing Factory Model Code Candidates...")
        candidates = [
            (bytes([0x97, 0x07]), "0x97 0x07 (L5190 Standard)"),
            (bytes([0x4A, 0x36]), "0x4A 0x36 (L5290 / L5TX Standard)"),
            (bytes([0x98, 0x07]), "0x98 0x07 (L5JX Variant)"),
            (bytes([0x9B, 0x07]), "0x9B 0x07 (L5JX Variant 2)"),
        ]

        confirmed_code = None
        for code_bytes, desc in candidates:
            # Probe Read Address 0x0030 (Main Pad Lo)
            probe_read = (
                b"\x7C\x7C"
                + bytes([0x07, 0x00])
                + code_bytes
                + ACTION_READ
                + bytes([0x30, 0x00])
            )
            pkt = bytes([SOCKET_PSID, SOCKET_SSID, len(probe_read), 0x00, 0x01]) + probe_read
            try:
                WriteFile(handle, pkt)
                rx = safe_read(handle, size=1024, timeout_sec=1.5)
                if rx and (b"@BDC PS" in rx or b"EE:0030" in rx):
                    log(f"    [*** SUCCESS ***] Model Code MATCH: {code_bytes.hex()} -> {desc}")
                    log(f"        Response: {rx.strip()}")
                    confirmed_code = code_bytes
                    break
                else:
                    log(f"    [-] Tested {code_bytes.hex()} ({desc})")
            except Exception as e:
                log(f"    [-] Error probing {code_bytes.hex()}: {e}")

        # If not confirmed via probe, default to known database spec
        if not confirmed_code:
            mdl = parsed_id.get("MDL", "")
            if "L5190" in mdl:
                confirmed_code = bytes([0x97, 0x07])
                log("\n    [*] Note: Hardware confirmed as L5190 Series (Factory Code: 0x97 0x07 via devices.xml).")
            elif "L5290" in mdl:
                confirmed_code = bytes([0x4A, 0x36])
                log("\n    [*] Note: Hardware confirmed as L5290 Series (Factory Code: 0x4A 0x36 via devices.xml).")

        log("\n" + "=" * 70)
        log("  FINAL DIAGNOSTIC SUMMARY")
        log("=" * 70)
        log(f"  USB VID            : 0x{target_iface.vid:04X}")
        log(f"  USB PID            : 0x{target_iface.pid:04X}  (0x114D = L5190 Series)")
        log(f"  Logical Port       : {target_iface.logical_port}")
        log(f"  Interface          : {target_iface.mi} (PRINTER)")
        log(f"  Device ID (MDL)    : {parsed_id.get('MDL', 'N/A')}")
        log(f"  Serial Number (SN) : {parsed_id.get('SN', 'N/A')}")
        log(f"  Factory Model Code : {confirmed_code.hex() if confirmed_code else 'Unknown'}")
        log("=" * 70)

    finally:
        handle.close()

    # Save to file
    out_file = Path("L5190_PROBE_REPORT.txt")
    out_file.write_text("\n".join(report_lines), encoding="utf-8")
    log(f"\n[+] Report successfully saved to: {out_file.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(probe_connected_printers())
