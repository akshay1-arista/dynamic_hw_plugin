from app.inventory import build_inventory, reserve_generated_hardware, update_hardware_availability


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
