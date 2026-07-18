from __future__ import annotations

import json
import logging
import re
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
import ssl
from typing import Any
from uuid import uuid4

import httpx

from .config import (
    INVENTORY_PATH,
    LAB_NAVIGATOR_API_KEY,
    LAB_NAVIGATOR_BASE_URL,
    LAB_NAVIGATOR_CA_BUNDLE,
    LAB_NAVIGATOR_TLS_VERIFY,
)
from .inventory import build_inventory, load_inventory, save_inventory
from .models import (
    InventoryConnection,
    InventoryDevice,
    InventoryFile,
    InventoryRefreshChange,
    InventoryRefreshRequest,
    InventoryRefreshResult,
    InventoryRefreshSummary,
    InventoryRefreshTargetStatus,
    SwitchCredentials,
    SwitchMetadata,
    ValidationMessage,
)


class DiscoveryError(ValueError):
    pass


logger = logging.getLogger(__name__)
_refresh_log_id: ContextVar[str | None] = ContextVar("refresh_log_id", default=None)


def _new_refresh_log_id() -> str:
    return uuid4().hex[:8]


def _current_refresh_log_id() -> str:
    return _refresh_log_id.get() or "-"


def _log(level: int, message: str, *args: Any) -> None:
    logger.log(level, "[refresh_id=%s] " + message, _current_refresh_log_id(), *args)


@dataclass
class RefreshBuildStats:
    discovered_connection_signatures: set[tuple[tuple[str, str], tuple[str, str]]] = field(default_factory=set)
    refreshed_device_ids: set[str] = field(default_factory=set)
    requested_edge_ids: set[str] = field(default_factory=set)
    preserved_connection_count: int = 0
    skipped_unresolved_remote_count: int = 0
    skipped_unsupported_peer_count: int = 0
    skipped_missing_interface_count: int = 0
    target_statuses: list[InventoryRefreshTargetStatus] = field(default_factory=list)

    def merge(self, other: "RefreshBuildStats") -> None:
        self.discovered_connection_signatures.update(other.discovered_connection_signatures)
        self.refreshed_device_ids.update(other.refreshed_device_ids)
        self.requested_edge_ids.update(other.requested_edge_ids)
        self.preserved_connection_count += other.preserved_connection_count
        self.skipped_unresolved_remote_count += other.skipped_unresolved_remote_count
        self.skipped_unsupported_peer_count += other.skipped_unsupported_peer_count
        self.skipped_missing_interface_count += other.skipped_missing_interface_count
        self.target_statuses.extend(other.target_statuses)

    @property
    def discovered_connection_count(self) -> int:
        return len(self.discovered_connection_signatures)

    @property
    def is_partial(self) -> bool:
        return any(
            (
                self.preserved_connection_count,
                self.skipped_unresolved_remote_count,
                self.skipped_unsupported_peer_count,
                self.skipped_missing_interface_count,
            )
        )


class LabNavigatorClient:
    def __init__(
        self,
        *,
        base_url: str = LAB_NAVIGATOR_BASE_URL,
        api_key: str = LAB_NAVIGATOR_API_KEY,
        timeout: float = 30.0,
        ca_bundle: Path | None = LAB_NAVIGATOR_CA_BUNDLE,
        tls_verify: bool = LAB_NAVIGATOR_TLS_VERIFY,
    ) -> None:
        normalized_api_key = api_key.strip()
        headers = {"Authorization": f"Bearer {normalized_api_key}"} if normalized_api_key else {}
        self.base_url = base_url.rstrip("/")
        verify: bool | ssl.SSLContext
        if not tls_verify:
            verify = False
        elif ca_bundle is not None:
            verify = ssl.create_default_context(cafile=str(ca_bundle))
        else:
            verify = True
        self.client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers=headers,
            verify=verify,
        )

    def close(self) -> None:
        self.client.close()

    def _get_json(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = self.client.get(path, params=params)
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            if status in (401, 403) and "Authorization" not in self.client.headers:
                raise DiscoveryError(
                    "Lab Navigator rejected anonymous discovery; configure LN_PROD_API_KEY for this deployment"
                ) from error
            raise DiscoveryError(f"Lab Navigator request failed with HTTP {status} for {path}") from error
        except httpx.RequestError as error:
            raise DiscoveryError(f"Lab Navigator request failed for {path}: {error}") from error
        try:
            return response.json()
        except ValueError as error:
            raise DiscoveryError(f"Lab Navigator returned invalid JSON for {path}") from error

    def search(self, query: str) -> list[dict[str, Any]]:
        return self._get_json("/api/search", params={"q": query}).get("devices", [])

    def list_inventory(self, filters: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return self._get_json("/api/inventory", params={"filters": json.dumps(filters)}).get("devices", [])

    def get_wiremap(self, device_id: int) -> dict[str, Any]:
        return self._get_json(f"/api/device/{device_id}/wiremap")

    def get_esxi_device_macs(self, device_id: int) -> Any:
        return self._get_json(f"/api/esxi/device-macs/{device_id}")

    def get_server_nics(self, device_id: int) -> Any:
        return self._get_json(f"/api/server/device-nics/{device_id}")


def preview_inventory_refresh(
    request: InventoryRefreshRequest,
    *,
    inventory_path: Path = INVENTORY_PATH,
    client: LabNavigatorClient | None = None,
) -> InventoryRefreshResult:
    token = _refresh_log_id.set(_refresh_log_id.get() or _new_refresh_log_id())
    _log(logging.INFO, "Inventory refresh preview requested for hardware_ids=%s", request.hardware_ids)
    inventory = load_inventory(inventory_path)
    owns_client = client is None
    client = client or LabNavigatorClient()
    try:
        proposed, stats = _build_refreshed_inventory(inventory, request.hardware_ids, client)
        changes = _diff_inventory(inventory, proposed)
        summary = _build_refresh_summary(request.hardware_ids, changes, stats)
        messages = _build_refresh_messages(summary, preview=True)
        _log(
            logging.INFO,
            "Inventory refresh preview completed for hardware_ids=%s changes=%d devices=%d connections=%d partial=%s "
            "discovered_connections=%d preserved_connections=%d skipped_unresolved=%d skipped_unsupported=%d skipped_missing_interface=%d",
            request.hardware_ids,
            len(changes),
            len(proposed.devices),
            len(proposed.connections),
            summary.status == "partial",
            summary.discovered_connection_count,
            summary.preserved_connection_count,
            summary.skipped_unresolved_remote_count,
            summary.skipped_unsupported_peer_count,
            summary.skipped_missing_interface_count,
        )
        return InventoryRefreshResult(
            hardware_ids=request.hardware_ids,
            summary=summary,
            changes=changes,
            inventory=proposed,
            messages=messages,
        )
    finally:
        if owns_client:
            client.close()
        _refresh_log_id.reset(token)


def apply_inventory_refresh(
    request: InventoryRefreshRequest,
    *,
    inventory_path: Path = INVENTORY_PATH,
    client: LabNavigatorClient | None = None,
) -> InventoryRefreshResult:
    token = _refresh_log_id.set(_refresh_log_id.get() or _new_refresh_log_id())
    try:
        _log(logging.INFO, "Inventory refresh apply requested for hardware_ids=%s", request.hardware_ids)
        preview = preview_inventory_refresh(request, inventory_path=inventory_path, client=client)
        saved = save_inventory(
            preview.inventory,
            inventory_path,
            preserve_local_state=True,
            write_source="refresh-apply",
            write_context={"hardware_ids": request.hardware_ids},
        )
        _log(
            logging.INFO,
            "Inventory refresh apply completed for hardware_ids=%s changes=%d saved_devices=%d saved_connections=%d partial=%s",
            request.hardware_ids,
            len(preview.changes),
            len(saved.devices),
            len(saved.connections),
            preview.summary.status == "partial",
        )
        return InventoryRefreshResult(
            hardware_ids=request.hardware_ids,
            summary=preview.summary,
            changes=preview.changes,
            inventory=saved,
            messages=[
                *preview.messages,
                ValidationMessage(
                    level="warning" if preview.summary.status == "partial" else "info",
                    message=(
                        "Applied Lab Navigator inventory refresh with partial results."
                        if preview.summary.status == "partial"
                        else "Applied Lab Navigator inventory refresh."
                    ),
                ),
            ],
        )
    finally:
        _refresh_log_id.reset(token)


def _build_refreshed_inventory(
    inventory: InventoryFile,
    hardware_ids: list[str],
    client: LabNavigatorClient,
) -> tuple[InventoryFile, RefreshBuildStats]:
    devices = {device_id: device.model_dump(mode="json") for device_id, device in inventory.devices.items()}
    connections = [connection.model_dump(mode="json") for connection in inventory.connections]
    hardware_by_id = {item.id: item for item in inventory.hardware}
    stats = RefreshBuildStats()

    for hardware_id in hardware_ids:
        members = _hardware_members(devices, hardware_id)
        if not members:
            raise DiscoveryError(f"{hardware_id} is missing edge devices in inventory")
        stats.requested_edge_ids.update(member.id for member in members)
        hardware_display_name = (
            hardware_by_id[hardware_id].display_name
            if hardware_id in hardware_by_id
            else ", ".join(member.display_name for member in members)
        )
        if hardware_id not in hardware_by_id:
            _log(
                logging.INFO,
                "Refreshing hidden inventory group hardware_id=%s from edge members because it is not currently derived in hardware",
                hardware_id,
            )
        _log(
            logging.INFO,
            "Refreshing inventory graph for hardware_id=%s member_edges=%s",
            hardware_id,
            [member.id for member in members],
        )
        refreshed_ids, refreshed_devices, refreshed_connections, refresh_stats = _discover_lab_navigator_subgraph(
            members,
            devices,
            client,
            hardware_id=hardware_id,
            hardware_display_name=hardware_display_name,
        )
        stats.merge(refresh_stats)
        stats.refreshed_device_ids.update(refreshed_ids)
        _log(
            logging.INFO,
            "Discovered Lab Navigator subgraph for hardware_id=%s inventory_devices=%d connections=%d refreshed_ids=%s "
            "target_status=%s skipped_unresolved=%d skipped_unsupported=%d skipped_missing_interface=%d",
            hardware_id,
            len(refreshed_devices),
            len(refreshed_connections),
            sorted(refreshed_ids),
            refresh_stats.target_statuses[0].status if refresh_stats.target_statuses else "success",
            refresh_stats.skipped_unresolved_remote_count,
            refresh_stats.skipped_unsupported_peer_count,
            refresh_stats.skipped_missing_interface_count,
        )
        for device in refreshed_devices.values():
            _merge_device(devices, device)
        for connection in refreshed_connections:
            if _is_lab_navigator_wiremap_connection(connection):
                _prune_conflicting_wiremap_connections(connections, connection)
            _merge_connection(connections, connection)
            if _is_lab_navigator_wiremap_connection(connection):
                stats.discovered_connection_signatures.add(_connection_signature(connection))

    proposed = build_inventory(devices, connections)
    stats.preserved_connection_count = _count_preserved_wiremap_connections(
        inventory,
        proposed,
        stats.requested_edge_ids,
        stats.discovered_connection_signatures,
    )
    if stats.preserved_connection_count:
        _log(
            logging.WARNING,
            "Preserved %d existing Lab Navigator wiremap connection(s) because discovery did not rediscover them",
            stats.preserved_connection_count,
        )
    return proposed, stats


def _hardware_members(
    devices: dict[str, dict[str, Any]],
    hardware_id: str,
) -> list[InventoryDevice]:
    members: list[InventoryDevice] = []
    for raw_device in devices.values():
        device = InventoryDevice.model_validate(raw_device)
        if device.type != "edge":
            continue
        group_id = device.ha_group_id or device.id
        if group_id == hardware_id:
            members.append(device)
    return members


def _discover_lab_navigator_subgraph(
    root_edges: list[InventoryDevice],
    devices: dict[str, dict[str, Any]],
    client: LabNavigatorClient,
    *,
    hardware_id: str,
    hardware_display_name: str,
) -> tuple[set[str], dict[str, dict[str, Any]], list[dict[str, Any]], RefreshBuildStats]:
    refreshed_ids = {edge.id for edge in root_edges}
    discovered_devices: dict[str, dict[str, Any]] = {}
    discovered_connections: list[dict[str, Any]] = []
    stats = RefreshBuildStats()
    unresolved_interfaces: set[str] = set()
    unsupported_interfaces: set[str] = set()
    missing_interface_data_interfaces: set[str] = set()

    # Phase 1: walk each root edge's wiremap to find edge-access connections and
    # the set of directly connected switches. Issues here are reported as partial failures.
    switch_queue: deque[tuple[InventoryDevice, dict[str, Any]]] = deque()
    visited_lab_navigator_ids: set[int] = set()
    for edge in root_edges:
        ln_edge = _resolve_inventory_device(client, edge)
        _log(
            logging.INFO,
            "Resolved root edge inventory_device=%s to_lab_navigator_id=%s name=%s",
            edge.id,
            ln_edge["id"],
            ln_edge.get("name"),
        )
        visited_lab_navigator_ids.add(ln_edge["id"])
        _log(logging.INFO, "Walking edge wiremap for inventory_device=%s lab_navigator_id=%s", edge.id, ln_edge["id"])
        for item in client.get_wiremap(ln_edge["id"]).get("connections", []):
            interface_name = _edge_interface_name(item)
            remote_name = _wiremap_remote_name(item)
            remote_device = _resolve_wiremap_remote_device(client, item)
            if not remote_device:
                if remote_name:
                    # LN returned a device name but couldn't resolve it — genuine discovery gap.
                    if interface_name:
                        unresolved_interfaces.add(interface_name)
                        stats.skipped_unresolved_remote_count += 1
                    _log(
                        logging.WARNING,
                        "Skipping wiremap entry for inventory_device=%s because remote device %r could not be resolved",
                        edge.id,
                        remote_name,
                    )
                else:
                    # No remote device name — LN is saying the port has no connection.
                    _log(
                        logging.INFO,
                        "Skipping unconnected wiremap entry for inventory_device=%s interface=%s",
                        edge.id,
                        interface_name or item.get("interface_name"),
                    )
                continue

            remote_inventory_device = _wiremap_remote_inventory_device(edge, remote_device, devices, discovered_devices)
            if remote_inventory_device is None:
                if interface_name:
                    unsupported_interfaces.add(interface_name)
                    stats.skipped_unsupported_peer_count += 1
                _log(
                    logging.INFO,
                    "Skipping unsupported wiremap peer for inventory_device=%s remote_name=%s remote_device_type=%s remote_model=%s",
                    edge.id,
                    remote_device.get("name"),
                    remote_device.get("device_type"),
                    _device_model(remote_device),
                )
                continue

            refreshed_ids.add(remote_inventory_device.id)
            discovered_devices[remote_inventory_device.id] = remote_inventory_device.model_dump(mode="json")
            connection = _build_wiremap_connection(edge, remote_inventory_device, item)
            if connection:
                _merge_connection(discovered_connections, connection)
                if _is_lab_navigator_wiremap_connection(connection):
                    stats.discovered_connection_signatures.add(_connection_signature(connection))
                _log(
                    logging.INFO,
                    "Imported wiremap connection role=%s local=%s:%s remote=%s:%s",
                    connection["role"],
                    connection["a"]["device_id"],
                    connection["a"]["interface"],
                    connection["b"]["device_id"],
                    connection["b"]["interface"],
                )
            else:
                if interface_name:
                    missing_interface_data_interfaces.add(interface_name)
                    stats.skipped_missing_interface_count += 1
                _log(
                    logging.WARNING,
                    "Skipped wiremap connection for local_device=%s remote_device=%s due to missing or unsupported interface data",
                    edge.id,
                    remote_inventory_device.id,
                )

            if remote_inventory_device.type == "switch" and remote_device["id"] not in visited_lab_navigator_ids:
                switch_queue.append((remote_inventory_device, remote_device))
                visited_lab_navigator_ids.add(remote_device["id"])

    # Phase 2: BFS through the switch subgraph to find switch-uplink and hypervisor-access
    # connections. Issues here (unresolved/unsupported ports on switches) are not counted as
    # partial failures for the hardware group — they are noise from the switch's full port list.
    while switch_queue:
        current_switch, ln_switch = switch_queue.popleft()
        _log(
            logging.INFO,
            "Walking switch wiremap for inventory_device=%s lab_navigator_id=%s",
            current_switch.id,
            ln_switch["id"],
        )
        for item in client.get_wiremap(ln_switch["id"]).get("connections", []):
            remote_device = _resolve_wiremap_remote_device(client, item)
            if not remote_device:
                _log(
                    logging.INFO,
                    "Skipping unresolved wiremap entry on switch inventory_device=%s: %s",
                    current_switch.id,
                    item,
                )
                continue

            remote_inventory_device = _wiremap_remote_inventory_device(
                current_switch, remote_device, devices, discovered_devices
            )
            if remote_inventory_device is None:
                _log(
                    logging.INFO,
                    "Skipping unsupported wiremap peer on switch inventory_device=%s remote_name=%s",
                    current_switch.id,
                    remote_device.get("name"),
                )
                continue

            refreshed_ids.add(remote_inventory_device.id)
            discovered_devices[remote_inventory_device.id] = remote_inventory_device.model_dump(mode="json")
            connection = _build_wiremap_connection(current_switch, remote_inventory_device, item)
            if connection:
                _merge_connection(discovered_connections, connection)
                if _is_lab_navigator_wiremap_connection(connection):
                    stats.discovered_connection_signatures.add(_connection_signature(connection))
                _log(
                    logging.INFO,
                    "Imported wiremap connection role=%s local=%s:%s remote=%s:%s",
                    connection["role"],
                    connection["a"]["device_id"],
                    connection["a"]["interface"],
                    connection["b"]["device_id"],
                    connection["b"]["interface"],
                )
            else:
                _log(
                    logging.INFO,
                    "Skipped wiremap connection on switch local_device=%s remote_device=%s due to missing or unsupported interface data",
                    current_switch.id,
                    remote_inventory_device.id,
                )

            if remote_inventory_device.type == "switch" and remote_device["id"] not in visited_lab_navigator_ids:
                switch_queue.append((remote_inventory_device, remote_device))
                visited_lab_navigator_ids.add(remote_device["id"])

    labels: list[str] = []
    if unresolved_interfaces:
        labels.append(f"unresolved interfaces: {', '.join(sorted(unresolved_interfaces, key=_refresh_issue_sort_key))}")
    if unsupported_interfaces:
        labels.append(f"unsupported peers on: {', '.join(sorted(unsupported_interfaces, key=_refresh_issue_sort_key))}")
    if missing_interface_data_interfaces:
        labels.append(
            "incomplete interface data on: "
            + ", ".join(sorted(missing_interface_data_interfaces, key=_refresh_issue_sort_key))
        )
    stats.target_statuses.append(
        InventoryRefreshTargetStatus(
            hardware_id=hardware_id,
            hardware_display_name=hardware_display_name,
            status="partial" if labels else "success",
            labels=labels,
        )
    )
    return refreshed_ids, discovered_devices, discovered_connections, stats


def _wiremap_remote_inventory_device(
    current_device: InventoryDevice,
    remote_device: dict[str, Any],
    devices: dict[str, dict[str, Any]],
    discovered_devices: dict[str, dict[str, Any]],
) -> InventoryDevice | None:
    remote_type = _infer_inventory_type(current_device.type, remote_device)
    if remote_type == "switch":
        switch_id = _safe_id(remote_device["name"])
        existing_devices = {**devices, **discovered_devices}
        return InventoryDevice.model_validate(
            _inventory_switch_device(
                remote_device["name"],
                _device_model(remote_device),
                remote_device.get("ip_address") or "",
                remote_device["id"],
                existing=_existing_inventory_device(existing_devices, switch_id),
            )
        )
    if remote_type == "hypervisor":
        return InventoryDevice.model_validate(_inventory_hypervisor_device(remote_device))
    return None


def _build_wiremap_connection(
    local_device: InventoryDevice,
    remote_device: InventoryDevice,
    wiremap_entry: dict[str, Any],
) -> dict[str, Any] | None:
    local_interface = _normalize_interface_name(
        wiremap_entry.get("interface_name") or "",
        local_device.type,
    )
    remote_interface = _normalize_interface_name(
        wiremap_entry.get("remote_interface_name") or wiremap_entry.get("remote_interface") or "",
        remote_device.type,
    )
    if not local_interface or not remote_interface:
        return None

    endpoints = _orient_wiremap_connection(
        local_device.type,
        local_device.id,
        local_interface,
        remote_device.type,
        remote_device.id,
        remote_interface,
    )
    if endpoints is None:
        return None

    role, _left_type, _right_type, left_id, left_if, right_id, right_if = endpoints
    return {
        "id": _build_connection_id(left_id, left_if, right_id, right_if),
        "a": {"device_id": left_id, "interface": left_if},
        "b": {"device_id": right_id, "interface": right_if},
        "vlans": [],
        "tagged_vlans": [],
        "untagged_vlan": None,
        "role": role,
        "notes": "Imported from Lab Navigator wiremap.",
    }


def _resolve_inventory_device(client: LabNavigatorClient, device: InventoryDevice) -> dict[str, Any]:
    if device.lab_navigator_id is not None:
        _log(
            logging.INFO,
            "Using stored Lab Navigator id for inventory_device=%s lab_navigator_id=%s",
            device.id,
            device.lab_navigator_id,
        )
        return {"id": device.lab_navigator_id, "name": device.display_name}

    if device.serial_number:
        serial_matches = [
            item
            for item in client.search(device.serial_number)
            if item.get("serial_number") == device.serial_number
        ]
        if len(serial_matches) == 1:
            _log(
                logging.INFO,
                "Resolved inventory_device=%s via serial_number=%s to_lab_navigator_id=%s",
                device.id,
                device.serial_number,
                serial_matches[0]["id"],
            )
            return serial_matches[0]

    name_matches = [
        item
        for item in client.search(device.display_name)
        if item.get("name") == device.display_name
    ]
    if len(name_matches) == 1:
        _log(
            logging.INFO,
            "Resolved inventory_device=%s via display_name=%s to_lab_navigator_id=%s",
            device.id,
            device.display_name,
            name_matches[0]["id"],
        )
        return name_matches[0]

    raise DiscoveryError(f"Could not resolve Lab Navigator device for inventory device {device.id}")


def _wiremap_remote_name(wiremap_entry: dict[str, Any]) -> str:
    remote_device = wiremap_entry.get("remote_device")
    if isinstance(remote_device, dict):
        return str(remote_device.get("name") or "").strip()
    return str(remote_device or "").strip()


def _resolve_wiremap_remote_device(
    client: LabNavigatorClient,
    wiremap_entry: dict[str, Any],
) -> dict[str, Any] | None:
    remote_device = wiremap_entry.get("remote_device")
    if isinstance(remote_device, dict):
        return remote_device

    remote_name = str(remote_device or "").strip()
    if not remote_name:
        return None
    matches = [item for item in client.search(remote_name) if item.get("name") == remote_name]
    if len(matches) == 1:
        return matches[0]
    return None


def _inventory_switch_device(
    name: str,
    model: str,
    ip_address: str,
    lab_navigator_id: int,
    *,
    existing: InventoryDevice | None = None,
) -> dict[str, Any]:
    existing_metadata = existing.switch_metadata if existing else None
    switch_ip = ip_address or (existing_metadata.connections.ip if existing_metadata else "")
    return InventoryDevice(
        id=_safe_id(name),
        type="switch",
        display_name=name,
        model=model,
        ip_address=switch_ip,
        lab_navigator_id=lab_navigator_id,
        switch_metadata=SwitchMetadata(
            name=name,
            device_type=existing_metadata.device_type if existing_metadata else "DELL",
            model=model or (existing_metadata.model if existing_metadata else "Dell"),
            os_family=existing_metadata.os_family if existing_metadata else None,
            connections={
                "ip": switch_ip,
                "port": existing_metadata.connections.port if existing_metadata else None,
            },
            credentials=existing_metadata.credentials if existing_metadata else SwitchCredentials(),
        ),
    ).model_dump(mode="json")


def _inventory_hypervisor_device(device: dict[str, Any]) -> dict[str, Any]:
    return InventoryDevice(
        id=_safe_id(device["name"]),
        type="hypervisor",
        display_name=device["name"],
        model=device.get("device_model") or device.get("display_model"),
        ip_address=device.get("ip_address"),
        lab_navigator_id=device["id"],
    ).model_dump(mode="json")


def _device_model(device: dict[str, Any]) -> str:
    return str(device.get("device_model") or device.get("display_model") or "")


def _merge_device(devices: dict[str, dict[str, Any]], device: dict[str, Any]) -> None:
    existing = devices.get(device["id"])
    if not existing:
        devices[device["id"]] = device
        return
    merged = {**existing, **{key: value for key, value in device.items() if value not in (None, "", [], {})}}
    devices[device["id"]] = merged


def _merge_connection(connections: list[dict[str, Any]], connection: dict[str, Any]) -> None:
    for index, existing in enumerate(connections):
        if existing["id"] == connection["id"]:
            connections[index] = {**existing, **connection}
            return
    connections.append(connection)


def _prune_conflicting_wiremap_connections(
    connections: list[dict[str, Any]],
    discovered_connection: dict[str, Any],
) -> None:
    discovered_endpoints = _connection_endpoint_set(discovered_connection)
    if not discovered_endpoints:
        return

    retained: list[dict[str, Any]] = []
    for existing in connections:
        if not _is_lab_navigator_wiremap_connection(existing):
            retained.append(existing)
            continue
        if existing.get("id") == discovered_connection.get("id"):
            continue
        if discovered_endpoints & _connection_endpoint_set(existing):
            continue
        retained.append(existing)
    connections[:] = retained


def _existing_inventory_device(
    devices: dict[str, dict[str, Any]],
    device_id: str,
) -> InventoryDevice | None:
    existing = devices.get(device_id)
    if not existing:
        return None
    return InventoryDevice.model_validate(existing)


def _diff_inventory(current: InventoryFile, proposed: InventoryFile) -> list[InventoryRefreshChange]:
    changes: list[InventoryRefreshChange] = []
    for device_id, proposed_device in proposed.devices.items():
        current_device = current.devices.get(device_id)
        if not current_device:
            changes.append(
                InventoryRefreshChange(
                    change_type="add-device",
                    target=device_id,
                    summary=f"Add {proposed_device.type} {proposed_device.display_name}",
                )
            )
            continue
        if current_device.model_dump(mode="json") != proposed_device.model_dump(mode="json"):
            changes.append(
                InventoryRefreshChange(
                    change_type="update-device",
                    target=device_id,
                    summary=f"Update {proposed_device.type} {proposed_device.display_name}",
                )
            )
    current_connections = {item.id: item for item in current.connections}
    for proposed_connection in proposed.connections:
        current_connection = current_connections.get(proposed_connection.id)
        if not current_connection:
            changes.append(
                InventoryRefreshChange(
                    change_type="add-connection",
                    target=proposed_connection.id,
                    summary=f"Add {proposed_connection.role or 'graph'} connection {proposed_connection.id}",
                )
            )
            continue
        if current_connection.model_dump(mode="json") != proposed_connection.model_dump(mode="json"):
            changes.append(
                InventoryRefreshChange(
                    change_type="update-connection",
                    target=proposed_connection.id,
                    summary=f"Update {proposed_connection.role or 'graph'} connection {proposed_connection.id}",
                )
            )
    return changes


def _safe_id(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _edge_interface_name(wiremap_entry: dict[str, Any]) -> str | None:
    interface_name = str(wiremap_entry.get("interface_name") or "").strip().upper()
    if not re.fullmatch(r"(GE|SFP)\d+", interface_name):
        return None
    return interface_name


def _refresh_issue_sort_key(interface_name: str) -> tuple[int, int]:
    match = re.fullmatch(r"([A-Z]+)(\d+)", interface_name)
    if not match:
        return (9, 999)
    prefix, number = match.groups()
    prefix_rank = {"GE": 0, "SFP": 1}.get(prefix, 8)
    return (prefix_rank, int(number))


def _build_refresh_summary(
    hardware_ids: list[str],
    changes: list[InventoryRefreshChange],
    stats: RefreshBuildStats,
) -> InventoryRefreshSummary:
    partial_targets = [target for target in stats.target_statuses if target.status == "partial"]
    return InventoryRefreshSummary(
        status="partial" if partial_targets or stats.preserved_connection_count else "success",
        requested_hardware_count=len(hardware_ids),
        change_count=len(changes),
        discovered_connection_count=stats.discovered_connection_count,
        preserved_connection_count=stats.preserved_connection_count,
        skipped_unresolved_remote_count=stats.skipped_unresolved_remote_count,
        skipped_unsupported_peer_count=stats.skipped_unsupported_peer_count,
        skipped_missing_interface_count=stats.skipped_missing_interface_count,
        targets=stats.target_statuses,
    )


def _build_refresh_messages(
    summary: InventoryRefreshSummary,
    *,
    preview: bool,
) -> list[ValidationMessage]:
    action = "Previewed" if preview else "Applied"
    messages = [
        ValidationMessage(
            level="info",
            message=(
                f"{action} {summary.change_count} inventory change(s) across "
                f"{summary.requested_hardware_count} hardware selection(s)."
            ),
        )
    ]
    if summary.preserved_connection_count:
        messages.append(
            ValidationMessage(
                level="warning",
                message="Kept existing Lab Navigator connections where rediscovery did not return a replacement.",
            )
        )
    return messages


def _count_preserved_wiremap_connections(
    current: InventoryFile,
    proposed: InventoryFile,
    requested_edge_ids: set[str],
    discovered_connection_signatures: set[tuple[tuple[str, str], tuple[str, str]]],
) -> int:
    current_wiremap_ids = {
        connection.id
        for connection in current.connections
        if _is_lab_navigator_wiremap_connection(connection) and _connection_touches_requested_edges(connection, requested_edge_ids)
    }
    preserved = 0
    for connection in proposed.connections:
        if connection.id not in current_wiremap_ids:
            continue
        if not _is_lab_navigator_wiremap_connection(connection):
            continue
        if _connection_signature(connection) in discovered_connection_signatures:
            continue
        preserved += 1
    return preserved


def _is_lab_navigator_wiremap_connection(connection: InventoryConnection | dict[str, Any]) -> bool:
    if isinstance(connection, InventoryConnection):
        notes = connection.notes or ""
    else:
        notes = str(connection.get("notes") or "")
    return "Lab Navigator wiremap" in notes


def _connection_within_inventory_ids(
    connection: InventoryConnection | dict[str, Any],
    inventory_device_ids: set[str],
) -> bool:
    if isinstance(connection, InventoryConnection):
        endpoint_ids = {connection.a.device_id, connection.b.device_id}
    else:
        endpoint_ids = {
            connection.get("a", {}).get("device_id"),
            connection.get("b", {}).get("device_id"),
        }
        endpoint_ids = {endpoint_id for endpoint_id in endpoint_ids if endpoint_id}
    return bool(endpoint_ids) and endpoint_ids <= inventory_device_ids


def _connection_touches_requested_edges(
    connection: InventoryConnection | dict[str, Any],
    requested_edge_ids: set[str],
) -> bool:
    if isinstance(connection, InventoryConnection):
        endpoint_ids = {connection.a.device_id, connection.b.device_id}
    else:
        endpoint_ids = {
            connection.get("a", {}).get("device_id"),
            connection.get("b", {}).get("device_id"),
        }
    return bool({endpoint_id for endpoint_id in endpoint_ids if endpoint_id} & requested_edge_ids)


def _connection_signature(
    connection: InventoryConnection | dict[str, Any],
) -> tuple[tuple[str, str], tuple[str, str]]:
    return tuple(sorted(_connection_endpoint_set(connection)))


def _connection_endpoint_set(
    connection: InventoryConnection | dict[str, Any],
) -> set[tuple[str, str]]:
    if isinstance(connection, InventoryConnection):
        return {
            (connection.a.device_id, connection.a.interface),
            (connection.b.device_id, connection.b.interface),
        }
    return {
        (str(connection.get("a", {}).get("device_id") or ""), str(connection.get("a", {}).get("interface") or "")),
        (str(connection.get("b", {}).get("device_id") or ""), str(connection.get("b", {}).get("interface") or "")),
    }


def _map_device_type(device_type: Any) -> str | None:
    normalized = str(device_type or "").lower()
    if normalized == "edge":
        return "edge"
    if normalized == "switch":
        return "switch"
    if normalized in {"server", "hypervisor"}:
        return "hypervisor"
    return None


def _infer_inventory_type(current_type: str, remote_device: dict[str, Any]) -> str | None:
    explicit = _map_device_type(remote_device.get("device_type"))
    if explicit:
        return explicit

    model = _device_model(remote_device).lower()
    if current_type == "edge":
        return "switch"
    if any(token in model for token in ("dell-3048", "dell-4048", "dell-4148", "switch")):
        return "switch"
    if any(token in model for token in ("esxi", "server", "r640", "r650", "hypervisor")):
        return "hypervisor"
    return None


def _orient_wiremap_connection(
    local_type: str,
    local_id: str,
    local_interface: str,
    remote_type: str,
    remote_id: str,
    remote_interface: str,
) -> tuple[str, str, str, str, str, str, str] | None:
    if {local_type, remote_type} == {"edge", "switch"}:
        if local_type == "edge":
            return ("edge-access", local_type, remote_type, local_id, local_interface, remote_id, remote_interface)
        return ("edge-access", remote_type, local_type, remote_id, remote_interface, local_id, local_interface)
    if local_type == "switch" and remote_type == "hypervisor":
        return ("hypervisor-access", local_type, remote_type, local_id, local_interface, remote_id, remote_interface)
    if local_type == "hypervisor" and remote_type == "switch":
        return ("hypervisor-access", remote_type, local_type, remote_id, remote_interface, local_id, local_interface)
    if local_type == "switch" and remote_type == "switch":
        left = (local_id, local_interface)
        right = (remote_id, remote_interface)
        if right < left:
            return ("switch-uplink", remote_type, local_type, remote_id, remote_interface, local_id, local_interface)
        return ("switch-uplink", local_type, remote_type, local_id, local_interface, remote_id, remote_interface)
    return None


def _normalize_interface_name(value: str, device_type: str) -> str:
    if device_type != "switch":
        return value
    text = (value or "").strip()
    mappings = (
        (r"^gi(\d+/\d+)$", "gigabitethernet"),
        (r"^te(\d+/\d+)$", "tengigabitethernet"),
        (r"^fo(\d+/\d+)$", "fortygigabitethernet"),
        (r"^ma(\d+/\d+)$", "managementethernet"),
    )
    for pattern, prefix in mappings:
        match = re.match(pattern, text, flags=re.IGNORECASE)
        if match:
            return f"{prefix}{match.group(1)}"
    return text.lower()


def _build_connection_id(left_id: str, left_if: str, right_id: str, right_if: str) -> str:
    return _safe_id(f"ln-{left_id}-{left_if}-{right_id}-{right_if}")
