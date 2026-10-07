"""
Layer A: EpsonUsbPortResolver for Epson L5190 (Family: L5JX).

Resolves logical USB port (e.g., USB038), queries Registry Port Number,
and ensures that only the PRINTER interface (MI_01) is targeted, explicitly
filtering out the FAX interface (MI_03) and Utility (MI_02).
"""

from dataclasses import dataclass
import logging
import re
from typing import Generator

import ctypes
from ctypes import byref, c_ulong, create_unicode_buffer, sizeof
from ctypes.wintypes import DWORD, HANDLE, HKEY

from .constants import (
    EPSON_VID,
    GUID_DEVINTERFACE_USBPRINT,
    INTERFACE_FAX_MI,
    INTERFACE_PRINTER_MI,
    L5190_PID,
    L5290_PID,
    SUPPORTED_L5_PIDS,
)
from ..win_usbprint.winapi import (
    DIGCF_DEVICEINTERFACE,
    DIGCF_PRESENT,
    ERROR_INSUFFICIENT_BUFFER,
    ERROR_NO_MORE_ITEMS,
    NULL,
    SP_DEVICE_INTERFACE_DATA,
    SP_DEVICE_INTERFACE_DETAIL_DATA,
    SP_DEVINFO_DATA,
    SetupDiDestroyDeviceInfoList,
    SetupDiEnumDeviceInterfaces,
    SetupDiGetClassDevs,
    SetupDiGetDeviceInterfaceDetail,
)

logger = logging.getLogger("ez_reset.l5190.resolver")

setupapi = ctypes.windll.setupapi
advapi32 = ctypes.windll.advapi32

KEY_READ = 0x20019
DICS_FLAG_INTERFACE = 0x00000002

SetupDiOpenDeviceInterfaceRegKey = setupapi.SetupDiOpenDeviceInterfaceRegKey
SetupDiOpenDeviceInterfaceRegKey.argtypes = [
    HANDLE,
    ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
    DWORD,
    DWORD,
]
SetupDiOpenDeviceInterfaceRegKey.restype = HKEY

RegCloseKey = advapi32.RegCloseKey
RegCloseKey.argtypes = [HKEY]
RegCloseKey.restype = DWORD

RegQueryValueExW = advapi32.RegQueryValueExW
RegQueryValueExW.argtypes = [
    HKEY,
    ctypes.c_wchar_p,
    ctypes.c_void_p,
    ctypes.POINTER(DWORD),
    ctypes.c_void_p,
    ctypes.POINTER(DWORD),
]
RegQueryValueExW.restype = DWORD


@dataclass
class ResolvedPort:
    device_path: str
    vid: int
    pid: int
    mi: str
    port_number: int | None
    logical_port: str
    is_printer_interface: bool
    is_fax_interface: bool


def _get_registry_port_number(devs_handle: HANDLE, iface_data: SP_DEVICE_INTERFACE_DATA) -> int | None:
    """Read 'Port Number' DWORD from device interface registry key."""
    INVALID_HANDLE_VALUE = HANDLE(-1).value
    hkey = SetupDiOpenDeviceInterfaceRegKey(
        devs_handle,
        byref(iface_data),
        0,
        KEY_READ,
    )
    if hkey == INVALID_HANDLE_VALUE or not hkey:
        return None

    try:
        val_type = DWORD()
        dw_val = DWORD()
        val_size = DWORD(sizeof(dw_val))

        status = RegQueryValueExW(
            hkey,
            "Port Number",
            None,
            byref(val_type),
            byref(dw_val),
            byref(val_size),
        )
        if status == 0 and val_type.value in (4, 3):  # REG_DWORD or REG_BINARY
            return dw_val.value

        # Some drivers store it as REG_SZ string
        buf = create_unicode_buffer(32)
        buf_size = DWORD(sizeof(buf))
        status = RegQueryValueExW(
            hkey,
            "Port Number",
            None,
            byref(val_type),
            buf,
            byref(buf_size),
        )
        if status == 0 and val_type.value == 1:  # REG_SZ
            try:
                return int(buf.value.strip())
            except ValueError:
                pass
    except Exception as e:
        logger.debug("Failed reading Port Number from registry: %s", e)
    finally:
        RegCloseKey(hkey)

    return None


def enumerate_l5190_interfaces() -> Generator[ResolvedPort, None, None]:
    """
    Enumerate all USBPRINT interfaces and classify Printer vs Fax interfaces.
    Maps Port Number to Logical Port (e.g. 38 -> USB038).
    """
    guid = ctypes.c_char_p(GUID_DEVINTERFACE_USBPRINT.encode("ascii"))
    guid_struct = ctypes.create_string_buffer(GUID_DEVINTERFACE_USBPRINT.encode("ascii"))

    from ..win_usbprint.winapi import GUID_DEVINTERFACE_USBPRINT as GUID_USBPRINT

    devs_handle = SetupDiGetClassDevs(
        byref(GUID_USBPRINT),
        None,
        NULL,
        DIGCF_PRESENT | DIGCF_DEVICEINTERFACE,
    )

    idx = 0
    try:
        while True:
            iface_data = SP_DEVICE_INTERFACE_DATA()
            iface_data.cb_size = sizeof(iface_data)

            if not SetupDiEnumDeviceInterfaces(
                devs_handle,
                None,
                byref(GUID_USBPRINT),
                idx,
                byref(iface_data),
            ):
                if ctypes.GetLastError() != ERROR_NO_MORE_ITEMS:
                    raise ctypes.WinError()
                break

            size = DWORD(0)
            SetupDiGetDeviceInterfaceDetail(
                devs_handle,
                byref(iface_data),
                None,
                0,
                byref(size),
                None,
            )

            dev_info = SP_DEVINFO_DATA()
            dev_info.cb_size = sizeof(dev_info)

            detail_data = SP_DEVICE_INTERFACE_DETAIL_DATA()
            detail_data.cb_size = sizeof(SP_DEVICE_INTERFACE_DETAIL_DATA)
            ctypes.resize(detail_data, size.value)

            if not SetupDiGetDeviceInterfaceDetail(
                devs_handle,
                byref(iface_data),
                byref(detail_data),
                size,
                None,
                byref(dev_info),
            ):
                idx += 1
                continue

            path = str(detail_data.get_string())
            port_num = _get_registry_port_number(devs_handle, iface_data)

            # Parse VID, PID, MI from device path
            vid_match = re.search(r"vid_([0-9a-f]{4})", path, re.IGNORECASE)
            pid_match = re.search(r"pid_([0-9a-f]{4})", path, re.IGNORECASE)
            mi_match = re.search(r"mi_([0-9a-f]{2})", path, re.IGNORECASE)

            vid = int(vid_match.group(1), 16) if vid_match else 0
            pid = int(pid_match.group(1), 16) if pid_match else 0
            mi = f"MI_{mi_match.group(1).upper()}" if mi_match else ""

            logical_port = f"USB{port_num:03d}" if port_num is not None else ""
            is_printer = (mi == INTERFACE_PRINTER_MI)
            is_fax = (mi == INTERFACE_FAX_MI)

            yield ResolvedPort(
                device_path=path,
                vid=vid,
                pid=pid,
                mi=mi,
                port_number=port_num,
                logical_port=logical_port,
                is_printer_interface=is_printer,
                is_fax_interface=is_fax,
            )

            idx += 1
    finally:
        SetupDiDestroyDeviceInfoList(devs_handle)


def resolve_l5190_printer(requested_logical_port: str | None = None) -> ResolvedPort | None:
    """
    Find the legitimate L5190 Printer interface (MI_01).
    Explicitly skips FAX interfaces (MI_03) and utility interfaces (MI_02).
    """
    for port in enumerate_l5190_interfaces():
        if port.vid != EPSON_VID:
            continue
        if port.pid not in SUPPORTED_L5_PIDS:
            continue

        # If user specified a logical port (e.g. "USB038" or 38), match it
        if requested_logical_port:
            clean_req = requested_logical_port.upper().strip()
            if clean_req.startswith("USB"):
                if port.logical_port != clean_req:
                    continue
            else:
                try:
                    if port.port_number != int(clean_req):
                        continue
                except ValueError:
                    pass

        # CRITICAL FILTER: Must be PRINTER interface (MI_01), NOT FAX (MI_03)
        if port.is_fax_interface:
            logger.warning("[PORT-RESOLVER] Rejecting FAX interface %s (MI_03)", port.device_path)
            continue

        if port.is_printer_interface or port.mi == INTERFACE_PRINTER_MI or not port.mi:
            logger.info(
                "[PORT-RESOLVER] Selected legitimate Printer Interface: %s (Logical: %s, MI: %s)",
                port.device_path,
                port.logical_port,
                port.mi,
            )
            return port

    return None
