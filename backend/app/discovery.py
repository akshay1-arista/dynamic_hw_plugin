from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    SwitchMetadata,
    ValidationMessage,
)


class DiscoveryError(ValueError):
    pass


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
        if not api_key:
            raise DiscoveryError("LN_PROD_API_KEY is required for Lab Navigator discovery")
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    def close(self) -> None:
        self.client.close()

    def search(self, query: str) -> list[dict[str, Any]]:
        response = self.client.get("/api/search", params={"q": query})
        response.raise_for_status()
        return response.json().get("devices", [])

    def list_inventory(self, filters: list[dict[str, Any]]) -> list[dict[str, Any]]:
        response = self.client.get("/api/inventory", params={"filters": json.dumps(filters)})
        response.raise_for_status()
        return response.json().get("devices", [])

    def get_wiremap(self, device_id: int) -> dict[str, Any]:
        response = self.client.get(f"/api/device/{device_id}/wiremap")
        response.raise_for_status()
        return response.json()

    def get_esxi_device_macs(self, device_id: int) -> Any:
        response = self.client.get(f"/api/esxi/device-macs/{device_id}")
        response.raise_for_status()
        return response.json()

    def get_server_nics(self, device_id: int) -> Any:
        response = self.client.get(f"/api/server/device-nics/{device_id}")
        response.raise_for_status()
        return response.json()


def preview_inventory_refresh(
    request: InventoryRefreshRequest,
    *,
    inventory_path: Path = INVENTORY_PATH,
    client: LabNavigatorClient | None = None,
) -> InventoryRefreshResult:
    inventory = load_inventory(inventory_path)
    owns_client = client is None
    client = client or LabNavigatorClient()
    try:
        proposed = _build_refreshed_inventory(inventory, request.hardware_ids, client)
        changes = _diff_inventory(inventory, proposed)
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


def apply_inventory_refresh(
    request: InventoryRefreshRequest,
    *,
    inventory_path: Path = INVENTORY_PATH,
    client: LabNavigatorClient | None = None,
) -> InventoryRefreshResult:
    preview = preview_inventory_refresh(request, inventory_path=inventory_path, client=client)
    saved = save_inventory(preview.inventory, inventory_path)
    return InventoryRefreshResult(
        hardware_ids=request.hardware_ids,
        changes=preview.changes,
        inventory=saved,
        messages=[
            *preview.messages,
            ValidationMessage(level="info", message="Applied Lab Navigator inventory refresh."),
        ],
    )


def _build_refreshed_inventory(
    inventory: InventoryFile,
    hardware_ids: list[str],
    client: LabNavigatorClient,
) -> InventoryFile:
    devices = {device_id: device.model_dump(mode="json") for device_id, device in inventory.devices.items()}
    connections = [connection.model_dump(mode="json") for connection in inventory.connections]
    hardware_by_id = {item.id: item for item in inventory.hardware}

    for hardware_id in hardware_ids:
        hardware = hardware_by_id.get(hardware_id)
        if not hardware:
            raise DiscoveryError(f"Unknown hardware id: {hardware_id}")
        if not hardware.switch:
            raise DiscoveryError(f"{hardware_id} is missing an access switch in inventory")
        if not hardware.hypervisor_ip:
            raise DiscoveryError(f"{hardware_id} is missing hypervisor_ip in inventory")

        candidate = _discover_candidate_path(hardware, client)
        _merge_device(devices, _inventory_switch_device(hardware.switch.name, hardware.switch.model, hardware.switch.connections.ip, candidate.access_switch["id"]))
        _merge_device(devices, _inventory_switch_device(candidate.upstream_switch["name"], _device_model(candidate.upstream_switch), candidate.upstream_switch.get("ip_address") or "", candidate.upstream_switch["id"]))
        _merge_device(devices, _inventory_hypervisor_device(candidate.hypervisor))
        _merge_connection(
            connections,
            {
                "id": f"{_safe_id(hardware.switch.name)}-{candidate.access_uplink_port}-{_safe_id(candidate.upstream_switch['name'])}",
                "a": {"device_id": _safe_id(hardware.switch.name), "interface": candidate.access_uplink_port},
                "b": {"device_id": _safe_id(candidate.upstream_switch["name"]), "interface": candidate.upstream_access_port},
                "vlans": [],
                "tagged_vlans": [],
                "untagged_vlan": None,
                "role": "switch-uplink",
            },
        )
        _merge_connection(
            connections,
            {
                "id": f"{_safe_id(candidate.upstream_switch['name'])}-{candidate.upstream_hypervisor_port}-{_safe_id(candidate.hypervisor['name'])}",
                "a": {"device_id": _safe_id(candidate.upstream_switch["name"]), "interface": candidate.upstream_hypervisor_port},
                "b": {"device_id": _safe_id(candidate.hypervisor["name"]), "interface": candidate.hypervisor_interface},
                "vlans": [],
                "tagged_vlans": [],
                "untagged_vlan": None,
                "role": "hypervisor-access",
            },
        )

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


def _inventory_switch_device(name: str, model: str, ip_address: str, lab_navigator_id: int) -> dict[str, Any]:
    return InventoryDevice(
        id=_safe_id(name),
        type="switch",
        display_name=name,
        model=model,
        ip_address=ip_address,
        lab_navigator_id=lab_navigator_id,
        switch_metadata=SwitchMetadata(
            name=name,
            model=model or "Dell",
            connections={"ip": ip_address, "port": None},
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
