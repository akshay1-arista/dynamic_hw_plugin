from pathlib import Path

import pytest

from app.config import INVENTORY_PATH
from app.generator import GenerationError, generate_topology
from app.inventory import build_inventory, load_inventory, save_inventory
from app.models import GenerateRequest


TARGET_HARDWARE_ID = "ln-ha-a01-327-dgd10q2-a01-328-16c10q2"
STANDALONE_PRIMARY_ID = "a01-680-standalone-a"
STANDALONE_SECONDARY_ID = "a01-680-standalone-b"


def _base_generate_request(**mapping_updates):
    mapping = {
        "hardware_id": TARGET_HARDWARE_ID,
        "branch_name": "branch2",
        "edge_name": "b2-edge1",
    }
    mapping.update(mapping_updates)
    return GenerateRequest.model_validate(
        {
            "topology_name": "generator-test",
            "reference_topology_id": "3-site",
            "hypervisor_ip": "10.68.136.50",
            "hypervisor_interface": "vmnic0",
            "requested_by": {"name": "Test User", "email": "test@example.com"},
            "mappings": [mapping],
        }
    )


def _available_inventory_copy(tmp_path):
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(Path(INVENTORY_PATH).read_text())
    inventory = load_inventory(inventory_path)
    hardware = next(item for item in inventory.hardware if item.id == TARGET_HARDWARE_ID)
    hardware.available = True
    hardware.reservation = None
    save_inventory(inventory, inventory_path)
    return inventory_path, load_inventory(inventory_path), hardware


def _minimal_unwired_inventory(tmp_path):
    _inventory_path, base_inventory, hardware = _available_inventory_copy(tmp_path)
    member_devices = {
        device_id: device.model_dump(mode="json")
        for device_id, device in base_inventory.devices.items()
        if device.type == "edge" and (device.ha_group_id or device.id) == TARGET_HARDWARE_ID
    }
    minimal_inventory = build_inventory(member_devices, [])
    inventory_path = tmp_path / "minimal-inventory.json"
    save_inventory(minimal_inventory, inventory_path)
    return inventory_path, hardware


def _standalone_ha_candidate_inventory(tmp_path):
    raw_devices = {
        STANDALONE_PRIMARY_ID: {
            "id": STANDALONE_PRIMARY_ID,
            "type": "edge",
            "display_name": "A01 680 Standalone A",
            "short_name": "a01-680-a",
            "model": "edge6X0",
            "model_suffix": "680",
            "serial_number": "STANDALONE-A",
            "available": True,
            "hypervisor_ip": "10.68.136.50",
        },
        STANDALONE_SECONDARY_ID: {
            "id": STANDALONE_SECONDARY_ID,
            "type": "edge",
            "display_name": "A01 680 Standalone B",
            "short_name": "a01-680-b",
            "model": "edge6X0",
            "model_suffix": "680",
            "serial_number": "STANDALONE-B",
            "available": True,
            "hypervisor_ip": "10.68.136.50",
        },
        "switch-a01": {
            "id": "switch-a01",
            "type": "switch",
            "display_name": "a01-access-switch",
            "model": "Dell-3048",
            "ip_address": "10.68.136.70",
            "available": True,
        },
        "hypervisor-1": {
            "id": "hypervisor-1",
            "type": "hypervisor",
            "display_name": "chn-rnd-srv-640-298VF33",
            "model": "Dell-R640",
            "serial_number": "298VF33",
            "ip_address": "10.68.136.50",
            "available": True,
        },
    }
    raw_connections = [
        {
            "id": "edge-a-ge1",
            "a": {"device_id": STANDALONE_PRIMARY_ID, "interface": "GE1"},
            "b": {"device_id": "switch-a01", "interface": "gigabitethernet1/11"},
            "role": "edge-access",
            "vlans": [1510],
            "tagged_vlans": [],
            "untagged_vlan": 1510,
        },
        {
            "id": "edge-a-ge2",
            "a": {"device_id": STANDALONE_PRIMARY_ID, "interface": "GE2"},
            "b": {"device_id": "switch-a01", "interface": "gigabitethernet1/12"},
            "role": "edge-access",
            "vlans": [1511, 1512],
            "tagged_vlans": [1512],
            "untagged_vlan": 1511,
        },
        {
            "id": "edge-b-ge1",
            "a": {"device_id": STANDALONE_SECONDARY_ID, "interface": "GE1"},
            "b": {"device_id": "switch-a01", "interface": "gigabitethernet1/21"},
            "role": "edge-access",
            "vlans": [1510],
            "tagged_vlans": [],
            "untagged_vlan": 1510,
        },
        {
            "id": "edge-b-ge2",
            "a": {"device_id": STANDALONE_SECONDARY_ID, "interface": "GE2"},
            "b": {"device_id": "switch-a01", "interface": "gigabitethernet1/22"},
            "role": "edge-access",
            "vlans": [1511, 1512],
            "tagged_vlans": [1512],
            "untagged_vlan": 1511,
        },
        {
            "id": "switch-to-hypervisor",
            "a": {"device_id": "switch-a01", "interface": "tengigabitethernet1/49"},
            "b": {"device_id": "hypervisor-1", "interface": "vmnic0"},
            "role": "hypervisor-access",
            "vlans": [1],
            "tagged_vlans": [],
            "untagged_vlan": 1,
        },
    ]
    inventory = build_inventory(
        raw_devices,
        raw_connections,
        managed_hardware_ids={STANDALONE_PRIMARY_ID, STANDALONE_SECONDARY_ID},
    )
    inventory_path = tmp_path / "standalone-ha-inventory.json"
    save_inventory(inventory, inventory_path)
    return inventory_path


def test_generate_topology_runs_one_pre_sync_for_selected_hardware(tmp_path, monkeypatch):
    inventory_path, _inventory, _hardware = _available_inventory_copy(tmp_path)
    outputs_root = tmp_path / "outputs"
    outputs_root.mkdir()
    sync_calls: list[list[str]] = []

    def fake_sync(hardware_ids, *, inventory_path=inventory_path, client=None):
        sync_calls.append(list(hardware_ids))
        return load_inventory(inventory_path), None

    monkeypatch.setattr("app.generator.sync_inventory_for_generate", fake_sync)

    result = generate_topology(
        _base_generate_request(),
        inventory_path=inventory_path,
        outputs_root=outputs_root,
    )

    assert sync_calls == [[TARGET_HARDWARE_ID]]
    assert Path(result.zip_path).exists()


def test_generate_topology_uses_saved_snapshot_when_synced_inventory_is_still_unusable(tmp_path, monkeypatch):
    inventory_path, hardware_snapshot = _minimal_unwired_inventory(tmp_path)
    outputs_root = tmp_path / "outputs"
    outputs_root.mkdir()

    monkeypatch.setattr(
        "app.generator.sync_inventory_for_generate",
        lambda hardware_ids, *, inventory_path=inventory_path, client=None: (load_inventory(inventory_path), None),
    )

    result = generate_topology(
        _base_generate_request(saved_hardware=hardware_snapshot.model_dump(mode="json")),
        inventory_path=inventory_path,
        outputs_root=outputs_root,
    )

    assert any("Using saved hardware snapshot" in message.message for message in result.messages)


def test_generate_topology_fails_when_synced_inventory_is_unusable_and_no_snapshot_exists(tmp_path, monkeypatch):
    inventory_path, _hardware_snapshot = _minimal_unwired_inventory(tmp_path)
    outputs_root = tmp_path / "outputs"
    outputs_root.mkdir()

    monkeypatch.setattr(
        "app.generator.sync_inventory_for_generate",
        lambda hardware_ids, *, inventory_path=inventory_path, client=None: (load_inventory(inventory_path), None),
    )

    with pytest.raises(GenerationError, match="did not produce usable switch connection data"):
        generate_topology(
            _base_generate_request(),
            inventory_path=inventory_path,
            outputs_root=outputs_root,
        )


def test_generate_topology_supports_ha_mode_from_two_standalones(tmp_path, monkeypatch):
    inventory_path = _standalone_ha_candidate_inventory(tmp_path)
    outputs_root = tmp_path / "outputs"
    outputs_root.mkdir()
    sync_calls: list[list[str]] = []

    def fake_sync(hardware_ids, *, inventory_path=inventory_path, client=None):
        sync_calls.append(list(hardware_ids))
        return load_inventory(inventory_path), None

    monkeypatch.setattr("app.generator.sync_inventory_for_generate", fake_sync)

    result = generate_topology(
        _base_generate_request(
            hardware_id=STANDALONE_PRIMARY_ID,
            secondary_hardware_id=STANDALONE_SECONDARY_ID,
            edge_ha_mode="ha",
        ),
        inventory_path=inventory_path,
        outputs_root=outputs_root,
    )

    assert sync_calls == [[STANDALONE_PRIMARY_ID, STANDALONE_SECONDARY_ID]]
    assert Path(result.zip_path).exists()
    assert result.mapping_statuses[0].path_resolved is True
    reserved_inventory = load_inventory(inventory_path)
    reserved_hardware = {item.id: item for item in reserved_inventory.hardware}
    assert reserved_hardware[STANDALONE_PRIMARY_ID].available is False
    assert reserved_hardware[STANDALONE_SECONDARY_ID].available is False


def test_generate_topology_requires_secondary_standalone_for_ha_mode(tmp_path, monkeypatch):
    inventory_path = _standalone_ha_candidate_inventory(tmp_path)
    outputs_root = tmp_path / "outputs"
    outputs_root.mkdir()

    monkeypatch.setattr(
        "app.generator.sync_inventory_for_generate",
        lambda hardware_ids, *, inventory_path=inventory_path, client=None: (load_inventory(inventory_path), None),
    )

    with pytest.raises(GenerationError, match="additional standalone device"):
        generate_topology(
            _base_generate_request(
                hardware_id=STANDALONE_PRIMARY_ID,
                edge_ha_mode="ha",
            ),
            inventory_path=inventory_path,
            outputs_root=outputs_root,
        )
