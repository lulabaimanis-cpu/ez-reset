"""
Epson L5190 (Family: L5JX) Dedicated Engine Package.

Exports:
- EpsonUsbPortResolver & resolve_l5190_printer
- ApdaTransport
- D4Session
- EpsonCtrlL5190
- L5190Workflow, CheckResult, InitializeResult
"""

from .constants import (
    EPSON_VID,
    L5190_PID,
    L5190_WIRE_KEY,
    MAIN_PAD_MAX,
    GOLDEN_CHECK_SEQUENCE,
    GOLDEN_INITIALIZE_SEQUENCE,
)
from .port_resolver import ResolvedPort, enumerate_l5190_interfaces, resolve_l5190_printer
from .transport import ApdaTransport, D4Session
from .ctrl import EpsonCtrlL5190
from .workflow import CheckResult, InitializeResult, L5190Workflow

__all__ = [
    "EPSON_VID",
    "L5190_PID",
    "L5190_WIRE_KEY",
    "MAIN_PAD_MAX",
    "GOLDEN_CHECK_SEQUENCE",
    "GOLDEN_INITIALIZE_SEQUENCE",
    "ResolvedPort",
    "enumerate_l5190_interfaces",
    "resolve_l5190_printer",
    "ApdaTransport",
    "D4Session",
    "EpsonCtrlL5190",
    "CheckResult",
    "InitializeResult",
    "L5190Workflow",
]
