from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .config import INVENTORY_PATH
from .models import HardwareEdge, InventoryFile, InventoryConnection, InventoryDevice


def load_inventory(path: Path = INVENTORY_PATH) -> InventoryFile:
    with path.open() as fh:
        raw = json.load(fh)
    if "devices" in raw and "connections" in raw:
        return InventoryFile.model_validate(
            {
                **raw,
                "hardware": _derive_hardware(raw["devices"], raw["connections"]),
            }
        )
    return InventoryFile.model_validate(
        {
            "devices": {},
            "connections": [],
            "hardware": raw.get("hardware", []),
        }
    )


def save_inventory(inventory: InventoryFile, path: Path = INVENTORY_PATH) -> InventoryFile:
    path.parent.mkdir(parents=True, exist_ok=True)
    _apply_hardware_availability(inventory)
    persisted = inventory.model_dump(mode="json", exclude={"hardware"})
    if not persisted["devices"] and inventory.hardware:
        persisted = _legacy_hardware_to_graph(inventory.hardware)
    with path.open("w") as fh:
        json.dump(persisted, fh, indent=2)
        fh.write("\n")
    return load_inventory(path)


def get_hardware_by_id(hardware_id: str, path: Path = INVENTORY_PATH) -> HardwareEdge | None:
    inventory = load_inventory(path)
    return next((item for item in inventory.hardware if item.id == hardware_id), None)


def _apply_hardware_availability(inventory: InventoryFile) -> None:
    if not inventory.devices or not inventory.hardware:
        return
    availability_by_id = {item.id: item.available for item in inventory.hardware}
    for device in inventory.devices.values():
        if device.type != "edge":
            continue
        group_id = device.ha_group_id or device.id
        if group_id in availability_by_id:
            device.available = availability_by_id[group_id]


def _derive_hardware(
    raw_devices: dict[str, Any],
    raw_connections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    devices = {device_id: InventoryDevice.model_validate(device) for device_id, device in raw_devices.items()}
    connections = [InventoryConnection.model_validate(connection) for connection in raw_connections]
    edge_devices = [device for device in devices.values() if device.type == "edge"]
    groups: dict[str, list[InventoryDevice]] = {}
    for device in edge_devices:
        group_id = device.ha_group_id or device.id
        groups.setdefault(group_id, []).append(device)

    hardware: list[dict[str, Any]] = []
    for group_id, members in sorted(groups.items()):
        active = _pick_edge_member(members, "active") or sorted(members, key=lambda item: item.id)[0]
        standby = _pick_edge_member(members, "standby")
        if standby and standby.id == active.id:
            standby = None

        ports = _derive_ports(active, standby, devices, connections, group_id)
        if not ports:
            continue
        switches = _derive_switches(ports, devices)
        if not switches:
            continue

        is_ha = standby is not None
        hardware.append(
            {
                "id": group_id,
                "short_name": active.short_name,
                "display_name": _hardware_display_name(active, standby),
                "model": active.model or "",
                "model_suffix": active.model_suffix or _model_suffix(active.model),
                "ha": is_ha,
                "dpdk_enabled": active.dpdk_enabled,
                "active_serial": active.serial_number or "",
                "standby_serial": standby.serial_number if standby else None,
                "free_vlans": active.free_vlans,
                "switch": switches[0],
                "switches": switches,
                "ports": ports,
                "available": active.available and (standby.available if standby else True),
                "notes": _join_notes(active.notes, standby.notes if standby else None),
            }
        )
    return hardware


def _pick_edge_member(members: list[InventoryDevice], role: str) -> InventoryDevice | None:
    return next((member for member in members if member.ha_role == role), None)


def _derive_ports(
    active: InventoryDevice,
    standby: InventoryDevice | None,
    devices: dict[str, InventoryDevice],
    connections: list[InventoryConnection],
    group_id: str,
) -> list[dict[str, Any]]:
    active_connections = _edge_connections(active.id, devices, connections)
    standby_connections = _edge_connections(standby.id, devices, connections) if standby else []
    standby_by_signature = {
        _standby_signature(connection, standby.id): connection for connection in standby_connections
    }

    ports: list[dict[str, Any]] = []
    for connection in sorted(active_connections, key=lambda item: _interface_sort_key(_edge_endpoint(item, active.id).interface)):
        edge_endpoint = _edge_endpoint(connection, active.id)
        switch_endpoint = _other_endpoint(connection, active.id)
        switch = devices.get(switch_endpoint.device_id)
        if not switch or switch.type != "switch":
            continue
        standby_connection = standby_by_signature.get(
            (
                edge_endpoint.interface.upper(),
                switch_endpoint.device_id,
                tuple(connection.vlans),
            )
        )
        standby_port = None
        if standby_connection and standby:
            standby_port = _other_endpoint(standby_connection, standby.id).interface

        logical_interface = edge_endpoint.interface.upper()
        ports.append(
            {
                "logical_name": logical_interface,
                "name": logical_interface.lower(),
                "logical_interface": logical_interface,
                "link": f"{_safe_id(group_id)}_{logical_interface.lower()}",
                "switch_name": switch.display_name,
                "switch_active_port": switch_endpoint.interface,
                "switch_standby_port": standby_port,
                "switch_vlans": connection.vlans,
                "tagged_vlans": connection.tagged_vlans,
                "untagged_vlan": connection.untagged_vlan,
                "edge_vlans": None,
                "segment_vlans": {},
                "wanlink_name": None,
            }
        )
    return ports


def _edge_connections(
    edge_id: str,
    devices: dict[str, InventoryDevice],
    connections: list[InventoryConnection],
) -> list[InventoryConnection]:
    result = []
    for connection in connections:
        if connection.a.device_id == edge_id and devices.get(connection.b.device_id, None):
            result.append(connection)
        elif connection.b.device_id == edge_id and devices.get(connection.a.device_id, None):
            result.append(connection)
    return result


def _edge_endpoint(connection: InventoryConnection, edge_id: str):
    return connection.a if connection.a.device_id == edge_id else connection.b


def _other_endpoint(connection: InventoryConnection, edge_id: str):
    return connection.b if connection.a.device_id == edge_id else connection.a


def _standby_signature(connection: InventoryConnection, edge_id: str) -> tuple[str, str, tuple[int, ...]]:
    edge_endpoint = _edge_endpoint(connection, edge_id)
    switch_endpoint = _other_endpoint(connection, edge_id)
    return (edge_endpoint.interface.upper(), switch_endpoint.device_id, tuple(connection.vlans))


def _derive_switches(ports: list[dict[str, Any]], devices: dict[str, InventoryDevice]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    switches: list[dict[str, Any]] = []
    for port in ports:
        switch = next(
            (
                device
                for device in devices.values()
                if device.type == "switch" and device.display_name == port["switch_name"]
            ),
            None,
        )
        if not switch or switch.id in seen:
            continue
        seen.add(switch.id)
        if switch.switch_metadata:
            switches.append(switch.switch_metadata.model_dump(mode="json"))
        else:
            switches.append(
                {
                    "name": switch.display_name,
                    "device_type": "DELL",
                    "model": switch.model or "Dell",
                    "connections": {"ip": switch.ip_address or "", "port": None},
                    "credentials": {"username": "velocloud", "password": "N#1sdwan"},
                }
            )
    return switches


def _hardware_display_name(active: InventoryDevice, standby: InventoryDevice | None) -> str:
    if standby:
        return f"HA Pair {active.display_name} + {standby.display_name}"
    return active.display_name


def _join_notes(*values: str | None) -> str | None:
    notes = [value for value in values if value]
    return " ".join(notes) if notes else None


def _model_suffix(model: str | None) -> str:
    if not model:
        return ""
    match = re.search(r"(\d+[a-z]*)", model.lower())
    return match.group(1) if match else model.removeprefix("edge")


def _interface_sort_key(interface: str) -> tuple[int, int]:
    upper = interface.upper()
    match = re.fullmatch(r"GE(\d+)", upper)
    if match:
        number = int(match.group(1))
        return (0 if number <= 4 else 2, number)
    match = re.fullmatch(r"SFP(\d+)", upper)
    if match:
        return (1, int(match.group(1)))
    return (9, 999)


def _safe_id(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _legacy_hardware_to_graph(hardware: list[HardwareEdge]) -> dict[str, Any]:
    devices: dict[str, dict[str, Any]] = {}
    connections: list[dict[str, Any]] = []
    for item in hardware:
        active_id = f"{item.id}-active" if item.ha else item.id
        devices[active_id] = {
            "id": active_id,
            "type": "edge",
            "display_name": item.display_name,
            "short_name": item.short_name,
            "model": item.model,
            "model_suffix": item.model_suffix,
            "serial_number": item.active_serial,
            "available": item.available,
            "ha_group_id": item.id,
            "ha_role": "active",
            "dpdk_enabled": item.dpdk_enabled,
            "free_vlans": item.free_vlans,
            "notes": item.notes,
        }
        standby_id = None
        if item.ha and item.standby_serial:
            standby_id = f"{item.id}-standby"
            devices[standby_id] = {
                **devices[active_id],
                "id": standby_id,
                "display_name": f"{item.display_name} standby",
                "serial_number": item.standby_serial,
                "ha_role": "standby",
            }
        for switch in item.switches or ([item.switch] if item.switch else []):
            switch_id = _safe_id(switch.name)
            devices[switch_id] = {
                "id": switch_id,
                "type": "switch",
                "display_name": switch.name,
                "model": switch.model,
                "ip_address": switch.connections.ip,
                "available": True,
                "switch_metadata": switch.model_dump(mode="json"),
            }
        for port in item.ports:
            switch_id = _safe_id(port.switch_name or (item.switches[0].name if item.switches else item.switch.name))
            connections.append(
                {
                    "id": f"{active_id}-{port.logical_interface}-{switch_id}",
                    "a": {"device_id": active_id, "interface": port.logical_interface},
                    "b": {"device_id": switch_id, "interface": port.switch_active_port},
                    "vlans": port.switch_vlans,
                    "tagged_vlans": port.tagged_vlans or list(port.segment_vlans.values()),
                    "untagged_vlan": port.untagged_vlan or (port.switch_vlans[0] if port.switch_vlans else None),
                }
            )
            if standby_id and port.switch_standby_port:
                connections.append(
                    {
                        "id": f"{standby_id}-{port.logical_interface}-{switch_id}",
                        "a": {"device_id": standby_id, "interface": port.logical_interface},
                        "b": {"device_id": switch_id, "interface": port.switch_standby_port},
                        "vlans": port.switch_vlans,
                        "tagged_vlans": port.tagged_vlans or list(port.segment_vlans.values()),
                        "untagged_vlan": port.untagged_vlan or (port.switch_vlans[0] if port.switch_vlans else None),
                    }
                )
    return {"devices": devices, "connections": connections}
