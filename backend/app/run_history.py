from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .config import INVENTORY_PATH, OUTPUTS_ROOT
from .generator import GenerationError, resolve_run_root
from .inventory import load_inventory, path_has_credentials
from .models import (
    GenerateMappingStatus,
    GenerateResult,
    HapyCommitResult,
    HapyPublishMetadata,
    InterfaceOverride,
    MappingRequest,
    RunMetadata,
    SavedGenerateRequest,
    SavedRunListResult,
    SavedRunLoadResult,
    SavedRunSummary,
    ValidationMessage,
)


class RunHistoryError(GenerationError):
    pass


def list_saved_runs(outputs_root: Path = OUTPUTS_ROOT) -> SavedRunListResult:
    if not outputs_root.exists():
        return SavedRunListResult()

    runs: list[SavedRunSummary] = []
    for candidate in outputs_root.iterdir():
        if not candidate.is_dir():
            continue
        metadata_path = candidate / "run_metadata.json"
        if not metadata_path.exists():
            continue
        try:
            metadata = _read_run_metadata(metadata_path)
        except RunHistoryError:
            continue
        publish = _latest_publish(metadata)
        created_at = metadata.created_at or _path_timestamp(metadata_path)
        updated_at = metadata.updated_at or (publish.updated_at if publish else _path_timestamp(candidate))
        request = metadata.request
        runs.append(
            SavedRunSummary(
                run_id=metadata.run_id,
                topology_name=metadata.topology_name,
                requested_topology_name=(
                    request.topology_name if request else _derive_requested_topology_name(metadata.topology_name)
                ),
                reference_topology_id=metadata.reference_topology_id,
                requested_by=metadata.requested_by,
                created_at=created_at,
                updated_at=updated_at,
                private_branch_name=publish.private_branch_name if publish else None,
                private_branch_pushed=publish.private_branch_pushed if publish else False,
            )
        )

    runs.sort(
        key=lambda item: (
            item.updated_at or "",
            item.created_at or "",
            item.run_id,
        ),
        reverse=True,
    )
    return SavedRunListResult(runs=runs)


def load_saved_run(
    run_id: str,
    *,
    inventory_path: Path = INVENTORY_PATH,
    outputs_root: Path = OUTPUTS_ROOT,
) -> SavedRunLoadResult:
    run_root = resolve_run_root(run_id, outputs_root)
    metadata_path = run_root / "run_metadata.json"
    if not metadata_path.exists():
        raise RunHistoryError(f"Run metadata not found for {run_id}")

    metadata = _read_run_metadata(metadata_path)
    inventory = load_inventory(inventory_path)
    saved_request = metadata.request or _reconstruct_request(metadata, run_root, inventory)
    return SavedRunLoadResult(
        request=saved_request,
        result=_build_generate_result(metadata, run_root, inventory),
        publish_result=_build_publish_result(_latest_publish(metadata)),
    )


def _read_run_metadata(path: Path) -> RunMetadata:
    try:
        with path.open() as fh:
            return RunMetadata.model_validate(json.load(fh))
    except (OSError, json.JSONDecodeError, ValueError) as error:
        raise RunHistoryError(f"Invalid run metadata at {path}") from error


def _build_generate_result(metadata: RunMetadata, run_root: Path, inventory) -> GenerateResult:
    topology_path = run_root / metadata.topology_name
    zip_path = run_root / f"{metadata.topology_name}.zip"
    if not zip_path.exists():
        zip_files = sorted(run_root.glob("*.zip"))
        if zip_files:
            zip_path = zip_files[0]

    saved_statuses = {
        (item.hardware_id, item.branch_name, item.edge_name): item for item in metadata.mapping_statuses
    }
    hardware_by_id = {item.id: item for item in inventory.hardware}
    mapping_statuses: list[GenerateMappingStatus] = []
    for mapping in metadata.mappings:
        saved_status = saved_statuses.get((mapping.hardware_id, mapping.branch_name, mapping.edge_name))
        hardware = hardware_by_id.get(mapping.hardware_id)
        path = mapping.path or (saved_status.path if saved_status else None)
        path_resolved = bool(path and path.complete)
        auto_config_ready = bool(path and path.complete and path_has_credentials(path, inventory))
        reason = None
        if not path_resolved:
            reason = (
                saved_status.reason
                if saved_status and saved_status.reason
                else "Stored run does not include a complete imported switch path."
            )
        elif not auto_config_ready:
            reason = "The resolved access or upstream switch is missing stored credentials."
        mapping_statuses.append(
            GenerateMappingStatus(
                hardware_id=mapping.hardware_id,
                hardware_display_name=(
                    hardware.display_name
                    if hardware
                    else saved_status.hardware_display_name
                    if saved_status
                    else mapping.hardware_id
                ),
                branch_name=mapping.branch_name,
                edge_name=mapping.edge_name,
                path_resolved=path_resolved,
                auto_config_ready=auto_config_ready,
                reason=reason,
                path=path,
            )
        )

    messages = list(metadata.messages)
    if not messages:
        messages.append(
            ValidationMessage(
                level="info",
                message=f"Loaded saved topology run {metadata.run_id}.",
            )
        )
    can_configure_switches = bool(mapping_statuses) and all(
        item.path_resolved and item.auto_config_ready for item in mapping_statuses
    )
    return GenerateResult(
        run_id=metadata.run_id,
        topology_name=metadata.topology_name,
        topology_path=str(topology_path),
        zip_path=str(zip_path),
        download_url=f"/api/runs/{metadata.run_id}/download",
        can_configure_switches=can_configure_switches,
        mapping_statuses=mapping_statuses,
        messages=messages,
    )


def _reconstruct_request(metadata: RunMetadata, run_root: Path, inventory) -> SavedGenerateRequest:
    generated_targets = _load_generated_targets(run_root / metadata.topology_name / "config.json")
    first_path = next((mapping.path for mapping in metadata.mappings if mapping.path and mapping.path.complete), None)
    hypervisor_ip = first_path.hypervisor_ip if first_path and first_path.hypervisor_ip else ""
    hypervisor_interface = _resolve_hypervisor_interface(first_path, inventory) if first_path else ""
    hardware_by_id = {item.id: item for item in inventory.hardware}

    mappings: list[MappingRequest] = []
    for mapping in metadata.mappings:
        hardware = hardware_by_id.get(mapping.hardware_id)
        target_branch_name, target_edge_name = _find_generated_target(hardware, generated_targets)
        default_edge_name = (
            f"{mapping.edge_name}-{hardware.model_suffix}"
            if hardware and hardware.model_suffix
            else ""
        )
        mappings.append(
            MappingRequest(
                hardware_id=mapping.hardware_id,
                branch_name=mapping.branch_name,
                edge_name=mapping.edge_name,
                target_branch_name=(
                    target_branch_name if target_branch_name and target_branch_name != mapping.branch_name else None
                ),
                target_edge_name=(
                    target_edge_name
                    if target_edge_name and target_edge_name != default_edge_name
                    else None
                ),
                interface_overrides=[
                    InterfaceOverride(
                        reference_interface=allocation.reference_interface,
                        hardware_interface=allocation.logical_interface,
                        switch_vlans=allocation.switch_vlans,
                    )
                    for allocation in mapping.allocations
                ],
            )
        )

    return SavedGenerateRequest(
        topology_name=_derive_requested_topology_name(metadata.topology_name),
        reference_topology_id=metadata.reference_topology_id,
        hypervisor_ip=hypervisor_ip,
        hypervisor_interface=hypervisor_interface,
        mappings=mappings,
    )


def _load_generated_targets(config_path: Path) -> list[dict[str, str]]:
    if not config_path.exists():
        return []
    try:
        with config_path.open() as fh:
            config = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []

    targets: list[dict[str, str]] = []
    for branch in config.get("topology", {}).get("branches", []):
        branch_name = branch.get("name")
        if not isinstance(branch_name, str) or not branch_name.strip():
            continue
        for edge in branch.get("edges", []):
            edge_name = edge.get("name")
            serial = str(edge.get("slno") or "").strip()
            standby_serial = str(edge.get("standby_slno") or "").strip()
            if not isinstance(edge_name, str) or not edge_name.strip() or not serial:
                continue
            targets.append(
                {
                    "branch_name": branch_name,
                    "edge_name": edge_name,
                    "serial": serial,
                    "standby_serial": standby_serial,
                }
            )
    return targets


def _find_generated_target(hardware, generated_targets: list[dict[str, str]]) -> tuple[str | None, str | None]:
    if hardware:
        target = next(
            (
                item
                for item in generated_targets
                if item["serial"] == hardware.active_serial
                and item["standby_serial"] == str(hardware.standby_serial or "").strip()
            ),
            None,
        )
        if target is None:
            target = next((item for item in generated_targets if item["serial"] == hardware.active_serial), None)
        if target:
            return target["branch_name"], target["edge_name"]

    return None, None


def _resolve_hypervisor_interface(path, inventory) -> str:
    if not path or not path.upstream_switch_id or not path.upstream_hypervisor_port:
        return ""

    for connection in inventory.connections:
        if connection.a.device_id == path.upstream_switch_id and connection.a.interface == path.upstream_hypervisor_port:
            if not path.hypervisor_id or connection.b.device_id == path.hypervisor_id:
                return connection.b.interface
        if connection.b.device_id == path.upstream_switch_id and connection.b.interface == path.upstream_hypervisor_port:
            if not path.hypervisor_id or connection.a.device_id == path.hypervisor_id:
                return connection.a.interface
    return ""


def _build_publish_result(publish: HapyPublishMetadata | None) -> HapyCommitResult | None:
    if publish is None:
        return None
    return HapyCommitResult(
        run_id=publish.run_id,
        topology_name=publish.topology_name,
        reference_topology_id=publish.reference_topology_id,
        repo_path=publish.repo_path,
        destination_path=publish.destination_path,
        destination_relative_path=publish.destination_relative_path,
        base_branch=publish.base_branch,
        private_branch_name=publish.private_branch_name,
        commit_sha=publish.commit_sha,
        commit_message=publish.commit_message,
        private_branch_pushed=publish.private_branch_pushed,
        remote_name=publish.remote_name,
        remote_branch_ref=publish.remote_branch_ref,
        fetch_command=publish.fetch_command,
        created_at=publish.created_at,
        updated_at=publish.updated_at,
        messages=[],
    )


def _latest_publish(metadata: RunMetadata) -> HapyPublishMetadata | None:
    if not metadata.hapy_publishes:
        return None
    return max(
        metadata.hapy_publishes,
        key=lambda item: (item.updated_at, item.created_at, item.private_branch_name),
    )


def _derive_requested_topology_name(topology_name: str) -> str:
    match = re.fullmatch(r"(.+)-[0-9a-f]{6}", topology_name)
    return match.group(1) if match else topology_name


def _path_timestamp(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
