from __future__ import annotations

import json
import logging
import re
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar, copy_context
from dataclasses import dataclass, field
from pathlib import Path
import ssl
from threading import Lock
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
from .edge_models import normalize_edge_model
from .discovery_runtime import (
    check_deadline, consume_preview, current_operation, discovery_operation, get_preview,
    inventory_revision, inventory_write_lock, progress, store_preview,
)
from .inventory import build_inventory, canonical_port_channel, infer_switch_platform, load_inventory, save_inventory
from .models import (
    HardwareImportRequest,
    InventoryConnection,
    InventoryDevice,
    InventoryFile,
    InventoryRefreshChange,
    InventoryRefreshRequest,
    InventoryRefreshResult,
    InventoryRefreshSummary,
    InventoryRefreshTargetStatus,
    LabNavigatorDeviceCandidate,
    LabNavigatorSearchResult,
    SwitchCredentials,
    SwitchMetadata,
    ValidationMessage,
)


class DiscoveryError(ValueError):
    pass


class DiscoveryDeviceNotFound(DiscoveryError):
    pass


logger = logging.getLogger(__name__)
_refresh_log_id: ContextVar[str | None] = ContextVar("refresh_log_id", default=None)


def _new_refresh_log_id() -> str:
    return uuid4().hex[:8]


def _current_refresh_log_id() -> str:
    operation = current_operation()
    return operation.request_id if operation else (_refresh_log_id.get() or "-")


def _log(level: int, message: str, *args: Any) -> None:
    if level == logging.INFO:
        level = logging.DEBUG
    logger.log(level, "[refresh_id=%s] " + message, _current_refresh_log_id(), *args)


@dataclass
class RefreshBuildStats:
    discovered_connection_signatures: set[tuple[tuple[str, str], tuple[str, str]]] = field(default_factory=set)
    refreshed_device_ids: set[str] = field(default_factory=set)
    requested_edge_ids: set[str] = field(default_factory=set)
    preserved_connection_count: int = 0
    skipped_conflicting_endpoint_count: int = 0
    skipped_unresolved_remote_count: int = 0
    skipped_unsupported_peer_count: int = 0
    skipped_missing_interface_count: int = 0
    target_statuses: list[InventoryRefreshTargetStatus] = field(default_factory=list)

    def merge(self, other: "RefreshBuildStats") -> None:
        self.discovered_connection_signatures.update(other.discovered_connection_signatures)
        self.refreshed_device_ids.update(other.refreshed_device_ids)
        self.requested_edge_ids.update(other.requested_edge_ids)
        self.preserved_connection_count += other.preserved_connection_count
        self.skipped_conflicting_endpoint_count += other.skipped_conflicting_endpoint_count
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
                self.skipped_conflicting_endpoint_count,
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
        self._inventory_devices: dict[int, dict[str, Any]] = {}
        self._cache: dict[tuple, Any] = {}
        self._cache_lock = Lock()
        self._key_locks: dict[tuple, Lock] = {}
        self._bulk_loaded = False
        self._metadata_index: dict[str, list[dict[str, Any]]] = {}
        self._timeout = min(timeout, 10.0)
        self._scope_id: str | None = None

    def close(self) -> None:
        self.client.close()

    def _get_json(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        check_deadline()
        key = (path, json.dumps(params, sort_keys=True))
        with self._cache_lock:
            lock = self._key_locks.setdefault(key, Lock())
        with lock:
            if key in self._cache:
                if operation := current_operation():
                    operation.increment("cache_hits")
                result = self._cache[key]
                if isinstance(result, DiscoveryError):
                    raise result
                return result
            try:
                result = self._fetch_json(path, params=params)
            except DiscoveryError as error:
                self._cache[key] = error
                raise
            self._cache[key] = result
            return result

    def _fetch_json(self, path: str, *, params=None):
        try:
            operation = current_operation()
            timeout = min(self._timeout, operation.remaining()) if operation else self._timeout
            if operation:
                operation.increment("upstream_calls")
            response = self.client.get(path, params=params, timeout=timeout)
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            if status == 404 and path.endswith("/wiremap"):
                raise DiscoveryDeviceNotFound(f"Lab Navigator device is unavailable (HTTP 404 for {path})") from error
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
        if query in self._metadata_index:
            return self._metadata_index[query]
        return self._get_json("/api/search", params={"q": query}).get("devices", [])

    def prepare_discovery(self):
        operation = current_operation()
        scope = operation.request_id if operation else None
        if self._scope_id is not None and self._scope_id != scope:
            self._cache.clear()
            self._key_locks.clear()
            self._inventory_devices.clear()
            self._metadata_index.clear()
            self._bulk_loaded = False
        self._scope_id = scope
        if self._bulk_loaded:
            return
        self._bulk_loaded = True
        progress("metadata", "Loading Lab Navigator device metadata")
        try:
            devices = self.list_inventory([])
        except DiscoveryError:
            progress(message="Bulk metadata unavailable; using cached individual lookups", level="warning")
            return
        for device in devices:
            if isinstance(device.get("id"), int):
                self._inventory_devices[device["id"]] = device
            for value in {device.get("name"), device.get("serial_number"), device.get("ip_address")} - {None, ""}:
                self._metadata_index.setdefault(value, []).append(device)

    def prefetch_wiremaps(self, device_ids):
        pending = [identifier for identifier in dict.fromkeys(device_ids)
                   if (f"/api/device/{identifier}/wiremap", "null") not in self._cache]
        if not pending:
            return
        def fetch(identifier):
            try:
                self.get_wiremap(identifier)
            except DiscoveryError:
                pass  # Required/optional handling remains with the graph walker.
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="discovery") as executor:
            futures = [executor.submit(copy_context().run, fetch, identifier) for identifier in pending]
            for future in futures:
                future.result()

    def list_inventory(self, filters: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return self._get_json("/api/inventory", params={"filters": json.dumps(filters)}).get("devices", [])

    def get_wiremap(self, device_id: int) -> dict[str, Any]:
        return self._get_json(f"/api/device/{device_id}/wiremap")

    def get_esxi_device_macs(self, device_id: int) -> Any:
        return self._get_json(f"/api/esxi/device-macs/{device_id}")

    def get_server_nics(self, device_id: int) -> Any:
        return self._get_json(f"/api/server/device-nics/{device_id}")

    def get_inventory_device(self, device_id: int) -> dict[str, Any]:
        if device_id in self._inventory_devices:
            return self._inventory_devices[device_id]
        matches = self.list_inventory([{"field": "id", "operator": "equals", "value": str(device_id)}])
        exact = [item for item in matches if item.get("id") == device_id]
        if len(exact) == 1:
            self._inventory_devices[device_id] = exact[0]
            return exact[0]
        wiremap = self.get_wiremap(device_id)
        device = wiremap.get("device")
        if isinstance(device, dict):
            self._inventory_devices[device_id] = device
            return device
        raise DiscoveryError(f"Could not resolve Lab Navigator device id {device_id}")


@discovery_operation("search")
def search_lab_navigator_devices(
    query: str,
    *,
    client: LabNavigatorClient | None = None,
) -> LabNavigatorSearchResult:
    cleaned = query.strip()
    if not cleaned:
        raise DiscoveryError("Search query is required")
    owns_client = client is None
    client = client or LabNavigatorClient()
    try:
        devices = [
            LabNavigatorDeviceCandidate.model_validate(item)
            for item in client.search(cleaned)
            if item.get("id") is not None and item.get("name")
        ]
        return LabNavigatorSearchResult(query=cleaned, count=len(devices), devices=devices)
    finally:
        if owns_client:
            client.close()


@discovery_operation("import-preview")
def preview_inventory_import(
    request: HardwareImportRequest,
    *,
    inventory_path: Path = INVENTORY_PATH,
    client: LabNavigatorClient | None = None,
) -> InventoryRefreshResult:
    inventory = load_inventory(inventory_path)
    owns_client = client is None
    client = client or LabNavigatorClient()
    try:
        _prepare_discovery(client)
        proposed, target_ids, stats = _build_imported_inventory(inventory, request, client)
        changes = _diff_inventory(inventory, proposed)
        summary = _build_refresh_summary(target_ids, changes, stats)
        messages = _build_refresh_messages(summary, preview=True)
        return InventoryRefreshResult(
            hardware_ids=target_ids,
            summary=summary,
            changes=changes,
            inventory=proposed,
            messages=messages,
        )
    finally:
        if owns_client:
            client.close()


@discovery_operation("import-apply")
def apply_inventory_import(
    request: HardwareImportRequest,
    *,
    inventory_path: Path = INVENTORY_PATH,
    client: LabNavigatorClient | None = None,
) -> InventoryRefreshResult:
    owns_client = client is None
    client = client or LabNavigatorClient()
    try:
        preview = preview_inventory_import(request, inventory_path=inventory_path, client=client)
        saved = save_inventory(
            preview.inventory,
            inventory_path,
            preserve_local_state=True,
            write_source="import-apply",
            write_context={"targets": preview.hardware_ids},
        )
        return InventoryRefreshResult(
            hardware_ids=preview.hardware_ids,
            summary=preview.summary,
            changes=preview.changes,
            inventory=saved,
            messages=[
                *preview.messages,
                ValidationMessage(
                    level="warning" if preview.summary.status == "partial" else "info",
                    message=(
                        "Applied Lab Navigator inventory import with partial results."
                        if preview.summary.status == "partial"
                        else "Applied Lab Navigator inventory import."
                    ),
                ),
            ],
        )
    finally:
        if owns_client:
            client.close()


@discovery_operation("refresh-preview")
def preview_inventory_refresh(
    request: InventoryRefreshRequest,
    *,
    inventory_path: Path = INVENTORY_PATH,
    client: LabNavigatorClient | None = None,
) -> InventoryRefreshResult:
    token = _refresh_log_id.set(_refresh_log_id.get() or _new_refresh_log_id())
    _log(logging.INFO, "Inventory refresh preview requested for hardware_ids=%s", request.hardware_ids)
    with inventory_write_lock:
        revision = inventory_revision(inventory_path)
        inventory = load_inventory(inventory_path)
    owns_client = client is None
    client = client or LabNavigatorClient()
    try:
        _prepare_discovery(client)
        proposed, stats = _build_refreshed_inventory(inventory, request.hardware_ids, client)
        progress("diff", "Calculating inventory changes")
        changes = _diff_inventory(inventory, proposed)
        summary = _build_refresh_summary(request.hardware_ids, changes, stats)
        messages = _build_refresh_messages(summary, preview=True)
        _log(
            logging.INFO,
            "Inventory refresh preview completed for hardware_ids=%s changes=%d devices=%d connections=%d partial=%s "
            "discovered_connections=%d preserved_connections=%d skipped_conflicting=%d skipped_unresolved=%d "
            "skipped_unsupported=%d skipped_missing_interface=%d",
            request.hardware_ids,
            len(changes),
            len(proposed.devices),
            len(proposed.connections),
            summary.status == "partial",
            summary.discovered_connection_count,
            summary.preserved_connection_count,
            summary.skipped_conflicting_endpoint_count,
            summary.skipped_unresolved_remote_count,
            summary.skipped_unsupported_peer_count,
            summary.skipped_missing_interface_count,
        )
        result = InventoryRefreshResult(
            hardware_ids=request.hardware_ids,
            summary=summary,
            changes=changes,
            inventory=proposed,
            messages=messages,
        )
        store_preview(result, inventory_path, revision)
        return result
    finally:
        if owns_client:
            client.close()
        _refresh_log_id.reset(token)


@discovery_operation("refresh-apply")
def apply_inventory_refresh(
    request: InventoryRefreshRequest,
    *,
    inventory_path: Path = INVENTORY_PATH,
    client: LabNavigatorClient | None = None,
) -> InventoryRefreshResult:
    token = _refresh_log_id.set(_refresh_log_id.get() or _new_refresh_log_id())
    try:
        _log(logging.INFO, "Inventory refresh apply requested for hardware_ids=%s", request.hardware_ids)
        if request.preview_id:
            with inventory_write_lock:
                preview = get_preview(request.preview_id, inventory_path, request.hardware_ids)
                progress("saving", "Saving the reviewed discovery result")
                saved = save_inventory(preview.inventory, inventory_path, preserve_local_state=True,
                                       write_source="refresh-apply", write_context={"hardware_ids": request.hardware_ids})
                consume_preview(request.preview_id)
        else:
            preview = preview_inventory_refresh(request, inventory_path=inventory_path, client=client)
            with inventory_write_lock:
                get_preview(preview.preview_id, inventory_path, request.hardware_ids)
                progress("saving", "Saving discovered inventory")
                saved = save_inventory(preview.inventory, inventory_path, preserve_local_state=True,
                                       write_source="refresh-apply", write_context={"hardware_ids": request.hardware_ids})
                consume_preview(preview.preview_id)
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


@discovery_operation("generation-sync")
def sync_inventory_for_generate(
    hardware_ids: list[str],
    *,
    inventory_path: Path = INVENTORY_PATH,
    client: LabNavigatorClient | None = None,
) -> tuple[InventoryFile, RefreshBuildStats]:
    target_ids = list(dict.fromkeys(item for item in hardware_ids if item))
    if not target_ids:
        return load_inventory(inventory_path), RefreshBuildStats()
    try:
        result = apply_inventory_refresh(
            InventoryRefreshRequest(hardware_ids=target_ids),
            inventory_path=inventory_path,
            client=client,
        )
        return result.inventory, RefreshBuildStats()
    except (DiscoveryError, ValueError, FileNotFoundError, OSError):
        if operation := current_operation():
            operation.partial = True
        progress("fallback", "Live discovery unavailable; using saved inventory", level="warning")
        return load_inventory(inventory_path), RefreshBuildStats()


def _prepare_discovery(client):
    prepare = getattr(client, "prepare_discovery", None)
    if prepare:
        prepare()


def _build_imported_inventory(
    inventory: InventoryFile,
    request: HardwareImportRequest,
    client: LabNavigatorClient,
) -> tuple[InventoryFile, list[str], RefreshBuildStats]:
    devices = {device_id: device.model_dump(mode="json") for device_id, device in inventory.devices.items()}
    connections = [connection.model_dump(mode="json") for connection in inventory.connections]
    stats = RefreshBuildStats()

    ln_targets = [_resolve_import_target(client, target) for target in request.targets]
    target_roles = [target.role for target in request.targets if target.role]
    ha_group_id = _ha_import_group_id(ln_targets, target_roles) if target_roles else None

    imported_roots: list[InventoryDevice] = []
    for ln_device, target in zip(ln_targets, request.targets):
        imported = _inventory_device_from_lab_navigator(
            ln_device,
            devices,
            role=target.role,
            ha_group_id=ha_group_id,
        )
        devices[imported.id] = imported.model_dump(mode="json")
        imported_roots.append(imported)

    target_ids: list[str] = []
    if ha_group_id:
        target_ids.append(ha_group_id)
    else:
        target_ids.extend(root.ha_group_id or root.id for root in imported_roots)

    if all(root.type == "edge" for root in imported_roots):
        groups = {}
        for root in imported_roots:
            group = root.ha_group_id or root.id
            groups.setdefault(group, (root.display_name, set()))[1].add(root.id)
        discovered_graphs = [_discover_lab_navigator_subgraph(
            imported_roots, devices, client, hardware_id=target_ids[0],
            hardware_display_name=imported_roots[0].display_name, groups=groups,
        )]
    else:
        discovered_graphs = [_discover_import_root_subgraph(
            root, devices, client, target_id=root.ha_group_id or root.id,
            target_display_name=root.display_name,
        ) for root in imported_roots]
    merged_connections = {connection["id"]: connection for connection in connections}
    for refreshed_ids, refreshed_devices, refreshed_connections, import_stats in discovered_graphs:
        stats.merge(import_stats)
        stats.refreshed_device_ids.update(refreshed_ids)
        for device in refreshed_devices.values():
            _merge_device(devices, device)
        for connection in refreshed_connections:
            _merge_connection(merged_connections, connection)
            if _is_lab_navigator_wiremap_connection(connection):
                stats.discovered_connection_signatures.add(_connection_signature(connection))
    connections = list(merged_connections.values())
    progress("deriving", "Deriving imported hardware paths")

    proposed = build_inventory(devices, connections)
    return proposed, list(dict.fromkeys(target_ids)), stats


def _resolve_import_target(client: LabNavigatorClient, target) -> dict[str, Any]:
    if target.lab_navigator_id is not None:
        return client.get_inventory_device(target.lab_navigator_id)

    query = (target.query or "").strip()
    matches = client.search(query)
    if len(matches) == 1:
        return client.get_inventory_device(matches[0]["id"])
    if not matches:
        raise DiscoveryError(f"No Lab Navigator device matched {query!r}")
    candidates = ", ".join(
        f"{item.get('name') or item.get('id')} ({item.get('serial_number') or item.get('ip_address') or 'no identifier'})"
        for item in matches[:8]
    )
    raise DiscoveryError(f"Lab Navigator import query {query!r} matched multiple devices: {candidates}")


def _ha_import_group_id(ln_targets: list[dict[str, Any]], roles: list[str | None]) -> str | None:
    if sorted(role for role in roles if role) != ["active", "standby"]:
        return None
    active = next(device for device, role in zip(ln_targets, roles) if role == "active")
    standby = next(device for device, role in zip(ln_targets, roles) if role == "standby")
    return _safe_id(
        "ln-ha-"
        + "-".join(
            [
                str(active.get("name") or active.get("id")),
                str(active.get("serial_number") or ""),
                str(standby.get("name") or standby.get("id")),
                str(standby.get("serial_number") or ""),
            ]
        )
    )


def _inventory_device_from_lab_navigator(
    device: dict[str, Any],
    existing_devices: dict[str, dict[str, Any]],
    *,
    role: str | None = None,
    ha_group_id: str | None = None,
) -> InventoryDevice:
    existing = _find_existing_by_lab_navigator_id(existing_devices, device["id"])
    if existing is None:
        existing = _existing_inventory_device(existing_devices, _safe_id(str(device.get("name") or device["id"])))
    inventory_type = _map_device_type(device.get("device_type")) or _infer_inventory_type("switch", device)
    if inventory_type == "switch":
        return InventoryDevice.model_validate(
            _inventory_switch_device(
                str(device.get("name") or device["id"]),
                _device_model(device),
                device.get("ip_address") or "",
                device["id"],
                existing=existing,
            )
        )
    if inventory_type == "hypervisor":
        return InventoryDevice.model_validate(_inventory_hypervisor_device(device, existing_id=existing.id if existing else None))
    if inventory_type != "edge":
        raise DiscoveryError(
            f"Lab Navigator device {device.get('name') or device.get('id')} has unsupported type {device.get('device_type')}"
        )
    model = _device_model(device)
    normalized_model, model_suffix = normalize_edge_model(model)
    return InventoryDevice(
        id=existing.id if existing else _safe_id(str(device.get("name") or device["id"])),
        type="edge",
        display_name=str(device.get("name") or device["id"]),
        short_name=device.get("hostname"),
        model=normalized_model,
        model_suffix=model_suffix,
        serial_number=device.get("serial_number"),
        ip_address=device.get("ip_address"),
        lab_navigator_id=device["id"],
        ha_group_id=ha_group_id or (existing.ha_group_id if existing else None),
        ha_role=role or (existing.ha_role if existing else "active"),
        dpdk_enabled=existing.dpdk_enabled if existing else None,
        free_vlans=existing.free_vlans if existing else [],
        vlan_range=existing.vlan_range if existing else None,
        hypervisor_ip=existing.hypervisor_ip if existing else None,
        notes=existing.notes if existing else None,
    )


def _discover_import_root_subgraph(
    root: InventoryDevice,
    devices: dict[str, dict[str, Any]],
    client: LabNavigatorClient,
    *,
    target_id: str,
    target_display_name: str,
) -> tuple[set[str], dict[str, dict[str, Any]], list[dict[str, Any]], RefreshBuildStats]:
    if root.type == "edge":
        return _discover_lab_navigator_subgraph(
            [root],
            devices,
            client,
            hardware_id=target_id,
            hardware_display_name=target_display_name,
        )

    refreshed_ids = {root.id}
    discovered_devices: dict[str, dict[str, Any]] = {}
    discovered_connections: dict[str, dict[str, Any]] = {}
    stats = RefreshBuildStats()
    queue: deque[tuple[InventoryDevice, dict[str, Any]]] = deque([(root, _resolve_inventory_device(client, root))])
    visited_lab_navigator_ids: set[int] = set()
    skipped_switch_wiremaps: list[str] = []

    progress("switches", "Discovering imported device topology", total_roots=1)
    while queue:
        check_deadline()
        prefetch = getattr(client, "prefetch_wiremaps", lambda ids: None)
        prefetch([item[1]["id"] for item in queue])
        current_device, ln_device = queue.popleft()
        if ln_device["id"] in visited_lab_navigator_ids:
            continue
        visited_lab_navigator_ids.add(ln_device["id"])
        progress(completed_roots=int(root.lab_navigator_id in visited_lab_navigator_ids),
                 completed_switches=len(visited_lab_navigator_ids) - 1,
                 discovered_switches=len(visited_lab_navigator_ids) - 1 + len({item[1]["id"] for item in queue}))
        required = current_device.id == root.id
        wiremap = _try_get_wiremap(client, ln_device["id"], required=required)
        if wiremap is None:
            skipped_switch_wiremaps.append(_wiremap_device_label(ln_device, current_device))
            continue
        for item in wiremap.get("connections", []):
            remote_device = _resolve_wiremap_remote_device(client, item)
            if not remote_device:
                continue
            remote_inventory_device = _wiremap_remote_inventory_device(
                current_device,
                remote_device,
                devices,
                discovered_devices,
            )
            if remote_inventory_device is None:
                continue
            refreshed_ids.add(remote_inventory_device.id)
            discovered_devices[remote_inventory_device.id] = remote_inventory_device.model_dump(mode="json")
            connection = _build_wiremap_connection(current_device, remote_inventory_device, item)
            if connection:
                _merge_connection(discovered_connections, connection)
                if _is_lab_navigator_wiremap_connection(connection):
                    stats.discovered_connection_signatures.add(_connection_signature(connection))
            if remote_inventory_device.type == "switch" and remote_device["id"] not in visited_lab_navigator_ids:
                queue.append((remote_inventory_device, remote_device))

    labels = []
    if skipped_switch_wiremaps:
        labels.append("skipped switch wiremaps: " + ", ".join(sorted(skipped_switch_wiremaps)))
    stats.target_statuses.append(
        InventoryRefreshTargetStatus(
            hardware_id=target_id,
            hardware_display_name=target_display_name,
            status="partial" if labels else "success",
            labels=labels,
        )
    )
    return refreshed_ids, discovered_devices, list(discovered_connections.values()), stats


def _build_refreshed_inventory(
    inventory: InventoryFile,
    hardware_ids: list[str],
    client: LabNavigatorClient,
) -> tuple[InventoryFile, RefreshBuildStats]:
    devices = {key: device.model_dump(mode="json") for key, device in inventory.devices.items()}
    hardware_by_id = {item.id: item for item in inventory.hardware}
    groups = {}
    roots = []
    for hardware_id in hardware_ids:
        members = _hardware_members(devices, hardware_id)
        if not members:
            raise DiscoveryError(f"{hardware_id} is missing edge devices in inventory")
        name = (hardware_by_id[hardware_id].display_name if hardware_id in hardware_by_id
                else ", ".join(member.display_name for member in members))
        groups[hardware_id] = (name, {member.id for member in members})
        roots.extend(members)
    refreshed_ids, discovered, connections, stats = _discover_lab_navigator_subgraph(
        roots, devices, client, hardware_id=hardware_ids[0],
        hardware_display_name=groups[hardware_ids[0]][0], groups=groups,
    )
    stats.refreshed_device_ids.update(refreshed_ids)
    progress("merging", "Merging the discovered topology")
    for device in discovered.values():
        _merge_device(devices, device)
    merged = _merge_discovered_connections(inventory, connections, stats, groups)
    progress("deriving", "Deriving hardware paths from the merged topology")
    proposed = build_inventory(devices, merged)
    stats.preserved_connection_count = _count_preserved_wiremap_connections(
        inventory, proposed, stats.requested_edge_ids, stats.discovered_connection_signatures,
    )
    return proposed, stats


def _merge_discovered_connections(inventory, discovered, stats, groups):
    # Keep insertion order and update endpoint indexes as stale connections are removed.
    connections = {item.id: item.model_dump(mode="json") for item in inventory.connections}
    endpoints: dict[tuple[str, str], set[str]] = {}
    for key, item in connections.items():
        if _is_lab_navigator_wiremap_connection(item):
            for endpoint in _connection_endpoint_set(item):
                endpoints.setdefault(endpoint, set()).add(key)
    conflicts = {key: set() for key in groups}
    for connection in discovered:
        check_deadline()
        touched = _connection_endpoint_set(connection)
        overlapping = set().union(*(endpoints.get(endpoint, set()) for endpoint in touched))
        protected = [connections[key] for key in overlapping if key != connection["id"]
                     and _connection_role(connections[key]) == "edge-access"
                     and not _connection_touches_requested_edges(connections[key], stats.requested_edge_ids)]
        if protected:
            stats.skipped_conflicting_endpoint_count += 1
            for group, (_, edges) in groups.items():
                interface = _requested_edge_interface(connection, edges)
                if interface:
                    conflicts[group].add(interface)
            continue
        for key in overlapping:
            old = connections.pop(key)
            for endpoint in _connection_endpoint_set(old):
                endpoints[endpoint].discard(key)
        existing = connections.get(connection["id"], {})
        connections[connection["id"]] = {**existing, **connection}
        for endpoint in touched:
            endpoints.setdefault(endpoint, set()).add(connection["id"])
    for target in stats.target_statuses:
        if conflicts[target.hardware_id]:
            target.status = "partial"
            target.labels.append("conflicting switch ports preserved for other inventory hardware: "
                                 + ", ".join(sorted(conflicts[target.hardware_id], key=_refresh_issue_sort_key)))
    return list(connections.values())


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
    groups: dict | None = None,
) -> tuple[set[str], dict[str, dict[str, Any]], list[dict[str, Any]], RefreshBuildStats]:
    groups = groups or {hardware_id: (hardware_display_name, {edge.id for edge in root_edges})}
    refreshed_ids = {edge.id for edge in root_edges}
    discovered_devices = {}
    discovered_connections = {}
    stats = RefreshBuildStats()
    unresolved = {edge.id: set() for edge in root_edges}
    root_switches = {edge.id: set() for edge in root_edges}
    adjacency: dict[int, set[int]] = {}
    failed_switches = {}
    missing_roots = {}
    scheduled: set[int] = set()
    switch_queue = deque()
    prefetch = getattr(client, "prefetch_wiremaps", lambda ids: None)
    resolved_roots = [(edge, _resolve_inventory_device(client, edge)) for edge in root_edges]
    progress("roots", "Discovering selected edge connections", total_roots=len(root_edges))
    prefetch([ln_edge["id"] for _, ln_edge in resolved_roots])

    def record(local, remote, item):
        refreshed_ids.add(remote.id)
        discovered_devices[remote.id] = remote.model_dump(mode="json")
        connection = _build_wiremap_connection(local, remote, item)
        if connection:
            _merge_connection(discovered_connections, connection)
            stats.discovered_connection_signatures.add(_connection_signature(connection))

    def schedule(remote, ln_remote):
        identifier = ln_remote["id"]
        if remote.type == "switch" and identifier not in scheduled:
            scheduled.add(identifier)
            switch_queue.append((remote, ln_remote))

    for completed, (edge, ln_edge) in enumerate(resolved_roots, 1):
        check_deadline()
        try:
            wiremap = _try_get_wiremap(client, ln_edge["id"], required=True)
        except DiscoveryDeviceNotFound:
            # A removed device must not prevent other groups from refreshing, or lose its old wiring.
            if len(groups) == 1:
                raise
            missing_roots[edge.id] = f"{edge.display_name} (Lab Navigator ID {ln_edge['id']})"
            progress(message=f"Preserving unavailable device {missing_roots[edge.id]}", level="warning",
                     completed_roots=completed, discovered_switches=len(scheduled))
            continue
        stats.requested_edge_ids.add(edge.id)
        for item in wiremap.get("connections", []):
            remote = _resolve_wiremap_remote_device(client, item)
            if not remote:
                interface = _edge_interface_name(item)
                if _wiremap_remote_name(item) and interface:
                    unresolved[edge.id].add(interface)
                    stats.skipped_unresolved_remote_count += 1
                continue
            peer = _wiremap_remote_inventory_device(edge, remote, devices, discovered_devices)
            if peer is None:
                continue
            record(edge, peer, item)
            if peer.type == "switch":
                root_switches[edge.id].add(remote["id"])
                schedule(peer, remote)
        progress(completed_roots=completed, discovered_switches=len(scheduled))

    completed_switches = 0
    progress("switches", "Discovering shared switches")
    while switch_queue:
        check_deadline()
        batch = list(switch_queue)
        switch_queue.clear()
        prefetch([ln_switch["id"] for _, ln_switch in batch])
        for switch, ln_switch in batch:
            check_deadline()
            identifier = ln_switch["id"]
            wiremap = _try_get_wiremap(client, identifier, required=False)
            if wiremap is None:
                failed_switches[identifier] = _wiremap_device_label(ln_switch, switch)
            else:
                for item in wiremap.get("connections", []):
                    remote = _resolve_wiremap_remote_device(client, item)
                    if not remote:
                        continue
                    peer = _wiremap_remote_inventory_device(switch, remote, devices, discovered_devices)
                    if peer is None:
                        continue
                    record(switch, peer, item)
                    if peer.type == "switch":
                        # Directed reachability preserves each group's original wiremap walk.
                        adjacency.setdefault(identifier, set()).add(remote["id"])
                        schedule(peer, remote)
            completed_switches += 1
            progress(completed_switches=completed_switches, discovered_switches=len(scheduled))

    for group_id, (name, edge_ids) in groups.items():
        interfaces = set().union(*(unresolved[edge_id] for edge_id in edge_ids))
        reachable = set().union(*(root_switches[edge_id] for edge_id in edge_ids))
        queue = deque(reachable)
        while queue:
            identifier = queue.popleft()
            for remote in adjacency.get(identifier, set()) - reachable:
                reachable.add(remote)
                queue.append(remote)
        labels = []
        missing = [missing_roots[edge_id] for edge_id in sorted(edge_ids) if edge_id in missing_roots]
        if missing:
            labels.append("unavailable devices preserved: " + ", ".join(missing))
        if interfaces:
            labels.append("unresolved interfaces: " + ", ".join(sorted(interfaces, key=_refresh_issue_sort_key)))
        failures = sorted(failed_switches[key] for key in reachable if key in failed_switches)
        if failures:
            labels.append("skipped switch wiremaps: " + ", ".join(failures))
            progress(message=f"Skipped {len(failures)} unavailable switch wiremap(s) for {name}", level="warning")
        stats.target_statuses.append(InventoryRefreshTargetStatus(
            hardware_id=group_id, hardware_display_name=name,
            status="partial" if labels else "success", labels=labels,
        ))
    return refreshed_ids, discovered_devices, list(discovered_connections.values()), stats


def _try_get_wiremap(client: LabNavigatorClient, device_id: int, *, required: bool) -> dict[str, Any] | None:
    try:
        return client.get_wiremap(device_id)
    except DiscoveryError:
        if required:
            raise
        return None


def _wiremap_device_label(ln_device: dict[str, Any], inventory_device: InventoryDevice | None = None) -> str:
    return str(ln_device.get("name") or (inventory_device.display_name if inventory_device else "") or ln_device.get("id"))


def _wiremap_remote_inventory_device(
    current_device: InventoryDevice,
    remote_device: dict[str, Any],
    devices: dict[str, dict[str, Any]],
    discovered_devices: dict[str, dict[str, Any]],
) -> InventoryDevice | None:
    remote_type = _infer_inventory_type(current_device.type, remote_device)
    if remote_type == "switch":
        existing_devices = {**devices, **discovered_devices}
        # Find any existing record for this LN device ID to reuse its inventory ID and metadata.
        existing = _find_existing_by_lab_navigator_id(existing_devices, remote_device["id"])
        if existing is None:
            existing = _existing_inventory_device(existing_devices, _safe_id(remote_device["name"]))
        return InventoryDevice.model_validate(
            _inventory_switch_device(
                remote_device["name"],
                _device_model(remote_device),
                remote_device.get("ip_address") or "",
                remote_device["id"],
                existing=existing,
            )
        )
    if remote_type == "hypervisor":
        existing_devices = {**devices, **discovered_devices}
        existing = _find_existing_by_lab_navigator_id(existing_devices, remote_device["id"])
        if existing is None:
            existing = _existing_inventory_device(existing_devices, _safe_id(remote_device["name"]))
        return InventoryDevice.model_validate(_inventory_hypervisor_device(remote_device, existing_id=existing.id if existing else None))
    if remote_type == "edge":
        existing_devices = {**devices, **discovered_devices}
        existing = _find_existing_by_lab_navigator_id(existing_devices, remote_device["id"])
        if existing is None:
            existing = _existing_inventory_device(existing_devices, _safe_id(remote_device["name"]))
        return _inventory_device_from_lab_navigator(remote_device, existing_devices)
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

    local_port_channel = canonical_port_channel(str(wiremap_entry.get("parent_interface") or ""))
    remote_port_channel = canonical_port_channel(str(wiremap_entry.get("remote_parent_interface") or ""))
    endpoints = _orient_wiremap_connection(
        local_device.type,
        local_device.id,
        local_interface,
        remote_device.type,
        remote_device.id,
        remote_interface,
        local_port_channel,
        remote_port_channel,
    )
    if endpoints is None:
        return None

    role, _left_type, _right_type, left_id, left_if, right_id, right_if, left_port_channel, right_port_channel = endpoints
    return {
        "id": _build_connection_id(left_id, left_if, right_id, right_if),
        "a": _wiremap_endpoint(left_id, left_if, left_port_channel),
        "b": _wiremap_endpoint(right_id, right_if, right_port_channel),
        "vlans": [],
        "tagged_vlans": [],
        "untagged_vlan": None,
        "role": role,
        "notes": "Imported from Lab Navigator wiremap.",
    }


def _wiremap_endpoint(device_id: str, interface: str, port_channel: str | None) -> dict[str, str]:
    endpoint = {"device_id": device_id, "interface": interface}
    if port_channel:
        endpoint["port_channel"] = port_channel
    return endpoint


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
    if not isinstance(remote_device, dict):
        remote_name = str(remote_device or "").strip()
        if not remote_name:
            return None
        matches = [item for item in client.search(remote_name) if item.get("name") == remote_name]
        if len(matches) != 1:
            return None
        remote_device = matches[0]

    # Wiremap peer summaries can omit both model fields even when inventory has them.
    if not _device_model(remote_device) and remote_device.get("id") is not None:
        try:
            full_device = client.get_inventory_device(remote_device["id"])
        except DiscoveryError as error:
            _log(
                logging.WARNING,
                "Could not fetch model for wiremap peer lab_navigator_id=%s: %s; preserving known device data",
                remote_device["id"],
                error,
            )
        else:
            remote_device = {
                **remote_device,
                **{key: value for key, value in full_device.items() if value not in (None, "")},
            }
    return remote_device


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
    resolved_model = (
        model
        or (existing.model if existing else None)
        or (existing_metadata.model if existing_metadata else "")
    )
    inferred_type, inferred_family = infer_switch_platform(resolved_model, name)
    if inferred_family == "eos":
        device_type = "ARISTA"
        os_family = "eos"
    elif existing_metadata:
        device_type = existing_metadata.device_type
        os_family = existing_metadata.os_family or inferred_family
    else:
        device_type = inferred_type
        os_family = inferred_family
    return InventoryDevice(
        id=existing.id if existing else _safe_id(name),  # reuse existing ID to avoid duplicates
        type="switch",
        display_name=name,
        model=resolved_model,
        ip_address=switch_ip,
        lab_navigator_id=lab_navigator_id,
        switch_metadata=SwitchMetadata(
            name=name,
            device_type=device_type,
            model=resolved_model,
            os_family=os_family,
            connections={
                "ip": switch_ip,
                "port": existing_metadata.connections.port if existing_metadata else None,
            },
            credentials=existing_metadata.credentials if existing_metadata else SwitchCredentials(),
        ),
    ).model_dump(mode="json")


def _inventory_hypervisor_device(device: dict[str, Any], *, existing_id: str | None = None) -> dict[str, Any]:
    return InventoryDevice(
        id=existing_id or _safe_id(device["name"]),  # reuse existing ID to avoid duplicates
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


def _merge_connection(connections: dict[str, dict[str, Any]], connection: dict[str, Any]) -> None:
    connections[connection["id"]] = {**connections.get(connection["id"], {}), **connection}


def _requested_edge_interface(connection: dict[str, Any], requested_edge_ids: set[str]) -> str | None:
    for endpoint_name in ("a", "b"):
        endpoint = connection.get(endpoint_name, {})
        if endpoint.get("device_id") in requested_edge_ids:
            interface_name = str(endpoint.get("interface") or "").strip().upper()
            return interface_name or None
    return None


def _existing_inventory_device(
    devices: dict[str, dict[str, Any]],
    device_id: str,
) -> InventoryDevice | None:
    existing = devices.get(device_id)
    if not existing:
        return None
    return InventoryDevice.model_validate(existing)


def _find_existing_by_lab_navigator_id(
    devices: dict[str, dict[str, Any]],
    lab_navigator_id: int,
) -> InventoryDevice | None:
    """Find an existing inventory device by its Lab Navigator ID, regardless of stored key."""
    for raw in devices.values():
        if raw.get("lab_navigator_id") == lab_navigator_id:
            return InventoryDevice.model_validate(raw)
    return None


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
        status=(
            "partial"
            if partial_targets or stats.preserved_connection_count or stats.skipped_conflicting_endpoint_count
            else "success"
        ),
        requested_hardware_count=len(hardware_ids),
        change_count=len(changes),
        discovered_connection_count=stats.discovered_connection_count,
        preserved_connection_count=stats.preserved_connection_count,
        skipped_conflicting_endpoint_count=stats.skipped_conflicting_endpoint_count,
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
    if summary.skipped_conflicting_endpoint_count:
        messages.append(
            ValidationMessage(
                level="warning",
                message="Preserved existing inventory links where Lab Navigator reported overlapping switch ports.",
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


def _connection_role(connection: InventoryConnection | dict[str, Any]) -> str:
    if isinstance(connection, InventoryConnection):
        return connection.role or ""
    return str(connection.get("role") or "")


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
    if any(token in model for token in ("dell-3048", "dell-4048", "dell-4148", "arista", "dcs-", "7050", "switch")):
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
    local_port_channel: str | None = None,
    remote_port_channel: str | None = None,
) -> tuple[str, str, str, str, str, str, str, str | None, str | None] | None:
    if {local_type, remote_type} == {"edge", "switch"}:
        if local_type == "edge":
            return ("edge-access", local_type, remote_type, local_id, local_interface, remote_id, remote_interface, local_port_channel, remote_port_channel)
        return ("edge-access", remote_type, local_type, remote_id, remote_interface, local_id, local_interface, remote_port_channel, local_port_channel)
    if local_type == "switch" and remote_type == "hypervisor":
        return ("hypervisor-access", local_type, remote_type, local_id, local_interface, remote_id, remote_interface, local_port_channel, remote_port_channel)
    if local_type == "hypervisor" and remote_type == "switch":
        return ("hypervisor-access", remote_type, local_type, remote_id, remote_interface, local_id, local_interface, remote_port_channel, local_port_channel)
    if local_type == "switch" and remote_type == "switch":
        left = (local_id, local_interface)
        right = (remote_id, remote_interface)
        if right < left:
            return ("switch-uplink", remote_type, local_type, remote_id, remote_interface, local_id, local_interface, remote_port_channel, local_port_channel)
        return ("switch-uplink", local_type, remote_type, local_id, local_interface, remote_id, remote_interface, local_port_channel, remote_port_channel)
    return None


def _normalize_interface_name(value: str, device_type: str) -> str:
    if device_type != "switch":
        return value
    text = (value or "").strip()
    port_channel = canonical_port_channel(text)
    if port_channel:
        return port_channel
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
