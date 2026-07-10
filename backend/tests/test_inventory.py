from app.inventory import build_inventory


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
