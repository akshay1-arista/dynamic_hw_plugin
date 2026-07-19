from __future__ import annotations

from functools import lru_cache
import json
import re
import shutil
import uuid
import zipfile
from pathlib import Path
from typing import Any

from .audit import append_audit_events
from .config import INVENTORY_PATH, OUTPUTS_ROOT, REFERENCE_CONFIG_ROOT
from .inventory import (
    load_inventory,
    path_has_credentials,
    reserve_generated_hardware,
    resolve_mapping_path,
)
from .models import (
    EdgePortMapping,
    GenerateRequest,
    GenerateResult,
    GenerateMappingStatus,
    HardwareAllocation,
    HardwareEdge,
    HardwarePortAllocation,
    InterfaceOverride,
    InventoryDevice,
    InventoryFile,
    JsonObject,
    RunMappingMetadata,
    RunMetadata,
    SavedGenerateRequest,
    ValidationMessage,
)
from .reference import resolve_reference_path


class GenerationError(ValueError):
    pass


def generate_topology(
    request: GenerateRequest,
    *,
    reference_root: Path = REFERENCE_CONFIG_ROOT,
    inventory_path: Path = INVENTORY_PATH,
    outputs_root: Path = OUTPUTS_ROOT,
) -> GenerateResult:
    reference_path = resolve_reference_path(request.reference_topology_id, reference_root)
    if not reference_path.exists():
        raise GenerationError(f"Reference topology does not exist: {request.reference_topology_id}")

    inventory = load_inventory(inventory_path)
    hardware_by_id = {item.id: item for item in inventory.hardware}
    recovered_hardware_ids = _merge_saved_hardware_snapshots(request.mappings, hardware_by_id)
    _validate_request(request, hardware_by_id)

    topology_suffix = uuid.uuid4().hex[:6]
    generated_topology_name = f"{request.topology_name}-{topology_suffix}"
    run_id = uuid.uuid4().hex[:12]
    run_root = outputs_root / f"{run_id}-{topology_suffix}"
    topology_path = run_root / generated_topology_name
    run_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(reference_path, topology_path)

    messages: list[ValidationMessage] = []
    config_path = topology_path / "config.json"
    config = _load_json(config_path)
    if "testbed" not in config:
        config["testbed"] = {}
    old_topology_name = config["testbed"].get("name")
    config["testbed"]["name"] = generated_topology_name
    config["testbed"]["description"] = f"Generated from {request.reference_topology_id} topology"

    global_replacements: list[tuple[str, str]] = []
    if old_topology_name:
        global_replacements.append((old_topology_name, generated_topology_name))

    now = _utc_now()
    run_metadata = RunMetadata(
        run_id=run_id,
        topology_name=generated_topology_name,
        reference_topology_id=request.reference_topology_id,
        requested_by=request.requested_by,
        request=SavedGenerateRequest(
            topology_name=request.topology_name,
            reference_topology_id=request.reference_topology_id,
            hypervisor_ip=request.hypervisor_ip,
            hypervisor_interface=request.hypervisor_interface,
            mappings=request.mappings,
        ),
        created_at=now,
        updated_at=now,
    )
    mapping_statuses: list[GenerateMappingStatus] = []

    for mapping in request.mappings:
        hardware = hardware_by_id[mapping.hardware_id]
        if mapping.hardware_id in recovered_hardware_ids:
            messages.append(
                ValidationMessage(
                    level="warning",
                    message=(
                        f"Using saved hardware snapshot for {hardware.display_name} because the current inventory "
                        "could not provide usable switch connection data. Stored port mappings and VLAN assignments "
                        "were reused."
                    ),
                )
            )
        branch = _find_branch(config, mapping.branch_name)
        edge = _find_edge(branch, mapping.edge_name)
        reference_branch = _clone_json(branch)

        old_branch_name = branch["name"]
        old_edge_name = edge["name"]
        reference_ha_enabled = bool(edge.get("ha_enabled"))
        new_branch_name = mapping.target_branch_name or (
            f"{old_branch_name}-{hardware.model_suffix}" if request.branch_rename else old_branch_name
        )
        new_edge_name = mapping.target_edge_name or f"{old_edge_name}-{hardware.model_suffix}"

        branch["name"] = new_branch_name
        if reference_ha_enabled and not hardware.ha:
            messages.append(
                ValidationMessage(
                    level="warning",
                    message=(
                        f"{old_branch_name}/{old_edge_name} is HA enabled in the reference topology, "
                        f"but {hardware.display_name} is standalone. Generated topology converts it to standalone."
                    ),
                )
            )
        _apply_hardware_to_edge(edge, hardware, new_edge_name)
        remote_updates, dropped_links, port_messages, allocation = _apply_port_mappings(
            edge,
            hardware,
            old_branch_name,
            old_edge_name,
            mapping.interface_overrides,
            inventory=inventory,
            reference_topology_id=request.reference_topology_id,
            reference_branch_name=mapping.branch_name,
            reference_edge_name=mapping.edge_name,
        )
        _apply_inventory_free_vlans_to_edge(edge, inventory, hardware.id, allocation.reserved_vlans)
        messages.extend(port_messages)
        mapping_path = resolve_mapping_path(
            inventory,
            [port.switch_name for port in allocation.ports],
            request.hypervisor_ip,
            request.hypervisor_interface,
        )
        edge["l2_switches"] = _build_l2_switches(
            hardware,
            request.hypervisor_ip,
            request.hypervisor_interface,
            mapping_path.access_uplink_port if mapping_path else None,
            {port.logical_interface.upper() for port in allocation.ports},
        )
        mapping_reason = None
        mapping_ready = False
        if mapping_path is None:
            mapping_reason = (
                f"Could not resolve a unique imported path from the selected access switch to hypervisor "
                f"{request.hypervisor_ip}."
            )
            messages.append(
                ValidationMessage(
                    level="warning",
                    message=(
                        f"Switch auto-config disabled for {mapping.branch_name}/{mapping.edge_name}: "
                        f"{mapping_reason}"
                    ),
                )
            )
        elif not path_has_credentials(mapping_path, inventory):
            mapping_reason = "The resolved access or upstream switch is missing stored credentials."
            messages.append(
                ValidationMessage(
                    level="warning",
                    message=(
                        f"Switch auto-config disabled for {mapping.branch_name}/{mapping.edge_name}: "
                        f"{mapping_reason}"
                    ),
                )
            )
        else:
            mapping_ready = True
        run_metadata.mappings.append(
            RunMappingMetadata(
                hardware_id=hardware.id,
                branch_name=mapping.branch_name,
                edge_name=mapping.edge_name,
                path=mapping_path,
                allocations=allocation.ports,
            )
        )
        mapping_statuses.append(
            GenerateMappingStatus(
                hardware_id=hardware.id,
                hardware_display_name=hardware.display_name,
                branch_name=mapping.branch_name,
                edge_name=mapping.edge_name,
                path_resolved=bool(mapping_path and mapping_path.complete),
                auto_config_ready=mapping_ready,
                reason=mapping_reason,
                path=mapping_path,
            )
        )

        _apply_remote_updates_to_config(config, edge, remote_updates)
        _drop_linked_interfaces(config, edge, dropped_links)
        _apply_companion_file_updates(topology_path, reference_branch, branch)

        global_replacements.extend(
            [
                (old_branch_name, new_branch_name),
                (old_edge_name, new_edge_name),
            ]
        )
        messages.append(
            ValidationMessage(
                level="info",
                message=f"Mapped {old_branch_name}/{old_edge_name} to {hardware.display_name}",
            )
        )

    _write_json(config_path, config)
    _apply_global_replacements(topology_path, global_replacements, skip_paths={config_path})
    validation_messages = _validate_generated_json(topology_path)
    messages.extend(validation_messages)
    zip_path = _zip_topology(topology_path)
    run_metadata.can_configure_switches = all(
        mapping.path and mapping.path.complete and path_has_credentials(mapping.path, inventory)
        for mapping in run_metadata.mappings
    )
    run_metadata.mapping_statuses = mapping_statuses
    run_metadata.messages = messages
    run_metadata.updated_at = _utc_now()
    _write_run_metadata(run_root, run_metadata)
    _saved_inventory, reservation_events = reserve_generated_hardware(
        [mapping.hardware_id for mapping in request.mappings],
        request.requested_by,
        run_id,
        generated_topology_name,
        inventory_path,
    )
    append_audit_events(reservation_events)

    return GenerateResult(
        run_id=run_id,
        topology_name=generated_topology_name,
        topology_path=str(topology_path),
        zip_path=str(zip_path),
        download_url=f"/api/runs/{run_id}/download",
        can_configure_switches=run_metadata.can_configure_switches,
        mapping_statuses=mapping_statuses,
        messages=messages,
    )


def _validate_request(request: GenerateRequest, hardware_by_id: dict[str, HardwareEdge]) -> None:
    hardware_ids = [mapping.hardware_id for mapping in request.mappings]
    target_edges = [(mapping.branch_name, mapping.edge_name) for mapping in request.mappings]
    if len(hardware_ids) != len(set(hardware_ids)):
        raise GenerationError("A hardware inventory item can only be mapped once per run")
    if len(target_edges) != len(set(target_edges)):
        raise GenerationError("A target branch/edge can only be mapped once per run")
    missing = [hardware_id for hardware_id in hardware_ids if hardware_id not in hardware_by_id]
    if missing:
        raise GenerationError(f"Unknown hardware inventory id: {', '.join(missing)}")
    for mapping in request.mappings:
        hardware = hardware_by_id[mapping.hardware_id]
        if not _hardware_is_available_for_request(hardware, request):
            reservation_actor = hardware.reservation.actor if hardware.reservation else None
            reserved_by = (
                f"{reservation_actor.name} ({reservation_actor.email})"
                if reservation_actor
                else "another user"
            )
            raise GenerationError(
                f"{hardware.display_name} is currently reserved. Mark it available before generating again. Reserved by {reserved_by}."
            )
        if not _topology_ports(hardware):
            raise GenerationError(f"No connected switch ports found for {hardware.id}")


def _hardware_is_available_for_request(hardware: HardwareEdge, request: GenerateRequest) -> bool:
    if hardware.available:
        return True
    reservation = hardware.reservation
    if reservation is None or reservation.reason != "topology-generation":
        return False
    return reservation.actor.email == request.requested_by.email


def _merge_saved_hardware_snapshots(
    mappings,
    hardware_by_id: dict[str, HardwareEdge],
) -> set[str]:
    recovered_hardware_ids: set[str] = set()
    for mapping in mappings:
        if mapping.saved_hardware is None:
            continue
        if mapping.saved_hardware.id != mapping.hardware_id:
            raise GenerationError(
                f"Saved hardware snapshot id mismatch for {mapping.branch_name}/{mapping.edge_name}: "
                f"{mapping.saved_hardware.id} != {mapping.hardware_id}"
            )
        current_hardware = hardware_by_id.get(mapping.hardware_id)
        if current_hardware is not None:
            if _topology_ports(current_hardware) or not _topology_ports(mapping.saved_hardware):
                continue
        hardware_by_id[mapping.hardware_id] = mapping.saved_hardware
        recovered_hardware_ids.add(mapping.hardware_id)
    return recovered_hardware_ids


def _load_json(path: Path) -> JsonObject:
    with path.open() as fh:
        return json.load(fh)


def _clone_json(data: JsonObject) -> JsonObject:
    return json.loads(json.dumps(data))


def _write_json(path: Path, data: JsonObject) -> None:
    with path.open("w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")


def _write_run_metadata(run_root: Path, metadata: RunMetadata) -> None:
    with (run_root / "run_metadata.json").open("w") as fh:
        json.dump(metadata.model_dump(mode="json"), fh, indent=2)
        fh.write("\n")


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def resolve_run_root(run_id: str, outputs_root: Path = OUTPUTS_ROOT) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise GenerationError("Invalid run id")

    outputs_root = outputs_root.resolve()
    candidates: list[Path] = []
    exact = outputs_root / run_id
    if exact.exists():
        candidates.append(exact.resolve())

    for path in outputs_root.glob(f"{run_id}-*"):
        if path.is_dir():
            candidates.append(path.resolve())

    unique_candidates = list(dict.fromkeys(candidates))
    if not unique_candidates:
        raise GenerationError(f"Run output not found for {run_id}")
    if len(unique_candidates) > 1:
        raise GenerationError(f"Multiple run outputs found for {run_id}")

    run_root = unique_candidates[0]
    if outputs_root not in run_root.parents and run_root != outputs_root:
        raise GenerationError("Invalid run id")
    return run_root


def _find_branch(config: JsonObject, branch_name: str) -> JsonObject:
    for branch in config.get("topology", {}).get("branches", []):
        if branch.get("name") == branch_name:
            return branch
    raise GenerationError(f"Branch not found in reference topology: {branch_name}")


def _find_edge(branch: JsonObject, edge_name: str) -> JsonObject:
    for edge in branch.get("edges", []):
        if edge.get("name") == edge_name:
            return edge
    raise GenerationError(f"Edge not found in branch {branch.get('name')}: {edge_name}")


def _apply_hardware_to_edge(edge: JsonObject, hardware: HardwareEdge, new_edge_name: str) -> None:
    edge["name"] = new_edge_name
    edge["model"] = hardware.model
    edge["ha_enabled"] = hardware.ha
    edge["dpdk_enabled"] = True
    edge["slno"] = hardware.active_serial
    if hardware.ha:
        edge["standby_slno"] = hardware.standby_serial
    else:
        edge.pop("standby_slno", None)


def _apply_port_mappings(
    edge: JsonObject,
    hardware: HardwareEdge,
    branch_name: str,
    edge_name: str,
    interface_overrides: list[InterfaceOverride] | None = None,
    *,
    inventory: InventoryFile | None = None,
    reference_topology_id: str = "",
    reference_branch_name: str = "",
    reference_edge_name: str = "",
) -> tuple[dict[str, dict[str, Any]], set[str], list[ValidationMessage], HardwareAllocation]:
    remote_updates: dict[str, dict[str, Any]] = {}
    dropped_links: set[str] = set()
    messages: list[ValidationMessage] = []
    reference_interfaces = list(edge.get("interfaces", []))
    mappable_reference_interfaces = [
        interface for interface in reference_interfaces if not _is_loopback_interface(interface)
    ]
    hardware_ports = _topology_ports(hardware)
    mapped_interfaces, mapped_ports, dropped_interfaces = _resolve_port_assignments(
        reference_interfaces,
        hardware_ports,
        interface_overrides or [],
        hardware,
    )
    allocation = (
        _reserve_vlans_for_mapping(
            inventory,
            hardware,
            reference_topology_id,
            reference_branch_name,
            reference_edge_name,
            edge,
            mapped_interfaces,
            mapped_ports,
            interface_overrides or [],
        )
        if inventory
        else _legacy_allocation_from_ports(
            hardware,
            branch_name,
            edge_name,
            edge,
            mapped_interfaces,
            mapped_ports,
            interface_overrides=interface_overrides or [],
        )
    )
    allocation_by_port = {item.logical_interface.upper(): item for item in allocation.ports}

    if dropped_interfaces:
        mapped_interface_ids = {id(interface) for interface in mapped_interfaces}
        edge["interfaces"] = [
            interface
            for interface in reference_interfaces
            if _is_loopback_interface(interface) or id(interface) in mapped_interface_ids
        ]
        for interface in dropped_interfaces:
            if interface.get("link"):
                dropped_links.add(interface["link"])
        messages.append(
            ValidationMessage(
                level="warning",
                message=(
                    f"{branch_name}/{edge_name} reference edge has {len(mappable_reference_interfaces)} "
                    f"physical interface(s), but only {len(mapped_interfaces)} mapped hardware connection(s) were kept"
                    f"{' after applying custom interface overrides' if interface_overrides else ''}. "
                    f"Dropped {len(dropped_interfaces)} unassigned reference interface(s) and matching remote interfaces where link names were available."
                ),
            )
        )

    ignored_hardware_count = max(len(hardware_ports) - len(mapped_ports), 0)
    if ignored_hardware_count:
        messages.append(
            ValidationMessage(
                level="warning",
                message=(
                    f"{hardware.display_name} has {len(hardware_ports)} connected hardware ports, "
                    f"but only {len(mapped_ports)} mapped reference interface(s) were used"
                    f"{' after applying custom interface overrides' if interface_overrides else ''}. "
                    f"Ignored {ignored_hardware_count} extra hardware connection(s)."
                ),
            )
        )

    for interface, port in zip(mapped_interfaces, mapped_ports):
        port_assignment = allocation_by_port.get(port.logical_interface.upper())
        if port_assignment:
            port.switch_vlans = list(port_assignment.switch_vlans)
            port.tagged_vlans = list(port_assignment.tagged_vlans)
            port.untagged_vlan = port_assignment.untagged_vlan
            port.segment_vlans = dict(port_assignment.segment_vlans)
        old_link = interface.get("link")
        old_logical_interface = interface.get("logical_interface")
        port.link = old_link or port.link
        if port_assignment:
            port_assignment.link = port.link
        port.logical_name = str(interface.get("logical_name") or old_logical_interface or port.logical_interface)
        interface["name"] = port.name
        interface["logical_interface"] = port.logical_interface
        interface["link"] = port.link
        _update_wanlink_name(interface, old_logical_interface, port.logical_interface)

        segment_vlans = _derive_segment_vlans(edge, interface, port)
        port.segment_vlans = segment_vlans

        if interface.get("mode") == "switched":
            _update_edge_vlan_table(edge, port)
        if old_link:
            remote_updates[old_link] = {
                "new_link": port.link,
                "global_vlan": port.global_vlan,
                "segment_vlans": port.segment_vlans,
                "old_logical_interface": old_logical_interface,
                "new_logical_interface": port.logical_interface,
            }

    return remote_updates, dropped_links, messages, allocation


def _reserve_vlans_for_mapping(
    inventory: InventoryFile,
    hardware: HardwareEdge,
    reference_topology_id: str,
    branch_name: str,
    edge_name: str,
    edge: JsonObject,
    mapped_interfaces: list[JsonObject],
    mapped_ports: list[EdgePortMapping],
    interface_overrides: list[InterfaceOverride],
) -> HardwareAllocation:
    override_map = _normalize_interface_overrides(interface_overrides)
    interface_fingerprint = _mapping_interface_fingerprint(
        edge,
        mapped_interfaces,
        mapped_ports,
        override_map=override_map,
    )

    pool = _inventory_vlan_pool(inventory, hardware.id)
    available = list(pool)
    reserved_vlans: list[int] = []
    port_allocations: list[HardwarePortAllocation] = []
    explicit_reserved: set[int] = set()
    dynamic_pairs: list[tuple[JsonObject, EdgePortMapping]] = []

    for interface, port in zip(mapped_interfaces, mapped_ports):
        reference_key = _reference_interface_key(interface) or port.logical_interface.upper()
        override = override_map.get(reference_key)
        if override and override.switch_vlans:
            if pool:
                invalid_vlans = sorted(vlan for vlan in override.switch_vlans if vlan not in pool)
                if invalid_vlans:
                    raise GenerationError(
                        f"VLAN override for {reference_key} on {hardware.display_name} must stay within the hardware VLAN range: "
                        f"{', '.join(str(vlan) for vlan in invalid_vlans)}"
                    )
            conflicts = sorted(vlan for vlan in override.switch_vlans if vlan in explicit_reserved)
            if conflicts:
                raise GenerationError(
                    f"VLAN override for {reference_key} on {hardware.display_name} conflicts with another interface in this mapping: "
                    f"{', '.join(str(vlan) for vlan in conflicts)}"
                )
            port_allocations.append(
                _port_allocation_from_override(edge, hardware, interface, port, override.switch_vlans)
            )
            reserved_vlans.extend(override.switch_vlans)
            explicit_reserved.update(override.switch_vlans)
        else:
            dynamic_pairs.append((interface, port))

    available = [vlan for vlan in available if vlan not in explicit_reserved]
    cursor = 0

    for interface, port in dynamic_pairs:
        required_vlan_count = _required_vlan_count(edge, interface)
        if required_vlan_count == 0:
            if port.switch_vlans or port.untagged_vlan is not None:
                port_allocations.append(_port_allocation_from_port(edge, hardware, interface, port))
                continue
            if available and cursor < len(available):
                switch_vlans = [available[cursor]]
                reserved_vlans.extend(switch_vlans)
                port_allocations.append(
                    _port_allocation_from_override(edge, hardware, interface, port, switch_vlans)
                )
                cursor += 1
                continue
            raise GenerationError(
                f"Reference interface {_reference_interface_key(interface) or port.logical_interface} on {hardware.display_name} "
                "needs 1 native VLAN for the mapped L2 switch port, but no free VLAN is available in the hardware range"
            )
        if available and cursor + required_vlan_count <= len(available):
            switch_vlans = available[cursor : cursor + required_vlan_count]
            reserved_vlans.extend(switch_vlans)
            port_allocations.append(
                _port_allocation_from_override(edge, hardware, interface, port, switch_vlans)
            )
            cursor += required_vlan_count
            continue
        port_allocations.append(_port_allocation_from_port(edge, hardware, interface, port))

    allocation = HardwareAllocation(
        hardware_id=hardware.id,
        branch_name=branch_name,
        edge_name=edge_name,
        reference_topology_id=reference_topology_id,
        interface_fingerprint=interface_fingerprint,
        reserved_vlans=sorted(set(reserved_vlans)),
        ports=port_allocations,
    )
    return allocation


def _mapping_interface_fingerprint(
    edge: JsonObject,
    mapped_interfaces: list[JsonObject],
    mapped_ports: list[EdgePortMapping],
    *,
    override_map: dict[str, InterfaceOverride] | None = None,
) -> str:
    tokens = []
    for interface, port in zip(mapped_interfaces, mapped_ports):
        needs_native, segment_names = _interface_vlan_requirements(edge, interface)
        reference_key = _reference_interface_key(interface) or ""
        override_switch_vlans = (
            ",".join(str(vlan) for vlan in override_map[reference_key].switch_vlans)
            if override_map and reference_key in override_map and override_map[reference_key].switch_vlans
            else ""
        )
        token_parts = [
            reference_key,
            port.logical_interface,
            "native" if needs_native else "none",
            str(len(segment_names)),
            ",".join(segment_names),
        ]
        if override_switch_vlans:
            token_parts.append(override_switch_vlans)
        tokens.append(
            ":".join(token_parts)
        )
    return "|".join(tokens)


def _interface_vlan_requirements(edge: JsonObject, interface: JsonObject) -> tuple[bool, list[str]]:
    vlans = interface.get("vlans") if isinstance(interface.get("vlans"), list) else []
    subinterfaces = interface.get("subinterfaces") if isinstance(interface.get("subinterfaces"), list) else []
    if interface.get("mode") == "switched":
        segment_names = _segment_names_for_interface(edge, interface)[: max(len(vlans) - 1, 0)]
        return True, segment_names
    if subinterfaces:
        segment_names = _segment_names_for_interface(edge, interface)
        return True, segment_names
    if vlans:
        return True, []
    return False, []


def _inventory_vlan_pool(inventory: InventoryFile, hardware_id: str) -> list[int]:
    active = _find_inventory_active_device(inventory, hardware_id)
    if not active:
        return []
    if active.free_vlans:
        return [vlan for vlan in active.free_vlans if 1 <= vlan <= 4094]
    if active.vlan_range:
        return list(range(active.vlan_range.start, active.vlan_range.end + 1))
    return []


def _inventory_free_vlans(
    inventory: InventoryFile,
    hardware_id: str,
    used_vlans: list[int] | None = None,
) -> list[int]:
    used = set(used_vlans or [])
    return [vlan for vlan in _inventory_vlan_pool(inventory, hardware_id) if vlan not in used]


def _apply_inventory_free_vlans_to_edge(
    edge: JsonObject,
    inventory: InventoryFile,
    hardware_id: str,
    used_vlans: list[int] | None = None,
) -> None:
    free_vlans = _inventory_free_vlans(inventory, hardware_id, used_vlans)
    edge.setdefault("custom_params", {})["free_vlans"] = free_vlans


def _find_inventory_active_device(inventory: InventoryFile, hardware_id: str) -> InventoryDevice | None:
    candidates = [
        device
        for device in inventory.devices.values()
        if device.type == "edge" and (device.ha_group_id or device.id) == hardware_id
    ]
    return next((device for device in candidates if device.ha_role == "active"), candidates[0] if candidates else None)


def _legacy_allocation_from_ports(
    hardware: HardwareEdge,
    branch_name: str,
    edge_name: str,
    edge: JsonObject,
    mapped_interfaces: list[JsonObject],
    mapped_ports: list[EdgePortMapping],
    *,
    reference_topology_id: str = "",
    interface_fingerprint: str | None = None,
    interface_overrides: list[InterfaceOverride] | None = None,
) -> HardwareAllocation:
    override_map = _normalize_interface_overrides(interface_overrides or [])
    ports = [
        _port_allocation_from_override(edge, hardware, interface, port, override_map[reference_key].switch_vlans)
        if (reference_key := (_reference_interface_key(interface) or port.logical_interface.upper())) in override_map
        and override_map[reference_key].switch_vlans
        else _port_allocation_from_port(edge, hardware, interface, port)
        for interface, port in zip(mapped_interfaces, mapped_ports)
    ]
    reserved_vlans = sorted(
        {
            vlan
            for port in ports
            for vlan in port.switch_vlans
        }
    )
    return HardwareAllocation(
        hardware_id=hardware.id,
        branch_name=branch_name,
        edge_name=edge_name,
        reference_topology_id=reference_topology_id or None,
        interface_fingerprint=interface_fingerprint
        or _mapping_interface_fingerprint(edge, mapped_interfaces, mapped_ports, override_map=override_map),
        reserved_vlans=reserved_vlans,
        ports=ports,
    )


def _required_vlan_count(edge: JsonObject, interface: JsonObject) -> int:
    needs_native, segment_names = _interface_vlan_requirements(edge, interface)
    return (1 if needs_native else 0) + len(segment_names)


def _allocation_switch_name(hardware: HardwareEdge, port: EdgePortMapping) -> str:
    return port.switch_name or (hardware.switches[0].name if hardware.switches else hardware.switch.name)


def _port_allocation_from_port(
    edge: JsonObject,
    hardware: HardwareEdge,
    interface: JsonObject,
    port: EdgePortMapping,
) -> HardwarePortAllocation:
    switch_vlans = list(port.switch_vlans)
    if not switch_vlans and port.untagged_vlan is not None:
        switch_vlans = [port.untagged_vlan]
    tagged_vlans = list(port.tagged_vlans)
    if not tagged_vlans and len(switch_vlans) > 1 and port.untagged_vlan is not None:
        tagged_vlans = switch_vlans[1:]
    _needs_native, segment_names = _interface_vlan_requirements(edge, interface)
    return HardwarePortAllocation(
        reference_interface=_reference_interface_key(interface) or port.logical_interface,
        logical_interface=port.logical_interface,
        link=port.link,
        switch_name=_allocation_switch_name(hardware, port),
        switch_active_port=port.switch_active_port,
        switch_standby_port=port.switch_standby_port,
        switch_vlans=switch_vlans,
        tagged_vlans=tagged_vlans,
        untagged_vlan=port.untagged_vlan,
        segment_vlans={
            segment_name: tagged_vlans[index]
            for index, segment_name in enumerate(segment_names)
            if index < len(tagged_vlans)
        },
    )


def _port_allocation_from_override(
    edge: JsonObject,
    hardware: HardwareEdge,
    interface: JsonObject,
    port: EdgePortMapping,
    switch_vlans: list[int],
) -> HardwarePortAllocation:
    needs_native, segment_names = _interface_vlan_requirements(edge, interface)
    required_count = (1 if needs_native else 0) + len(segment_names)
    if required_count == 0:
        if len(switch_vlans) != 1:
            raise GenerationError(
                f"Reference interface {_reference_interface_key(interface) or port.logical_interface} uses the switch as an untagged access link "
                f"and requires exactly 1 native VLAN value, but received {len(switch_vlans)}"
            )
        native_vlan = switch_vlans[0]
        return HardwarePortAllocation(
            reference_interface=_reference_interface_key(interface) or port.logical_interface,
            logical_interface=port.logical_interface,
            link=port.link,
            switch_name=_allocation_switch_name(hardware, port),
            switch_active_port=port.switch_active_port,
            switch_standby_port=port.switch_standby_port,
            switch_vlans=list(switch_vlans),
            tagged_vlans=[],
            untagged_vlan=native_vlan,
            segment_vlans={},
        )
    if len(switch_vlans) != required_count:
        raise GenerationError(
            f"Reference interface {_reference_interface_key(interface) or port.logical_interface} requires {required_count} VLAN value(s), "
            f"but received {len(switch_vlans)}"
        )
    native_vlan = switch_vlans[0] if needs_native else None
    tagged_vlans = switch_vlans[1:] if needs_native else list(switch_vlans)
    return HardwarePortAllocation(
        reference_interface=_reference_interface_key(interface) or port.logical_interface,
        logical_interface=port.logical_interface,
        link=port.link,
        switch_name=_allocation_switch_name(hardware, port),
        switch_active_port=port.switch_active_port,
        switch_standby_port=port.switch_standby_port,
        switch_vlans=list(switch_vlans),
        tagged_vlans=tagged_vlans,
        untagged_vlan=native_vlan,
        segment_vlans={
            segment_name: tagged_vlans[index]
            for index, segment_name in enumerate(segment_names)
            if index < len(tagged_vlans)
        },
    )


def _resolve_port_assignments(
    reference_interfaces: list[JsonObject],
    hardware_ports: list[EdgePortMapping],
    interface_overrides: list[InterfaceOverride],
    hardware: HardwareEdge,
) -> tuple[list[JsonObject], list[EdgePortMapping], list[JsonObject]]:
    reference_interfaces = [
        interface for interface in reference_interfaces if not _is_loopback_interface(interface)
    ]
    auto_assignable_hardware_ports = [port for port in hardware_ports if not port.manual_mapping_required]
    if not interface_overrides:
        mapped_count = min(len(reference_interfaces), len(auto_assignable_hardware_ports))
        mapped_interfaces = reference_interfaces[:mapped_count]
        mapped_ports = _match_ports_to_reference_interfaces(mapped_interfaces, auto_assignable_hardware_ports)
        dropped_interfaces = reference_interfaces[mapped_count:]
        return mapped_interfaces, mapped_ports, dropped_interfaces

    override_map = _normalize_interface_overrides(interface_overrides)
    loopback_overrides = sorted(reference_key for reference_key in override_map if _is_loopback_name(reference_key))
    if loopback_overrides:
        raise GenerationError(
            "Loopback interface override(s) are not allowed because loopback interfaces are preserved as-is: "
            f"{', '.join(loopback_overrides)}"
        )

    hardware_ports_by_name = {port.logical_interface.upper(): port for port in auto_assignable_hardware_ports}
    all_ports_by_name = {port.logical_interface.upper(): port for port in _ordered_ports(hardware)}
    reference_keys = [_reference_interface_key(interface) for interface in reference_interfaces]
    missing_reference = sorted(
        reference_key for reference_key in override_map if reference_key not in {key for key in reference_keys if key}
    )
    if missing_reference:
        raise GenerationError(
            f"Unknown reference interface override(s): {', '.join(missing_reference)}"
        )

    invalid_hardware = []
    for hardware_key in [override.hardware_interface for override in override_map.values() if override.hardware_interface]:
        if hardware_key not in all_ports_by_name:
            invalid_hardware.append(hardware_key)
    if invalid_hardware:
        raise GenerationError(
            f"Unknown hardware interface override(s): {', '.join(sorted(invalid_hardware))}"
        )

    explicit_pairs: dict[str, EdgePortMapping] = {}
    explicit_drop_keys = {
        reference_key
        for reference_key, override in override_map.items()
        if override.hardware_interface is None
    }
    used_hardware_keys: set[str] = set()
    for reference_key, override in override_map.items():
        hardware_key = override.hardware_interface
        if hardware_key is None:
            continue
        explicit_pairs[reference_key] = all_ports_by_name[hardware_key]
        used_hardware_keys.add(hardware_key)

    remaining_interfaces = [
        interface
        for interface in reference_interfaces
        if (reference_key := _reference_interface_key(interface)) and reference_key not in override_map
    ]
    remaining_hardware_ports = [
        port for port in auto_assignable_hardware_ports if port.logical_interface.upper() not in used_hardware_keys
    ]
    auto_count = min(len(remaining_interfaces), len(remaining_hardware_ports))
    auto_interfaces = remaining_interfaces[:auto_count]
    auto_ports = _match_ports_to_reference_interfaces(auto_interfaces, remaining_hardware_ports)
    auto_pairs = {
        _reference_interface_key(interface): port
        for interface, port in zip(auto_interfaces, auto_ports)
    }

    mapped_interfaces: list[JsonObject] = []
    mapped_ports: list[EdgePortMapping] = []
    dropped_interfaces: list[JsonObject] = []

    for interface in reference_interfaces:
        reference_key = _reference_interface_key(interface)
        if not reference_key:
            dropped_interfaces.append(interface)
            continue
        if reference_key in explicit_drop_keys:
            dropped_interfaces.append(interface)
            continue
        port = explicit_pairs.get(reference_key) or auto_pairs.get(reference_key)
        if port:
            mapped_interfaces.append(interface)
            mapped_ports.append(port)
        else:
            dropped_interfaces.append(interface)

    if not mapped_interfaces:
        raise GenerationError("Custom interface overrides left no mapped reference interfaces")

    return mapped_interfaces, mapped_ports, dropped_interfaces


def _normalize_interface_overrides(interface_overrides: list[InterfaceOverride]) -> dict[str, InterfaceOverride]:
    override_map: dict[str, InterfaceOverride] = {}
    used_hardware_interfaces: set[str] = set()
    for override in interface_overrides:
        reference_key = override.reference_interface.strip().upper()
        hardware_key = override.hardware_interface.strip().upper() if override.hardware_interface else None
        if reference_key in override_map:
            raise GenerationError(f"Reference interface override specified more than once: {reference_key}")
        if hardware_key and hardware_key in used_hardware_interfaces:
            raise GenerationError(f"Hardware interface override specified more than once: {hardware_key}")
        override_map[reference_key] = InterfaceOverride(
            reference_interface=reference_key,
            hardware_interface=hardware_key,
            switch_vlans=list(override.switch_vlans),
        )
        if hardware_key:
            used_hardware_interfaces.add(hardware_key)
    return override_map


def _reference_interface_key(interface: JsonObject) -> str | None:
    for key in ("logical_interface", "logical_name", "name"):
        value = interface.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
    return None


def _ordered_ports(hardware: HardwareEdge) -> list[EdgePortMapping]:
    return sorted(hardware.ports, key=lambda port: _interface_sort_key(port.logical_interface))


def _topology_ports(hardware: HardwareEdge) -> list[EdgePortMapping]:
    return _ordered_ports(hardware)


def _match_ports_to_reference_interfaces(
    reference_interfaces: list[JsonObject],
    hardware_ports: list[EdgePortMapping],
) -> list[EdgePortMapping]:
    if not reference_interfaces or not hardware_ports:
        return []
    metadata_backed_ports = [port for port in hardware_ports if port.switch_vlans]
    if len(metadata_backed_ports) >= len(reference_interfaces):
        hardware_ports = metadata_backed_ports

    @lru_cache(maxsize=None)
    def assign(reference_index: int, used_mask: int) -> tuple[int, tuple[int, ...]]:
        if reference_index == len(reference_interfaces):
            return 0, ()

        best_score: int | None = None
        best_order: tuple[int, ...] = ()
        for port_index, port in enumerate(hardware_ports):
            if used_mask & (1 << port_index):
                continue
            next_score, next_order = assign(reference_index + 1, used_mask | (1 << port_index))
            score = _port_match_score(reference_interfaces[reference_index], port) + next_score
            if best_score is None or score > best_score:
                best_score = score
                best_order = (port_index, *next_order)

        return (best_score or 0), best_order

    _, order = assign(0, 0)
    return [hardware_ports[index] for index in order]


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


def _port_match_score(interface: JsonObject, port: EdgePortMapping) -> int:
    reference_has_untagged, reference_has_tagged, reference_tagged_count = _reference_vlan_profile(interface)
    port_has_untagged, port_has_tagged, port_tagged_count = _hardware_vlan_profile(port)

    score = 0
    if (reference_has_untagged, reference_has_tagged) == (port_has_untagged, port_has_tagged):
        score += 100
    score += 40 if reference_has_tagged == port_has_tagged else -60
    score += 30 if reference_has_untagged == port_has_untagged else -45
    score -= abs(reference_tagged_count - port_tagged_count) * 5
    if port.switch_vlans:
        score += 10
    return score


def _reference_vlan_profile(interface: JsonObject) -> tuple[bool, bool, int]:
    vlans = interface.get("vlans") if isinstance(interface.get("vlans"), list) else []
    subinterfaces = interface.get("subinterfaces") if isinstance(interface.get("subinterfaces"), list) else []

    if interface.get("mode") == "switched":
        tagged_count = max(len(vlans) - 1, 0)
    else:
        tagged_count = len(subinterfaces)

    has_untagged = bool(vlans)
    has_tagged = tagged_count > 0
    return has_untagged, has_tagged, tagged_count


def _hardware_vlan_profile(port: EdgePortMapping) -> tuple[bool, bool, int]:
    tagged_vlans = list(port.tagged_vlans)
    if not tagged_vlans and len(port.switch_vlans) > 1 and port.untagged_vlan is not None:
        tagged_vlans = port.switch_vlans[1:]

    tagged_count = len(tagged_vlans)
    has_tagged = tagged_count > 0
    has_untagged = port.untagged_vlan is not None or (bool(port.switch_vlans) and not has_tagged)
    return has_untagged, has_tagged, tagged_count


def _derive_segment_vlans(edge: JsonObject, interface: JsonObject, port: EdgePortMapping) -> dict[str, int]:
    existing_vlans = interface.get("vlans") if isinstance(interface.get("vlans"), list) else []
    tagged_vlans = list(port.tagged_vlans)
    if not tagged_vlans and len(port.switch_vlans) > 1:
        tagged_vlans = port.switch_vlans[1:]

    segment_names = _segment_names_for_interface(edge, interface)
    segment_vlans = {
        segment_name: tagged_vlans[index]
        for index, segment_name in enumerate(segment_names)
        if index < len(tagged_vlans)
    }

    if interface.get("mode") == "switched":
        if existing_vlans:
            interface["vlans"] = [existing_vlans[0], *segment_vlans.values()]
        elif segment_vlans:
            interface["vlans"] = list(segment_vlans.values())
        else:
            interface["vlans"] = []

    for subinterface in interface.get("subinterfaces", []):
        segment_name = subinterface.get("segment_name")
        if segment_name in segment_vlans:
            vlan = segment_vlans[segment_name]
            subinterface["vlan"] = vlan
            subinterface["name"] = f"{port.logical_interface}.{vlan}"

    return segment_vlans


def _segment_names_for_interface(edge: JsonObject, interface: JsonObject) -> list[str]:
    names = [item.get("segment_name") for item in interface.get("subinterfaces", []) if item.get("segment_name")]
    if names:
        return names
    if interface.get("mode") == "switched":
        return [
            item.get("segment_name")
            for item in edge.get("vlans", [])
            if item.get("segment_name") and item.get("segment_name") != "Global Segment"
        ]
    return []


def _update_edge_vlan_table(edge: JsonObject, port: EdgePortMapping) -> None:
    if not port.segment_vlans:
        return
    for vlan_entry in edge.get("vlans", []):
        segment_name = vlan_entry.get("segment_name")
        if segment_name in port.segment_vlans:
            vlan_entry["vlan"] = port.segment_vlans[segment_name]


def _build_l2_switches(
    hardware: HardwareEdge,
    hypervisor_ip: str,
    hypervisor_interface: str,
    hypervisor_switch_port: str | None = None,
    included_ports: set[str] | None = None,
) -> list[JsonObject]:
    switch_by_name = _switches_by_name(hardware)
    default_switch_name = next(iter(switch_by_name))
    interfaces_by_switch: dict[str, list[JsonObject]] = {name: [] for name in switch_by_name}

    for port in [item for item in _topology_ports(hardware) if item.switch_vlans]:
        if included_ports and port.logical_interface.upper() not in included_ports:
            continue
        switch_name = port.switch_name or default_switch_name
        if switch_name not in interfaces_by_switch:
            switch_name = default_switch_name
        if port.switch_active_port:
            interfaces_by_switch[switch_name].append(
                {
                    "name": port.switch_active_port,
                    "link": port.link,
                    "mode": "switched",
                    "vlans": port.switch_vlans,
                }
            )
        if hardware.ha and port.switch_standby_port:
            interfaces_by_switch[switch_name].append(
                {
                    "name": port.switch_standby_port,
                    "link": f"standby_{port.link}",
                    "mode": "switched",
                    "vlans": port.switch_vlans,
                }
            )

    l2_switches: list[JsonObject] = []
    for switch_name, switch_metadata in switch_by_name.items():
        interfaces = interfaces_by_switch.get(switch_name, [])
        if not interfaces:
            continue
        switch = switch_metadata.model_dump(mode="json", exclude={"os_family"})
        switch["interfaces"] = [
            *interfaces,
            {
                "name": hypervisor_switch_port or hypervisor_interface,
                "link": hypervisor_interface,
                "logical_name": "HYPERVISOR",
                "default_gateway": hypervisor_ip,
            },
        ]
        l2_switches.append(switch)
    return l2_switches


def _switches_by_name(hardware: HardwareEdge) -> dict[str, Any]:
    switches = hardware.switches or ([hardware.switch] if hardware.switch else [])
    return {switch.name: switch for switch in switches}


def _apply_remote_updates_to_config(
    config: JsonObject,
    edge: JsonObject,
    remote_updates: dict[str, dict[str, Any]],
) -> None:
    skip_ids = {id(interface) for interface in edge.get("interfaces", [])}
    skip_ids.update(id(switch) for switch in edge.get("l2_switches", []))

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if id(value) in skip_ids:
                return
            link = value.get("link")
            if link in remote_updates:
                update = remote_updates[link]
                value["link"] = update["new_link"]
                if update["global_vlan"] and _is_interface_dict(value):
                    value["name"] = _tagged_name(str(value.get("name", "")), update["global_vlan"])
                _update_segments(value, update["segment_vlans"])
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(config)


def _drop_linked_interfaces(config: JsonObject, edge: JsonObject, dropped_links: set[str]) -> None:
    if not dropped_links:
        return
    skip_ids = {id(interface) for interface in edge.get("interfaces", [])}

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            kept = []
            for item in value:
                if (
                    isinstance(item, dict)
                    and id(item) not in skip_ids
                    and item.get("link") in dropped_links
                    and _is_interface_dict(item)
                ):
                    continue
                walk(item)
                kept.append(item)
            value[:] = kept

    walk(config)


def _is_interface_dict(value: JsonObject) -> bool:
    name = value.get("name")
    return isinstance(name, str) and name.startswith(("eth", "ge", "sfp", "Gi", "Te"))


def _is_loopback_name(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return value.strip().split(".", 1)[0].lower() in {"lo", "lo0", "lo1"}


def _is_loopback_interface(interface: JsonObject) -> bool:
    if str(interface.get("type", "")).lower() == "loopback":
        return True
    return any(_is_loopback_name(interface.get(key)) for key in ("logical_interface", "logical_name", "name"))


def _tagged_name(name: str, vlan: int) -> str:
    if not name:
        return name
    base = name.split(".", 1)[0]
    if base in {"lo", "lo0", "lo1"}:
        return name
    return f"{base}.{vlan}"


def _update_wanlink_name(
    interface: JsonObject,
    old_logical_interface: Any,
    new_logical_interface: str,
) -> None:
    wanlink = interface.get("wanlink")
    if not isinstance(wanlink, dict):
        return
    name = wanlink.get("name")
    if not isinstance(name, str):
        return

    old_name = str(old_logical_interface or "").strip()
    new_name = str(new_logical_interface or "").strip()
    if not old_name or not new_name or old_name == new_name:
        return
    if name == old_name:
        wanlink["name"] = new_name
        return

    prefix = f"{old_name}_"
    if name.startswith(prefix):
        wanlink["name"] = f"{new_name}_{name[len(prefix):]}"


def _update_segments(interface: JsonObject, segment_vlans: dict[str, int]) -> None:
    for segment in interface.get("segments", []):
        segment_name = segment.get("name")
        if segment_name in segment_vlans:
            segment["vlan"] = segment_vlans[segment_name]


def _apply_companion_file_updates(
    topology_path: Path,
    reference_branch: JsonObject,
    generated_branch: JsonObject,
) -> None:
    branch_names = {
        name
        for name in (reference_branch.get("name"), generated_branch.get("name"))
        if isinstance(name, str) and name
    }
    device_updates = _build_companion_device_updates(reference_branch, generated_branch)
    for path in _json_paths(topology_path):
        if path.name == "config.json":
            continue
        data = _load_json(path)
        _update_companion_interfaces(data, branch_names, device_updates)
        _write_json(path, data)


def _build_companion_device_updates(
    reference_branch: JsonObject,
    generated_branch: JsonObject,
) -> dict[str, dict[str, dict[str, str]]]:
    device_updates: dict[str, dict[str, dict[str, str]]] = {}

    def register_device_pair(reference_device: Any, generated_device: Any) -> None:
        if not isinstance(reference_device, dict) or not isinstance(generated_device, dict):
            return
        device_name = reference_device.get("name")
        if not isinstance(device_name, str) or not device_name:
            return

        name_map: dict[str, str] = {}
        logical_interface_map: dict[str, str] = {}
        reference_interfaces = reference_device.get("interfaces", [])
        generated_interfaces = generated_device.get("interfaces", [])
        for reference_interface, generated_interface in zip(reference_interfaces, generated_interfaces):
            if not isinstance(reference_interface, dict) or not isinstance(generated_interface, dict):
                continue

            reference_name = reference_interface.get("name")
            generated_name = generated_interface.get("name")
            if isinstance(reference_name, str) and isinstance(generated_name, str) and reference_name != generated_name:
                name_map[reference_name] = generated_name

            reference_logical = reference_interface.get("logical_interface")
            generated_logical = generated_interface.get("logical_interface")
            if (
                isinstance(reference_logical, str)
                and isinstance(generated_logical, str)
                and reference_logical != generated_logical
            ):
                logical_interface_map[reference_logical] = generated_logical

        if name_map or logical_interface_map:
            device_updates[device_name] = {
                "name_map": name_map,
                "logical_interface_map": logical_interface_map,
            }

    for key in ("edges", "CEs", "l3switches"):
        reference_devices = reference_branch.get(key, [])
        generated_devices = generated_branch.get(key, [])
        for reference_device, generated_device in zip(reference_devices, generated_devices):
            register_device_pair(reference_device, generated_device)
            if key != "edges":
                continue
            reference_clients = reference_device.get("direct_clients", [])
            generated_clients = generated_device.get("direct_clients", [])
            for reference_client, generated_client in zip(reference_clients, generated_clients):
                register_device_pair(reference_client, generated_client)

    return device_updates


def _update_companion_interfaces(
    data: Any,
    branch_names: set[str],
    device_updates: dict[str, dict[str, dict[str, str]]],
) -> None:
    def walk(
        value: Any,
        active_branch: bool = False,
        active_device_update: dict[str, dict[str, str]] | None = None,
    ) -> None:
        if isinstance(value, dict):
            current_device_update = active_device_update
            name = value.get("name")
            branch_context = active_branch or (isinstance(name, str) and name in branch_names)
            if branch_context and isinstance(name, str) and name in device_updates:
                current_device_update = device_updates[name]

            if current_device_update:
                interface_name = value.get("name")
                if isinstance(interface_name, str):
                    new_name = current_device_update["name_map"].get(interface_name)
                    if new_name:
                        value["name"] = new_name

                logical_interface = value.get("logical_interface")
                if isinstance(logical_interface, str):
                    new_logical = current_device_update["logical_interface_map"].get(logical_interface)
                    if new_logical:
                        value["logical_interface"] = new_logical

            for child in value.values():
                walk(child, branch_context, current_device_update)
        elif isinstance(value, list):
            for item in value:
                walk(item, active_branch, active_device_update)

    walk(data)


def _apply_global_replacements(
    topology_path: Path,
    replacements: list[tuple[str, str]],
    *,
    skip_paths: set[Path] | None = None,
) -> None:
    skip_paths = skip_paths or set()
    for path in _json_paths(topology_path):
        if path in skip_paths:
            continue
        data = _load_json(path)
        data = _replace_strings(data, replacements)
        _write_json(path, data)


def _replace_strings(value: Any, replacements: list[tuple[str, str]]) -> Any:
    if isinstance(value, str):
        result = value
        for old, new in replacements:
            if old != new:
                result = result.replace(old, new)
        return result
    if isinstance(value, list):
        return [_replace_strings(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _replace_strings(child, replacements) for key, child in value.items()}
    return value


def _json_paths(topology_path: Path) -> list[Path]:
    return sorted(topology_path.rglob("*.json"))


def _validate_generated_json(topology_path: Path) -> list[ValidationMessage]:
    messages: list[ValidationMessage] = []
    for path in _json_paths(topology_path):
        try:
            _load_json(path)
        except json.JSONDecodeError as error:
            messages.append(ValidationMessage(level="error", message=f"Invalid JSON {path}: {error}"))
    if not messages:
        messages.append(ValidationMessage(level="info", message="All generated JSON files parsed successfully"))
    return messages


def _zip_topology(topology_path: Path) -> Path:
    zip_path = topology_path.parent / f"{topology_path.name}.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(topology_path.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(topology_path.parent))
    return zip_path
