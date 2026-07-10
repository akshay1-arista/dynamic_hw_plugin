from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path

from .config import INVENTORY_PATH, OUTPUTS_ROOT
from .generator import GenerationError, resolve_run_root
from .inventory import load_inventory
from .models import (
    HardwareEdge,
    HardwarePathSummary,
    HardwarePortAllocation,
    InventoryConnection,
    InventoryDevice,
    InventoryFile,
    RunMetadata,
    SwitchCommandPlan,
    SwitchConfigureRequest,
    SwitchConfigureResult,
    ValidationMessage,
)


class SwitchConfigError(GenerationError):
    pass


SSH_CONNECT_TIMEOUT_SECONDS = 10
SSH_COMMAND_TIMEOUT_SECONDS = 20


def configure_switches_for_run(
    run_id: str,
    request: SwitchConfigureRequest,
    *,
    inventory_path: Path = INVENTORY_PATH,
    outputs_root: Path = OUTPUTS_ROOT,
) -> SwitchConfigureResult:
    metadata = _load_run_metadata(run_id, outputs_root)
    inventory = load_inventory(inventory_path)
    hardware_by_id = {item.id: item for item in inventory.hardware}
    generated_switch_links = _load_generated_switch_links(run_id, metadata, outputs_root)
    plans = _build_plans(metadata, inventory, hardware_by_id, generated_switch_links)
    plans = _apply_command_overrides(plans, request)
    if not request.dry_run:
        for plan, device in plans:
            _execute_switch_plan(plan, device)
    return SwitchConfigureResult(
        run_id=run_id,
        applied=not request.dry_run,
        devices=[plan for plan, _device in plans],
        messages=[
            ValidationMessage(
                level="info",
                message="Applied switch configuration." if not request.dry_run else "Generated switch configuration preview.",
            )
        ],
    )


def _load_run_metadata(run_id: str, outputs_root: Path) -> RunMetadata:
    path = resolve_run_root(run_id, outputs_root) / "run_metadata.json"
    if not path.exists():
        raise SwitchConfigError(f"Run metadata not found for {run_id}")
    with path.open() as fh:
        return RunMetadata.model_validate(json.load(fh))


def _load_generated_switch_links(
    run_id: str,
    metadata: RunMetadata,
    outputs_root: Path,
) -> dict[tuple[str, str], str]:
    config_path = resolve_run_root(run_id, outputs_root) / metadata.topology_name / "config.json"
    if not config_path.exists():
        return {}

    with config_path.open() as fh:
        config = json.load(fh)

    links: dict[tuple[str, str], str] = {}
    for branch in config.get("topology", {}).get("branches", []):
        for edge in branch.get("edges", []):
            for switch in edge.get("l2_switches", []):
                switch_name = switch.get("name")
                if not isinstance(switch_name, str) or not switch_name.strip():
                    continue
                for interface in switch.get("interfaces", []):
                    interface_name = interface.get("name")
                    link = interface.get("link")
                    if not isinstance(interface_name, str) or not interface_name.strip():
                        continue
                    if not isinstance(link, str) or not link.strip():
                        continue
                    links[(switch_name, interface_name)] = link
    return links


def _build_plans(
    metadata: RunMetadata,
    inventory: InventoryFile,
    hardware_by_id: dict[str, HardwareEdge],
    generated_switch_links: dict[tuple[str, str], str],
) -> list[tuple[SwitchCommandPlan, InventoryDevice]]:
    device_states: dict[str, dict[str, object]] = {}
    for mapping in metadata.mappings:
        hardware = hardware_by_id.get(mapping.hardware_id)
        path = mapping.path or (hardware.path if hardware else None)
        if not hardware or not path or not path.complete:
            raise SwitchConfigError(f"Run {metadata.run_id} is missing complete path data for {mapping.hardware_id}")
        access_switch = inventory.devices.get(path.access_switch_id or "")
        upstream_switch = inventory.devices.get(path.upstream_switch_id or "")
        if not access_switch or not upstream_switch:
            raise SwitchConfigError(f"Run {metadata.run_id} is missing switch inventory data for {mapping.hardware_id}")
        _assert_supported_switch(access_switch)
        _assert_supported_switch(upstream_switch)

        access_state = _ensure_device_state(device_states, access_switch)
        upstream_state = _ensure_device_state(device_states, upstream_switch)
        access_ports: list[HardwarePortAllocation] = []
        upstream_ports: list[HardwarePortAllocation] = []
        for port in mapping.allocations:
            port_switch = _resolve_mapping_switch(port.switch_name, access_switch, upstream_switch)
            if not port_switch:
                raise SwitchConfigError(
                    f"Run {metadata.run_id} contains an unsupported switch allocation for {port.switch_name}"
                )
            port_state = _ensure_device_state(device_states, port_switch)
            port_state["target_vlans"].update(vlan for vlan in port.switch_vlans if vlan is not None)
            _add_edge_port_to_state(
                port_state,
                hardware,
                port,
                standby=False,
                generated_switch_links=generated_switch_links,
            )
            if port.switch_standby_port:
                _add_edge_port_to_state(
                    port_state,
                    hardware,
                    port,
                    standby=True,
                    generated_switch_links=generated_switch_links,
                )
            _add_access_vlan_state(port_state, port, generated_switch_links)
            if port_switch.id == access_switch.id:
                access_ports.append(port)
            elif port_switch.id == upstream_switch.id:
                upstream_ports.append(port)

        access_transport_vlans = sorted(
            {
                vlan
                for port in access_ports
                for vlan in _transport_vlans_for_port(port, generated_switch_links)
            }
        )
        hypervisor_transport_vlans = sorted(
            {
                vlan
                for port in (access_ports + upstream_ports)
                for vlan in _transport_vlans_for_port(port, generated_switch_links)
            }
        )
        upstream_state["target_vlans"].update(hypervisor_transport_vlans)

        if path.access_uplink_port:
            _add_shared_port(
                access_state,
                path.access_uplink_port,
                description=_switch_link_description(upstream_switch, path.upstream_access_port),
            )
            _add_shared_transport(access_state, path.access_uplink_port, access_transport_vlans)
            _add_access_uplink_vlan_state(access_state, path.access_uplink_port, access_transport_vlans)

        if path.upstream_access_port:
            uplink_native = _connection_native_vlan(
                inventory.connections,
                path.access_switch_id,
                path.access_uplink_port,
                path.upstream_switch_id,
                path.upstream_access_port,
            )
            _add_shared_port(
                upstream_state,
                path.upstream_access_port,
                description=_switch_link_description(access_switch, path.access_uplink_port),
                native_vlan=uplink_native,
            )
            _add_shared_transport(upstream_state, path.upstream_access_port, access_transport_vlans)

        if path.upstream_hypervisor_port:
            hypervisor_native = _connection_native_vlan(
                inventory.connections,
                path.upstream_switch_id,
                path.upstream_hypervisor_port,
                path.hypervisor_id,
                None,
            )
            _add_shared_port(
                upstream_state,
                path.upstream_hypervisor_port,
                description=_hypervisor_link_description(path),
                native_vlan=hypervisor_native,
            )
            _add_shared_transport(upstream_state, path.upstream_hypervisor_port, hypervisor_transport_vlans)
            _add_upstream_vlan_state(upstream_state, path, access_transport_vlans)
            _add_hypervisor_vlan_state(upstream_state, path.upstream_hypervisor_port, hypervisor_transport_vlans)

    plans: list[tuple[SwitchCommandPlan, InventoryDevice]] = []
    for device_id in sorted(device_states):
        state = device_states[device_id]
        device = state["device"]
        commands = _render_commands(state)
        if not commands:
            continue
        plans.append(
            (
                SwitchCommandPlan(
                    device_id=device.id,
                    device_name=device.display_name,
                    device_ip=device.ip_address or "",
                    interface="multiple",
                    commands=commands,
                ),
                device,
            )
        )
    return plans


def _ensure_device_state(
    states: dict[str, dict[str, object]],
    device: InventoryDevice,
) -> dict[str, object]:
    if device.id not in states:
        states[device.id] = {
            "device": device,
            "family": _switch_family(device),
            "port_configs": {},
            "os9_vlans": defaultdict(lambda: {"member": set(), "untagged": set(), "tagged": set()}),
            "cleanup_ports": set(),
            "target_vlans": set(),
        }
    return states[device.id]


def _port_link(
    port: HardwarePortAllocation,
    generated_switch_links: dict[tuple[str, str], str],
) -> str | None:
    if port.link:
        return port.link
    return generated_switch_links.get((port.switch_name, port.switch_active_port))


def _transport_vlans_for_port(
    port: HardwarePortAllocation,
    generated_switch_links: dict[tuple[str, str], str],
) -> list[int]:
    link = _port_link(port, generated_switch_links)
    if link is None:
        if not port.tagged_vlans:
            return []
    elif "_HA" in link.upper():
        return []
    return [vlan for vlan in port.switch_vlans if vlan is not None]


def _uses_vlan_stack_access(
    port: HardwarePortAllocation,
    generated_switch_links: dict[tuple[str, str], str],
) -> bool:
    if port.untagged_vlan is None or port.tagged_vlans:
        return False
    link = _port_link(port, generated_switch_links)
    if link is None:
        return True
    return "_HA" in link.upper()


def _add_edge_port_to_state(
    state: dict[str, object],
    hardware: HardwareEdge,
    port: HardwarePortAllocation,
    *,
    standby: bool,
    generated_switch_links: dict[tuple[str, str], str],
) -> None:
    interface_name = port.switch_standby_port if standby else port.switch_active_port
    if not interface_name:
        return
    config = _ensure_port_config(state, interface_name)
    config["description"] = _edge_port_description(hardware, port, standby=standby)
    config["native_vlan"] = port.untagged_vlan
    config["tagged_vlans"].update(port.tagged_vlans)
    config["shared"] = False
    config["flowcontrol_receive_on"] = True
    if _uses_vlan_stack_access(port, generated_switch_links):
        config["vlan_stack_access"] = True
    state["cleanup_ports"].add(interface_name)


def _add_access_vlan_state(
    state: dict[str, object],
    port: HardwarePortAllocation,
    generated_switch_links: dict[tuple[str, str], str],
) -> None:
    if state["family"] != "os9":
        return
    interface_names = [port.switch_active_port]
    if port.switch_standby_port:
        interface_names.append(port.switch_standby_port)

    if port.tagged_vlans:
        if port.untagged_vlan is not None:
            vlan_state = state["os9_vlans"][port.untagged_vlan]
            vlan_state["untagged"].update(interface_names)
        for vlan in port.tagged_vlans:
            vlan_state = state["os9_vlans"][vlan]
            vlan_state["tagged"].update(interface_names)
        return

    if port.untagged_vlan is not None:
        vlan_state = state["os9_vlans"][port.untagged_vlan]
        target = "member" if _uses_vlan_stack_access(port, generated_switch_links) else "untagged"
        vlan_state[target].update(interface_names)


def _add_shared_port(
    state: dict[str, object],
    interface_name: str,
    *,
    description: str | None = None,
    native_vlan: int | None = None,
) -> None:
    if not interface_name:
        return
    config = _ensure_port_config(state, interface_name)
    if description:
        config["description"] = description
    if native_vlan is not None and config["native_vlan"] is None:
        config["native_vlan"] = native_vlan
    config["shared"] = True
    config["flowcontrol_receive_on"] = True


def _add_shared_transport(state: dict[str, object], interface_name: str, vlans: list[int]) -> None:
    if not interface_name or not vlans:
        return
    config = _ensure_port_config(state, interface_name)
    config["tagged_vlans"].update(vlans)
    if config["native_vlan"] in config["tagged_vlans"]:
        config["tagged_vlans"].discard(config["native_vlan"])


def _add_access_uplink_vlan_state(
    state: dict[str, object],
    interface_name: str,
    transported_vlans: list[int],
) -> None:
    if state["family"] != "os9":
        return
    for vlan in transported_vlans:
        state["os9_vlans"][vlan]["tagged"].add(interface_name)


def _add_upstream_vlan_state(
    state: dict[str, object],
    path: HardwarePathSummary,
    transported_vlans: list[int],
) -> None:
    if state["family"] != "os9":
        return
    if not path.upstream_access_port or not path.upstream_hypervisor_port:
        return
    for vlan in transported_vlans:
        vlan_state = state["os9_vlans"][vlan]
        vlan_state["tagged"].add(path.upstream_access_port)
        vlan_state["tagged"].add(path.upstream_hypervisor_port)


def _add_hypervisor_vlan_state(
    state: dict[str, object],
    hypervisor_port: str,
    transported_vlans: list[int],
) -> None:
    if state["family"] != "os9":
        return
    if not hypervisor_port:
        return
    for vlan in transported_vlans:
        state["os9_vlans"][vlan]["tagged"].add(hypervisor_port)


def _ensure_port_config(state: dict[str, object], interface_name: str) -> dict[str, object]:
    port_configs = state["port_configs"]
    if interface_name not in port_configs:
        port_configs[interface_name] = {
            "description": None,
            "native_vlan": None,
            "tagged_vlans": set(),
            "vlan_stack_access": False,
            "shared": False,
            "flowcontrol_receive_on": False,
        }
    return port_configs[interface_name]


def _render_commands(state: dict[str, object]) -> list[str]:
    family = state["family"]
    if family == "os9":
        return _render_os9_commands(state)
    if family == "os10":
        return _render_os10_commands(state)
    raise SwitchConfigError(f"Unsupported switch OS family: {family}")


def _render_os9_commands(state: dict[str, object]) -> list[str]:
    device = state["device"]
    running_config = _fetch_running_config(device)
    cleanup_vlans = set(state["target_vlans"])
    cleanup_vlans.update(_find_os9_cleanup_vlans(running_config, state["cleanup_ports"]))

    commands: list[str] = []
    for vlan in sorted(cleanup_vlans):
        commands.append(f"no interface vlan {vlan}")

    for interface_name in sorted(state["port_configs"], key=_interface_sort_key):
        config = state["port_configs"][interface_name]
        commands.extend(
            [
                f"interface {_format_os9_interface(interface_name)}",
                f' description "{config["description"]}"' if config["description"] else None,
                " no ip address",
                " portmode hybrid",
                " switchport",
                " vlan-stack access" if config["vlan_stack_access"] else None,
                " no shutdown",
                " exit",
            ]
        )

    for vlan in sorted(state["os9_vlans"]):
        vlan_state = state["os9_vlans"][vlan]
        commands.extend(_render_os9_vlan_block(vlan, vlan_state))

    return [command for command in commands if command]


def _render_os9_vlan_block(vlan: int, vlan_state: dict[str, set[str]]) -> list[str]:
    commands = [
        f"interface Vlan {vlan}",
        " no ip address",
    ]
    if vlan_state["member"]:
        commands.append(" vlan-stack compatible")
        commands.extend(_os9_member_commands("member", vlan_state["member"]))
    if vlan_state["untagged"]:
        commands.extend(_os9_member_commands("untagged", vlan_state["untagged"]))
    if vlan_state["tagged"]:
        commands.extend(_os9_member_commands("tagged", vlan_state["tagged"]))
    if vlan_state["untagged"] or vlan_state["tagged"]:
        commands.append(" no ipv6 mld snooping")
    commands.extend([" no shutdown", " exit"])
    return commands


def _render_os10_commands(state: dict[str, object]) -> list[str]:
    device = state["device"]
    commands: list[str] = []
    for interface_name in sorted(state["port_configs"], key=_interface_sort_key):
        config = state["port_configs"][interface_name]
        existing = _fetch_os10_interface_config(device, interface_name) if config["shared"] else None
        native_vlan = config["native_vlan"]
        tagged_vlans = set(config["tagged_vlans"])
        if config["shared"]:
            existing_native, existing_tagged = _parse_os10_interface_config(existing)
            native_vlan = existing_native if existing_native is not None else native_vlan
            if native_vlan is None:
                native_vlan = 1
            tagged_vlans.update(existing_tagged)
        if native_vlan in tagged_vlans:
            tagged_vlans.discard(native_vlan)

        commands.append(f"interface {_format_os10_interface(interface_name)}")
        if config["description"]:
            commands.append(f' description "{config["description"]}"')
        commands.append(" no shutdown")
        if config["shared"]:
            commands.append(" switchport mode trunk")
            commands.append(f" switchport access vlan {native_vlan or 1}")
            if tagged_vlans:
                commands.append(" no switchport trunk allowed vlan")
                commands.append(f" switchport trunk allowed vlan {_collapse_vlan_ranges(sorted(tagged_vlans))}")
        elif tagged_vlans:
            commands.append(" switchport mode trunk")
            commands.append(f" switchport access vlan {native_vlan or 1}")
            commands.append(" no switchport trunk allowed vlan")
            commands.append(f" switchport trunk allowed vlan {_collapse_vlan_ranges(sorted(tagged_vlans))}")
        elif native_vlan is not None:
            commands.append(" switchport mode access")
            commands.append(f" switchport access vlan {native_vlan}")
        else:
            commands.append(" switchport mode trunk")
            commands.append(" switchport access vlan 1")
        if config["flowcontrol_receive_on"]:
            commands.append(" flowcontrol receive on")
        commands.append(" exit")
    return commands


def _apply_command_overrides(
    plans: list[tuple[SwitchCommandPlan, InventoryDevice]],
    request: SwitchConfigureRequest,
) -> list[tuple[SwitchCommandPlan, InventoryDevice]]:
    if not request.command_overrides:
        return plans

    overrides_by_device: dict[str, list[str]] = {}
    for override in request.command_overrides:
        if override.device_id in overrides_by_device:
            raise SwitchConfigError(f"Duplicate command override supplied for {override.device_id}")
        overrides_by_device[override.device_id] = override.commands

    known_device_ids = {plan.device_id for plan, _device in plans}
    unknown_device_ids = sorted(device_id for device_id in overrides_by_device if device_id not in known_device_ids)
    if unknown_device_ids:
        raise SwitchConfigError(
            f"Command overrides include unknown switch device ids: {', '.join(unknown_device_ids)}"
        )

    updated_plans: list[tuple[SwitchCommandPlan, InventoryDevice]] = []
    for plan, device in plans:
        if plan.device_id in overrides_by_device:
            plan = plan.model_copy(update={"commands": overrides_by_device[plan.device_id]})
        updated_plans.append((plan, device))
    return updated_plans


def _assert_supported_switch(device: InventoryDevice) -> None:
    metadata_model = device.switch_metadata.model if device.switch_metadata else ""
    model = str(device.model or metadata_model)
    if not any(token in model for token in ("3048", "4048", "4148", "3248")):
        raise SwitchConfigError(f"Unsupported switch model for auto-config: {device.display_name} ({model})")
    metadata = device.switch_metadata
    if not metadata or not metadata.credentials.username or not metadata.credentials.password or not metadata.connections.ip:
        raise SwitchConfigError(f"Missing switch credentials for {device.display_name}")


def _switch_family(device: InventoryDevice) -> str:
    metadata = device.switch_metadata
    if metadata and getattr(metadata, "os_family", None):
        return str(metadata.os_family)
    model = str(device.model or (metadata.model if metadata else "")).lower()
    if "3048" in model or "4048" in model:
        return "os9"
    if "4148" in model or "3248" in model:
        return "os10"
    raise SwitchConfigError(f"Cannot infer switch OS family for {device.display_name} ({model})")


def _switch_link_description(device: InventoryDevice, remote_port: str | None) -> str:
    if not remote_port:
        return device.display_name
    return f"{device.display_name}:{_interface_terminal_label(remote_port)}"


def _hypervisor_link_description(path: HardwarePathSummary) -> str | None:
    if not path.hypervisor_name:
        return None
    if not path.hypervisor_id:
        return path.hypervisor_name
    return path.hypervisor_name


def _edge_port_description(hardware: HardwareEdge, port: HardwarePortAllocation, *, standby: bool) -> str:
    serial = hardware.standby_serial if standby else hardware.active_serial
    return f"Edge {hardware.model_suffix}_{serial}_{port.logical_interface}"


def _connection_native_vlan(
    connections: list[InventoryConnection],
    left_device_id: str | None,
    left_interface: str | None,
    right_device_id: str | None,
    right_interface: str | None,
) -> int | None:
    for connection in connections:
        endpoints = {
            (connection.a.device_id, connection.a.interface),
            (connection.b.device_id, connection.b.interface),
        }
        if left_device_id and left_interface and (left_device_id, left_interface) not in endpoints:
            continue
        if right_device_id and right_interface and (right_device_id, right_interface) not in endpoints:
            continue
        if right_device_id and right_interface is None and right_device_id not in {
            connection.a.device_id,
            connection.b.device_id,
        }:
            continue
        return connection.untagged_vlan
    return None


def _find_os9_cleanup_vlans(running_config: str, interfaces: set[str]) -> set[int]:
    if not running_config or not interfaces:
        return set()
    formatted_interfaces = {_format_os9_interface(interface) for interface in interfaces}
    cleanup_vlans: set[int] = set()
    for vlan, section in _iter_os9_vlan_sections(running_config):
        existing_interfaces = _extract_os9_interfaces(section)
        if existing_interfaces & formatted_interfaces:
            cleanup_vlans.add(vlan)
    return cleanup_vlans


def _iter_os9_vlan_sections(running_config: str) -> list[tuple[int, str]]:
    sections: list[tuple[int, str]] = []
    matches = list(re.finditer(r"(?im)^interface vlan\s+(\d+)\s*$", running_config))
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(running_config)
        sections.append((int(match.group(1)), running_config[start:end]))
    return sections


def _extract_os9_interfaces(section: str) -> set[str]:
    interfaces: set[str] = set()
    for line in section.splitlines():
        match = re.match(r"\s*(member|untagged|tagged)\s+(.+)$", line, re.IGNORECASE)
        if not match:
            continue
        interfaces.update(_expand_os9_interface_expression(match.group(2)))
    return interfaces


def _expand_os9_interface_expression(expression: str) -> list[str]:
    parts = [part.strip() for part in expression.split(",") if part.strip()]
    expanded: list[str] = []
    current_prefix: str | None = None
    for part in parts:
        match = re.match(r"([A-Za-z]+Ethernet)\s+(.+)$", part)
        if match:
            current_prefix = match.group(1)
            expanded.append(f"{current_prefix} {match.group(2).strip()}")
            continue
        if current_prefix:
            expanded.append(f"{current_prefix} {part}")
    return expanded


def _os9_member_commands(command: str, interfaces: set[str]) -> list[str]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for interface_name in sorted((_format_os9_interface(interface) for interface in interfaces), key=_interface_sort_key):
        if " " not in interface_name:
            raise SwitchConfigError(f"Cannot render OS9 interface expression for {interface_name}")
        prefix, suffix = interface_name.split(" ", 1)
        grouped[prefix].append(suffix)
    commands: list[str] = []
    for prefix in sorted(grouped):
        commands.append(f" {command} {prefix} {','.join(grouped[prefix])}")
    return commands


def _resolve_mapping_switch(
    switch_name: str,
    access_switch: InventoryDevice,
    upstream_switch: InventoryDevice,
) -> InventoryDevice | None:
    normalized = _normalize_switch_name(switch_name)
    for candidate in (access_switch, upstream_switch):
        if normalized in _switch_name_candidates(candidate):
            return candidate
    return None


def _switch_name_candidates(device: InventoryDevice) -> set[str]:
    values = {device.id, device.display_name}
    if device.switch_metadata and device.switch_metadata.name:
        values.add(device.switch_metadata.name)
    return {_normalize_switch_name(value) for value in values if value}


def _normalize_switch_name(value: str | None) -> str:
    return (value or "").strip().lower()


def _parse_os10_interface_config(running_config: str) -> tuple[int | None, set[int]]:
    if not running_config:
        return None, set()
    native_vlan: int | None = None
    tagged_vlans: set[int] = set()
    for line in running_config.splitlines():
        access_match = re.match(r"\s*switchport access vlan\s+(\d+)\s*$", line, re.IGNORECASE)
        if access_match:
            native_vlan = int(access_match.group(1))
            continue
        trunk_match = re.match(r"\s*switchport trunk allowed vlan\s+(.+)$", line, re.IGNORECASE)
        if trunk_match:
            tagged_vlans.update(_expand_vlan_ranges(trunk_match.group(1).strip()))
    return native_vlan, tagged_vlans


def _collapse_vlan_ranges(vlans: list[int]) -> str:
    if not vlans:
        return ""
    ranges: list[str] = []
    start = previous = vlans[0]
    for vlan in vlans[1:]:
        if vlan == previous + 1:
            previous = vlan
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = vlan
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(ranges)


def _expand_vlan_ranges(value: str) -> set[int]:
    vlans: set[int] = set()
    for part in value.split(","):
        token = part.strip()
        if not token:
            continue
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            start = int(start_text)
            end = int(end_text)
            vlans.update(range(start, end + 1))
            continue
        vlans.add(int(token))
    return vlans


def _format_os9_interface(interface_name: str) -> str:
    normalized = interface_name.replace(" ", "").lower()
    for raw_prefix, display_prefix in (
        ("fortygigabitethernet", "FortyGigabitEthernet "),
        ("tengigabitethernet", "TenGigabitEthernet "),
        ("managementethernet", "ManagementEthernet "),
        ("gigabitethernet", "GigabitEthernet "),
    ):
        if normalized.startswith(raw_prefix):
            return f"{display_prefix}{normalized[len(raw_prefix):]}"
    return interface_name


def _format_os10_interface(interface_name: str) -> str:
    normalized = interface_name.replace(" ", "").lower()
    if normalized.startswith("eth"):
        return f"ethernet{normalized[3:]}"
    return normalized


def _interface_terminal_label(interface_name: str | None) -> str:
    if not interface_name:
        return ""
    cleaned = interface_name.replace(" ", "")
    if "/" not in cleaned:
        return cleaned
    return cleaned.split("/")[-1]


def _interface_sort_key(interface_name: str) -> tuple[str, list[int], str]:
    normalized = interface_name.replace(" ", "")
    prefix = re.sub(r"[\d/].*$", "", normalized).lower()
    numbers = [int(part) for part in re.findall(r"\d+", normalized)]
    return prefix, numbers, normalized.lower()


def _fetch_running_config(device: InventoryDevice) -> str:
    return _run_ssh_script(
        device,
        [
            "terminal length 0",
            "show running-config",
            "exit",
        ],
        f"running-config lookup failed for {device.display_name}",
    )


def _fetch_os10_interface_config(device: InventoryDevice, interface_name: str) -> str:
    return _run_ssh_script(
        device,
        [
            "terminal length 0",
            f"show running-configuration interface {_format_os10_interface(interface_name)}",
            "exit",
        ],
        f"interface config lookup failed for {device.display_name} {_format_os10_interface(interface_name)}",
    )


def _run_ssh_script(device: InventoryDevice, script_lines: list[str], error_prefix: str) -> str:
    command = _build_ssh_command(device, force_tty=False)
    try:
        result = subprocess.run(
            command,
            input="\n".join(script_lines) + "\n",
            text=True,
            check=True,
            capture_output=True,
            timeout=SSH_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise SwitchConfigError(
            f"{error_prefix}: timed out after {SSH_COMMAND_TIMEOUT_SECONDS}s while waiting for switch response"
        ) from error
    except subprocess.CalledProcessError as error:
        raise SwitchConfigError(f"{error_prefix}: {error.stderr.strip() or error.stdout.strip()}") from error
    return result.stdout


def _execute_switch_plan(plan: SwitchCommandPlan, device: InventoryDevice) -> None:
    command = _build_ssh_command(device, force_tty=False)
    payload = "configure terminal\n" + "\n".join(plan.commands) + "\nend\nwrite memory\n"
    try:
        subprocess.run(
            command,
            input=payload,
            text=True,
            check=True,
            capture_output=True,
            timeout=SSH_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise SwitchConfigError(
            f"Switch config failed for {device.display_name}: timed out after "
            f"{SSH_COMMAND_TIMEOUT_SECONDS}s while waiting for switch response"
        ) from error
    except subprocess.CalledProcessError as error:
        raise SwitchConfigError(
            f"Switch config failed for {device.display_name}: {error.stderr.strip() or error.stdout.strip()}"
        ) from error


def _build_ssh_command(device: InventoryDevice, *, force_tty: bool) -> list[str]:
    if not shutil.which("sshpass"):
        raise SwitchConfigError("sshpass is required for switch auto-config")
    metadata = device.switch_metadata
    if not metadata:
        raise SwitchConfigError(f"Missing switch metadata for {device.display_name}")

    command = [
        "sshpass",
        "-p",
        metadata.credentials.password,
        "ssh",
        "-F",
        "/dev/null",
        "-o",
        "BatchMode=no",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        f"ConnectTimeout={SSH_CONNECT_TIMEOUT_SECONDS}",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        "PreferredAuthentications=password",
        "-o",
        "PubkeyAuthentication=no",
        "-o",
        "KbdInteractiveAuthentication=no",
        "-o",
        "NumberOfPasswordPrompts=1",
        "-o",
        "KexAlgorithms=+diffie-hellman-group14-sha1,diffie-hellman-group-exchange-sha1,diffie-hellman-group1-sha1",
        "-o",
        "HostKeyAlgorithms=+ssh-rsa",
    ]
    if force_tty:
        # Leave PTY allocation optional. These switches accept piped CLI commands
        # without a PTY, and forcing one can leave non-interactive sessions stuck
        # at the prompt after command output.
        command.append("-tt")
    if metadata.connections.port:
        command.extend(["-p", str(metadata.connections.port)])
    command.append(f"{metadata.credentials.username}@{metadata.connections.ip}")
    return command
