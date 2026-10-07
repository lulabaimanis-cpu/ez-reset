import xml.etree.ElementTree as ET
from dataclasses import dataclass
from importlib.resources import files
from itertools import batched
import re
from typing import TYPE_CHECKING

from .exceptions import DeviceError

if TYPE_CHECKING:
    from xml.etree.ElementTree import Element

import sys
from pathlib import Path

def _load_devices_xml() -> bytes:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        meipass = Path(sys._MEIPASS)
        for cand in [meipass / "ez_reset" / "devices.xml", meipass / "devices.xml"]:
            if cand.exists():
                return cand.read_bytes()
    try:
        return files(__package__).joinpath("devices.xml").read_bytes()
    except Exception:
        local_cand = Path(__file__).parent / "devices.xml"
        if local_cand.exists():
            return local_cand.read_bytes()
        raise

xml_content = _load_devices_xml()
devices = ET.fromstring(xml_content)


@dataclass
class Counter:
    addresses: list[int]
    max: int


@dataclass
class Device:
    model: bytes
    key: bytes
    counters: list[Counter]
    reset: dict[int, int]
    model_name: str = ""


def get_all_models() -> list[str]:
    """Return all supported printer model names from devices.xml."""
    return sorted(
        {el.attrib.get("model", "") for el in devices.findall(".//printer") if el.attrib.get("model")}
    )


def by_model(model: str) -> Device:
    """Find device configuration by printer model name with fuzzy fallback."""
    clean_model = model.strip()
    printer_el: Element | None = devices.find(f".//printer[@model='{clean_model}']")

    if printer_el is None:
        # Try stripping common suffixes like " Series", " (Copy 1)", etc.
        normalized = re.sub(r"\s*(series|network|\(copy\s*\d+\))\s*$", "", clean_model, flags=re.IGNORECASE).strip()
        printer_el = devices.find(f".//printer[@model='{normalized}']")

    if printer_el is None:
        # Search case-insensitively
        target_lower = clean_model.lower()
        for p in devices.findall(".//printer"):
            m = p.attrib.get("model", "")
            if m.lower() == target_lower or m.lower() in target_lower or target_lower in m.lower():
                printer_el = p
                clean_model = m
                break

    if printer_el is None:
        msg = f"Printer model '{model}' not found in devices definition database."
        raise DeviceError(msg)

    device = Device(
        model=b"",
        key=b"",
        counters=[],
        reset={},
        model_name=printer_el.attrib.get("model", clean_model),
    )

    for spec in printer_el.attrib.get("specs", "").split(","):
        if not spec.strip():
            continue
        spec_el: Element | None = devices.find(f".//devices/{spec.strip()}")
        if spec_el is None:
            continue

        service_el = spec_el.find(".//service")
        if service_el is not None:
            factory_el = service_el.find(".//factory")
            if factory_el is not None and factory_el.text:
                device.model = bytes(int(byte, 0) for byte in factory_el.text.split())

            keyword_el = service_el.find(".//keyword")
            if keyword_el is not None and keyword_el.text:
                device.key = bytes(int(byte, 0) for byte in keyword_el.text.split())

        waste_el = spec_el.find(".//waste")
        if waste_el is not None:
            query_el = waste_el.find(".//query")
            if query_el is not None:
                for counter_el in query_el.findall(".//counter"):
                    counter = Counter(addresses=[], max=0)
                    entry_el = counter_el.find(".//entry")
                    raw_addresses = entry_el.text if entry_el is not None and entry_el.text else counter_el.text

                    if raw_addresses:
                        counter.addresses = [int(raw, 0) for raw in raw_addresses.split()]

                    max_el = counter_el.find(".//max")
                    counter.max = int(max_el.text if max_el is not None and max_el.text else 0)
                    device.counters.append(counter)

            reset_el = waste_el.find(".//reset")
            if reset_el is not None and reset_el.text:
                device.reset = {int(addr, 0): int(val, 0) for (addr, val) in batched(reset_el.text.split(), 2)}

    return device
