from pathlib import Path
import json

import pytest

from app.generator import GenerationError
from app.inventory import build_inventory, load_inventory, save_inventory
from app.models import HardwareReservation, SwitchConfigOnlyRequest, SwitchConfigureRequest
from app.switch_config import configure_switches_for_run
from app.switch_config_only import create_switch_config_run


STANDALONE_PRIMARY_ID = "a01-680-standalone-a"
STANDALONE_SECONDARY_ID = "a01-680-standalone-b"


def _switch_config_inventory(tmp_path, *, ha_pair=False):
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
            "vlan_range": {"start": 200, "end": 210},
            "free_vlans": [200, 201, 202],
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
            "vlan_range": {"start": 200, "end": 210},
            "free_vlans": [200, 201, 202],
            "hypervisor_ip": "10.68.136.50",
        },
        "switch-a01": {
            "id": "switch-a01",
            "type": "switch",
            "display_name": "a01-access-switch",
            "model": "Dell-3048",
            "ip_address": "10.68.136.70",
            "available": True,
            "switch_metadata": {
                "name": "a01-access-switch",
                "model": "Dell-3048",
                "os_family": "os9",
                "connections": {"ip": "10.68.136.70"},
                "credentials": {"username": "velo", "password": "secret"},
            },
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
    inventory = build_inventory(raw_devices, raw_connections)
    inventory_path = tmp_path / "switch-config-inventory.json"
    save_inventory(inventory, inventory_path)
    return inventory_path


def _two_switch_standalone_inventory(tmp_path):
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
            "vlan_range": {"start": 200, "end": 210},
            "free_vlans": [200, 201, 202],
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
            "vlan_range": {"start": 200, "end": 210},
            "free_vlans": [200, 201, 202],
            "hypervisor_ip": "10.68.136.50",
        },
        "switch-a": {
            "id": "switch-a",
            "type": "switch",
            "display_name": "a01-access-switch-a",
            "model": "Dell-3048",
            "ip_address": "10.68.136.70",
            "available": True,
            "switch_metadata": {
                "name": "a01-access-switch-a",
                "model": "Dell-3048",
                "os_family": "os9",
                "connections": {"ip": "10.68.136.70"},
                "credentials": {"username": "velo", "password": "secret"},
            },
        },
        "switch-b": {
            "id": "switch-b",
            "type": "switch",
            "display_name": "a01-access-switch-b",
            "model": "Dell-3048",
            "ip_address": "10.68.136.71",
            "available": True,
            "switch_metadata": {
                "name": "a01-access-switch-b",
                "model": "Dell-3048",
                "os_family": "os9",
                "connections": {"ip": "10.68.136.71"},
                "credentials": {"username": "velo", "password": "secret"},
            },
        },
        "upstream-sw": {
            "id": "upstream-sw",
            "type": "switch",
            "display_name": "a01-upstream-switch",
            "model": "Dell-4048",
            "ip_address": "10.68.136.72",
            "available": True,
            "switch_metadata": {
                "name": "a01-upstream-switch",
                "model": "Dell-4048",
                "os_family": "os9",
                "connections": {"ip": "10.68.136.72"},
                "credentials": {"username": "velo", "password": "secret"},
            },
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
            "b": {"device_id": "switch-a", "interface": "gigabitethernet1/11"},
            "role": "edge-access",
            "vlans": [1510],
            "tagged_vlans": [],
            "untagged_vlan": 1510,
        },
        {
            "id": "edge-a-ge2",
            "a": {"device_id": STANDALONE_PRIMARY_ID, "interface": "GE2"},
            "b": {"device_id": "switch-a", "interface": "gigabitethernet1/12"},
            "role": "edge-access",
            "vlans": [1511, 1512],
            "tagged_vlans": [1512],
            "untagged_vlan": 1511,
        },
        {
            "id": "edge-b-ge1",
            "a": {"device_id": STANDALONE_SECONDARY_ID, "interface": "GE1"},
            "b": {"device_id": "switch-b", "interface": "gigabitethernet1/21"},
            "role": "edge-access",
            "vlans": [1510],
            "tagged_vlans": [],
            "untagged_vlan": 1510,
        },
        {
            "id": "edge-b-ge2",
            "a": {"device_id": STANDALONE_SECONDARY_ID, "interface": "GE2"},
            "b": {"device_id": "switch-b", "interface": "gigabitethernet1/22"},
            "role": "edge-access",
            "vlans": [1511, 1512],
            "tagged_vlans": [1512],
            "untagged_vlan": 1511,
        },
        {
            "id": "switch-a-to-upstream",
            "a": {"device_id": "switch-a", "interface": "tengigabitethernet1/51"},
            "b": {"device_id": "upstream-sw", "interface": "tengigabitethernet1/43"},
            "role": "switch-uplink",
            "vlans": [1, 200, 201, 202],
            "tagged_vlans": [200, 201, 202],
            "untagged_vlan": 1,
        },
        {
            "id": "switch-b-to-upstream",
            "a": {"device_id": "switch-b", "interface": "tengigabitethernet1/51"},
            "b": {"device_id": "upstream-sw", "interface": "tengigabitethernet1/44"},
            "role": "switch-uplink",
            "vlans": [1, 200, 201, 202],
            "tagged_vlans": [200, 201, 202],
            "untagged_vlan": 1,
        },
        {
            "id": "upstream-to-hypervisor",
            "a": {"device_id": "upstream-sw", "interface": "tengigabitethernet1/9"},
            "b": {"device_id": "hypervisor-1", "interface": "vmnic0"},
            "role": "hypervisor-access",
            "vlans": [1],
            "tagged_vlans": [],
            "untagged_vlan": 1,
        },
    ]
    inventory = build_inventory(raw_devices, raw_connections)
    inventory_path = tmp_path / "two-switch-standalone.json"
    save_inventory(inventory, inventory_path)
    return inventory_path


def _base_request(**mapping_updates):
    mapping = {
        "hardware_id": STANDALONE_PRIMARY_ID,
        "edge_ha_mode": "single_active",
        "interfaces": [
            {"hardware_interface": "GE1", "untagged_vlan": 200},
            {"hardware_interface": "GE2", "untagged_vlan": 201, "tagged_vlans": [202]},
        ],
    }
    mapping.update(mapping_updates)
    return SwitchConfigOnlyRequest.model_validate(
        {
            "hypervisor_ip": "10.68.136.50",
            "hypervisor_interface": "vmnic0",
            "requested_by": {"name": "Test User", "email": "test@example.com"},
            "mappings": [mapping],
        }
    )


def test_switch_config_only_creates_metadata_without_topology_files(tmp_path, monkeypatch):
    inventory_path = _switch_config_inventory(tmp_path)
    outputs_root = tmp_path / "outputs"
    outputs_root.mkdir()
    monkeypatch.setattr(
        "app.switch_config_only.sync_inventory_for_generate",
        lambda hardware_ids, *, inventory_path=inventory_path, client=None: (load_inventory(inventory_path), None),
    )

    result = create_switch_config_run(
        _base_request(),
        inventory_path=inventory_path,
        outputs_root=outputs_root,
    )

    assert result.switch_config_only is True
    assert result.can_configure_switches is True
    assert result.zip_path == ""
    assert result.download_url == ""
    assert not list(Path(result.topology_path).glob("*.zip"))
    assert not list(Path(result.topology_path).glob("**/config.json"))
    assert result.mapping_statuses[0].path_resolved is True
    allocation = result.mapping_statuses[0]
    assert allocation.auto_config_ready is True
    assert any("Recommended VLAN range" in message.message for message in result.messages)

    metadata_path = Path(result.topology_path) / "run_metadata.json"
    assert metadata_path.exists()
    reserved = load_inventory(inventory_path)
    hardware = next(item for item in reserved.hardware if item.id == STANDALONE_PRIMARY_ID)
    assert hardware.available is False
    assert hardware.reservation.reason == "switch-config"


def test_switch_config_only_requires_vlans(tmp_path, monkeypatch):
    inventory_path = _switch_config_inventory(tmp_path)
    outputs_root = tmp_path / "outputs"
    outputs_root.mkdir()
    monkeypatch.setattr(
        "app.switch_config_only.sync_inventory_for_generate",
        lambda hardware_ids, *, inventory_path=inventory_path, client=None: (load_inventory(inventory_path), None),
    )

    with pytest.raises(GenerationError, match="Select at least one untagged or tagged VLAN"):
        create_switch_config_run(
            _base_request(interfaces=[]),
            inventory_path=inventory_path,
            outputs_root=outputs_root,
        )


def test_switch_config_only_supports_ha_from_two_standalones(tmp_path, monkeypatch):
    inventory_path = _switch_config_inventory(tmp_path, ha_pair=True)
    outputs_root = tmp_path / "outputs"
    outputs_root.mkdir()
    monkeypatch.setattr(
        "app.switch_config_only.sync_inventory_for_generate",
        lambda hardware_ids, *, inventory_path=inventory_path, client=None: (load_inventory(inventory_path), None),
    )

    result = create_switch_config_run(
        _base_request(
            secondary_hardware_id=STANDALONE_SECONDARY_ID,
            edge_ha_mode="ha",
        ),
        inventory_path=inventory_path,
        outputs_root=outputs_root,
    )

    assert result.can_configure_switches is True
    assert result.mapping_statuses[0].path_resolved is True


def test_switch_config_only_run_can_preview_switch_commands(tmp_path, monkeypatch):
    inventory_path = _switch_config_inventory(tmp_path)
    outputs_root = tmp_path / "outputs"
    outputs_root.mkdir()
    monkeypatch.setattr(
        "app.switch_config_only.sync_inventory_for_generate",
        lambda hardware_ids, *, inventory_path=inventory_path, client=None: (load_inventory(inventory_path), None),
    )
    result = create_switch_config_run(
        _base_request(),
        inventory_path=inventory_path,
        outputs_root=outputs_root,
    )
    monkeypatch.setattr("app.switch_config.load_inventory", lambda _path: load_inventory(inventory_path))
    monkeypatch.setattr("app.switch_config._fetch_running_config", lambda _device: "")

    preview = configure_switches_for_run(
        result.run_id,
        SwitchConfigureRequest(dry_run=True),
        inventory_path=inventory_path,
        outputs_root=outputs_root,
    )

    assert preview.applied is False
    assert preview.devices
    command_text = "\n".join(command for device in preview.devices for command in device.commands)
    assert "gigabitethernet 1/11" in command_text.lower()
    assert "200" in command_text


def test_switch_config_only_ha_rejects_secondary_reserved_by_another_user(tmp_path, monkeypatch):
    inventory_path = _switch_config_inventory(tmp_path)
    outputs_root = tmp_path / "outputs"
    outputs_root.mkdir()
    inventory = load_inventory(inventory_path)
    other = HardwareReservation(
        actor={"name": "Other User", "email": "other@example.com"},
        reserved_at="2026-01-01T00:00:00Z",
        reason="switch-config",
    )
    secondary = next(item for item in inventory.hardware if item.id == STANDALONE_SECONDARY_ID)
    secondary.available = False
    secondary.reservation = other
    for member in secondary.members:
        member.available = False
        member.reservation = other
    inventory.devices[STANDALONE_SECONDARY_ID].available = False
    inventory.devices[STANDALONE_SECONDARY_ID].reservation = other
    monkeypatch.setattr(
        "app.switch_config_only.sync_inventory_for_generate",
        lambda hardware_ids, *, inventory_path=inventory_path, client=None: (inventory, None),
    )
    monkeypatch.setattr("app.switch_config_only.load_inventory", lambda _path: inventory)

    with pytest.raises(GenerationError, match="Reserved by Other User"):
        create_switch_config_run(
            _base_request(
                secondary_hardware_id=STANDALONE_SECONDARY_ID,
                edge_ha_mode="ha",
            ),
            inventory_path=inventory_path,
            outputs_root=outputs_root,
        )


def test_switch_config_only_ha_from_standalones_keeps_standby_switch(tmp_path, monkeypatch):
    inventory_path = _two_switch_standalone_inventory(tmp_path)
    outputs_root = tmp_path / "outputs"
    outputs_root.mkdir()
    monkeypatch.setattr(
        "app.switch_config_only.sync_inventory_for_generate",
        lambda hardware_ids, *, inventory_path=inventory_path, client=None: (load_inventory(inventory_path), None),
    )
    result = create_switch_config_run(
        _base_request(
            secondary_hardware_id=STANDALONE_SECONDARY_ID,
            edge_ha_mode="ha",
        ),
        inventory_path=inventory_path,
        outputs_root=outputs_root,
    )
    metadata = json.loads((Path(result.topology_path) / "run_metadata.json").read_text())
    allocations = metadata["mappings"][0]["allocations"]
    ge1 = next(port for port in allocations if port["logical_interface"] == "GE1")
    assert ge1["switch_name"] == "a01-access-switch-a"
    assert ge1["switch_standby_name"] == "a01-access-switch-b"
    assert ge1["switch_active_port"] == "gigabitethernet1/11"
    assert ge1["switch_standby_port"] == "gigabitethernet1/21"

    monkeypatch.setattr("app.switch_config.load_inventory", lambda _path: load_inventory(inventory_path))
    monkeypatch.setattr("app.switch_config._fetch_running_config", lambda _device: "")
    preview = configure_switches_for_run(
        result.run_id,
        SwitchConfigureRequest(dry_run=True),
        inventory_path=inventory_path,
        outputs_root=outputs_root,
    )
    primary_commands = next(item.commands for item in preview.devices if item.device_id == "switch-a")
    secondary_commands = next(item.commands for item in preview.devices if item.device_id == "switch-b")
    assert "interface GigabitEthernet 1/11" in primary_commands
    assert "interface GigabitEthernet 1/21" not in primary_commands
    assert "interface GigabitEthernet 1/21" in secondary_commands
    assert "interface GigabitEthernet 1/11" not in secondary_commands
