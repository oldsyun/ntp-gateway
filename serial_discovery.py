"""Discover serial ports exposed by the host hardware."""

from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


PROFILE_ENV = "NTP_GATEWAY_HARDWARE_PROFILE"
PROFILE_NAMES = ("hardware_profile.json", "hardware-profile.json")
UART_PREFIXES = (
    "ttyS",
    "ttyAMA",
    "ttymxc",
    "ttyO",
    "ttyPS",
    "ttySC",
    "ttyLP",
    "ttySTM",
    "ttyTHS",
    "ttyMSM",
    "ttyHSL",
    "ttyRPMSG",
)
USB_PREFIXES = ("ttyUSB", "ttyACM")
VIRTUAL_NAMES = {"ttyprintk"}


class SerialDiscoveryError(RuntimeError):
    pass


def discover_serial_ports(
    app_dir: Path | str,
    *,
    dev_root: Path | str = "/dev",
    sysfs_root: Path | str = "/sys",
    proc_serial: Path | str = "/proc/tty/driver/serial",
) -> List[Dict[str, Any]]:
    profile = load_hardware_profile(app_dir)
    if profile is not None:
        return profile
    return detect_serial_ports(
        dev_root=dev_root, sysfs_root=sysfs_root, proc_serial=proc_serial
    )


def load_hardware_profile(app_dir: Path | str) -> Optional[List[Dict[str, Any]]]:
    configured = os.environ.get(PROFILE_ENV, "").strip()
    candidates = [Path(configured)] if configured else []
    candidates.extend(Path(app_dir) / name for name in PROFILE_NAMES)
    profile_path = next((path for path in candidates if path.is_file()), None)
    if profile_path is None:
        return None
    try:
        data = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SerialDiscoveryError(f"invalid hardware profile {profile_path}: {exc}") from exc

    entries = data.get("serial_ports") if isinstance(data, dict) else data
    if not isinstance(entries, list) or not entries:
        raise SerialDiscoveryError(
            f"hardware profile {profile_path} must contain a non-empty serial_ports list"
        )

    ports: List[Dict[str, Any]] = []
    for index, entry in enumerate(entries, 1):
        if isinstance(entry, str):
            entry = {"device": entry}
        if not isinstance(entry, dict):
            raise SerialDiscoveryError(f"invalid serial port entry #{index}")
        device = str(entry.get("device", "")).strip()
        if not device.startswith("/dev/"):
            raise SerialDiscoveryError(
                f"serial port #{index} device must be an absolute /dev node"
            )
        item = copy.deepcopy(entry)
        item["device"] = device
        item.setdefault("name", f"COM{index}")
        item.setdefault("source", "profile")
        ports.append(item)
    return ports


def detect_serial_ports(
    *,
    dev_root: Path | str = "/dev",
    sysfs_root: Path | str = "/sys",
    proc_serial: Path | str = "/proc/tty/driver/serial",
) -> List[Dict[str, Any]]:
    dev_root = Path(dev_root)
    sysfs_root = Path(sysfs_root)
    consoles = _active_consoles(sysfs_root)
    onboard: List[Dict[str, Any]] = []
    usb: List[Dict[str, Any]] = []

    for item in _ports_from_proc(proc_serial, dev_root, consoles):
        onboard.append(item)
    for item in _ports_from_sysfs(sysfs_root, dev_root, consoles):
        target = usb if item.pop("_usb", False) else onboard
        target.append(item)

    selected = _deduplicate(onboard)
    if not selected:
        selected = _deduplicate(usb)
    if not selected:
        selected = _fallback_dev_scan(dev_root, consoles)

    selected.sort(key=lambda item: _natural_key(Path(item["device"]).name))
    for index, item in enumerate(selected, 1):
        item["name"] = f"COM{index}"
    return selected


def build_port_configs(
    discovered: Iterable[Dict[str, Any]],
    template: Dict[str, Any],
    existing_ports: Optional[Iterable[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    existing = {
        str(port.get("device")): port
        for port in (existing_ports or [])
        if isinstance(port, dict) and port.get("device")
    }
    ports: List[Dict[str, Any]] = []
    for index, entry in enumerate(discovered, 1):
        item = copy.deepcopy(entry)
        device = str(item.get("device", ""))
        base = copy.deepcopy(existing.get(device, template))
        overrides = {
            key: copy.deepcopy(value)
            for key, value in item.items()
            if key not in {"id", "source", "_usb", "description"}
        }
        base.update(overrides)
        base["id"] = index
        base["name"] = str(item.get("name") or f"COM{index}")
        base["device"] = device
        base.setdefault("enabled", False)
        ports.append(base)
    return ports


def _active_consoles(sysfs_root: Path) -> set[str]:
    path = sysfs_root / "class" / "tty" / "console" / "active"
    try:
        return set(path.read_text(encoding="utf-8").split())
    except OSError:
        return set()


def _ports_from_proc(
    proc_serial: Path | str, dev_root: Path, consoles: set[str]
) -> List[Dict[str, Any]]:
    try:
        text = Path(proc_serial).read_text(encoding="utf-8")
    except OSError:
        return []
    ports: List[Dict[str, Any]] = []
    for line in text.splitlines():
        match = re.match(r"\s*(\d+):\s+uart:([^\s]+)", line)
        if not match:
            continue
        name = f"ttyS{int(match.group(1))}"
        if name in consoles:
            continue
        device = dev_root / name
        if not device.exists():
            continue
        ports.append(
            {
                "device": str(device),
                "source": "uart",
                "description": match.group(2),
            }
        )
    return ports


def _ports_from_sysfs(
    sysfs_root: Path, dev_root: Path, consoles: set[str]
) -> List[Dict[str, Any]]:
    class_dir = sysfs_root / "class" / "tty"
    if not class_dir.is_dir():
        return []
    ports: List[Dict[str, Any]] = []
    prefixes = UART_PREFIXES + USB_PREFIXES
    for entry in class_dir.iterdir():
        name = entry.name
        if name in consoles or name in VIRTUAL_NAMES or not name.startswith(prefixes):
            continue
        device_link = entry / "device"
        if not device_link.exists() and not device_link.is_symlink():
            continue
        device = dev_root / name
        if not device.exists():
            continue
        resolved = os.path.realpath(device_link)
        is_usb = name.startswith(USB_PREFIXES) or "/usb" in resolved.lower()
        ports.append(
            {
                "device": str(device),
                "source": "usb" if is_usb else "uart",
                "description": Path(resolved).name,
                "_usb": is_usb,
            }
        )
    return ports


def _fallback_dev_scan(dev_root: Path, consoles: set[str]) -> List[Dict[str, Any]]:
    if not dev_root.is_dir():
        return []
    prefixes = UART_PREFIXES + USB_PREFIXES
    ports: List[Dict[str, Any]] = []
    for entry in dev_root.iterdir():
        if entry.name in consoles or not entry.name.startswith(prefixes):
            continue
        is_usb = entry.name.startswith(USB_PREFIXES)
        ports.append(
            {
                "device": str(entry),
                "source": "usb" if is_usb else "uart",
            }
        )
    return ports


def _deduplicate(ports: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in ports:
        device = str(item.get("device", ""))
        if not device or device in seen:
            continue
        seen.add(device)
        result.append(item)
    return result


def _natural_key(value: str) -> tuple:
    return tuple(
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", value)
    )
