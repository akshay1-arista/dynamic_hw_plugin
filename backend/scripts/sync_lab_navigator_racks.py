from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.discovery import LabNavigatorClient
from app.inventory import load_inventory, save_inventory
from app.models import (
    InventoryConnection,
    InventoryDevice,
    SwitchConnection,
    SwitchCredentials,
    SwitchMetadata,
)


EDGE_MODEL_MAP = {
    "VeloCloud-3400": ("edge3X00", "3400"),
    "VeloCloud-3800": ("edge3X00", "3800"),
    "VeloCloud-3810": ("edge3X10", "3810"),
    "VeloCloud-510": ("edge510", "510"),
    "VeloCloud-510lte": ("edge510lte", "510"),
    "VeloCloud-520": ("edge5X0", "520"),
    "VeloCloud-540": ("edge5X0", "540"),
    "VeloCloud-610": ("edge6X0", "610"),
    "VeloCloud-620": ("edge6X0", "620"),
    "VeloCloud-640": ("edge6X0", "640"),
    "VeloCloud-680": ("edge6X0", "680"),
    "VeloCloud-710": ("edge710", "710"),
    "VeloCloud-720": ("edge7X0", "720"),
    "VeloCloud-740": ("edge7X0", "740"),
    "VeloCloud-840": ("edge840", "840"),
    "VeloCloud-4100": ("edge4100", "4100"),
    "VeloCloud-5100": ("edge5100", "5100"),
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync Lab Navigator rack devices and wiremap into hardware inventory.")
    parser.add_argument("--rack", action="append", dest="racks", required=True, help="Rack label to import, e.g. A01")
    parser.add_argument(
        "--inventory-path",
        default=str(BACKEND_ROOT / "data" / "hardware_inventory.json"),
        help="Inventory JSON path",
    )
    args = parser.parse_args()

    inventory_path = Path(args.inventory_path)
    inventory = load_inventory(inventory_path)
    racks = [rack.upper() for rack in args.racks]

    client = LabNavigatorClient()
    try:
        rack_set = set(racks)
        rack_devices: dict[int, dict] = {}
        for device in client.list_inventory([]):
            if (device.get("rack") or "").upper() not in rack_set:
                continue
            rack_devices[device["id"]] = device

        inventory_id_by_ln_id: dict[int, str] = {}
        for device in rack_devices.values():
            inventory_id = upsert_device(inventory, device)
            inventory_id_by_ln_id[device["id"]] = inventory_id

        connection_count = 0
        for ln_device in rack_devices.values():
            wiremap = client.get_wiremap(ln_device["id"])
            local_inventory_id = inventory_id_by_ln_id[ln_device["id"]]
            local_inventory_device = inventory.devices[local_inventory_id]
            for item in wiremap.get("connections", []):
                remote = item.get("remote_device")
                if not isinstance(remote, dict):
                    continue
                if remote.get("rack", "").upper() not in racks:
                    continue
                remote_type = map_device_type(remote.get("device_type", ""))
                if remote_type is None:
                    continue
                remote_inventory_id = inventory_id_by_ln_id.get(remote["id"])
                if not remote_inventory_id:
                    remote_inventory_id = upsert_device(inventory, remote)
                    inventory_id_by_ln_id[remote["id"]] = remote_inventory_id
                remote_inventory_device = inventory.devices[remote_inventory_id]

                connection = build_connection(
                    local_inventory_device,
                    local_inventory_id,
                    item.get("interface_name") or "",
                    remote_inventory_device,
                    remote_inventory_id,
                    item.get("remote_interface_name") or "",
                    item.get("local_vlans") or "",
                    item.get("remote_vlans") or "",
                    item.get("vlans") or "",
                )
                if connection is None:
                    continue
                upsert_connection(inventory, connection)
                connection_count += 1
    finally:
        client.close()

    save_inventory(inventory, inventory_path)
    print(
        f"Synced racks {', '.join(racks)} into {inventory_path}. "
        f"Imported/updated {len(rack_devices)} devices and processed {connection_count} wiremap links."
    )
    return 0


def upsert_device(inventory, device: dict) -> str:
    inventory_type = map_device_type(device.get("device_type", ""))
    if inventory_type is None:
        raise ValueError(f"Unsupported Lab Navigator device type: {device.get('device_type')}")

    existing_id = find_existing_device_id(inventory, device, inventory_type)
    model, model_suffix = normalize_model(device, inventory_type)
    note = f"Imported from Lab Navigator rack {device.get('rack', '')}, device id {device['id']}."

    if inventory_type == "switch":
        existing_switch = inventory.devices.get(existing_id) if existing_id else None
        existing_metadata = existing_switch.switch_metadata if existing_switch else None
        switch_metadata = SwitchMetadata(
            name=device["name"],
            device_type="DELL",
            model=model,
            connections=SwitchConnection(ip=device.get("ip_address") or "", port=None),
            credentials=existing_metadata.credentials if existing_metadata else SwitchCredentials(),
        )
    else:
        switch_metadata = None

    inventory_device = InventoryDevice(
        id=existing_id or build_inventory_id(device, inventory_type),
        type=inventory_type,
        display_name=device["name"],
        short_name=(inventory.devices[existing_id].short_name if existing_id else None),
        model=model,
        model_suffix=model_suffix,
        serial_number=device.get("serial_number") or None,
        ip_address=device.get("ip_address") or None,
        available=resolve_availability(device.get("availability")),
        ha_group_id=(inventory.devices[existing_id].ha_group_id if existing_id else None),
        ha_role=(inventory.devices[existing_id].ha_role if existing_id else None),
        dpdk_enabled=(inventory.devices[existing_id].dpdk_enabled if existing_id else None),
        free_vlans=list(inventory.devices[existing_id].free_vlans) if existing_id else [],
        vlan_range=(inventory.devices[existing_id].vlan_range if existing_id else None),
        switch_metadata=switch_metadata,
        lab_navigator_id=device["id"],
        hypervisor_ip=(inventory.devices[existing_id].hypervisor_ip if existing_id else None),
        notes=merge_notes(inventory.devices[existing_id].notes if existing_id else None, note),
    )
    inventory.devices[inventory_device.id] = inventory_device
    return inventory_device.id


def build_connection(
    local_device: InventoryDevice,
    local_device_id: str,
    local_interface: str,
    remote_device: InventoryDevice,
    remote_device_id: str,
    remote_interface: str,
    local_vlans: str,
    remote_vlans: str,
    raw_vlans: str,
) -> InventoryConnection | None:
    local_interface = normalize_interface_name(local_interface, local_device.type)
    remote_interface = normalize_interface_name(remote_interface, remote_device.type)
    endpoints = orient_connection(
        local_device.type,
        local_device_id,
        local_interface,
        remote_device.type,
        remote_device_id,
        remote_interface,
    )
    if endpoints is None:
        return None

    role, left_type, right_type, left_id, left_if, right_id, right_if = endpoints
    vlan_source = raw_vlans
    if left_type == "switch":
        vlan_source = local_vlans if left_id == local_device_id else remote_vlans
    elif right_type == "switch":
        vlan_source = remote_vlans if right_id == remote_device_id else local_vlans
    parsed = parse_vlans(vlan_source)

    return InventoryConnection(
        id=build_connection_id(left_id, left_if, right_id, right_if),
        a={"device_id": left_id, "interface": left_if},
        b={"device_id": right_id, "interface": right_if},
        vlans=parsed["vlans"],
        tagged_vlans=parsed["tagged_vlans"],
        untagged_vlan=parsed["untagged_vlan"],
        role=role,
        notes="Imported from Lab Navigator wiremap.",
    )


def orient_connection(
    local_type: str,
    local_id: str,
    local_interface: str,
    remote_type: str,
    remote_id: str,
    remote_interface: str,
):
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


def upsert_connection(inventory, connection: InventoryConnection) -> None:
    key = canonical_connection_key(connection.a.device_id, connection.a.interface, connection.b.device_id, connection.b.interface)
    for index, existing in enumerate(inventory.connections):
        existing_key = canonical_connection_key(
            existing.a.device_id,
            existing.a.interface,
            existing.b.device_id,
            existing.b.interface,
        )
        if existing_key == key:
            inventory.connections[index] = pick_richer_connection(existing, connection)
            return
    inventory.connections.append(connection)


def find_existing_device_id(inventory, device: dict, inventory_type: str) -> str | None:
    for existing in inventory.devices.values():
        if existing.lab_navigator_id == device["id"]:
            return existing.id
    serial = device.get("serial_number") or ""
    if serial:
        for existing in inventory.devices.values():
            if existing.serial_number == serial:
                return existing.id
    ip_address = device.get("ip_address") or ""
    if inventory_type == "switch" and ip_address:
        for existing in inventory.devices.values():
            if existing.type == "switch" and existing.ip_address == ip_address:
                return existing.id
    for existing in inventory.devices.values():
        if existing.display_name == device["name"]:
            return existing.id
    return None


def map_device_type(device_type: str) -> str | None:
    normalized = (device_type or "").lower()
    if normalized == "edge":
        return "edge"
    if normalized == "switch":
        return "switch"
    if normalized in {"server", "hypervisor"}:
        return "hypervisor"
    return None


def normalize_model(device: dict, inventory_type: str) -> tuple[str | None, str | None]:
    model = device.get("display_model") or device.get("device_model") or None
    if inventory_type != "edge" or not model:
        suffix = extract_model_suffix(model) if model else None
        return model, suffix
    return EDGE_MODEL_MAP.get(model, (model, extract_model_suffix(model)))


def resolve_availability(value: str | None) -> bool:
    if not value:
        return True
    return value.lower() == "available"


def parse_vlans(value: str) -> dict[str, object]:
    tagged: list[int] = []
    untagged: int | None = None
    for part in (value or "").split("|"):
        chunk = part.strip()
        if chunk.startswith("Tagged:"):
            tagged.extend(parse_vlan_numbers(chunk.removeprefix("Tagged:")))
        elif chunk.startswith("Untagged:"):
            numbers = parse_vlan_numbers(chunk.removeprefix("Untagged:"))
            if numbers:
                untagged = numbers[0]
    ordered_vlans = [untagged] if untagged is not None else []
    ordered_vlans.extend(vlan for vlan in tagged if vlan != untagged)
    return {
        "vlans": ordered_vlans,
        "tagged_vlans": [vlan for vlan in tagged if vlan != untagged],
        "untagged_vlan": untagged,
    }


def parse_vlan_numbers(value: str) -> list[int]:
    return [int(match) for match in re.findall(r"\d+", value)]


def normalize_interface_name(value: str, device_type: str) -> str:
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


def pick_richer_connection(existing: InventoryConnection, new: InventoryConnection) -> InventoryConnection:
    existing_score = (len(existing.vlans), len(existing.tagged_vlans), 1 if existing.untagged_vlan is not None else 0)
    new_score = (len(new.vlans), len(new.tagged_vlans), 1 if new.untagged_vlan is not None else 0)
    if new_score > existing_score:
        return new
    return existing


def merge_notes(existing: str | None, note: str) -> str:
    if not existing:
        return note
    if note in existing:
        return existing
    return f"{existing} {note}"


def extract_model_suffix(model: str | None) -> str | None:
    if not model:
        return None
    match = re.search(r"(\d+[a-z]*)", model.lower())
    return match.group(1) if match else None


def build_inventory_id(device: dict, inventory_type: str) -> str:
    rack = (device.get("rack") or "rack").lower()
    serial = device.get("serial_number") or device.get("name") or str(device["id"])
    return safe_id(f"ln-{rack}-{device['id']}-{inventory_type}-{serial}")


def build_connection_id(left_id: str, left_if: str, right_id: str, right_if: str) -> str:
    return safe_id(f"ln-{left_id}-{left_if}-{right_id}-{right_if}")


def canonical_connection_key(left_id: str, left_if: str, right_id: str, right_if: str) -> tuple[tuple[str, str], tuple[str, str]]:
    endpoints = sorted(((left_id, left_if), (right_id, right_if)))
    return endpoints[0], endpoints[1]


def safe_id(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


if __name__ == "__main__":
    raise SystemExit(main())
