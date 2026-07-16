from __future__ import annotations

import json
import logging
import re
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from .config import INVENTORY_PATH, LAB_NAVIGATOR_API_KEY, LAB_NAVIGATOR_BASE_URL
from .inventory import build_inventory, load_inventory, save_inventory
from .models import (
    HardwareEdge,
    InventoryConnection,
    InventoryDevice,
    InventoryFile,
    InventoryRefreshChange,
    InventoryRefreshRequest,
    InventoryRefreshResult,
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
class WiremapCandidate:
    access_switch: dict[str, Any]
    upstream_switch: dict[str, Any]
    hypervisor: dict[str, Any]
    access_uplink_port: str
    upstream_access_port: str
    upstream_hypervisor_port: str
    hypervisor_interface: str
    reciprocal: bool

    @property
    def rank(self) -> tuple[int, int, int]:
        model = str(self.upstream_switch.get("device_model") or self.upstream_switch.get("display_model") or "")
        preferred_model = 1 if any(token in model for token in ("4048", "4148")) else 0
        return (preferred_model, 1 if self.reciprocal else 0, -2)


class LabNavigatorClient:
    def __init__(
        self,
        *,
        base_url: str = LAB_NAVIGATOR_BASE_URL,
        api_key: str = LAB_NAVIGATOR_API_KEY,
        timeout: float = 30.0,
    ) -> None:
        normalized_api_key = api_key.strip()
        headers = {"Authorization": f"Bearer {normalized_api_key}"} if normalized_api_key else {}
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers=headers,
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
        proposed = _build_refreshed_inventory(inventory, request.hardware_ids, client)
        changes = _diff_inventory(inventory, proposed)
        _log(
            logging.INFO,
            "Inventory refresh preview completed for hardware_ids=%s changes=%d devices=%d connections=%d",
            request.hardware_ids,
            len(changes),
            len(proposed.devices),
            len(proposed.connections),
        )
        messages = [ValidationMessage(level="info", message=f"Previewed {len(changes)} inventory change(s).")]
        return InventoryRefreshResult(
            hardware_ids=request.hardware_ids,
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
        saved = save_inventory(preview.inventory, inventory_path, preserve_local_state=True)
        _log(
            logging.INFO,
            "Inventory refresh apply completed for hardware_ids=%s changes=%d saved_devices=%d saved_connections=%d",
            request.hardware_ids,
            len(preview.changes),
            len(saved.devices),
            len(saved.connections),
        )
        return InventoryRefreshResult(
            hardware_ids=request.hardware_ids,
            changes=preview.changes,
            inventory=saved,
            messages=[
                *preview.messages,
                ValidationMessage(level="info", message="Applied Lab Navigator inventory refresh."),
            ],
        )
    finally:
        _refresh_log_id.reset(token)


def _build_refreshed_inventory(
    inventory: InventoryFile,
    hardware_ids: list[str],
    client: LabNavigatorClient,
) -> InventoryFile:
    devices = {device_id: device.model_dump(mode="json") for device_id, device in inventory.devices.items()}
    connections = [connection.model_dump(mode="json") for connection in inventory.connections]
    hardware_by_id = {item.id: item for item in inventory.hardware}

    for hardware_id in hardware_ids:
        if hardware_id not in hardware_by_id:
            raise DiscoveryError(f"Unknown hardware id: {hardware_id}")
        members = _hardware_members(devices, hardware_id)
        if not members:
            raise DiscoveryError(f"{hardware_id} is missing edge devices in inventory")
        _log(
            logging.INFO,
            "Refreshing inventory graph for hardware_id=%s member_edges=%s",
            hardware_id,
            [member.id for member in members],
        )
        refreshed_ids, refreshed_devices, refreshed_connections = _discover_lab_navigator_subgraph(
            members,
            devices,
            client,
        )
        _log(
            logging.INFO,
            "Discovered Lab Navigator subgraph for hardware_id=%s inventory_devices=%d connections=%d refreshed_ids=%s",
            hardware_id,
            len(refreshed_devices),
            len(refreshed_connections),
            sorted(refreshed_ids),
        )
        _drop_lab_navigator_wiremap_connections(connections, refreshed_ids)
        for device in refreshed_devices.values():
            _merge_device(devices, device)
        for connection in refreshed_connections:
            _merge_connection(connections, connection)

    return build_inventory(devices, connections)


def _discover_candidate_path(hardware: HardwareEdge, client: LabNavigatorClient) -> WiremapCandidate:
    access_switch = _resolve_by_ip(client, hardware.switch.connections.ip)
    hypervisor = _resolve_by_ip(client, hardware.hypervisor_ip or "")
    access_wiremap = client.get_wiremap(access_switch["id"])
    candidates: list[WiremapCandidate] = []
    for connection in access_wiremap.get("connections", []):
        upstream_name = connection.get("remote_device")
        if not upstream_name:
            continue
        upstream_switch = _resolve_by_name(client, upstream_name)
        upstream_wiremap = client.get_wiremap(upstream_switch["id"])
        reciprocal = _has_reciprocal_link(
            upstream_wiremap.get("connections", []),
            access_switch["name"],
            connection.get("remote_interface"),
            connection.get("interface_name"),
        )
        hypervisor_link = _find_hypervisor_link(upstream_wiremap.get("connections", []), hypervisor["name"])
        if not hypervisor_link:
            continue
        candidates.append(
            WiremapCandidate(
                access_switch=access_switch,
                upstream_switch=upstream_switch,
                hypervisor=hypervisor,
                access_uplink_port=connection.get("interface_name") or "",
                upstream_access_port=connection.get("remote_interface") or "",
                upstream_hypervisor_port=hypervisor_link.get("interface_name") or "",
                hypervisor_interface=hypervisor_link.get("remote_interface") or "",
                reciprocal=reciprocal,
            )
        )

    if not candidates:
        raise DiscoveryError(
            f"Could not resolve complete 3048 -> 4048/4148 -> hypervisor path for {hardware.id}"
        )
    candidates.sort(key=lambda item: item.rank, reverse=True)
    best = candidates[0]
    if len(candidates) > 1 and candidates[1].rank == best.rank:
        raise DiscoveryError(f"Multiple equally ranked paths found for {hardware.id}")
    return best


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
) -> tuple[set[str], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    refreshed_ids = {edge.id for edge in root_edges}
    discovered_devices: dict[str, dict[str, Any]] = {}
    discovered_connections: list[dict[str, Any]] = []
    queue: deque[tuple[InventoryDevice, dict[str, Any]]] = deque()
    visited_lab_navigator_ids: set[int] = set()

    for edge in root_edges:
        lab_navigator_device = _resolve_inventory_device(client, edge)
        _log(
            logging.INFO,
            "Resolved root edge inventory_device=%s to_lab_navigator_id=%s name=%s",
            edge.id,
            lab_navigator_device["id"],
            lab_navigator_device.get("name"),
        )
        queue.append((edge, lab_navigator_device))
        visited_lab_navigator_ids.add(lab_navigator_device["id"])

    while queue:
        current_device, lab_navigator_device = queue.popleft()
        _log(
            logging.INFO,
            "Walking wiremap for inventory_device=%s type=%s lab_navigator_id=%s",
            current_device.id,
            current_device.type,
            lab_navigator_device["id"],
        )
        for item in client.get_wiremap(lab_navigator_device["id"]).get("connections", []):
            remote_device = _resolve_wiremap_remote_device(client, item)
            if not remote_device:
                _log(
                    logging.WARNING,
                    "Skipping wiremap entry for inventory_device=%s because remote device could not be resolved: %s",
                    current_device.id,
                    item,
                )
                continue

            remote_inventory_device = _wiremap_remote_inventory_device(
                current_device,
                remote_device,
                devices,
                discovered_devices,
            )
            if remote_inventory_device is None:
                _log(
                    logging.INFO,
                    "Skipping unsupported wiremap peer for inventory_device=%s remote_name=%s remote_device_type=%s remote_model=%s",
                    current_device.id,
                    remote_device.get("name"),
                    remote_device.get("device_type"),
                    _device_model(remote_device),
                )
                continue

            refreshed_ids.add(remote_inventory_device.id)
            discovered_devices[remote_inventory_device.id] = remote_inventory_device.model_dump(mode="json")
            connection = _build_wiremap_connection(current_device, remote_inventory_device, item)
            if connection:
                _merge_connection(discovered_connections, connection)
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
                    logging.WARNING,
                    "Skipped wiremap connection for local_device=%s remote_device=%s due to missing or unsupported interface data",
                    current_device.id,
                    remote_inventory_device.id,
                )

            if (
                remote_inventory_device.type == "switch"
                and remote_device["id"] not in visited_lab_navigator_ids
            ):
                queue.append((remote_inventory_device, remote_device))
                visited_lab_navigator_ids.add(remote_device["id"])

    return refreshed_ids, discovered_devices, discovered_connections


def _drop_lab_navigator_wiremap_connections(
    connections: list[dict[str, Any]],
    inventory_device_ids: set[str],
) -> None:
    retained: list[dict[str, Any]] = []
    removed = 0
    for connection in connections:
        notes = str(connection.get("notes") or "")
        endpoints = {
            connection.get("a", {}).get("device_id"),
            connection.get("b", {}).get("device_id"),
        }
        if "Lab Navigator wiremap" in notes and endpoints & inventory_device_ids:
            removed += 1
            continue
        retained.append(connection)
    connections[:] = retained
    _log(
        logging.INFO,
        "Dropped %d existing Lab Navigator wiremap connection(s) for inventory_device_ids=%s",
        removed,
        sorted(inventory_device_ids),
    )


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

    role, left_type, right_type, left_id, left_if, right_id, right_if = endpoints
    if left_type == "switch":
        vlan_source = (
            wiremap_entry.get("local_vlans") or ""
            if left_id == local_device.id
            else wiremap_entry.get("remote_vlans") or ""
        )
    elif right_type == "switch":
        vlan_source = (
            wiremap_entry.get("remote_vlans") or ""
            if right_id == remote_device.id
            else wiremap_entry.get("local_vlans") or ""
        )
    else:
        vlan_source = wiremap_entry.get("vlans") or ""
    parsed_vlans = _parse_wiremap_vlans("", "", vlan_source)
    return {
        "id": _build_connection_id(left_id, left_if, right_id, right_if),
        "a": {"device_id": left_id, "interface": left_if},
        "b": {"device_id": right_id, "interface": right_if},
        "vlans": parsed_vlans["vlans"],
        "tagged_vlans": parsed_vlans["tagged_vlans"],
        "untagged_vlan": parsed_vlans["untagged_vlan"],
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
    if len(matches) != 1:
        raise DiscoveryError(f"Expected exactly one Lab Navigator device named {remote_name}")
    return matches[0]


def _resolve_by_ip(client: LabNavigatorClient, ip_address: str) -> dict[str, Any]:
    if not ip_address:
        raise DiscoveryError("Discovery requires an exact IP address")
    matches = [item for item in client.search(ip_address) if item.get("ip_address") == ip_address]
    if len(matches) != 1:
        raise DiscoveryError(f"Expected exactly one Lab Navigator device with IP {ip_address}")
    return matches[0]


def _resolve_by_name(client: LabNavigatorClient, name: str) -> dict[str, Any]:
    matches = [item for item in client.search(name) if item.get("name") == name]
    if len(matches) != 1:
        raise DiscoveryError(f"Expected exactly one Lab Navigator device named {name}")
    return matches[0]


def _find_hypervisor_link(connections: list[dict[str, Any]], hypervisor_name: str) -> dict[str, Any] | None:
    for connection in connections:
        if connection.get("remote_device") == hypervisor_name:
            return connection
    return None


def _has_reciprocal_link(
    connections: list[dict[str, Any]],
    access_switch_name: str,
    local_interface: str | None,
    remote_interface: str | None,
) -> bool:
    for connection in connections:
        if connection.get("remote_device") != access_switch_name:
            continue
        if connection.get("interface_name") == local_interface and connection.get("remote_interface") == remote_interface:
            return True
    return False


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


def _parse_wiremap_vlans(local_vlans: str, remote_vlans: str, raw_vlans: str) -> dict[str, Any]:
    vlan_source = raw_vlans or remote_vlans or local_vlans
    tagged: list[int] = []
    untagged: int | None = None
    for part in str(vlan_source or "").split("|"):
        chunk = part.strip()
        if chunk.startswith("Tagged:"):
            tagged.extend(_parse_vlan_numbers(chunk.removeprefix("Tagged:")))
        elif chunk.startswith("Untagged:"):
            numbers = _parse_vlan_numbers(chunk.removeprefix("Untagged:"))
            if numbers:
                untagged = numbers[0]
    ordered_vlans = [untagged] if untagged is not None else []
    ordered_vlans.extend(vlan for vlan in tagged if vlan != untagged)
    return {
        "vlans": ordered_vlans,
        "tagged_vlans": [vlan for vlan in tagged if vlan != untagged],
        "untagged_vlan": untagged,
    }


def _parse_vlan_numbers(value: str) -> list[int]:
    return [int(match) for match in re.findall(r"\d+", value)]


def _build_connection_id(left_id: str, left_if: str, right_id: str, right_if: str) -> str:
    return _safe_id(f"ln-{left_id}-{left_if}-{right_id}-{right_if}")
