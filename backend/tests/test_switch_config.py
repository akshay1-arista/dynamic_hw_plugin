import json
import subprocess

from app.models import InventoryDevice, InventoryFile, SwitchConfigureRequest
from app.switch_config import _build_ssh_command
from app.switch_config import _merge_shared_os10_tagged_vlans
from app.switch_config import _run_ssh_script, configure_switches_for_run
from app.switch_config import SSH_COMMAND_TIMEOUT_SECONDS, SSH_CONNECT_TIMEOUT_SECONDS, SwitchConfigError


def test_configure_switches_builds_os9_cleanup_and_vlan_interface_plans(tmp_path, monkeypatch):
    inventory = InventoryFile.model_validate(
        {
            "devices": {
                "access_sw": {
                    "id": "access_sw",
                    "type": "switch",
                    "display_name": "A01-PF-S3048-5",
                    "model": "Dell-3048",
                    "ip_address": "10.0.0.10",
                    "switch_metadata": {
                        "name": "A01-PF-S3048-5",
                        "model": "Dell-3048",
                        "os_family": "os9",
                        "connections": {"ip": "10.0.0.10", "port": None},
                        "credentials": {"username": "velocloud", "password": "N#1sdwan"},
                    },
                },
                "upstream_sw": {
                    "id": "upstream_sw",
                    "type": "switch",
                    "display_name": "A01-PF-S4048-1",
                    "model": "Dell-4048",
                    "ip_address": "10.0.0.11",
                    "switch_metadata": {
                        "name": "A01-PF-S4048-1",
                        "model": "Dell-4048",
                        "os_family": "os9",
                        "connections": {"ip": "10.0.0.11", "port": None},
                        "credentials": {"username": "velocloud", "password": "N#1sdwan"},
                    },
                },
                "esxi_01": {
                    "id": "esxi_01",
                    "type": "hypervisor",
                    "display_name": "esxi-01",
                    "ip_address": "10.0.0.20",
                },
            },
            "connections": [
                {
                    "id": "access-upstream",
                    "a": {"device_id": "access_sw", "interface": "tengigabitethernet1/51"},
                    "b": {"device_id": "upstream_sw", "interface": "tengigabitethernet1/43"},
                    "vlans": [1, 102, 103],
                    "tagged_vlans": [102, 103],
                    "untagged_vlan": 1,
                    "role": "switch-uplink",
                },
                {
                    "id": "upstream-hypervisor",
                    "a": {"device_id": "upstream_sw", "interface": "tengigabitethernet1/9"},
                    "b": {"device_id": "esxi_01", "interface": "vmnic0"},
                    "vlans": [1],
                    "tagged_vlans": [],
                    "untagged_vlan": 1,
                    "role": "hypervisor-access",
                },
            ],
            "hardware": [
                {
                    "id": "edge-3800",
                    "display_name": "chn-rnd-edge-3800-DGD10Q2",
                    "model": "edge3X00",
                    "model_suffix": "3800",
                    "ha": True,
                    "active_serial": "14DQ363",
                    "standby_serial": "15DQ363",
                    "switch": {
                        "name": "A01-PF-S3048-5",
                        "model": "Dell-3048",
                        "os_family": "os9",
                        "connections": {"ip": "10.0.0.10", "port": None},
                        "credentials": {"username": "velocloud", "password": "N#1sdwan"},
                    },
                    "ports": [
                        {
                            "logical_name": "GE1",
                            "name": "ge1",
                            "logical_interface": "GE1",
                            "link": "lan1",
                            "switch_active_port": "gigabitethernet1/1",
                            "switch_standby_port": "gigabitethernet1/6",
                            "switch_vlans": [101],
                            "tagged_vlans": [],
                            "untagged_vlan": 101,
                        },
                        {
                            "logical_name": "GE2",
                            "name": "ge2",
                            "logical_interface": "GE2",
                            "link": "lan2",
                            "switch_active_port": "gigabitethernet1/2",
                            "switch_standby_port": "gigabitethernet1/7",
                            "switch_vlans": [102, 103],
                            "tagged_vlans": [103],
                            "untagged_vlan": 102,
                        },
                    ],
                    "path": {
                        "hops": [
                            {"switch_id": "access_sw", "switch_name": "A01-PF-S3048-5", "switch_ip": "10.0.0.10", "egress_port": "tengigabitethernet1/51"},
                            {"switch_id": "upstream_sw", "switch_name": "A01-PF-S4048-1", "switch_ip": "10.0.0.11", "ingress_port": "tengigabitethernet1/43", "egress_port": "tengigabitethernet1/9"},
                        ],
                        "hypervisor_id": "esxi_01",
                        "hypervisor_name": "esxi-01",
                        "hypervisor_ip": "10.0.0.20",
                        "complete": True,
                    },
                }
            ],
        }
    )
    run_root = tmp_path / "outputs" / "run123-abcdef"
    run_root.mkdir(parents=True)
    (run_root / "run_metadata.json").write_text(
        json.dumps(
            {
                "run_id": "run123",
                "topology_name": "topo-1",
                "reference_topology_id": "3-site",
                "mappings": [
                    {
                        "hardware_id": "edge-3800",
                        "branch_name": "branch1",
                        "edge_name": "edge1",
                        "path": inventory.hardware[0].path.model_dump(mode="json"),
                        "allocations": [
                            {
                                "reference_interface": "GE1",
                                "logical_interface": "GE1",
                                "switch_name": "A01-PF-S3048-5",
                                "switch_active_port": "gigabitethernet1/1",
                                "switch_standby_port": "gigabitethernet1/6",
                                "switch_vlans": [101],
                                "tagged_vlans": [],
                                "untagged_vlan": 101,
                                "segment_vlans": {},
                            },
                            {
                                "reference_interface": "GE2",
                                "logical_interface": "GE2",
                                "switch_name": "A01-PF-S3048-5",
                                "switch_active_port": "gigabitethernet1/2",
                                "switch_standby_port": "gigabitethernet1/7",
                                "switch_vlans": [102, 103],
                                "tagged_vlans": [103],
                                "untagged_vlan": 102,
                                "segment_vlans": {},
                            },
                        ],
                    }
                ],
            }
        )
    )

    monkeypatch.setattr("app.switch_config.load_inventory", lambda _path: inventory)
    monkeypatch.setattr(
        "app.switch_config._fetch_running_config",
        lambda device: (
            """
interface Vlan 101
 member GigabitEthernet 1/1,1/6
 no shutdown
interface Vlan 102
 untagged GigabitEthernet 1/2,1/7
 tagged TenGigabitEthernet 1/51
 no shutdown
interface Vlan 103
 tagged GigabitEthernet 1/2,1/7
 tagged TenGigabitEthernet 1/51
 no shutdown
interface Vlan 104
 tagged GigabitEthernet 1/2
 no shutdown
interface Vlan 200
 tagged TenGigabitEthernet 1/51
 no shutdown
"""
            if device.id == "access_sw"
            else """
interface Vlan 102
 tagged TenGigabitEthernet 1/9,1/43
 no shutdown
interface Vlan 103
 tagged TenGigabitEthernet 1/9,1/43
 no shutdown
"""
        ),
    )

    result = configure_switches_for_run(
        "run123",
        SwitchConfigureRequest(dry_run=True),
        inventory_path=tmp_path / "inventory.json",
        outputs_root=tmp_path / "outputs",
    )

    assert result.applied is False
    access_commands = next(item.commands for item in result.devices if item.device_id == "access_sw")
    upstream_commands = next(item.commands for item in result.devices if item.device_id == "upstream_sw")

    assert access_commands[:14] == [
        "interface Vlan 101",
        " no member GigabitEthernet 1/1,1/6",
        " exit",
        "interface Vlan 102",
        " no untagged GigabitEthernet 1/2,1/7",
        " no tagged TenGigabitEthernet 1/51",
        " exit",
        "interface Vlan 103",
        " no tagged GigabitEthernet 1/2,1/7",
        " no tagged TenGigabitEthernet 1/51",
        " exit",
        "interface Vlan 104",
        " no tagged GigabitEthernet 1/2",
        " exit",
    ]
    vlan101_start = [index for index, command in enumerate(access_commands) if command == "interface Vlan 101"][-1]
    vlan102_start = [index for index, command in enumerate(access_commands) if command == "interface Vlan 102"][-1]
    vlan101_block = access_commands[vlan101_start:vlan102_start]
    vlan103_start = [index for index, command in enumerate(access_commands) if command == "interface Vlan 103"][-1]
    vlan102_block = access_commands[vlan102_start:vlan103_start]

    assert "interface GigabitEthernet 1/1" in access_commands
    assert ' description "Edge 3800_14DQ363_GE1"' in access_commands
    assert " vlan-stack access" in access_commands
    assert " member GigabitEthernet 1/1,1/6" in vlan101_block
    assert " tagged TenGigabitEthernet 1/51" not in vlan101_block
    assert " untagged GigabitEthernet 1/2,1/7" in vlan102_block
    assert " tagged TenGigabitEthernet 1/51" in vlan102_block
    assert "interface Vlan 200" not in access_commands
    assert "interface TenGigabitEthernet 1/43" in upstream_commands
    assert " no tagged TenGigabitEthernet 1/9,1/43" in upstream_commands
    assert " tagged TenGigabitEthernet 1/9,1/43" in upstream_commands


def test_configure_switches_os9_moves_ports_off_default_vlan_without_deleting_vlan_1(tmp_path, monkeypatch):
    inventory = InventoryFile.model_validate(
        {
            "devices": {
                "access_sw": {
                    "id": "access_sw",
                    "type": "switch",
                    "display_name": "A01-PF-S3048-5",
                    "model": "Dell-3048",
                    "ip_address": "10.0.0.10",
                    "switch_metadata": {
                        "name": "A01-PF-S3048-5",
                        "model": "Dell-3048",
                        "os_family": "os9",
                        "connections": {"ip": "10.0.0.10", "port": None},
                        "credentials": {"username": "velocloud", "password": "N#1sdwan"},
                    },
                },
                "upstream_sw": {
                    "id": "upstream_sw",
                    "type": "switch",
                    "display_name": "A01-PF-S4048-1",
                    "model": "Dell-4048",
                    "ip_address": "10.0.0.11",
                    "switch_metadata": {
                        "name": "A01-PF-S4048-1",
                        "model": "Dell-4048",
                        "os_family": "os9",
                        "connections": {"ip": "10.0.0.11", "port": None},
                        "credentials": {"username": "velocloud", "password": "N#1sdwan"},
                    },
                },
                "esxi_01": {
                    "id": "esxi_01",
                    "type": "hypervisor",
                    "display_name": "esxi-01",
                    "ip_address": "10.0.0.20",
                },
            },
            "connections": [
                {
                    "id": "access-upstream",
                    "a": {"device_id": "access_sw", "interface": "tengigabitethernet1/51"},
                    "b": {"device_id": "upstream_sw", "interface": "tengigabitethernet1/43"},
                    "vlans": [1],
                    "tagged_vlans": [],
                    "untagged_vlan": 1,
                    "role": "switch-uplink",
                },
                {
                    "id": "upstream-hypervisor",
                    "a": {"device_id": "upstream_sw", "interface": "tengigabitethernet1/9"},
                    "b": {"device_id": "esxi_01", "interface": "vmnic0"},
                    "vlans": [1],
                    "tagged_vlans": [],
                    "untagged_vlan": 1,
                    "role": "hypervisor-access",
                },
            ],
            "hardware": [
                {
                    "id": "edge-3800",
                    "display_name": "chn-rnd-edge-3800-DGD10Q2",
                    "model": "edge3X00",
                    "model_suffix": "3800",
                    "ha": False,
                    "active_serial": "14DQ363",
                    "standby_serial": None,
                    "switch": {
                        "name": "A01-PF-S3048-5",
                        "model": "Dell-3048",
                        "os_family": "os9",
                        "connections": {"ip": "10.0.0.10", "port": None},
                        "credentials": {"username": "velocloud", "password": "N#1sdwan"},
                    },
                    "ports": [
                        {
                            "logical_name": "GE3",
                            "name": "ge3",
                            "logical_interface": "GE3",
                            "link": "lan3",
                            "switch_active_port": "gigabitethernet1/3",
                            "switch_vlans": [1510],
                            "tagged_vlans": [],
                            "untagged_vlan": 1510,
                        }
                    ],
                    "path": {
                        "hops": [
                            {"switch_id": "access_sw", "switch_name": "A01-PF-S3048-5", "switch_ip": "10.0.0.10", "egress_port": "tengigabitethernet1/51"},
                            {"switch_id": "upstream_sw", "switch_name": "A01-PF-S4048-1", "switch_ip": "10.0.0.11", "ingress_port": "tengigabitethernet1/43", "egress_port": "tengigabitethernet1/9"},
                        ],
                        "hypervisor_id": "esxi_01",
                        "hypervisor_name": "esxi-01",
                        "hypervisor_ip": "10.0.0.20",
                        "complete": True,
                    },
                }
            ],
        }
    )
    run_root = tmp_path / "outputs" / "run123"
    run_root.mkdir(parents=True)
    (run_root / "run_metadata.json").write_text(
        json.dumps(
            {
                "run_id": "run123",
                "topology_name": "topo-1",
                "reference_topology_id": "1-site",
                "mappings": [
                    {
                        "hardware_id": "edge-3800",
                        "branch_name": "branch1",
                        "edge_name": "edge1",
                        "path": inventory.hardware[0].path.model_dump(mode="json"),
                        "allocations": [
                            {
                                "reference_interface": "GE3",
                                "logical_interface": "GE3",
                                "link": "lan3",
                                "switch_name": "A01-PF-S3048-5",
                                "switch_active_port": "gigabitethernet1/3",
                                "switch_vlans": [1510],
                                "tagged_vlans": [],
                                "untagged_vlan": 1510,
                                "segment_vlans": {},
                            }
                        ],
                    }
                ],
            }
        )
    )

    monkeypatch.setattr("app.switch_config.load_inventory", lambda _path: inventory)
    monkeypatch.setattr(
        "app.switch_config._fetch_running_config",
        lambda device: (
            """
interface Vlan 1
 untagged GigabitEthernet 1/3
 tagged TenGigabitEthernet 1/51
 no shutdown
"""
            if device.id == "access_sw"
            else ""
        ),
    )

    result = configure_switches_for_run(
        "run123",
        SwitchConfigureRequest(dry_run=True),
        inventory_path=tmp_path / "inventory.json",
        outputs_root=tmp_path / "outputs",
    )

    access_commands = next(item.commands for item in result.devices if item.device_id == "access_sw")

    assert "no interface vlan 1" not in access_commands
    assert " no untagged GigabitEthernet 1/3" not in access_commands
    assert access_commands[0] == "interface GigabitEthernet 1/3"
    vlan1510_start = access_commands.index("interface Vlan 1510")
    vlan1510_block = access_commands[vlan1510_start:]
    assert " untagged GigabitEthernet 1/3" in vlan1510_block


def test_configure_switches_builds_os10_plans_and_preserves_existing_shared_trunks(tmp_path, monkeypatch):
    inventory = InventoryFile.model_validate(
        {
            "devices": {
                "access_sw": {
                    "id": "access_sw",
                    "type": "switch",
                    "display_name": "OS10-ACCESS",
                    "model": "Dell-3248",
                    "ip_address": "10.0.0.30",
                    "switch_metadata": {
                        "name": "OS10-ACCESS",
                        "model": "Dell-3248",
                        "os_family": "os10",
                        "connections": {"ip": "10.0.0.30", "port": None},
                        "credentials": {"username": "velocloud", "password": "N#1sdwan"},
                    },
                },
                "upstream_sw": {
                    "id": "upstream_sw",
                    "type": "switch",
                    "display_name": "OS10-UPSTREAM",
                    "model": "Dell-4148",
                    "ip_address": "10.0.0.31",
                    "switch_metadata": {
                        "name": "OS10-UPSTREAM",
                        "model": "Dell-4148",
                        "os_family": "os10",
                        "connections": {"ip": "10.0.0.31", "port": None},
                        "credentials": {"username": "velocloud", "password": "N#1sdwan"},
                    },
                },
                "esxi_01": {
                    "id": "esxi_01",
                    "type": "hypervisor",
                    "display_name": "esxi-01",
                    "ip_address": "10.0.0.50",
                },
            },
            "connections": [
                {
                    "id": "access-upstream",
                    "a": {"device_id": "access_sw", "interface": "eth1/1/49"},
                    "b": {"device_id": "upstream_sw", "interface": "eth1/1/43"},
                    "vlans": [1],
                    "tagged_vlans": [],
                    "untagged_vlan": 1,
                    "role": "switch-uplink",
                },
                {
                    "id": "upstream-hypervisor",
                    "a": {"device_id": "upstream_sw", "interface": "eth1/1/54"},
                    "b": {"device_id": "esxi_01", "interface": "vmnic0"},
                    "vlans": [1],
                    "tagged_vlans": [],
                    "untagged_vlan": 1,
                    "role": "hypervisor-access",
                },
            ],
            "hardware": [
                {
                    "id": "edge-os10",
                    "display_name": "OS10 Edge",
                    "model": "edge6X0",
                    "model_suffix": "680",
                    "ha": False,
                    "active_serial": "ABC123",
                    "standby_serial": None,
                    "switch": {
                        "name": "OS10-ACCESS",
                        "model": "Dell-3248",
                        "os_family": "os10",
                        "connections": {"ip": "10.0.0.30", "port": None},
                        "credentials": {"username": "velocloud", "password": "N#1sdwan"},
                    },
                    "ports": [
                        {
                            "logical_name": "GE2",
                            "name": "ge2",
                            "logical_interface": "GE2",
                            "link": "lan2",
                            "switch_active_port": "eth1/1/3",
                            "switch_vlans": [214, 215, 216],
                            "tagged_vlans": [215, 216],
                            "untagged_vlan": 214,
                        }
                    ],
                    "path": {
                        "hops": [
                            {"switch_id": "access_sw", "switch_name": "OS10-ACCESS", "switch_ip": "10.0.0.30", "egress_port": "eth1/1/49"},
                            {"switch_id": "upstream_sw", "switch_name": "OS10-UPSTREAM", "switch_ip": "10.0.0.31", "ingress_port": "eth1/1/43", "egress_port": "eth1/1/54"},
                        ],
                        "hypervisor_id": "esxi_01",
                        "hypervisor_name": "esxi-01",
                        "hypervisor_ip": "10.0.0.50",
                        "complete": True,
                    },
                }
            ],
        }
    )
    run_root = tmp_path / "outputs" / "run123"
    run_root.mkdir(parents=True)
    (run_root / "run_metadata.json").write_text(
        json.dumps(
            {
                "run_id": "run123",
                "topology_name": "topo-1",
                "reference_topology_id": "3-site",
                "mappings": [
                    {
                        "hardware_id": "edge-os10",
                        "branch_name": "branch1",
                        "edge_name": "edge1",
                        "path": inventory.hardware[0].path.model_dump(mode="json"),
                        "allocations": [
                            {
                                "reference_interface": "GE2",
                                "logical_interface": "GE2",
                                "switch_name": "OS10-ACCESS",
                                "switch_active_port": "eth1/1/3",
                                "switch_vlans": [214, 215, 216],
                                "tagged_vlans": [215, 216],
                                "untagged_vlan": 214,
                                "segment_vlans": {},
                            }
                        ],
                    }
                ],
            }
        )
    )

    monkeypatch.setattr("app.switch_config.load_inventory", lambda _path: inventory)
    monkeypatch.setattr("app.switch_config._fetch_running_config", lambda _device: "")
    monkeypatch.setattr(
        "app.switch_config._fetch_os10_interface_config",
        lambda _device, interface: {
            "eth1/1/49": """
interface ethernet1/1/49
 switchport mode trunk
 switchport access vlan 1
 switchport trunk allowed vlan 202-203
""",
            "eth1/1/43": """
interface ethernet1/1/43
 switchport mode trunk
 switchport access vlan 1
 switchport trunk allowed vlan 202-203
""",
            "eth1/1/54": """
interface ethernet1/1/54
 switchport mode trunk
 switchport access vlan 1
 switchport trunk allowed vlan 202-216,402-416
""",
        }.get(interface, ""),
    )

    result = configure_switches_for_run(
        "run123",
        SwitchConfigureRequest(dry_run=True),
        inventory_path=tmp_path / "inventory.json",
        outputs_root=tmp_path / "outputs",
    )

    access_commands = next(item.commands for item in result.devices if item.device_id == "access_sw")
    upstream_commands = next(item.commands for item in result.devices if item.device_id == "upstream_sw")

    assert "interface ethernet1/1/3" in access_commands
    assert " switchport access vlan 214" in access_commands
    assert " switchport trunk allowed vlan 215-216" in access_commands
    assert "interface ethernet1/1/49" in access_commands
    assert " switchport trunk allowed vlan 202-203,214-216" in access_commands
    assert "interface ethernet1/1/54" in upstream_commands
    assert " switchport trunk allowed vlan 202-216,402-416" in upstream_commands


def test_merge_shared_os10_tagged_vlans_replaces_only_targeted_shared_vlans():
    merged = _merge_shared_os10_tagged_vlans(
        existing_tagged={202, 203, 214, 215, 216},
        desired_tagged={214},
        cleanup_target_vlans={214, 215, 216},
    )

    assert merged == {202, 203, 214}


def test_configure_switches_routes_mixed_allocations_to_their_actual_switches(tmp_path, monkeypatch):
    inventory = InventoryFile.model_validate(
        {
            "devices": {
                "access_sw": {
                    "id": "access_sw",
                    "type": "switch",
                    "display_name": "chn-rnd-sw-3048-J8Y00Q2",
                    "model": "Dell-3048",
                    "ip_address": "10.68.136.28",
                    "switch_metadata": {
                        "name": "chn-rnd-sw-3048-J8Y00Q2",
                        "model": "Dell-3048",
                        "os_family": "os9",
                        "connections": {"ip": "10.68.136.28", "port": None},
                        "credentials": {"username": "velocloud", "password": "N#1sdwan"},
                    },
                },
                "upstream_sw": {
                    "id": "upstream_sw",
                    "type": "switch",
                    "display_name": "chn-rnd-sw-4148-F19CV43",
                    "model": "Dell-4148",
                    "ip_address": "10.68.137.247",
                    "switch_metadata": {
                        "name": "chn-rnd-sw-4148-F19CV43",
                        "model": "Dell-4148",
                        "os_family": "os10",
                        "connections": {"ip": "10.68.137.247", "port": None},
                        "credentials": {"username": "velocloud", "password": "N#1sdwan"},
                    },
                },
                "hypervisor": {
                    "id": "hypervisor",
                    "type": "hypervisor",
                    "display_name": "chn-rnd-srv-650-G1HVKJ3",
                    "ip_address": "10.68.137.104",
                },
            },
            "connections": [
                {
                    "id": "access-upstream",
                    "a": {"device_id": "access_sw", "interface": "tengigabitethernet1/52"},
                    "b": {"device_id": "upstream_sw", "interface": "eth1/1/53"},
                    "vlans": [1, 403, 404, 408, 409],
                    "tagged_vlans": [403, 404, 408, 409],
                    "untagged_vlan": 1,
                    "role": "switch-uplink",
                },
                {
                    "id": "upstream-hypervisor",
                    "a": {"device_id": "upstream_sw", "interface": "eth1/1/54"},
                    "b": {"device_id": "hypervisor", "interface": "vmnic2"},
                    "vlans": [1, 403, 404, 408, 409, 412, 413, 415, 416],
                    "tagged_vlans": [403, 404, 408, 409, 412, 413, 415, 416],
                    "untagged_vlan": 1,
                    "role": "hypervisor-access",
                },
            ],
            "hardware": [
                {
                    "id": "edge-740-ha",
                    "display_name": "HA Pair chn-rnd-edge-740-8202197 + chn-rnd-edge-740-8202193",
                    "model": "edge7X0",
                    "model_suffix": "740",
                    "ha": True,
                    "active_serial": "248202197",
                    "standby_serial": "248202193",
                    "switch": {
                        "name": "chn-rnd-sw-3048-J8Y00Q2",
                        "model": "Dell-3048",
                        "os_family": "os9",
                        "connections": {"ip": "10.68.136.28", "port": None},
                        "credentials": {"username": "velocloud", "password": "N#1sdwan"},
                    },
                    "switches": [
                        {
                            "name": "chn-rnd-sw-3048-J8Y00Q2",
                            "model": "Dell-3048",
                            "os_family": "os9",
                            "connections": {"ip": "10.68.136.28", "port": None},
                            "credentials": {"username": "velocloud", "password": "N#1sdwan"},
                        },
                        {
                            "name": "chn-rnd-sw-4148-F19CV43",
                            "model": "Dell-4148",
                            "os_family": "os10",
                            "connections": {"ip": "10.68.137.247", "port": None},
                            "credentials": {"username": "velocloud", "password": "N#1sdwan"},
                        },
                    ],
                    "ports": [
                        {
                            "logical_name": "GE1",
                            "name": "ge1",
                            "logical_interface": "GE1",
                            "link": "lan1",
                            "switch_name": "chn-rnd-sw-3048-J8Y00Q2",
                            "switch_active_port": "gigabitethernet1/1",
                            "switch_standby_port": "gigabitethernet1/13",
                            "switch_vlans": [401],
                            "tagged_vlans": [],
                            "untagged_vlan": 401,
                        }
                    ],
                    "path": {
                        "hops": [
                            {"switch_id": "access_sw", "switch_name": "chn-rnd-sw-3048-J8Y00Q2", "switch_ip": "10.68.136.28", "egress_port": "tengigabitethernet1/52"},
                            {"switch_id": "upstream_sw", "switch_name": "chn-rnd-sw-4148-F19CV43", "switch_ip": "10.68.137.247", "ingress_port": "eth1/1/53", "egress_port": "eth1/1/54"},
                        ],
                        "hypervisor_id": "hypervisor",
                        "hypervisor_name": "chn-rnd-srv-650-G1HVKJ3",
                        "hypervisor_ip": "10.68.137.104",
                        "complete": True,
                    },
                }
            ],
        }
    )
    run_root = tmp_path / "outputs" / "run123"
    run_root.mkdir(parents=True)
    (run_root / "run_metadata.json").write_text(
        json.dumps(
            {
                "run_id": "run123",
                "topology_name": "3-site-hw-176186",
                "reference_topology_id": "3-site",
                "mappings": [
                    {
                        "hardware_id": "edge-740-ha",
                        "branch_name": "branch2",
                        "edge_name": "b2-edge1",
                        "path": inventory.hardware[0].path.model_dump(mode="json"),
                        "allocations": [
                            {
                                "reference_interface": "GE1",
                                "logical_interface": "GE1",
                                "switch_name": "chn-rnd-sw-3048-J8Y00Q2",
                                "switch_active_port": "gigabitethernet1/1",
                                "switch_standby_port": "gigabitethernet1/13",
                                "switch_vlans": [401],
                                "tagged_vlans": [],
                                "untagged_vlan": 401,
                                "segment_vlans": {},
                            },
                            {
                                "reference_interface": "GE2",
                                "logical_interface": "GE2",
                                "switch_name": "chn-rnd-sw-3048-J8Y00Q2",
                                "switch_active_port": "gigabitethernet1/2",
                                "switch_standby_port": "gigabitethernet1/14",
                                "switch_vlans": [402, 403, 404],
                                "tagged_vlans": [403, 404],
                                "untagged_vlan": 402,
                                "segment_vlans": {},
                            },
                            {
                                "reference_interface": "GE7",
                                "logical_interface": "GE5",
                                "switch_name": "chn-rnd-sw-3048-J8Y00Q2",
                                "switch_active_port": "gigabitethernet1/5",
                                "switch_standby_port": "gigabitethernet1/17",
                                "switch_vlans": [407, 408, 409],
                                "tagged_vlans": [408, 409],
                                "untagged_vlan": 407,
                                "segment_vlans": {},
                            },
                            {
                                "reference_interface": "GE5",
                                "logical_interface": "SFP1",
                                "switch_name": "chn-rnd-sw-4148-F19CV43",
                                "switch_active_port": "eth1/1/5",
                                "switch_standby_port": "eth1/1/6",
                                "switch_vlans": [411, 412, 413],
                                "tagged_vlans": [412, 413],
                                "untagged_vlan": 411,
                                "segment_vlans": {},
                            },
                            {
                                "reference_interface": "GE6",
                                "logical_interface": "SFP2",
                                "switch_name": "chn-rnd-sw-4148-F19CV43",
                                "switch_active_port": "eth1/1/7",
                                "switch_standby_port": "eth1/1/8",
                                "switch_vlans": [414, 415, 416],
                                "tagged_vlans": [415, 416],
                                "untagged_vlan": 414,
                                "segment_vlans": {},
                            },
                        ],
                    }
                ],
            }
        )
    )

    monkeypatch.setattr("app.switch_config.load_inventory", lambda _path: inventory)
    monkeypatch.setattr("app.switch_config._fetch_running_config", lambda _device: "")
    monkeypatch.setattr("app.switch_config._fetch_os10_interface_config", lambda _device, _interface: "")

    result = configure_switches_for_run(
        "run123",
        SwitchConfigureRequest(dry_run=True),
        inventory_path=tmp_path / "inventory.json",
        outputs_root=tmp_path / "outputs",
    )

    access_commands = next(item.commands for item in result.devices if item.device_id == "access_sw")
    upstream_commands = next(item.commands for item in result.devices if item.device_id == "upstream_sw")

    assert "interface GigabitEthernet 1/1" in access_commands
    assert "interface ethernet1/1/5" not in access_commands
    assert not any("412" in command or "415" in command for command in access_commands)
    assert "interface ethernet1/1/5" in upstream_commands
    assert "interface ethernet1/1/53" in upstream_commands
    assert "interface ethernet1/1/54" in upstream_commands
    assert " switchport trunk allowed vlan 402-404,407-409" in upstream_commands
    assert " switchport trunk allowed vlan 402-404,407-409,411-416" in upstream_commands


def test_configure_switches_applies_command_overrides(tmp_path, monkeypatch):
    inventory = InventoryFile.model_validate(
        {
            "devices": {
                "access_sw": {
                    "id": "access_sw",
                    "type": "switch",
                    "display_name": "access-sw",
                    "model": "Dell-3048",
                    "ip_address": "10.0.0.10",
                    "switch_metadata": {
                        "name": "access-sw",
                        "model": "Dell-3048",
                        "os_family": "os9",
                        "connections": {"ip": "10.0.0.10", "port": None},
                        "credentials": {"username": "velocloud", "password": "N#1sdwan"},
                    },
                },
                "upstream_sw": {
                    "id": "upstream_sw",
                    "type": "switch",
                    "display_name": "upstream-sw",
                    "model": "Dell-4048",
                    "ip_address": "10.0.0.11",
                    "switch_metadata": {
                        "name": "upstream-sw",
                        "model": "Dell-4048",
                        "os_family": "os9",
                        "connections": {"ip": "10.0.0.11", "port": None},
                        "credentials": {"username": "velocloud", "password": "N#1sdwan"},
                    },
                },
                "esxi_01": {
                    "id": "esxi_01",
                    "type": "hypervisor",
                    "display_name": "esxi-01",
                    "ip_address": "10.0.0.20",
                },
            },
            "connections": [
                {
                    "id": "access-upstream",
                    "a": {"device_id": "access_sw", "interface": "tengigabitethernet1/51"},
                    "b": {"device_id": "upstream_sw", "interface": "tengigabitethernet1/43"},
                    "vlans": [1, 102, 103],
                    "tagged_vlans": [102, 103],
                    "untagged_vlan": 1,
                    "role": "switch-uplink",
                },
                {
                    "id": "upstream-hypervisor",
                    "a": {"device_id": "upstream_sw", "interface": "tengigabitethernet1/9"},
                    "b": {"device_id": "esxi_01", "interface": "vmnic0"},
                    "vlans": [1],
                    "tagged_vlans": [],
                    "untagged_vlan": 1,
                    "role": "hypervisor-access",
                },
            ],
            "hardware": [
                {
                    "id": "edge-1",
                    "display_name": "Edge 1",
                    "model": "edge3X00",
                    "model_suffix": "3800",
                    "ha": False,
                    "active_serial": "SERIAL1",
                    "standby_serial": None,
                    "switch": {
                        "name": "access-sw",
                        "model": "Dell-3048",
                        "os_family": "os9",
                        "connections": {"ip": "10.0.0.10", "port": None},
                        "credentials": {"username": "velocloud", "password": "N#1sdwan"},
                    },
                    "ports": [
                        {
                            "logical_name": "GE2",
                            "name": "ge2",
                            "logical_interface": "GE2",
                            "link": "lan2",
                            "switch_active_port": "gigabitethernet1/2",
                            "switch_vlans": [102, 103],
                            "tagged_vlans": [103],
                            "untagged_vlan": 102,
                        }
                    ],
                    "path": {
                        "hops": [
                            {"switch_id": "access_sw", "switch_name": "access-sw", "switch_ip": "10.0.0.10", "egress_port": "tengigabitethernet1/51"},
                            {"switch_id": "upstream_sw", "switch_name": "upstream-sw", "switch_ip": "10.0.0.11", "ingress_port": "tengigabitethernet1/43", "egress_port": "tengigabitethernet1/9"},
                        ],
                        "hypervisor_id": "esxi_01",
                        "hypervisor_name": "esxi-01",
                        "hypervisor_ip": "10.0.0.20",
                        "complete": True,
                    },
                }
            ],
        }
    )
    run_root = tmp_path / "outputs" / "run123"
    run_root.mkdir(parents=True)
    (run_root / "run_metadata.json").write_text(
        json.dumps(
            {
                "run_id": "run123",
                "topology_name": "topo-1",
                "reference_topology_id": "3-site",
                "mappings": [
                    {
                        "hardware_id": "edge-1",
                        "branch_name": "branch1",
                        "edge_name": "edge1",
                        "path": inventory.hardware[0].path.model_dump(mode="json"),
                        "allocations": [
                            {
                                "reference_interface": "GE2",
                                "logical_interface": "GE2",
                                "switch_name": "access-sw",
                                "switch_active_port": "gigabitethernet1/2",
                                "switch_vlans": [102, 103],
                                "tagged_vlans": [103],
                                "untagged_vlan": 102,
                                "segment_vlans": {},
                            }
                        ],
                    }
                ],
            }
        )
    )

    executed = []

    monkeypatch.setattr("app.switch_config.load_inventory", lambda _path: inventory)
    monkeypatch.setattr("app.switch_config._fetch_running_config", lambda _device: "")
    monkeypatch.setattr(
        "app.switch_config._execute_switch_plan",
        lambda plan, device: executed.append((plan.device_id, plan.commands, device.display_name)),
    )

    configure_switches_for_run(
        "run123",
        SwitchConfigureRequest(
            command_overrides=[
                {
                    "device_id": "access_sw",
                    "commands": ["interface GigabitEthernet 1/2", " description \"manual\""],
                }
            ]
        ),
        inventory_path=tmp_path / "inventory.json",
        outputs_root=tmp_path / "outputs",
    )

    access_commands = next(item[1] for item in executed if item[0] == "access_sw")
    upstream_commands = next(item[1] for item in executed if item[0] == "upstream_sw")
    assert access_commands == ["interface GigabitEthernet 1/2", ' description "manual"']
    assert "interface TenGigabitEthernet 1/43" in upstream_commands


def test_configure_switches_uses_generated_links_to_exclude_only_ha_vlan_from_transport(tmp_path, monkeypatch):
    inventory = InventoryFile.model_validate(
        {
            "devices": {
                "access_sw": {
                    "id": "access_sw",
                    "type": "switch",
                    "display_name": "chn-rnd-sw-3048-J8Y00Q2",
                    "model": "Dell-3048",
                    "ip_address": "10.68.136.28",
                    "switch_metadata": {
                        "name": "chn-rnd-sw-3048-J8Y00Q2",
                        "model": "Dell-3048",
                        "os_family": "os9",
                        "connections": {"ip": "10.68.136.28", "port": None},
                        "credentials": {"username": "velocloud", "password": "N#1sdwan"},
                    },
                },
                "upstream_sw": {
                    "id": "upstream_sw",
                    "type": "switch",
                    "display_name": "chn-rnd-sw-4148-F19CV43",
                    "model": "Dell-4148",
                    "ip_address": "10.68.137.247",
                    "switch_metadata": {
                        "name": "chn-rnd-sw-4148-F19CV43",
                        "model": "Dell-4148",
                        "os_family": "os10",
                        "connections": {"ip": "10.68.137.247", "port": None},
                        "credentials": {"username": "velocloud", "password": "N#1sdwan"},
                    },
                },
                "hypervisor": {
                    "id": "hypervisor",
                    "type": "hypervisor",
                    "display_name": "chn-rnd-srv-650-G1HVKJ3",
                    "ip_address": "10.68.137.104",
                },
            },
            "connections": [
                {
                    "id": "access-upstream",
                    "a": {"device_id": "access_sw", "interface": "tengigabitethernet1/52"},
                    "b": {"device_id": "upstream_sw", "interface": "eth1/1/53"},
                    "vlans": [1, 401, 402, 403, 404, 405, 406],
                    "tagged_vlans": [401, 402, 403, 404, 405, 406],
                    "untagged_vlan": 1,
                    "role": "switch-uplink",
                },
                {
                    "id": "upstream-hypervisor",
                    "a": {"device_id": "upstream_sw", "interface": "eth1/1/54"},
                    "b": {"device_id": "hypervisor", "interface": "vmnic2"},
                    "vlans": [1, 401, 402, 403, 404, 405, 406],
                    "tagged_vlans": [401, 402, 403, 404, 405, 406],
                    "untagged_vlan": 1,
                    "role": "hypervisor-access",
                },
            ],
            "hardware": [
                {
                    "id": "edge-740-ha",
                    "display_name": "HA Pair chn-rnd-edge-740-8202197 + chn-rnd-edge-740-8202193",
                    "model": "edge7X0",
                    "model_suffix": "740",
                    "ha": True,
                    "active_serial": "248202197",
                    "standby_serial": "248202193",
                    "switch": {
                        "name": "chn-rnd-sw-3048-J8Y00Q2",
                        "model": "Dell-3048",
                        "os_family": "os9",
                        "connections": {"ip": "10.68.136.28", "port": None},
                        "credentials": {"username": "velocloud", "password": "N#1sdwan"},
                    },
                    "ports": [
                        {
                            "logical_name": "GE1",
                            "name": "ge1",
                            "logical_interface": "GE1",
                            "link": "B2E1_HA",
                            "switch_name": "chn-rnd-sw-3048-J8Y00Q2",
                            "switch_active_port": "gigabitethernet1/1",
                            "switch_standby_port": "gigabitethernet1/13",
                            "switch_vlans": [401],
                            "tagged_vlans": [],
                            "untagged_vlan": 401,
                        },
                        {
                            "logical_name": "GE2",
                            "name": "ge2",
                            "logical_interface": "GE2",
                            "link": "E21C1",
                            "switch_name": "chn-rnd-sw-3048-J8Y00Q2",
                            "switch_active_port": "gigabitethernet1/2",
                            "switch_standby_port": "gigabitethernet1/14",
                            "switch_vlans": [402, 403, 404],
                            "tagged_vlans": [403, 404],
                            "untagged_vlan": 402,
                        },
                        {
                            "logical_name": "GE3",
                            "name": "ge3",
                            "logical_interface": "GE3",
                            "link": "cr2w1",
                            "switch_name": "chn-rnd-sw-3048-J8Y00Q2",
                            "switch_active_port": "gigabitethernet1/3",
                            "switch_standby_port": "gigabitethernet1/15",
                            "switch_vlans": [405],
                            "tagged_vlans": [],
                            "untagged_vlan": 405,
                        },
                        {
                            "logical_name": "GE4",
                            "name": "ge4",
                            "logical_interface": "GE4",
                            "link": "cr1e2",
                            "switch_name": "chn-rnd-sw-3048-J8Y00Q2",
                            "switch_active_port": "gigabitethernet1/4",
                            "switch_standby_port": "gigabitethernet1/16",
                            "switch_vlans": [406],
                            "tagged_vlans": [],
                            "untagged_vlan": 406,
                        },
                    ],
                    "path": {
                        "hops": [
                            {"switch_id": "access_sw", "switch_name": "chn-rnd-sw-3048-J8Y00Q2", "switch_ip": "10.68.136.28", "egress_port": "tengigabitethernet1/52"},
                            {"switch_id": "upstream_sw", "switch_name": "chn-rnd-sw-4148-F19CV43", "switch_ip": "10.68.137.247", "ingress_port": "eth1/1/53", "egress_port": "eth1/1/54"},
                        ],
                        "hypervisor_id": "hypervisor",
                        "hypervisor_name": "chn-rnd-srv-650-G1HVKJ3",
                        "hypervisor_ip": "10.68.137.104",
                        "complete": True,
                    },
                }
            ],
        }
    )
    run_root = tmp_path / "outputs" / "run123"
    topology_root = run_root / "3-site-spirent-hw-db3a18"
    topology_root.mkdir(parents=True)
    (run_root / "run_metadata.json").write_text(
        json.dumps(
            {
                "run_id": "run123",
                "topology_name": "3-site-spirent-hw-db3a18",
                "reference_topology_id": "3-site/spirent",
                "mappings": [
                    {
                        "hardware_id": "edge-740-ha",
                        "branch_name": "branch2",
                        "edge_name": "b2-edge1",
                        "path": inventory.hardware[0].path.model_dump(mode="json"),
                        "allocations": [
                            {
                                "reference_interface": "GE1",
                                "logical_interface": "GE1",
                                "switch_name": "chn-rnd-sw-3048-J8Y00Q2",
                                "switch_active_port": "gigabitethernet1/1",
                                "switch_standby_port": "gigabitethernet1/13",
                                "switch_vlans": [401],
                                "tagged_vlans": [],
                                "untagged_vlan": 401,
                                "segment_vlans": {},
                            },
                            {
                                "reference_interface": "GE2",
                                "logical_interface": "GE2",
                                "switch_name": "chn-rnd-sw-3048-J8Y00Q2",
                                "switch_active_port": "gigabitethernet1/2",
                                "switch_standby_port": "gigabitethernet1/14",
                                "switch_vlans": [402, 403, 404],
                                "tagged_vlans": [403, 404],
                                "untagged_vlan": 402,
                                "segment_vlans": {},
                            },
                            {
                                "reference_interface": "GE3",
                                "logical_interface": "GE3",
                                "switch_name": "chn-rnd-sw-3048-J8Y00Q2",
                                "switch_active_port": "gigabitethernet1/3",
                                "switch_standby_port": "gigabitethernet1/15",
                                "switch_vlans": [405],
                                "tagged_vlans": [],
                                "untagged_vlan": 405,
                                "segment_vlans": {},
                            },
                            {
                                "reference_interface": "GE4",
                                "logical_interface": "GE4",
                                "switch_name": "chn-rnd-sw-3048-J8Y00Q2",
                                "switch_active_port": "gigabitethernet1/4",
                                "switch_standby_port": "gigabitethernet1/16",
                                "switch_vlans": [406],
                                "tagged_vlans": [],
                                "untagged_vlan": 406,
                                "segment_vlans": {},
                            },
                        ],
                    }
                ],
            }
        )
    )
    (topology_root / "config.json").write_text(
        json.dumps(
            {
                "topology": {
                    "branches": [
                        {
                            "edges": [
                                {
                                    "l2_switches": [
                                        {
                                            "name": "chn-rnd-sw-3048-J8Y00Q2",
                                            "interfaces": [
                                                {"name": "gigabitethernet1/1", "link": "B2E1_HA"},
                                                {"name": "gigabitethernet1/2", "link": "E21C1"},
                                                {"name": "gigabitethernet1/3", "link": "cr2w1"},
                                                {"name": "gigabitethernet1/4", "link": "cr1e2"},
                                            ],
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            }
        )
    )

    monkeypatch.setattr("app.switch_config.load_inventory", lambda _path: inventory)
    monkeypatch.setattr("app.switch_config._fetch_running_config", lambda _device: "")
    monkeypatch.setattr("app.switch_config._fetch_os10_interface_config", lambda _device, _interface: "")

    result = configure_switches_for_run(
        "run123",
        SwitchConfigureRequest(dry_run=True),
        inventory_path=tmp_path / "inventory.json",
        outputs_root=tmp_path / "outputs",
    )

    access_commands = next(item.commands for item in result.devices if item.device_id == "access_sw")
    upstream_commands = next(item.commands for item in result.devices if item.device_id == "upstream_sw")

    ge1_start = access_commands.index("interface GigabitEthernet 1/1")
    ge2_start = access_commands.index("interface GigabitEthernet 1/2")
    ge3_start = access_commands.index("interface GigabitEthernet 1/3")
    ge4_start = access_commands.index("interface GigabitEthernet 1/4")

    ge1_block = access_commands[ge1_start:ge2_start]
    ge3_block = access_commands[ge3_start:ge4_start]
    ge4_block = access_commands[ge4_start:access_commands.index("interface GigabitEthernet 1/13")]

    vlan401_start = access_commands.index("interface Vlan 401")
    vlan402_start = access_commands.index("interface Vlan 402")
    vlan405_start = access_commands.index("interface Vlan 405")
    vlan406_start = access_commands.index("interface Vlan 406")

    vlan401_block = access_commands[vlan401_start:vlan402_start]
    vlan405_block = access_commands[vlan405_start:vlan406_start]
    vlan406_block = access_commands[vlan406_start:]

    assert " vlan-stack access" in ge1_block
    assert " vlan-stack access" not in ge3_block
    assert " vlan-stack access" not in ge4_block
    assert " member GigabitEthernet 1/1,1/13" in vlan401_block
    assert " vlan-stack compatible" in vlan401_block
    assert " untagged GigabitEthernet 1/3,1/15" in vlan405_block
    assert " member GigabitEthernet 1/3,1/15" not in vlan405_block
    assert " vlan-stack compatible" not in vlan405_block
    assert " untagged GigabitEthernet 1/4,1/16" in vlan406_block
    assert " member GigabitEthernet 1/4,1/16" not in vlan406_block
    assert " tagged TenGigabitEthernet 1/52" not in vlan401_block
    assert " tagged TenGigabitEthernet 1/52" in vlan405_block
    assert " tagged TenGigabitEthernet 1/52" in vlan406_block
    assert " switchport trunk allowed vlan 402-406" in upstream_commands


def test_run_ssh_script_uses_clean_compatibility_options(monkeypatch):
    device = InventoryDevice.model_validate(
        {
            "id": "access_sw",
            "type": "switch",
            "display_name": "legacy-switch",
            "model": "Dell-3048",
            "ip_address": "10.0.0.10",
            "switch_metadata": {
                "name": "legacy-switch",
                "model": "Dell-3048",
                "connections": {"ip": "10.0.0.10", "port": 2222},
                "credentials": {"username": "velo", "password": "secret"},
            },
        }
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr("app.switch_config.shutil.which", lambda tool: "/opt/homebrew/bin/sshpass" if tool == "sshpass" else None)

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, stdout="running-config", stderr="")

    monkeypatch.setattr("app.switch_config.subprocess.run", fake_run)

    output = _run_ssh_script(device, ["terminal length 0", "show running-config", "exit"], "lookup failed")

    assert output == "running-config"
    command = captured["command"]
    assert command[:6] == ["sshpass", "-p", "secret", "ssh", "-F", "/dev/null"]
    assert "-tt" not in command
    assert f"ConnectTimeout={SSH_CONNECT_TIMEOUT_SECONDS}" in command
    assert "ConnectionAttempts=1" in command
    assert "PreferredAuthentications=password" in command
    assert "PubkeyAuthentication=no" in command
    assert "KbdInteractiveAuthentication=no" in command
    assert "NumberOfPasswordPrompts=1" in command
    assert (
        "KexAlgorithms=+diffie-hellman-group14-sha1,diffie-hellman-group-exchange-sha1,diffie-hellman-group1-sha1"
        in command
    )
    assert "HostKeyAlgorithms=+ssh-rsa" in command
    assert command[-3:] == ["-p", "2222", "velo@10.0.0.10"]
    assert captured["kwargs"]["input"] == "terminal length 0\nshow running-config\nexit\n"
    assert captured["kwargs"]["text"] is True
    assert captured["kwargs"]["check"] is True
    assert captured["kwargs"]["capture_output"] is True
    assert captured["kwargs"]["timeout"] == SSH_COMMAND_TIMEOUT_SECONDS


def test_build_ssh_command_requires_sshpass(monkeypatch):
    device = InventoryDevice.model_validate(
        {
            "id": "access_sw",
            "type": "switch",
            "display_name": "legacy-switch",
            "model": "Dell-3048",
            "ip_address": "10.0.0.10",
            "switch_metadata": {
                "name": "legacy-switch",
                "model": "Dell-3048",
                "connections": {"ip": "10.0.0.10", "port": 2222},
                "credentials": {"username": "velo", "password": "secret"},
            },
        }
    )

    monkeypatch.setattr("app.switch_config.shutil.which", lambda _tool: None)

    try:
        _build_ssh_command(device, force_tty=False)
    except SwitchConfigError as error:
        assert str(error) == "sshpass is required for switch auto-config. Install the host package `sshpass` and retry."
    else:
        raise AssertionError("Expected SwitchConfigError when sshpass is unavailable")


def test_run_ssh_script_surfaces_timeout_errors(monkeypatch):
    device = InventoryDevice.model_validate(
        {
            "id": "access_sw",
            "type": "switch",
            "display_name": "legacy-switch",
            "model": "Dell-3048",
            "ip_address": "10.0.0.10",
            "switch_metadata": {
                "name": "legacy-switch",
                "model": "Dell-3048",
                "connections": {"ip": "10.0.0.10", "port": None},
                "credentials": {"username": "velo", "password": "secret"},
            },
        }
    )

    monkeypatch.setattr("app.switch_config.shutil.which", lambda tool: "/opt/homebrew/bin/sshpass" if tool == "sshpass" else None)

    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr("app.switch_config.subprocess.run", fake_run)

    try:
        _run_ssh_script(device, ["show running-config"], "lookup failed")
    except SwitchConfigError as error:
        assert (
            str(error)
            == f"lookup failed: timed out after {SSH_COMMAND_TIMEOUT_SECONDS}s while waiting for switch response"
        )
    else:
        raise AssertionError("Expected SwitchConfigError for SSH timeout")
