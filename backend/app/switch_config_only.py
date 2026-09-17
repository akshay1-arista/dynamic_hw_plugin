from __future__ import annotations

import uuid
from pathlib import Path

from .audit import append_audit_events
from .config import INVENTORY_PATH, OUTPUTS_ROOT
from .discovery import DiscoveryError, sync_inventory_for_generate
from .generator import (
    GenerationError,
    _resolve_mapping_hardware_view,
    _topology_ports,
    _utc_now,
    _validate_request,
    _write_run_metadata,
)
from .inventory import load_inventory, path_has_credentials, reserve_generated_hardware, resolve_mapping_path
from .models import (
    GenerateMappingStatus,
    GenerateRequest,
    GenerateResult,
    HardwareEdge,
    HardwarePortAllocation,
    InterfaceOverride,
    MappingRequest,
    RunMappingMetadata,
    RunMetadata,
    SWITCH_CONFIG_ONLY_BRANCH_NAME,
    SWITCH_CONFIG_ONLY_REFERENCE_ID,
    SavedGenerateRequest,
    SwitchConfigMappingRequest,
    SwitchConfigOnlyRequest,
    SwitchPortVlanAssignment,
    ValidationMessage,
)


def create_switch_config_run(
    request: SwitchConfigOnlyRequest,
    *,
    inventory_path: Path = INVENTORY_PATH,
    outputs_root: Path = OUTPUTS_ROOT,
) -> GenerateResult:
    selected_hardware_ids = list(
        dict.fromkeys(
            hardware_id
            for mapping in request.mappings
            for hardware_id in [mapping.hardware_id, mapping.secondary_hardware_id]
            if hardware_id
        )
    )
    try:
        sync_inventory_for_generate(selected_hardware_ids, inventory_path=inventory_path)
    except (DiscoveryError, ValueError, FileNotFoundError, OSError):
        pass

    inventory = load_inventory(inventory_path)
    hardware_by_id = {item.id: item for item in inventory.hardware}
    generate_request = _as_generate_request(request)
    _validate_request(generate_request, hardware_by_id)
    _validate_vlan_assignments(request, hardware_by_id)

    topology_suffix = uuid.uuid4().hex[:6]
    generated_topology_name = f"switch-config-{topology_suffix}"
    run_id = uuid.uuid4().hex[:12]
    run_root = outputs_root / f"{run_id}-{topology_suffix}"
    run_root.mkdir(parents=True, exist_ok=True)

    now = _utc_now()
    messages: list[ValidationMessage] = []
    mapping_statuses: list[GenerateMappingStatus] = []
    run_mappings: list[RunMappingMetadata] = []

    for mapping, generate_mapping in zip(request.mappings, generate_request.mappings):
        hardware = hardware_by_id[mapping.hardware_id]
        mapping_view = _resolve_mapping_hardware_view(
            generate_mapping,
            hardware,
            hardware_by_id,
            False,
            generate_request,
        )
        hardware = mapping_view.hardware
        allocations, allocation_messages = _build_port_allocations(hardware, mapping.interfaces)
        messages.extend(allocation_messages)
        if not allocations:
            raise GenerationError(
                f"Select at least one untagged or tagged VLAN on a connected interface for {hardware.display_name}"
            )

        mapping_path = resolve_mapping_path(
            inventory,
            [port.switch_name for port in allocations],
            request.hypervisor_ip,
            request.hypervisor_interface,
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
                    message=f"Switch auto-config disabled for {hardware.display_name}: {mapping_reason}",
                )
            )
        elif not path_has_credentials(mapping_path, inventory):
            mapping_reason = "The resolved access or upstream switch is missing stored credentials."
            messages.append(
                ValidationMessage(
                    level="warning",
                    message=f"Switch auto-config disabled for {hardware.display_name}: {mapping_reason}",
                )
            )
        else:
            mapping_ready = True

        recommended = _recommended_vlan_summary(hardware)
        if recommended:
            messages.append(ValidationMessage(level="info", message=recommended))

        edge_name = hardware.short_name or hardware.id
        run_mappings.append(
            RunMappingMetadata(
                hardware_id=mapping.hardware_id,
                secondary_hardware_id=mapping.secondary_hardware_id,
                branch_name=SWITCH_CONFIG_ONLY_BRANCH_NAME,
                edge_name=edge_name,
                edge_ha_mode=mapping.edge_ha_mode,
                resolved_edge_ha_mode=mapping_view.resolved_mode,
                reserved_device_ids=mapping_view.reserved_device_ids,
                generated_branch_name=SWITCH_CONFIG_ONLY_BRANCH_NAME,
                generated_edge_name=edge_name,
                path=mapping_path,
                allocations=allocations,
            )
        )
        mapping_statuses.append(
            GenerateMappingStatus(
                hardware_id=mapping.hardware_id,
                hardware_display_name=hardware.display_name,
                branch_name=SWITCH_CONFIG_ONLY_BRANCH_NAME,
                edge_name=edge_name,
                path_resolved=bool(mapping_path and mapping_path.complete),
                auto_config_ready=mapping_ready,
                reason=mapping_reason,
                path=mapping_path,
            )
        )
        messages.append(
            ValidationMessage(
                level="info",
                message=f"Mapped switch path for {hardware.display_name}",
            )
        )

    can_configure_switches = bool(mapping_statuses) and all(
        item.path_resolved and item.auto_config_ready for item in mapping_statuses
    )
    run_metadata = RunMetadata(
        run_id=run_id,
        topology_name=generated_topology_name,
        reference_topology_id=SWITCH_CONFIG_ONLY_REFERENCE_ID,
        requested_by=request.requested_by,
        request=_saved_request(request, generate_request, generated_topology_name),
        created_at=now,
        updated_at=now,
        can_configure_switches=can_configure_switches,
        switch_config_only=True,
        mapping_statuses=mapping_statuses,
        messages=messages,
        mappings=run_mappings,
    )
    _write_run_metadata(run_root, run_metadata)
    reserved_device_ids = [
        device_id for mapping in run_mappings for device_id in mapping.reserved_device_ids
    ]
    _saved_inventory, reservation_events = reserve_generated_hardware(
        [mapping.hardware_id for mapping in request.mappings],
        request.requested_by,
        run_id,
        generated_topology_name,
        inventory_path,
        member_device_ids=reserved_device_ids or None,
        reason="switch-config",
    )
    append_audit_events(reservation_events)

    return GenerateResult(
        run_id=run_id,
        topology_name=generated_topology_name,
        topology_path=str(run_root),
        zip_path="",
        download_url="",
        can_configure_switches=can_configure_switches,
        switch_config_only=True,
        mapping_statuses=mapping_statuses,
        messages=messages,
    )


def _as_generate_request(request: SwitchConfigOnlyRequest) -> GenerateRequest:
    return GenerateRequest(
        topology_name="switch-config",
        reference_topology_id=SWITCH_CONFIG_ONLY_REFERENCE_ID,
        hypervisor_ip=request.hypervisor_ip,
        hypervisor_interface=request.hypervisor_interface,
        mappings=[
            MappingRequest(
                hardware_id=mapping.hardware_id,
                secondary_hardware_id=mapping.secondary_hardware_id,
                branch_name=SWITCH_CONFIG_ONLY_BRANCH_NAME,
                edge_name=mapping.hardware_id,
                edge_ha_mode=mapping.edge_ha_mode,
            )
            for mapping in request.mappings
        ],
        requested_by=request.requested_by,
    )


def _saved_request(
    request: SwitchConfigOnlyRequest,
    generate_request: GenerateRequest,
    topology_name: str,
) -> SavedGenerateRequest:
    saved_mappings: list[MappingRequest] = []
    for mapping, generate_mapping in zip(request.mappings, generate_request.mappings):
        saved_mappings.append(
            generate_mapping.model_copy(
                update={
                    "interface_overrides": [
                        InterfaceOverride(
                            reference_interface=item.hardware_interface,
                            hardware_interface=item.hardware_interface,
                            untagged_vlan=item.untagged_vlan,
                            tagged_vlans=list(item.tagged_vlans),
                            switch_vlans=_combined_vlans(item),
                        )
                        for item in mapping.interfaces
                        if item.has_vlans
                    ]
                }
            )
        )
    return SavedGenerateRequest(
        topology_name=topology_name,
        reference_topology_id=SWITCH_CONFIG_ONLY_REFERENCE_ID,
        hypervisor_ip=request.hypervisor_ip,
        hypervisor_interface=request.hypervisor_interface,
        mappings=saved_mappings,
    )


def _validate_vlan_assignments(
    request: SwitchConfigOnlyRequest,
    hardware_by_id: dict[str, HardwareEdge],
) -> None:
    for mapping in request.mappings:
        hardware = hardware_by_id[mapping.hardware_id]
        if not any(item.has_vlans for item in mapping.interfaces):
            raise GenerationError(
                f"Select at least one untagged or tagged VLAN for {hardware.display_name}"
            )


def _build_port_allocations(
    hardware: HardwareEdge,
    assignments: list[SwitchPortVlanAssignment],
) -> tuple[list[HardwarePortAllocation], list[ValidationMessage]]:
    ports_by_interface = {port.logical_interface.upper(): port for port in _topology_ports(hardware)}
    allocations: list[HardwarePortAllocation] = []
    messages: list[ValidationMessage] = []
    for assignment in assignments:
        if not assignment.has_vlans:
            continue
        port = ports_by_interface.get(assignment.hardware_interface.upper())
        if port is None:
            messages.append(
                ValidationMessage(
                    level="warning",
                    message=(
                        f"{hardware.display_name} interface {assignment.hardware_interface} is not connected "
                        "to a switch and was skipped."
                    ),
                )
            )
            continue
        switch_name = _allocation_switch_name(port, hardware)
        if not switch_name or (not port.switch_active_port and not port.switch_standby_port):
            messages.append(
                ValidationMessage(
                    level="warning",
                    message=(
                        f"{hardware.display_name} interface {assignment.hardware_interface} has no switch "
                        "member port and was skipped."
                    ),
                )
            )
            continue
        allocations.append(
            HardwarePortAllocation(
                reference_interface=port.logical_interface,
                logical_interface=port.logical_interface,
                link=port.link or port.logical_interface.lower(),
                switch_name=switch_name,
                switch_standby_name=port.switch_standby_name,
                switch_active_port=port.switch_active_port,
                switch_standby_port=port.switch_standby_port,
                switch_vlans=_combined_vlans(assignment),
                tagged_vlans=list(assignment.tagged_vlans),
                untagged_vlan=assignment.untagged_vlan,
            )
        )
    return allocations, messages


def _allocation_switch_name(port, hardware: HardwareEdge) -> str | None:
    if port.switch_name:
        return port.switch_name
    if hardware.switch and hardware.switch.name:
        return hardware.switch.name
    if hardware.switches:
        return hardware.switches[0].name
    return None


def _combined_vlans(assignment: SwitchPortVlanAssignment) -> list[int]:
    vlans: list[int] = []
    if assignment.untagged_vlan is not None:
        vlans.append(assignment.untagged_vlan)
    for vlan in assignment.tagged_vlans:
        if vlan not in vlans:
            vlans.append(vlan)
    return vlans


def _recommended_vlan_summary(hardware: HardwareEdge) -> str | None:
    if hardware.vlan_range:
        range_text = f"{hardware.vlan_range.start}-{hardware.vlan_range.end}"
    elif hardware.free_vlans:
        range_text = f"{min(hardware.free_vlans)}-{max(hardware.free_vlans)}"
    else:
        return None
    free = ", ".join(str(vlan) for vlan in hardware.free_vlans[:12]) if hardware.free_vlans else range_text
    extra = "" if not hardware.free_vlans or len(hardware.free_vlans) <= 12 else ", ..."
    return f"Recommended VLAN range for {hardware.display_name}: {range_text}. Free VLANs: {free}{extra}."
