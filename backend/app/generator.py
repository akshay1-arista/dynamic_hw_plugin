from __future__ import annotations

from functools import lru_cache
import json
import re
import shutil
import uuid
import zipfile
from pathlib import Path
from typing import Any

from .config import INVENTORY_PATH, OUTPUTS_ROOT, REFERENCE_CONFIG_ROOT
from .inventory import load_inventory
from .models import (
    EdgePortMapping,
    GenerateRequest,
    GenerateResult,
    HardwareEdge,
    InterfaceOverride,
    JsonObject,
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
    _validate_request(request, hardware_by_id)

    run_id = uuid.uuid4().hex[:12]
    run_root = outputs_root / run_id
    topology_path = run_root / request.topology_name
    run_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(reference_path, topology_path)

    messages: list[ValidationMessage] = []
    config_path = topology_path / "config.json"
    config = _load_json(config_path)
    if "testbed" not in config:
        config["testbed"] = {}
    old_topology_name = config["testbed"].get("name")
    generated_testbed_name = f"{request.topology_name}-{uuid.uuid4().hex[:6]}"
    config["testbed"]["name"] = generated_testbed_name
    config["testbed"]["description"] = f"Generated from {request.reference_topology_id} topology"

    global_replacements: list[tuple[str, str]] = []
    if old_topology_name:
        global_replacements.append((old_topology_name, generated_testbed_name))

    for mapping in request.mappings:
        hardware = hardware_by_id[mapping.hardware_id]
        branch = _find_branch(config, mapping.branch_name)
        edge = _find_edge(branch, mapping.edge_name)

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
        remote_updates, dropped_links, port_messages = _apply_port_mappings(
            edge,
            hardware,
            old_branch_name,
            old_edge_name,
            mapping.interface_overrides,
        )
        messages.extend(port_messages)
        edge["l2_switches"] = _build_l2_switches(hardware, request.hypervisor_ip, request.hypervisor_interface)

        _apply_remote_updates_to_config(config, edge, remote_updates)
        _drop_linked_interfaces(config, edge, dropped_links)
        _apply_companion_file_updates(topology_path, new_branch_name, new_edge_name, hardware, remote_updates)

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

    return GenerateResult(
        run_id=run_id,
        topology_name=generated_testbed_name,
        topology_path=str(topology_path),
        zip_path=str(zip_path),
        download_url=f"/api/runs/{run_id}/download",
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
        if not _topology_ports(hardware):
            raise GenerationError(f"No VLAN-backed switch ports found for {hardware.id}")


def _load_json(path: Path) -> JsonObject:
    with path.open() as fh:
        return json.load(fh)


def _write_json(path: Path, data: JsonObject) -> None:
    with path.open("w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")


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
    if hardware.free_vlans:
        edge.setdefault("custom_params", {})["free_vlans"] = hardware.free_vlans


def _apply_port_mappings(
    edge: JsonObject,
    hardware: HardwareEdge,
    branch_name: str,
    edge_name: str,
    interface_overrides: list[InterfaceOverride] | None = None,
) -> tuple[dict[str, dict[str, Any]], set[str], list[ValidationMessage]]:
    remote_updates: dict[str, dict[str, Any]] = {}
    dropped_links: set[str] = set()
    messages: list[ValidationMessage] = []
    reference_interfaces = list(edge.get("interfaces", []))
    mappable_reference_interfaces = [
        interface for interface in reference_interfaces if not _is_loopback_interface(interface)
    ]
    hardware_ports = _topology_ports(hardware)
    no_vlan_ports = [port for port in _ordered_ports(hardware) if not port.switch_vlans]
    mapped_interfaces, mapped_ports, dropped_interfaces = _resolve_port_assignments(
        reference_interfaces,
        hardware_ports,
        interface_overrides or [],
        hardware,
    )

    if no_vlan_ports:
        messages.append(
            ValidationMessage(
                level="warning",
                message=(
                    f"{hardware.display_name} has {len(no_vlan_ports)} connected hardware port(s) "
                    f"without VLAN metadata. Kept in inventory but excluded from generated topology: "
                    f"{', '.join(port.logical_interface for port in no_vlan_ports)}."
                ),
            )
        )

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
        old_link = interface.get("link")
        old_logical_interface = interface.get("logical_interface")
        port.link = old_link or port.link
        port.logical_name = str(interface.get("logical_name") or old_logical_interface or port.logical_interface)
        interface["name"] = port.name
        interface["logical_interface"] = port.logical_interface
        interface["link"] = port.link

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

    return remote_updates, dropped_links, messages


def _resolve_port_assignments(
    reference_interfaces: list[JsonObject],
    hardware_ports: list[EdgePortMapping],
    interface_overrides: list[InterfaceOverride],
    hardware: HardwareEdge,
) -> tuple[list[JsonObject], list[EdgePortMapping], list[JsonObject]]:
    reference_interfaces = [
        interface for interface in reference_interfaces if not _is_loopback_interface(interface)
    ]
    if not interface_overrides:
        mapped_count = min(len(reference_interfaces), len(hardware_ports))
        mapped_interfaces = reference_interfaces[:mapped_count]
        mapped_ports = _match_ports_to_reference_interfaces(mapped_interfaces, hardware_ports)
        dropped_interfaces = reference_interfaces[mapped_count:]
        return mapped_interfaces, mapped_ports, dropped_interfaces

    override_map = _normalize_interface_overrides(interface_overrides)
    loopback_overrides = sorted(reference_key for reference_key in override_map if _is_loopback_name(reference_key))
    if loopback_overrides:
        raise GenerationError(
            "Loopback interface override(s) are not allowed because loopback interfaces are preserved as-is: "
            f"{', '.join(loopback_overrides)}"
        )

    hardware_ports_by_name = {port.logical_interface.upper(): port for port in hardware_ports}
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
    excluded_hardware = []
    for hardware_key in [value for value in override_map.values() if value]:
        if hardware_key not in all_ports_by_name:
            invalid_hardware.append(hardware_key)
        elif hardware_key not in hardware_ports_by_name:
            excluded_hardware.append(hardware_key)
    if invalid_hardware:
        raise GenerationError(
            f"Unknown hardware interface override(s): {', '.join(sorted(invalid_hardware))}"
        )
    if excluded_hardware:
        raise GenerationError(
            "Hardware interface override(s) lack VLAN metadata and cannot be used in generated topology: "
            f"{', '.join(sorted(excluded_hardware))}"
        )

    explicit_pairs: dict[str, EdgePortMapping] = {}
    explicit_drop_keys = {reference_key for reference_key, hardware_key in override_map.items() if hardware_key is None}
    used_hardware_keys: set[str] = set()
    for reference_key, hardware_key in override_map.items():
        if hardware_key is None:
            continue
        explicit_pairs[reference_key] = hardware_ports_by_name[hardware_key]
        used_hardware_keys.add(hardware_key)

    remaining_interfaces = [
        interface
        for interface in reference_interfaces
        if (reference_key := _reference_interface_key(interface)) and reference_key not in override_map
    ]
    remaining_hardware_ports = [
        port for port in hardware_ports if port.logical_interface.upper() not in used_hardware_keys
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


def _normalize_interface_overrides(interface_overrides: list[InterfaceOverride]) -> dict[str, str | None]:
    override_map: dict[str, str | None] = {}
    used_hardware_interfaces: set[str] = set()
    for override in interface_overrides:
        reference_key = override.reference_interface.strip().upper()
        hardware_key = override.hardware_interface.strip().upper() if override.hardware_interface else None
        if reference_key in override_map:
            raise GenerationError(f"Reference interface override specified more than once: {reference_key}")
        if hardware_key and hardware_key in used_hardware_interfaces:
            raise GenerationError(f"Hardware interface override specified more than once: {hardware_key}")
        override_map[reference_key] = hardware_key
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
    return [port for port in _ordered_ports(hardware) if port.switch_vlans]


def _match_ports_to_reference_interfaces(
    reference_interfaces: list[JsonObject],
    hardware_ports: list[EdgePortMapping],
) -> list[EdgePortMapping]:
    if not reference_interfaces or not hardware_ports:
        return []

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
        interface["vlans"] = [1, *segment_vlans.values()] if segment_vlans else [1]

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
) -> list[JsonObject]:
    switch_by_name = _switches_by_name(hardware)
    default_switch_name = next(iter(switch_by_name))
    interfaces_by_switch: dict[str, list[JsonObject]] = {name: [] for name in switch_by_name}

    for port in _topology_ports(hardware):
        switch_name = port.switch_name or default_switch_name
        if switch_name not in interfaces_by_switch:
            switch_name = default_switch_name
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
        switch = switch_metadata.model_dump(mode="json")
        switch["interfaces"] = [
            *interfaces,
            {
                "name": hypervisor_interface,
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


def _update_segments(interface: JsonObject, segment_vlans: dict[str, int]) -> None:
    for segment in interface.get("segments", []):
        segment_name = segment.get("name")
        if segment_name in segment_vlans:
            segment["vlan"] = segment_vlans[segment_name]


def _apply_companion_file_updates(
    topology_path: Path,
    branch_name: str,
    edge_name: str,
    hardware: HardwareEdge,
    remote_updates: dict[str, dict[str, Any]],
) -> None:
    for path in _json_paths(topology_path):
        if path.name == "config.json":
            continue
        data = _load_json(path)
        _update_companion_edge_interfaces(data, branch_name, edge_name, hardware)
        _update_companion_remote_interfaces(data, remote_updates)
        _write_json(path, data)


def _update_companion_edge_interfaces(data: Any, branch_name: str, edge_name: str, hardware: HardwareEdge) -> None:
    logical_map = {port.logical_name: port.logical_interface for port in hardware.ports}
    logical_map.update({port.logical_interface: port.logical_interface for port in hardware.ports})

    def walk(value: Any, active_branch: bool = False, active_edge: bool = False) -> None:
        if isinstance(value, dict):
            branch_context = active_branch or value.get("name") == branch_name
            edge_context = active_edge or (branch_context and value.get("name") == edge_name)
            if edge_context and value.get("logical_interface") in logical_map:
                value["logical_interface"] = logical_map[value["logical_interface"]]
            for child in value.values():
                walk(child, branch_context, edge_context)
        elif isinstance(value, list):
            for item in value:
                walk(item, active_branch, active_edge)

    walk(data)


def _update_companion_remote_interfaces(data: Any, remote_updates: dict[str, dict[str, Any]]) -> None:
    # Companion characteristic files usually do not carry link names. Update interface
    # names conservatively when a name already has a stale tag for a mapped link.
    old_tag_pattern = re.compile(r"^(?P<base>eth\d+)\.(100|101)$")

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            name = value.get("name")
            if isinstance(name, str):
                match = old_tag_pattern.match(name)
                if match:
                    # Use the first mapped global VLAN as the safest correction when
                    # link context is absent from the companion file.
                    first_update = next(iter(remote_updates.values()), None)
                    if first_update and first_update.get("global_vlan"):
                        value["name"] = _tagged_name(match.group("base"), first_update["global_vlan"])
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for item in value:
                walk(item)

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
