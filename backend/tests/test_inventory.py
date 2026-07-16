import json

from app.inventory import build_inventory, load_inventory, reserve_generated_hardware, save_inventory, update_hardware_availability


def test_build_inventory_sanitizes_conflicting_remote_interface_connections():
    inventory = build_inventory(
        {
            "edge-1": {
                "id": "edge-1",
                "type": "edge",
                "display_name": "Edge 1",
                "model": "edge6X0",
                "model_suffix": "680",
                "serial_number": "SERIAL1",
            },
            "switch-1": {
                "id": "switch-1",
                "type": "switch",
                "display_name": "switch1",
                "model": "Dell-3048",
                "ip_address": "10.0.0.1",
                "switch_metadata": {
                    "name": "switch1",
                    "model": "Dell-3048",
                    "connections": {"ip": "10.0.0.1", "port": None},
                    "credentials": {"username": "velocloud", "password": "N#1sdwan"},
                },
            },
        },
        [
            {
                "id": "manual-ge4",
                "a": {"device_id": "edge-1", "interface": "GE4"},
                "b": {"device_id": "switch-1", "interface": "gigabitethernet1/3"},
                "vlans": [105],
                "tagged_vlans": [],
                "untagged_vlan": 105,
                "role": None,
                "notes": None,
            },
            {
                "id": "manual-ge5",
                "a": {"device_id": "edge-1", "interface": "GE5"},
                "b": {"device_id": "switch-1", "interface": "gigabitethernet1/30"},
                "vlans": [],
                "tagged_vlans": [],
                "untagged_vlan": None,
                "role": None,
                "notes": "Manually updated from supplied connection table; VLAN metadata was empty.",
            },
            {
                "id": "wiremap-ge5",
                "a": {"device_id": "edge-1", "interface": "GE5"},
                "b": {"device_id": "switch-1", "interface": "gigabitethernet1/3"},
                "vlans": [105],
                "tagged_vlans": [],
                "untagged_vlan": 105,
                "role": "edge-access",
                "notes": "Imported from Lab Navigator wiremap.",
            },
        ],
    )

    switch_port_connections = [
        connection
        for connection in inventory.connections
        if {connection.a.device_id, connection.b.device_id} == {"edge-1", "switch-1"}
        and (
            connection.a.interface == "gigabitethernet1/3"
            or connection.b.interface == "gigabitethernet1/3"
        )
    ]

    assert len(switch_port_connections) == 1
    chosen = switch_port_connections[0]
    assert {chosen.a.interface, chosen.b.interface} == {"GE5", "gigabitethernet1/3"}

    ports = {port.logical_interface: port.switch_active_port for port in inventory.hardware[0].ports}
    assert ports["GE5"] == "gigabitethernet1/3"
    assert "GE4" not in ports


def test_build_inventory_falls_back_to_vlan_matching_for_shifted_ha_ports():
    inventory = build_inventory(
        {
            "edge-active": {
                "id": "edge-active",
                "type": "edge",
                "display_name": "Edge Active",
                "model": "edge3X00",
                "model_suffix": "3800",
                "serial_number": "ACTIVE1",
                "ha_group_id": "edge-ha",
                "ha_role": "active",
            },
            "edge-standby": {
                "id": "edge-standby",
                "type": "edge",
                "display_name": "Edge Standby",
                "model": "edge3X00",
                "model_suffix": "3800",
                "serial_number": "STANDBY1",
                "ha_group_id": "edge-ha",
                "ha_role": "standby",
            },
            "switch-1": {
                "id": "switch-1",
                "type": "switch",
                "display_name": "switch1",
                "model": "Dell-3048",
                "ip_address": "10.0.0.1",
            },
        },
        [
            {
                "id": "active-ge1",
                "a": {"device_id": "edge-active", "interface": "GE1"},
                "b": {"device_id": "switch-1", "interface": "Gi1/16"},
                "vlans": [1],
                "tagged_vlans": [],
                "untagged_vlan": 1,
                "role": "edge-access",
            },
            {
                "id": "active-ge2",
                "a": {"device_id": "edge-active", "interface": "GE2"},
                "b": {"device_id": "switch-1", "interface": "Gi1/17"},
                "vlans": [202, 203, 204],
                "tagged_vlans": [203, 204],
                "untagged_vlan": 202,
                "role": "edge-access",
            },
            {
                "id": "active-ge3",
                "a": {"device_id": "edge-active", "interface": "GE3"},
                "b": {"device_id": "switch-1", "interface": "Gi1/18"},
                "vlans": [205],
                "tagged_vlans": [],
                "untagged_vlan": 205,
                "role": "edge-access",
            },
            {
                "id": "standby-ge1",
                "a": {"device_id": "edge-standby", "interface": "GE1"},
                "b": {"device_id": "switch-1", "interface": "Gi1/12"},
                "vlans": [202, 203, 204],
                "tagged_vlans": [203, 204],
                "untagged_vlan": 202,
                "role": "edge-access",
            },
            {
                "id": "standby-ge2",
                "a": {"device_id": "edge-standby", "interface": "GE2"},
                "b": {"device_id": "switch-1", "interface": "Gi1/13"},
                "vlans": [205],
                "tagged_vlans": [],
                "untagged_vlan": 205,
                "role": "edge-access",
            },
        ],
    )

    hardware = inventory.hardware[0]
    ports = {port.logical_interface: port for port in hardware.ports}

    assert ports["GE1"].switch_standby_port is None
    assert ports["GE2"].switch_standby_port == "Gi1/12"
    assert ports["GE3"].switch_standby_port == "Gi1/13"


def test_build_inventory_keeps_asymmetric_ha_ports_for_manual_mapping():
    inventory = build_inventory(
        {
            "edge-active": {
                "id": "edge-active",
                "type": "edge",
                "display_name": "Edge Active",
                "model": "edge3X00",
                "model_suffix": "3800",
                "serial_number": "ACTIVE1",
                "ha_group_id": "edge-ha",
                "ha_role": "active",
            },
            "edge-standby": {
                "id": "edge-standby",
                "type": "edge",
                "display_name": "Edge Standby",
                "model": "edge3X00",
                "model_suffix": "3800",
                "serial_number": "STANDBY1",
                "ha_group_id": "edge-ha",
                "ha_role": "standby",
            },
            "switch-1": {
                "id": "switch-1",
                "type": "switch",
                "display_name": "switch1",
                "model": "Dell-3048",
                "ip_address": "10.0.0.1",
            },
        },
        [
            {
                "id": "active-ge1",
                "a": {"device_id": "edge-active", "interface": "GE1"},
                "b": {"device_id": "switch-1", "interface": "Gi1/16"},
                "vlans": [101],
                "tagged_vlans": [],
                "untagged_vlan": 101,
                "role": "edge-access",
            },
            {
                "id": "standby-ge2",
                "a": {"device_id": "edge-standby", "interface": "GE2"},
                "b": {"device_id": "switch-1", "interface": "Gi1/17"},
                "vlans": [102],
                "tagged_vlans": [],
                "untagged_vlan": 102,
                "role": "edge-access",
            },
        ],
    )

    hardware = inventory.hardware[0]
    ports = {port.logical_interface: port for port in hardware.ports}

    assert ports["GE1"].switch_active_port == "Gi1/16"
    assert ports["GE1"].switch_standby_port is None
    assert ports["GE1"].manual_mapping_required is True
    assert "active-member switch connection" in ports["GE1"].port_warning

    assert ports["GE2"].switch_active_port is None
    assert ports["GE2"].switch_standby_port == "Gi1/17"
    assert ports["GE2"].manual_mapping_required is True
    assert "standby-member switch connection" in ports["GE2"].port_warning


def test_reserve_generated_hardware_and_release_round_trip(tmp_path):
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(
        """
{
  "devices": {
    "edge-1": {
      "id": "edge-1",
      "type": "edge",
      "display_name": "Edge 1",
      "model": "edge6X0",
      "model_suffix": "680",
      "serial_number": "SERIAL1",
      "ha_group_id": "edge-1",
      "ha_role": "active",
      "available": true
    },
    "switch-1": {
      "id": "switch-1",
      "type": "switch",
      "display_name": "Switch 1",
      "model": "Dell-3048",
      "ip_address": "10.0.0.1"
    }
  },
  "connections": [
    {
      "id": "edge-1-ge1-switch-1",
      "a": {"device_id": "edge-1", "interface": "GE1"},
      "b": {"device_id": "switch-1", "interface": "Gi1/1"},
      "role": "edge-access"
    }
  ],
  "allocations": []
}
""".strip()
    )

    reserved_inventory, reserve_events = reserve_generated_hardware(
        ["edge-1"],
        {"name": "Test User", "email": "test@example.com"},
        "run123",
        "topology-123",
        inventory_path,
    )

    assert reserved_inventory.hardware[0].available is False
    assert reserved_inventory.hardware[0].reservation.actor.email == "test@example.com"
    assert reserve_events[0].action == "hardware_reserved"

    released_inventory, release_events = update_hardware_availability(
        "edge-1",
        True,
        {"name": "Test User", "email": "test@example.com"},
        inventory_path,
    )

    assert released_inventory.hardware[0].available is True
    assert released_inventory.hardware[0].reservation is None
    assert release_events[0].action == "hardware_released"

    state_path = tmp_path / "inventory.local.json"
    assert not state_path.exists()


def test_save_inventory_moves_local_state_to_sidecar(tmp_path):
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(
        """
{
  "devices": {
    "edge-1": {
      "id": "edge-1",
      "type": "edge",
      "display_name": "Edge 1",
      "model": "edge6X0",
      "model_suffix": "680",
      "serial_number": "SERIAL1",
      "ha_group_id": "edge-1",
      "ha_role": "active",
      "available": false,
      "reservation": {
        "actor": {
          "name": "Test User",
          "email": "test@example.com"
        },
        "reserved_at": "2026-07-16T00:00:00+00:00",
        "reason": "topology-generation",
        "run_id": "run123",
        "topology_name": "demo-topology"
      }
    },
    "switch-1": {
      "id": "switch-1",
      "type": "switch",
      "display_name": "Switch 1",
      "model": "Dell-3048",
      "ip_address": "10.0.0.1"
    }
  },
  "connections": [
    {
      "id": "edge-1-ge1-switch-1",
      "a": {"device_id": "edge-1", "interface": "GE1"},
      "b": {"device_id": "switch-1", "interface": "Gi1/1"},
      "role": "edge-access"
    }
  ],
  "allocations": []
}
""".strip()
    )

    inventory = load_inventory(inventory_path)
    saved = save_inventory(inventory, inventory_path)

    persisted = json.loads(inventory_path.read_text())
    edge = persisted["devices"]["edge-1"]
    assert "available" not in edge
    assert "reservation" not in edge

    state_path = tmp_path / "inventory.local.json"
    state = json.loads(state_path.read_text())
    assert state["hardware"]["edge-1"]["available"] is False
    assert state["hardware"]["edge-1"]["reservation"]["run_id"] == "run123"

    assert saved.hardware[0].available is False
    assert saved.hardware[0].reservation is not None


def test_save_inventory_can_preserve_existing_local_state(tmp_path):
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(
        """
{
  "devices": {
    "edge-1": {
      "id": "edge-1",
      "type": "edge",
      "display_name": "Edge 1",
      "model": "edge6X0",
      "model_suffix": "680",
      "serial_number": "SERIAL1",
      "ha_group_id": "edge-1",
      "ha_role": "active"
    },
    "switch-1": {
      "id": "switch-1",
      "type": "switch",
      "display_name": "Switch 1",
      "model": "Dell-3048",
      "ip_address": "10.0.0.1"
    }
  },
  "connections": [
    {
      "id": "edge-1-ge1-switch-1",
      "a": {"device_id": "edge-1", "interface": "GE1"},
      "b": {"device_id": "switch-1", "interface": "Gi1/1"},
      "role": "edge-access"
    }
  ],
  "allocations": []
}
""".strip()
    )

    inventory = load_inventory(inventory_path)
    inventory.hardware[0].available = False
    inventory.hardware[0].reservation = {
        "actor": {"name": "Test User", "email": "test@example.com"},
        "reserved_at": "2026-07-16T00:00:00+00:00",
        "reason": "topology-generation",
        "run_id": "run123",
        "topology_name": "demo-topology",
    }
    save_inventory(inventory, inventory_path)

    refreshed = build_inventory(
        {
            "edge-1": {
                "id": "edge-1",
                "type": "edge",
                "display_name": "Edge 1",
                "model": "edge6X0",
                "model_suffix": "680",
                "serial_number": "SERIAL1",
                "ha_group_id": "edge-1",
                "ha_role": "active",
            },
            "switch-1": {
                "id": "switch-1",
                "type": "switch",
                "display_name": "Switch 1",
                "model": "Dell-3048",
                "ip_address": "10.0.0.1",
            },
        },
        [
            {
                "id": "edge-1-ge1-switch-1",
                "a": {"device_id": "edge-1", "interface": "GE1"},
                "b": {"device_id": "switch-1", "interface": "Gi1/1"},
                "role": "edge-access",
            }
        ],
    )

    saved = save_inventory(refreshed, inventory_path, preserve_local_state=True)

    assert saved.hardware[0].available is False
    assert saved.hardware[0].reservation is not None
    assert saved.hardware[0].reservation.run_id == "run123"
