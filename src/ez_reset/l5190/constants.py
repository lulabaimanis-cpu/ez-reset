"""
Golden constants and specifications for Epson L5190 / L5196 / L5198 (Family: L5JX).
Extracted from hardware APDA golden trace.
"""

from typing import Final

# USB Device Identifiers
EPSON_VID: Final[int] = 0x04B8
L5190_PID: Final[int] = 0x114D
L5290_PID: Final[int] = 0x1185

SUPPORTED_L5_PIDS: Final[dict[int, str]] = {
    0x114D: "Epson L5190 / L5196 / L5198 (L5JX)",
    0x1185: "Epson L5290 / L5296 / L5298 (L5TX)",
}

# Interface IDs
INTERFACE_PRINTER_MI: Final[str] = "MI_01"  # Port Number 38 -> USB038 (TARGET)
INTERFACE_UTILITY_MI: Final[str] = "MI_02"  # WINUSB (EXCLUDED)
INTERFACE_FAX_MI: Final[str] = "MI_03"      # Port Number 39 -> USB039 (EXCLUDED)

# SetupAPI USBPRINT Interface GUID
GUID_DEVINTERFACE_USBPRINT: Final[str] = "{28D78FAD-5A12-11D1-AE5B-0000F803A8C2}"

# IOCTL codes
IOCTL_AD_INIT_DEVICE: Final[int] = 0x220040  # Input: 0x01, len: 1
IOCTL_AD_GET_DEVICE_ID: Final[int] = 0x220034

# IEEE 1284.4 (D4) Handshake Packets
D4_ENTER_PACKET: Final[bytes] = (
    b"\x00\x00\x00\x1B\x01\x40\x45\x4A\x4C\x20\x31\x32\x38\x34\x2E\x34\x0A"
    b"\x40\x45\x4A\x4C\x0A\x40\x45\x4A\x4C\x0A"
)
D4_ENTER_ACK: Final[bytes] = b"\x00\x00\x00\x08\x01\x00\xC5\x00"

D4_INIT_PACKET: Final[bytes] = b"\x00\x00\x00\x08\x01\x00\x00\x10"
D4_INIT_ACK_PREFIX: Final[bytes] = b"\x00\x00\x00\x09\x01\x00\x80\x00\x10"

# Fixed Socket Parameters
SOCKET_PSID: Final[int] = 0x02
SOCKET_SSID: Final[int] = 0x02
PACKET_SIZE: Final[int] = 0x0100

# EPSON-CTRL Status Prime
STATUS_PRIME_CMD: Final[bytes] = b"\x73\x74\x01\x00\x01"  # "st 01"
STATUS_PRIME_EXPECTED: Final[bytes] = b"@BDC ST2\r\n"

# Factory Control Codes
FACTORY_MODEL_CODE: Final[bytes] = bytes([0x97, 0x07])  # 151, 7
ACTION_READ: Final[bytes] = bytes([0x41, 0xBE, 0xA0])   # Read action
ACTION_WRITE: Final[bytes] = bytes([0x42, 0xBD, 0x21])  # Write action

# Wire Write Key (AdjProg Golden Trace: "Nbsjcbzb")
L5190_WIRE_KEY: Final[bytes] = bytes([
    0x4E, 0x62, 0x73, 0x6A, 0x63, 0x62, 0x7A, 0x62
])

# Expected Write ACK
WRITE_ACK_EXPECTED: Final[bytes] = b"||:42:OK;"

# Counter Addresses & Limits
ADDR_MAIN_LOW: Final[int] = 0x0030
ADDR_MAIN_HIGH: Final[int] = 0x0031
ADDR_PLATEN_LOW: Final[int] = 0x0032
ADDR_PLATEN_HIGH: Final[int] = 0x0033
ADDR_GUARD_2F: Final[int] = 0x002F

MAIN_PAD_MAX: Final[int] = 6346

# Golden Check EEPROM Sequence
GOLDEN_CHECK_SEQUENCE: Final[list[int]] = [
    0x002F,  # Guard read
    0x0030,  # Main pad low
    0x0031,  # Main pad high
    0x002F,  # Guard read
    0x0032,  # Platen pad low
    0x0033,  # Platen pad high
]

# Golden Initialize Step Definition: (Action, Address, Value, Description)
GOLDEN_INITIALIZE_SEQUENCE: Final[list[tuple[str, int, int, str]]] = [
    ("WRITE", 0x0030, 0x00, "W0030 = 00 (Main Pad Lo)"),
    ("WRITE", 0x0031, 0x00, "W0031 = 00 (Main Pad Hi)"),
    ("READ",  0x002F, 0x00, "R002F (Guard 1)"),
    ("WRITE", 0x002F, 0x00, "W002F = 00 (Guard 1 Reset)"),
    ("WRITE", 0x0034, 0x00, "W0034 = 00 (Aux Counter 1)"),
    ("WRITE", 0x0035, 0x00, "W0035 = 00 (Aux Counter 2)"),
    ("WRITE", 0x0036, 0x5E, "W0036 = 5E (Maintenance Req Lvl 1: 94)"),
    ("WRITE", 0x0032, 0x00, "W0032 = 00 (Platen Pad Lo)"),
    ("WRITE", 0x0033, 0x00, "W0033 = 00 (Platen Pad Hi)"),
    ("READ",  0x002F, 0x00, "R002F (Guard 2)"),
    ("WRITE", 0x002F, 0x00, "W002F = 00 (Guard 2 Reset)"),
    ("WRITE", 0x0037, 0x5E, "W0037 = 5E (Maintenance Req Lvl 2: 94)"),
    ("WRITE", 0x001C, 0x00, "W001C = 00 (Final Counter)"),
]
