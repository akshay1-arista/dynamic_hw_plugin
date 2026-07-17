from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .audit import build_audit_event
from .config import INVENTORY_PATH, INVENTORY_STATE_PATH
from .models import (
    ActorIdentity,
    AuditEvent,
    HardwareEdge,
    HardwareLocalState,
    HardwareReservation,
    HardwarePathSummary,
    InventoryConnection,
    InventoryDevice,
    InventoryFile,
    InventoryStateFile,
    SwitchMetadata,
    VlanRange,
)


def load_inventory(path: Path = INVENTORY_PATH, *, state_path: Path | None = None) -> InventoryFile:
    with path.open() as fh:
        raw = json.load(fh)
    if "devices" in raw and "connections" in raw:
        inventory = build_inventory(raw["devices"], raw["connections"], raw.get("allocations"))
    else:
        inventory = InventoryFile.model_validate(
            {
                "devices": {},
                "connections": [],
                "allocations": raw.get("allocations", []),
                "hardware": raw.get("hardware", []),
            }
        )
    resolved_state_path = _resolve_inventory_state_path(path, state_path)
    local_state = _load_inventory_state(resolved_state_path)
    _apply_local_inventory_state(inventory, local_state, clear_missing=resolved_state_path.exists())
    return inventory


def build_inventory(
    raw_devices: dict[str, Any],
    raw_connections: list[dict[str, Any]],
    raw_allocations: list[dict[str, Any]] | None = None,
) -> InventoryFile:
    connections = _sanitize_connections(
        [InventoryConnection.model_validate(connection) for connection in raw_connections]
    )
    return InventoryFile.model_validate(
        {
            "devices": raw_devices,
            "connections": [connection.model_dump(mode="json") for connection in connections],
            "allocations": raw_allocations or [],
            "hardware": _derive_hardware(
                raw_devices,
                [connection.model_dump(mode="json") for connection in connections],
            ),
        }
    )


def save_inventory(
    inventory: InventoryFile,
    path: Path = INVENTORY_PATH,
    *,
    preserve_local_state: bool = False,
    state_path: Path | None = None,
) -> InventoryFile:
    path.parent.mkdir(parents=True, exist_ok=True)
    resolved_state_path = _resolve_inventory_state_path(path, state_path)
    existing_local_state = _load_inventory_state(resolved_state_path)
    inventory.connections = _sanitize_connections(inventory.connections)
    inventory.allocations = []
    _normalize_hardware_state(inventory)
    _apply_shared_hardware_state(inventory)
    local_state = _build_local_inventory_state(
        inventory,
        existing_local_state,
        preserve_existing=preserve_local_state,
    )
    _apply_local_inventory_state(inventory, local_state, clear_missing=True)
    persisted = inventory.model_dump(mode="json", exclude={"hardware"})
    if not persisted["devices"] and inventory.hardware:
        persisted = _legacy_hardware_to_graph(inventory.hardware)
    _strip_local_state_from_persisted_inventory(persisted)
    with path.open("w") as fh:
        json.dump(persisted, fh, indent=2)
        fh.write("\n")
    _save_inventory_state(local_state, resolved_state_path)
    return load_inventory(path, state_path=resolved_state_path)


def save_inventory_hardware_edits(
    inventory: InventoryFile,
    path: Path = INVENTORY_PATH,
    *,
    state_path: Path | None = None,
) -> InventoryFile:
    current = load_inventory(path, state_path=state_path)
    requested_hardware = {item.id: item for item in inventory.hardware}

    for hardware in current.hardware:
        updated = requested_hardware.get(hardware.id)
        if updated is None:
            continue
        hardware.vlan_range = updated.vlan_range

    return save_inventory(current, path, state_path=state_path)


def get_hardware_by_id(hardware_id: str, path: Path = INVENTORY_PATH) -> HardwareEdge | None:
    inventory = load_inventory(path)
    return next((item for item in inventory.hardware if item.id == hardware_id), None)


def reserve_generated_hardware(
    hardware_ids: list[str],
    actor: ActorIdentity,
    run_id: str,
    topology_name: str,
    path: Path = INVENTORY_PATH,
) -> tuple[InventoryFile, list[AuditEvent]]:
    inventory = load_inventory(path)
    hardware_set = set(hardware_ids)
    events: list[AuditEvent] = []
    for hardware in inventory.hardware:
        if hardware.id not in hardware_set:
            continue
        hardware.available = False
        hardware.reservation = HardwareReservation(
            actor=actor,
            reserved_at=_utc_now(),
            reason="topology-generation",
            run_id=run_id,
            topology_name=topology_name,
        )
        events.append(
            build_audit_event(
                action="hardware_reserved",
                actor=actor,
                target_type="hardware",
                target_id=hardware.id,
                summary=f"Reserved {hardware.display_name} for generated topology {topology_name}.",
                details={
                    "hardware_id": hardware.id,
                    "hardware_display_name": hardware.display_name,
                    "run_id": run_id,
                    "topology_name": topology_name,
                },
            )
        )
    saved = save_inventory(inventory, path)
    return saved, events


def update_hardware_availability(
    hardware_id: str,
    available: bool,
    actor: ActorIdentity,
    path: Path = INVENTORY_PATH,
) -> tuple[InventoryFile, list[AuditEvent]]:
    inventory = load_inventory(path)
    hardware = next((item for item in inventory.hardware if item.id == hardware_id), None)
    if hardware is None:
        raise ValueError(f"Unknown hardware inventory id: {hardware_id}")

    previous_reservation = hardware.reservation
    hardware.available = available
    if available:
        hardware.reservation = None
        action = "hardware_released"
        summary = f"Marked {hardware.display_name} as available."
        details = {
            "hardware_id": hardware.id,
            "hardware_display_name": hardware.display_name,
            "released_previous_reservation": previous_reservation.model_dump(mode="json") if previous_reservation else None,
        }
    else:
        hardware.reservation = HardwareReservation(
            actor=actor,
            reserved_at=_utc_now(),
            reason="manual-unavailable",
        )
        action = "hardware_marked_unavailable"
        summary = f"Marked {hardware.display_name} as unavailable."
        details = {
            "hardware_id": hardware.id,
            "hardware_display_name": hardware.display_name,
        }

    saved = save_inventory(inventory, path)
    event = build_audit_event(
        action=action,
        actor=actor,
        target_type="hardware",
        target_id=hardware.id,
        summary=summary,
        details=details,
    )
    return saved, [event]


def _sanitize_connections(connections: list[InventoryConnection]) -> list[InventoryConnection]:
    candidates = list(enumerate(connections))
    selected_indexes: list[int] = []
    used_endpoints: set[tuple[str, str]] = set()

    for index, connection in sorted(
        candidates,
        key=lambda item: (_connection_preference(item[1]), item[0]),
        reverse=True,
    ):
        endpoints = (
            (connection.a.device_id, connection.a.interface),
            (connection.b.device_id, connection.b.interface),
        )
        if any(endpoint in used_endpoints for endpoint in endpoints):
            continue
        selected_indexes.append(index)
        used_endpoints.update(endpoints)

    return [connections[index] for index in sorted(selected_indexes)]


def _connection_preference(connection: InventoryConnection) -> tuple[int, int, int, int, int, int]:
    notes = connection.notes or ""
    return (
        1 if "Lab Navigator wiremap" in notes else 0,
        1 if connection.role is not None else 0,
        len(connection.vlans),
        len(connection.tagged_vlans),
        1 if connection.untagged_vlan is not None else 0,
        1 if notes else 0,
    )


def resolve_mapping_path(
    inventory: InventoryFile,
    switch_names: list[str],
    hypervisor_ip: str,
    hypervisor_interface: str | None = None,
) -> HardwarePathSummary | None:
    normalized_switch_names = sorted({name for name in switch_names if name})
    if not normalized_switch_names or not hypervisor_ip:
        return None

    candidates: list[HardwarePathSummary] = []
    for switch_name in normalized_switch_names:
        access_switch = _find_switch_by_name(inventory.devices, switch_name)
        if not access_switch:
            continue
        for uplink in _switch_links(access_switch.id, inventory.devices, inventory.connections):
            access_endpoint = _edge_endpoint(uplink, access_switch.id)
            upstream_endpoint = _other_endpoint(uplink, access_switch.id)
            upstream_switch = inventory.devices.get(upstream_endpoint.device_id)
            if not upstream_switch or upstream_switch.type != "switch":
                continue

            model = str(upstream_switch.model or "")
            if not any(token in model for token in ("4048", "4148")):
                continue

            for hypervisor_link in inventory.connections:
                if not _is_hypervisor_access_connection(
                    hypervisor_link,
                    upstream_switch.id,
                    inventory.devices,
                    hypervisor_ip,
                ):
                    continue
                upstream_hypervisor_endpoint = _edge_endpoint(hypervisor_link, upstream_switch.id)
                hypervisor_endpoint = _other_endpoint(hypervisor_link, upstream_switch.id)
                hypervisor = inventory.devices.get(hypervisor_endpoint.device_id)
                candidates.append(
                    HardwarePathSummary(
                        access_switch_id=access_switch.id,
                        access_switch_name=access_switch.display_name,
                        access_switch_ip=access_switch.ip_address,
                        access_uplink_port=access_endpoint.interface,
                        upstream_switch_id=upstream_switch.id,
                        upstream_switch_name=upstream_switch.display_name,
                        upstream_switch_model=upstream_switch.model,
                        upstream_switch_ip=upstream_switch.ip_address,
                        upstream_access_port=upstream_endpoint.interface,
                        upstream_hypervisor_port=upstream_hypervisor_endpoint.interface,
                        hypervisor_id=hypervisor.id if hypervisor else None,
                        hypervisor_name=hypervisor.display_name if hypervisor else None,
                        hypervisor_ip=hypervisor_ip,
                        complete=True,
                    )
                )

    if len(candidates) == 1:
        return candidates[0]

    normalized_interface = (hypervisor_interface or "").strip().lower()
    if normalized_interface:
        matched_candidates = [
            candidate
            for candidate in candidates
            if _candidate_matches_hypervisor_interface(
                candidate,
                inventory.connections,
                normalized_interface,
            )
        ]
        if len(matched_candidates) == 1:
            return matched_candidates[0]

    if len(candidates) != 1:
        return None
    return candidates[0]


def _candidate_matches_hypervisor_interface(
    candidate: HardwarePathSummary,
    connections: list[InventoryConnection],
    normalized_hypervisor_interface: str,
) -> bool:
    if not candidate.upstream_switch_id or not candidate.upstream_hypervisor_port:
        return False
    for connection in connections:
        if connection.a.device_id == candidate.upstream_switch_id and connection.a.interface == candidate.upstream_hypervisor_port:
            return connection.b.interface.strip().lower() == normalized_hypervisor_interface
        if connection.b.device_id == candidate.upstream_switch_id and connection.b.interface == candidate.upstream_hypervisor_port:
            return connection.a.interface.strip().lower() == normalized_hypervisor_interface
    return False


def path_has_credentials(path: HardwarePathSummary | None, inventory: InventoryFile) -> bool:
    if not path:
        return False
    return _path_has_credentials(path, inventory.devices)


def _apply_shared_hardware_state(inventory: InventoryFile) -> None:
    if not inventory.devices or not inventory.hardware:
        return
    hardware_by_id = {item.id: item for item in inventory.hardware}
    for device in inventory.devices.values():
        if device.type != "edge":
            continue
        group_id = device.ha_group_id or device.id
        hardware = hardware_by_id.get(group_id)
        if not hardware:
            continue
        device.hypervisor_ip = hardware.hypervisor_ip
        device.vlan_range = hardware.vlan_range


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

        vlan_range = active.vlan_range or _range_from_pool(active.free_vlans)
        free_vlans = _vlan_pool(active)
        path = _derive_path_summary(active, devices, connections)
        is_ha = standby is not None
        reservation = active.reservation or (standby.reservation if standby else None)
        available = active.available and (standby.available if standby else True)
        if available:
            reservation = None
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
                "free_vlans": free_vlans,
                "vlan_range": vlan_range,
                "switch": switches[0],
                "switches": switches,
                "ports": ports,
                "allocations": [],
                "path": path.model_dump(mode="json") if path else None,
                "path_complete": bool(path and path.complete),
                "auto_config_ready": bool(path and path.complete and _path_has_credentials(path, devices)),
                "hypervisor_ip": active.hypervisor_ip,
                "available": available,
                "reservation": reservation.model_dump(mode="json") if reservation else None,
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
    standby_by_vlan_signature: dict[tuple[str, tuple[int, ...], tuple[int, ...], int | None], list[InventoryConnection]] = {}
    for connection in standby_connections:
        standby_by_vlan_signature.setdefault(_standby_vlan_signature(connection, standby.id), []).append(connection)

    active_port_rows: list[tuple[InventoryConnection, Any, Any, InventoryDevice]] = []
    for connection in sorted(
        active_connections,
        key=lambda item: _interface_sort_key(_edge_endpoint(item, active.id).interface),
    ):
        edge_endpoint = _edge_endpoint(connection, active.id)
        switch_endpoint = _other_endpoint(connection, active.id)
        switch = devices.get(switch_endpoint.device_id)
        if not switch or switch.type != "switch":
            continue
        active_port_rows.append((connection, edge_endpoint, switch_endpoint, switch))

    matched_standby_connections: dict[str, InventoryConnection] = {}
    used_standby_connection_ids: set[str] = set()
    for connection, edge_endpoint, switch_endpoint, _switch in active_port_rows:
        standby_connection = standby_by_signature.get(
            (
                edge_endpoint.interface.upper(),
                switch_endpoint.device_id,
                tuple(connection.vlans),
            )
        )
        if standby_connection:
            matched_standby_connections[connection.id] = standby_connection
            used_standby_connection_ids.add(standby_connection.id)

    for connection, _edge_port, _switch_port, _switch in active_port_rows:
        if connection.id in matched_standby_connections:
            continue
        candidates = [
            candidate
            for candidate in standby_by_vlan_signature.get(_standby_vlan_signature(connection, active.id), [])
            if candidate.id not in used_standby_connection_ids
        ]
        if len(candidates) != 1:
            continue
        matched_standby_connections[connection.id] = candidates[0]
        used_standby_connection_ids.add(candidates[0].id)

    ports: list[dict[str, Any]] = []
    for connection, edge_endpoint, switch_endpoint, switch in active_port_rows:
        standby_connection = matched_standby_connections.get(connection.id)
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
                "manual_mapping_required": bool(standby and not standby_port),
                "port_warning": (
                    _ha_port_warning(logical_interface, active_port=True, standby_port=False)
                    if standby and not standby_port
                    else None
                ),
            }
        )

    standby_only_rows: list[tuple[InventoryConnection, Any, Any, InventoryDevice]] = []
    for connection in sorted(
        standby_connections,
        key=lambda item: _interface_sort_key(_edge_endpoint(item, standby.id).interface),
    ):
        if connection.id in used_standby_connection_ids:
            continue
        edge_endpoint = _edge_endpoint(connection, standby.id)
        switch_endpoint = _other_endpoint(connection, standby.id)
        switch = devices.get(switch_endpoint.device_id)
        if not switch or switch.type != "switch":
            continue
        standby_only_rows.append((connection, edge_endpoint, switch_endpoint, switch))

    for connection, edge_endpoint, switch_endpoint, switch in standby_only_rows:
        logical_interface = edge_endpoint.interface.upper()
        existing_port = next(
            (
                port
                for port in ports
                if port["logical_interface"] == logical_interface and not port.get("switch_standby_port")
            ),
            None,
        )
        if existing_port is not None:
            existing_port["switch_standby_port"] = switch_endpoint.interface
            existing_port["manual_mapping_required"] = True
            existing_port["port_warning"] = (
                f"{logical_interface} active and standby switch connections differ. "
                "Review interface mapping before generation."
            )
            continue
        ports.append(
            {
                "logical_name": logical_interface,
                "name": logical_interface.lower(),
                "logical_interface": logical_interface,
                "link": f"{_safe_id(group_id)}_{logical_interface.lower()}",
                "switch_name": switch.display_name,
                "switch_active_port": None,
                "switch_standby_port": switch_endpoint.interface,
                "switch_vlans": connection.vlans,
                "tagged_vlans": connection.tagged_vlans,
                "untagged_vlan": connection.untagged_vlan,
                "edge_vlans": None,
                "segment_vlans": {},
                "wanlink_name": None,
                "manual_mapping_required": True,
                "port_warning": _ha_port_warning(logical_interface, active_port=False, standby_port=True),
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
        if not _is_edge_access_connection(connection, edge_id, devices):
            continue
        if connection.a.device_id == edge_id or connection.b.device_id == edge_id:
            result.append(connection)
    return result


def _is_edge_access_connection(
    connection: InventoryConnection,
    edge_id: str,
    devices: dict[str, InventoryDevice],
) -> bool:
    if connection.a.device_id != edge_id and connection.b.device_id != edge_id:
        return False
    remote_endpoint = _other_endpoint(connection, edge_id)
    remote_device = devices.get(remote_endpoint.device_id)
    if not remote_device or remote_device.type != "switch":
        return False
    return connection.role in {None, "edge-access"}


def _edge_endpoint(connection: InventoryConnection, edge_id: str):
    return connection.a if connection.a.device_id == edge_id else connection.b


def _other_endpoint(connection: InventoryConnection, edge_id: str):
    return connection.b if connection.a.device_id == edge_id else connection.a


def _standby_signature(connection: InventoryConnection, edge_id: str) -> tuple[str, str, tuple[int, ...]]:
    edge_endpoint = _edge_endpoint(connection, edge_id)
    switch_endpoint = _other_endpoint(connection, edge_id)
    return (edge_endpoint.interface.upper(), switch_endpoint.device_id, tuple(connection.vlans))


def _standby_vlan_signature(
    connection: InventoryConnection,
    edge_id: str,
) -> tuple[str, tuple[int, ...], tuple[int, ...], int | None]:
    switch_endpoint = _other_endpoint(connection, edge_id)
    return (
        switch_endpoint.device_id,
        tuple(connection.vlans),
        tuple(connection.tagged_vlans),
        connection.untagged_vlan,
    )


def _ha_port_warning(logical_interface: str, *, active_port: bool, standby_port: bool) -> str | None:
    if active_port and standby_port:
        return None
    if active_port:
        return (
            f"{logical_interface} has only an active-member switch connection. "
            "Review interface mapping before generation."
        )
    if standby_port:
        return (
            f"{logical_interface} has only a standby-member switch connection. "
            "Review interface mapping before generation."
        )
    return None


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
        switches.append(_switch_metadata(switch).model_dump(mode="json"))
    return switches


def _switch_metadata(device: InventoryDevice) -> SwitchMetadata:
    if device.switch_metadata:
        return device.switch_metadata
    return SwitchMetadata(
        name=device.display_name,
        device_type="DELL",
        model=device.model or "Dell",
        connections={"ip": device.ip_address or "", "port": None},
    )


def _derive_path_summary(
    active: InventoryDevice,
    devices: dict[str, InventoryDevice],
    connections: list[InventoryConnection],
) -> HardwarePathSummary | None:
    edge_connections = _edge_connections(active.id, devices, connections)
    if not edge_connections:
        return None

    access_switch_ids = sorted({_other_endpoint(connection, active.id).device_id for connection in edge_connections})
    access_switch_id = access_switch_ids[0] if len(access_switch_ids) == 1 else None
    access_switch = devices.get(access_switch_id) if access_switch_id else None
    summary = HardwarePathSummary(
        access_switch_id=access_switch_id,
        access_switch_name=access_switch.display_name if access_switch else None,
        access_switch_ip=access_switch.ip_address if access_switch else None,
        hypervisor_ip=active.hypervisor_ip,
    )
    if not access_switch_id or not active.hypervisor_ip:
        return summary

    uplinks = _switch_links(access_switch_id, devices, connections)
    if len(uplinks) != 1:
        return summary

    uplink = uplinks[0]
    remote_endpoint = _other_endpoint(uplink, access_switch_id)
    upstream_switch = devices.get(remote_endpoint.device_id)
    if not upstream_switch or upstream_switch.type != "switch":
        return summary

    hypervisor_links = [
        connection
        for connection in connections
        if _is_hypervisor_access_connection(connection, upstream_switch.id, devices, active.hypervisor_ip)
    ]
    if len(hypervisor_links) != 1:
        return summary

    hypervisor_link = hypervisor_links[0]
    upstream_endpoint = _edge_endpoint(hypervisor_link, upstream_switch.id)
    hypervisor_endpoint = _other_endpoint(hypervisor_link, upstream_switch.id)
    hypervisor_device = devices.get(hypervisor_endpoint.device_id)
    summary.access_uplink_port = _edge_endpoint(uplink, access_switch_id).interface
    summary.upstream_switch_id = upstream_switch.id
    summary.upstream_switch_name = upstream_switch.display_name
    summary.upstream_switch_model = upstream_switch.model
    summary.upstream_switch_ip = upstream_switch.ip_address
    summary.upstream_access_port = remote_endpoint.interface
    summary.upstream_hypervisor_port = upstream_endpoint.interface
    summary.hypervisor_id = hypervisor_device.id if hypervisor_device else None
    summary.hypervisor_name = hypervisor_device.display_name if hypervisor_device else None
    summary.complete = True
    return summary


def _switch_links(
    switch_id: str,
    devices: dict[str, InventoryDevice],
    connections: list[InventoryConnection],
) -> list[InventoryConnection]:
    result: list[InventoryConnection] = []
    for connection in connections:
        if connection.a.device_id != switch_id and connection.b.device_id != switch_id:
            continue
        remote_endpoint = _other_endpoint(connection, switch_id)
        remote_device = devices.get(remote_endpoint.device_id)
        if not remote_device or remote_device.type != "switch":
            continue
        if connection.role in {None, "switch-uplink"}:
            result.append(connection)
    return result


def _is_hypervisor_access_connection(
    connection: InventoryConnection,
    switch_id: str,
    devices: dict[str, InventoryDevice],
    hypervisor_ip: str,
) -> bool:
    if connection.a.device_id != switch_id and connection.b.device_id != switch_id:
        return False
    remote_endpoint = _other_endpoint(connection, switch_id)
    remote_device = devices.get(remote_endpoint.device_id)
    if not remote_device or remote_device.type != "hypervisor":
        return False
    if remote_device.ip_address != hypervisor_ip:
        return False
    return connection.role in {None, "hypervisor-access"}


def _vlan_pool(active: InventoryDevice) -> list[int]:
    if active.free_vlans:
        return [vlan for vlan in active.free_vlans if 1 <= vlan <= 4094]
    if active.vlan_range:
        return list(range(active.vlan_range.start, active.vlan_range.end + 1))
    return []


def _range_from_pool(pool: list[int]) -> VlanRange | None:
    if not pool:
        return None
    ordered = sorted(set(pool))
    return VlanRange(start=ordered[0], end=ordered[-1])


def _path_has_credentials(path: HardwarePathSummary, devices: dict[str, InventoryDevice]) -> bool:
    if not path.access_switch_id or not path.upstream_switch_id:
        return False
    access = devices.get(path.access_switch_id)
    upstream = devices.get(path.upstream_switch_id)
    return _device_has_credentials(access) and _device_has_credentials(upstream)


def _device_has_credentials(device: InventoryDevice | None) -> bool:
    if not device or device.type != "switch":
        return False
    metadata = _switch_metadata(device)
    return bool(metadata.connections.ip and metadata.credentials.username and metadata.credentials.password)


def _find_switch_by_name(devices: dict[str, InventoryDevice], switch_name: str) -> InventoryDevice | None:
    for device in devices.values():
        if device.type != "switch":
            continue
        metadata_name = device.switch_metadata.name if device.switch_metadata else None
        if device.display_name == switch_name or metadata_name == switch_name:
            return device
    return None


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


def _normalize_hardware_state(inventory: InventoryFile) -> None:
    for hardware in inventory.hardware:
        if hardware.available:
            hardware.reservation = None


def _resolve_inventory_state_path(path: Path, state_path: Path | None) -> Path:
    if state_path is not None:
        return state_path
    if path == INVENTORY_PATH:
        return INVENTORY_STATE_PATH
    suffix = "".join(path.suffixes)
    if not suffix:
        return path.with_name(f"{path.name}.local")
    stem = path.name[: -len(suffix)]
    return path.with_name(f"{stem}.local{suffix}")


def _load_inventory_state(path: Path) -> InventoryStateFile:
    if not path.exists():
        return InventoryStateFile()
    with path.open() as fh:
        return InventoryStateFile.model_validate(json.load(fh))


def _save_inventory_state(state: InventoryStateFile, path: Path) -> None:
    if not state.hardware:
        if path.exists():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        json.dump(state.model_dump(mode="json"), fh, indent=2)
        fh.write("\n")


def _build_local_inventory_state(
    inventory: InventoryFile,
    existing: InventoryStateFile,
    *,
    preserve_existing: bool,
) -> InventoryStateFile:
    hardware_state: dict[str, HardwareLocalState] = {}
    for hardware in inventory.hardware:
        state = HardwareLocalState(available=hardware.available, reservation=hardware.reservation)
        if preserve_existing and hardware.id in existing.hardware:
            state = existing.hardware[hardware.id]
        if state.available and state.reservation is None:
            continue
        hardware_state[hardware.id] = state
    return InventoryStateFile(hardware=hardware_state)


def _apply_local_inventory_state(
    inventory: InventoryFile,
    state: InventoryStateFile,
    *,
    clear_missing: bool,
) -> None:
    if clear_missing:
        for hardware in inventory.hardware:
            hardware.available = True
            hardware.reservation = None
        for device in inventory.devices.values():
            if device.type == "edge":
                device.available = True
                device.reservation = None

    for hardware in inventory.hardware:
        local_state = state.hardware.get(hardware.id)
        if not local_state:
            continue
        hardware.available = local_state.available
        hardware.reservation = local_state.reservation

    if not inventory.devices:
        return
    for device in inventory.devices.values():
        if device.type != "edge":
            continue
        group_id = device.ha_group_id or device.id
        local_state = state.hardware.get(group_id)
        if not local_state:
            continue
        device.available = local_state.available
        device.reservation = local_state.reservation


def _strip_local_state_from_persisted_inventory(persisted: dict[str, Any]) -> None:
    for device in persisted.get("devices", {}).values():
        device.pop("available", None)
        device.pop("reservation", None)


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
            "vlan_range": item.vlan_range.model_dump(mode="json") if item.vlan_range else None,
            "hypervisor_ip": item.hypervisor_ip,
            "reservation": item.reservation.model_dump(mode="json") if item.reservation else None,
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
            if port.switch_active_port:
                connections.append(
                    {
                        "id": f"{active_id}-{port.logical_interface}-{switch_id}",
                        "a": {"device_id": active_id, "interface": port.logical_interface},
                        "b": {"device_id": switch_id, "interface": port.switch_active_port},
                        "vlans": port.switch_vlans,
                        "tagged_vlans": port.tagged_vlans or list(port.segment_vlans.values()),
                        "untagged_vlan": port.untagged_vlan or (port.switch_vlans[0] if port.switch_vlans else None),
                        "role": "edge-access",
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
                        "role": "edge-access",
                    }
                )
    return {"devices": devices, "connections": connections, "allocations": []}


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
