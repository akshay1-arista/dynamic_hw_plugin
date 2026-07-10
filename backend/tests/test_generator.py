import json
import re
import zipfile
from pathlib import Path

import pytest

from app.generator import (
    GenerationError,
    _apply_hardware_to_edge,
    _apply_inventory_free_vlans_to_edge,
    _apply_port_mappings,
    _apply_remote_updates_to_config,
    _build_l2_switches,
    generate_topology,
)
from app.config import INVENTORY_PATH
from app.inventory import build_inventory, resolve_mapping_path
from app.models import GenerateRequest, HardwareEdge, InterfaceOverride, InventoryFile


def make_request(**overrides):
    payload = {
        "topology_name": "unit-3-site-3800",
        "reference_topology_id": "3-site",
        "hypervisor_ip": "10.68.136.50",
        "hypervisor_interface": "vmnic7",
        "mappings": [
            {
                "hardware_id": "chn-3800-8-ha",
                "branch_name": "branch2",
                "edge_name": "b2-edge1",
            }
        ],
    }
    payload.update(overrides)
    return GenerateRequest.model_validate(payload)


def load_config(result):
    with Path(result.topology_path, "config.json").open() as fh:
        return json.load(fh)


def load_json(result, relative_path):
    with Path(result.topology_path, relative_path).open() as fh:
        return json.load(fh)


def copy_inventory(tmp_path):
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(INVENTORY_PATH.read_text())
    return inventory_path


def test_generate_3_site_hardware_branch(tmp_path):
    result = generate_topology(make_request(), inventory_path=copy_inventory(tmp_path), outputs_root=tmp_path)
    config = load_config(result)
    characteristics = load_json(result, "characteristics.json")
    ibgp_characteristics = load_json(result, "ibgp/characteristics.json")
    branch = next(item for item in config["topology"]["branches"] if item["name"] == "branch2")
    characteristics_branch = next(item for item in characteristics["topology"]["branches"] if item["name"] == "branch2")
    ibgp_branch = next(item for item in ibgp_characteristics["topology"]["branches"] if item["name"] == "branch2")
    edge = branch["edges"][0]

    assert re.fullmatch(r"unit-3-site-3800-[0-9a-f]{6}", config["testbed"]["name"])
    assert result.topology_name == config["testbed"]["name"]
    assert Path(result.topology_path).name == result.topology_name
    suffix = result.topology_name.rsplit("-", 1)[-1]
    assert Path(result.topology_path).parent.name.endswith(f"-{suffix}")
    assert Path(result.zip_path).name == f"{result.topology_name}.zip"
    assert config["testbed"]["description"] == "Generated from 3-site topology"
    assert edge["name"] == "b2-edge1-3800"
    assert edge["model"] == "edge3X00"
    assert edge["slno"] == "13WR363"
    assert edge["standby_slno"] == "47YP363"
    assert edge["dpdk_enabled"] is True
    assert edge["vlans"][1]["segment_name"] == "segment1"
    assert edge["interfaces"][4]["logical_interface"] == "SFP1"
    assert edge["interfaces"][4]["subinterfaces"][0]["name"].startswith("SFP1.")
    assert edge["custom_params"]["free_vlans"] == [1524]
    assert "os_family" not in edge["l2_switches"][0]
    assert edge["l2_switches"][0]["interfaces"][-1]["link"] == "vmnic7"
    assert "ip" not in edge["l2_switches"][0]["interfaces"][-1]
    assert edge["l2_switches"][0]["interfaces"][-1]["default_gateway"] == "10.68.136.50"
    assert edge["direct_clients"][0]["interfaces"][1]["name"].startswith("eth1.")
    assert edge["direct_clients"][0]["interfaces"][1]["segments"][0]["vlan"] == edge["vlans"][1]["vlan"]
    assert branch["l3switches"][0]["interfaces"][1]["name"] == "eth1.1507"
    assert branch["CEs"][0]["interfaces"][2]["name"] == "eth2.1510"
    assert characteristics_branch["edges"][0]["interfaces"][1]["logical_interface"] == "SFP1"
    assert characteristics_branch["edges"][0]["interfaces"][2]["logical_interface"] == "SFP2"
    assert characteristics_branch["edges"][0]["interfaces"][3]["logical_interface"] == "GE5"
    assert characteristics_branch["CEs"][0]["interfaces"][0]["name"] == "eth2.1510"
    assert characteristics_branch["edges"][0]["direct_clients"][0]["interfaces"][0]["name"] == "eth1.1521"
    assert characteristics_branch["l3switches"][0]["interfaces"][0]["name"] == "eth1.1507"
    assert ibgp_branch["edges"][0]["interfaces"][1]["logical_interface"] == "SFP1"
    assert ibgp_branch["edges"][0]["interfaces"][2]["logical_interface"] == "SFP2"
    assert ibgp_branch["edges"][0]["interfaces"][3]["logical_interface"] == "GE5"
    assert ibgp_branch["CEs"][0]["interfaces"][0]["name"] == "eth2.1510"
    assert ibgp_branch["edges"][0]["direct_clients"][0]["interfaces"][0]["name"] == "eth1.1521"
    assert ibgp_branch["l3switches"][0]["interfaces"][0]["name"] == "eth1.1507"
    assert Path(result.zip_path).exists()
    with zipfile.ZipFile(result.zip_path) as archive:
        assert f"{result.topology_name}/config.json" in archive.namelist()


def test_generated_json_files_parse(tmp_path):
    result = generate_topology(
        make_request(topology_name="parse-check"),
        inventory_path=copy_inventory(tmp_path),
        outputs_root=tmp_path,
    )
    for path in Path(result.topology_path).rglob("*.json"):
        with path.open() as fh:
            json.load(fh)


def test_apply_inventory_free_vlans_to_edge_emits_empty_list():
    inventory = InventoryFile.model_validate(
        {
            "devices": {
                "edge-1": {
                    "id": "edge-1",
                    "type": "edge",
                    "display_name": "Edge 1",
                    "ha_role": "active",
                    "free_vlans": [],
                }
            },
            "connections": [],
            "allocations": [],
            "hardware": [],
        }
    )
    edge = {"custom_params": {"role": "mapped-edge"}}

    _apply_inventory_free_vlans_to_edge(edge, inventory, "edge-1")

    assert edge["custom_params"]["role"] == "mapped-edge"
    assert edge["custom_params"]["free_vlans"] == []


def test_apply_inventory_free_vlans_to_edge_excludes_generation_allocations():
    inventory = InventoryFile.model_validate(
        {
            "devices": {
                "edge-1": {
                    "id": "edge-1",
                    "type": "edge",
                    "display_name": "Edge 1",
                    "ha_role": "active",
                    "vlan_range": {"start": 200, "end": 204},
                }
            },
            "connections": [],
            "allocations": [],
            "hardware": [],
        }
    )
    edge = {"custom_params": {"role": "mapped-edge"}}

    _apply_inventory_free_vlans_to_edge(edge, inventory, "edge-1", [200, 202])

    assert edge["custom_params"]["role"] == "mapped-edge"
    assert edge["custom_params"]["free_vlans"] == [201, 203, 204]


def test_extra_ports_without_legacy_vlan_metadata_are_ignored_when_unmapped(tmp_path):
    with INVENTORY_PATH.open() as fh:
        inventory = json.load(fh)
    inventory["connections"].append(
        {
            "id": "chn-3800-8-ha-active-GE99-b2e1_l2_switch",
            "a": {"device_id": "chn-3800-8-ha-active", "interface": "GE99"},
            "b": {"device_id": "b2e1_l2_switch", "interface": "gigabitethernet1/99"},
            "vlans": [],
            "tagged_vlans": [],
            "untagged_vlan": None,
        }
    )
    inventory_path = tmp_path / "inventory.json"
    with inventory_path.open("w") as fh:
        json.dump(inventory, fh)

    result = generate_topology(make_request(), inventory_path=inventory_path, outputs_root=tmp_path)
    config = load_config(result)
    branch = next(item for item in config["topology"]["branches"] if item["name"] == "branch2")
    edge = branch["edges"][0]
    messages = [item.message for item in result.messages]
    switch_interfaces = edge["l2_switches"][0]["interfaces"]

    assert any("Ignored 1 extra hardware connection" in message for message in messages)
    assert all(interface.get("vlans") for interface in switch_interfaces if interface["name"] != "vmnic7")


def test_port_mapping_prefers_matching_vlan_shape_over_port_order():
    edge = {
        "interfaces": [
            {
                "name": "eth0",
                "logical_name": "LAN1",
                "logical_interface": "GE1",
                "link": "mixed-link",
                "mode": "switched",
                "vlans": [1, 100],
            },
            {
                "name": "eth1",
                "logical_name": "LAN2",
                "logical_interface": "GE2",
                "link": "untagged-link",
                "mode": "switched",
                "vlans": [1],
            },
        ],
        "vlans": [
            {"segment_name": "Global Segment", "vlan": 1},
            {"segment_name": "segment1", "vlan": 100},
        ],
    }
    hardware = HardwareEdge.model_validate(
        {
            "id": "shape-match-640",
            "display_name": "shape-match-640",
            "model": "edge640",
            "model_suffix": "640",
            "active_serial": "SERIAL1",
            "switch": {
                "name": "switch1",
                "connections": {"ip": "10.0.0.1"},
            },
            "ports": [
                {
                    "logical_name": "GE1",
                    "name": "ge1",
                    "logical_interface": "GE1",
                    "link": "shape-match_ge1",
                    "switch_active_port": "Gi1/0/1",
                    "switch_vlans": [165],
                    "tagged_vlans": [],
                    "untagged_vlan": 165,
                },
                {
                    "logical_name": "GE2",
                    "name": "ge2",
                    "logical_interface": "GE2",
                    "link": "shape-match_ge2",
                    "switch_active_port": "Gi1/0/2",
                    "switch_vlans": [162, 163],
                    "tagged_vlans": [163],
                    "untagged_vlan": 162,
                },
            ],
        }
    )

    _apply_port_mappings(edge, hardware, "branch1", "b1-edge2")

    assert edge["interfaces"][0]["logical_interface"] == "GE2"
    assert edge["interfaces"][0]["name"] == "ge2"
    assert edge["interfaces"][0]["vlans"] == [1, 163]
    assert edge["interfaces"][1]["logical_interface"] == "GE1"
    assert edge["interfaces"][1]["name"] == "ge1"
    assert edge["interfaces"][1]["vlans"] == [1]


def test_port_mapping_respects_manual_interface_overrides():
    edge = {
        "interfaces": [
            {
                "name": "eth0",
                "logical_name": "LAN1",
                "logical_interface": "GE1",
                "link": "mixed-link",
                "mode": "switched",
                "vlans": [1, 100],
            },
            {
                "name": "eth1",
                "logical_name": "LAN2",
                "logical_interface": "GE2",
                "link": "untagged-link",
                "mode": "switched",
                "vlans": [1],
            },
        ],
        "vlans": [
            {"segment_name": "Global Segment", "vlan": 1},
            {"segment_name": "segment1", "vlan": 100},
        ],
    }
    hardware = HardwareEdge.model_validate(
        {
            "id": "shape-match-640",
            "display_name": "shape-match-640",
            "model": "edge640",
            "model_suffix": "640",
            "active_serial": "SERIAL1",
            "switch": {
                "name": "switch1",
                "connections": {"ip": "10.0.0.1"},
            },
            "ports": [
                {
                    "logical_name": "GE1",
                    "name": "ge1",
                    "logical_interface": "GE1",
                    "link": "shape-match_ge1",
                    "switch_active_port": "Gi1/0/1",
                    "switch_vlans": [165],
                    "tagged_vlans": [],
                    "untagged_vlan": 165,
                },
                {
                    "logical_name": "GE2",
                    "name": "ge2",
                    "logical_interface": "GE2",
                    "link": "shape-match_ge2",
                    "switch_active_port": "Gi1/0/2",
                    "switch_vlans": [162, 163],
                    "tagged_vlans": [163],
                    "untagged_vlan": 162,
                },
            ],
        }
    )

    allocation = _apply_port_mappings(
        edge,
        hardware,
        "branch1",
        "b1-edge2",
        [
            InterfaceOverride(reference_interface="GE1", hardware_interface="GE1"),
            InterfaceOverride(reference_interface="GE2", hardware_interface="GE2"),
        ],
    )

    assert edge["interfaces"][0]["logical_interface"] == "GE1"
    assert edge["interfaces"][0]["name"] == "ge1"
    assert edge["interfaces"][1]["logical_interface"] == "GE2"
    assert edge["interfaces"][1]["name"] == "ge2"


def test_port_mapping_keeps_loopback_interfaces_unmapped():
    edge = {
        "interfaces": [
            {
                "name": "lo",
                "logical_interface": "lo",
                "link": "loopback-link",
                "type": "loopback",
                "ip": "1.1.1.1",
            },
            {
                "name": "eth0",
                "logical_name": "LAN1",
                "logical_interface": "GE1",
                "link": "lan1-link",
                "mode": "switched",
                "vlans": [1],
            },
            {
                "name": "eth1",
                "logical_name": "LAN2",
                "logical_interface": "GE2",
                "link": "lan2-link",
                "mode": "switched",
                "vlans": [1],
            },
        ],
        "vlans": [{"segment_name": "Global Segment", "vlan": 1}],
    }
    hardware = HardwareEdge.model_validate(
        {
            "id": "loopback-safe-640",
            "display_name": "loopback-safe-640",
            "model": "edge640",
            "model_suffix": "640",
            "active_serial": "SERIAL1",
            "switch": {
                "name": "switch1",
                "connections": {"ip": "10.0.0.1"},
            },
            "ports": [
                {
                    "logical_name": "GE1",
                    "name": "ge1",
                    "logical_interface": "GE1",
                    "link": "shape-match_ge1",
                    "switch_active_port": "Gi1/0/1",
                    "switch_vlans": [165],
                    "tagged_vlans": [],
                    "untagged_vlan": 165,
                },
                {
                    "logical_name": "GE2",
                    "name": "ge2",
                    "logical_interface": "GE2",
                    "link": "shape-match_ge2",
                    "switch_active_port": "Gi1/0/2",
                    "switch_vlans": [166],
                    "tagged_vlans": [],
                    "untagged_vlan": 166,
                },
            ],
        }
    )

    _apply_port_mappings(edge, hardware, "branch1", "b1-edge2")

    assert len(edge["interfaces"]) == 3
    assert edge["interfaces"][0]["name"] == "lo"
    assert edge["interfaces"][0]["logical_interface"] == "lo"
    assert edge["interfaces"][0]["link"] == "loopback-link"
    assert edge["interfaces"][1]["logical_interface"] == "GE1"
    assert edge["interfaces"][1]["name"] == "ge1"
    assert edge["interfaces"][2]["logical_interface"] == "GE2"
    assert edge["interfaces"][2]["name"] == "ge2"


def test_dynamic_vlan_allocations_are_reserved_and_reused():
    edge = {
        "interfaces": [
            {
                "name": "eth0",
                "logical_name": "LAN1",
                "logical_interface": "GE1",
                "link": "lan1-link",
                "mode": "switched",
                "vlans": [1, 100],
            },
            {
                "name": "eth1",
                "logical_name": "LAN2",
                "logical_interface": "GE2",
                "link": "lan2-link",
                "mode": "switched",
                "vlans": [1],
            },
        ],
        "vlans": [
            {"segment_name": "Global Segment", "vlan": 1},
            {"segment_name": "segment1", "vlan": 100},
        ],
    }
    inventory = build_inventory(
        {
            "edge-1-active": {
                "id": "edge-1-active",
                "type": "edge",
                "display_name": "Edge 1",
                "model": "edge6X0",
                "model_suffix": "680",
                "serial_number": "SERIAL1",
                "ha_group_id": "edge-1",
                "ha_role": "active",
                "vlan_range": {"start": 200, "end": 210},
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
                "id": "edge-1-ge1",
                "a": {"device_id": "edge-1-active", "interface": "GE1"},
                "b": {"device_id": "switch-1", "interface": "Gi1/1"},
                "role": "edge-access",
            },
            {
                "id": "edge-1-ge2",
                "a": {"device_id": "edge-1-active", "interface": "GE2"},
                "b": {"device_id": "switch-1", "interface": "Gi1/2"},
                "role": "edge-access",
            },
        ],
        [],
    )
    hardware = inventory.hardware[0]

    _remote_updates, _dropped_links, _messages, allocation = _apply_port_mappings(
        edge,
        hardware,
        "branch1",
        "edge1",
        inventory=inventory,
        reference_topology_id="3-site",
        reference_branch_name="branch1",
        reference_edge_name="edge1",
    )

    assert allocation.reserved_vlans == [200, 201, 202]
    assert edge["interfaces"][0]["vlans"] == [1, 201]
    assert edge["interfaces"][1]["vlans"] == [1]
    assert inventory.allocations == []

    repeated_edge = json.loads(json.dumps(edge))
    _remote_updates, _dropped_links, _messages, repeated_allocation = _apply_port_mappings(
        repeated_edge,
        hardware,
        "branch1",
        "edge1",
        inventory=inventory,
        reference_topology_id="3-site",
        reference_branch_name="branch1",
        reference_edge_name="edge1",
    )

    assert repeated_allocation.reserved_vlans == [200, 201, 202]
    assert inventory.allocations == []


def test_manual_vlan_override_replaces_allocator_for_one_interface():
    edge = {
        "interfaces": [
            {
                "name": "eth0",
                "logical_name": "LAN1",
                "logical_interface": "GE1",
                "link": "lan1-link",
                "mode": "switched",
                "vlans": [1, 100],
            },
            {
                "name": "eth1",
                "logical_name": "LAN2",
                "logical_interface": "GE2",
                "link": "lan2-link",
                "mode": "switched",
                "vlans": [1],
            },
        ],
        "vlans": [
            {"segment_name": "Global Segment", "vlan": 1},
            {"segment_name": "segment1", "vlan": 100},
        ],
    }
    inventory = build_inventory(
        {
            "edge-1-active": {
                "id": "edge-1-active",
                "type": "edge",
                "display_name": "Edge 1",
                "model": "edge6X0",
                "model_suffix": "680",
                "serial_number": "SERIAL1",
                "ha_group_id": "edge-1",
                "ha_role": "active",
                "vlan_range": {"start": 200, "end": 210},
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
                "id": "edge-1-ge1",
                "a": {"device_id": "edge-1-active", "interface": "GE1"},
                "b": {"device_id": "switch-1", "interface": "Gi1/1"},
                "role": "edge-access",
            },
            {
                "id": "edge-1-ge2",
                "a": {"device_id": "edge-1-active", "interface": "GE2"},
                "b": {"device_id": "switch-1", "interface": "Gi1/2"},
                "role": "edge-access",
            },
        ],
        [],
    )
    hardware = inventory.hardware[0]

    _remote_updates, _dropped_links, _messages, allocation = _apply_port_mappings(
        edge,
        hardware,
        "branch1",
        "edge1",
        [
            InterfaceOverride(reference_interface="GE1", hardware_interface="GE1", switch_vlans=[205, 206]),
        ],
        inventory=inventory,
        reference_topology_id="3-site",
        reference_branch_name="branch1",
        reference_edge_name="edge1",
    )

    assert edge["interfaces"][0]["vlans"] == [1, 206]
    assert edge["interfaces"][1]["vlans"] == [1]
    assert allocation.reserved_vlans == [200, 205, 206]
    assert allocation.ports[0].switch_vlans == [205, 206]


def test_switch_only_interface_auto_allocates_native_vlan_for_dynamic_port():
    edge = {
        "interfaces": [
            {
                "name": "eth2",
                "logical_name": "INTERNET1",
                "logical_interface": "GE3",
                "link": "internet1-link",
            },
        ],
        "vlans": [],
    }
    inventory = build_inventory(
        {
            "edge-1-active": {
                "id": "edge-1-active",
                "type": "edge",
                "display_name": "Edge 1",
                "model": "edge6X0",
                "model_suffix": "680",
                "serial_number": "SERIAL1",
                "ha_group_id": "edge-1",
                "ha_role": "active",
                "vlan_range": {"start": 200, "end": 210},
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
                "id": "edge-1-ge3",
                "a": {"device_id": "edge-1-active", "interface": "GE3"},
                "b": {"device_id": "switch-1", "interface": "Gi1/3"},
                "role": "edge-access",
                "vlans": [],
                "tagged_vlans": [],
                "untagged_vlan": None,
            },
        ],
        [],
    )
    hardware = inventory.hardware[0]

    _remote_updates, _dropped_links, _messages, allocation = _apply_port_mappings(
        edge,
        hardware,
        "branch1",
        "edge1",
        inventory=inventory,
        reference_topology_id="3-site",
        reference_branch_name="branch1",
        reference_edge_name="edge1",
    )

    assert allocation.reserved_vlans == [200]
    assert allocation.ports[0].switch_vlans == [200]
    assert allocation.ports[0].untagged_vlan == 200

    l2_switches = _build_l2_switches(hardware, "10.0.0.50", "vmnic0")
    assert "os_family" not in l2_switches[0]
    assert l2_switches[0]["interfaces"][0]["name"] == "Gi1/3"
    assert l2_switches[0]["interfaces"][0]["vlans"] == [200]


def test_private_wanlink_name_tracks_mapped_logical_interface():
    edge = {
        "interfaces": [
            {
                "name": "sfp2",
                "logical_name": "INTERNET4",
                "logical_interface": "GE6",
                "link": "b1ce1",
                "mode": "routed",
                "nexthop": "b1-ce1",
                "addressing_type": "static",
                "ip": "172.16.1.10",
                "netmask": "255.255.255.248",
                "default_gateway": "172.16.1.11",
                "type": "private",
                "wan_overlay": "user_defined",
                "wanlink": {
                    "name": "GE6_Private",
                    "link_type": "private",
                },
            },
        ],
        "vlans": [],
    }
    hardware = HardwareEdge.model_validate(
        {
            "id": "edge-sfp2",
            "display_name": "edge-sfp2",
            "model": "edge6X0",
            "model_suffix": "680",
            "ha": False,
            "active_serial": "SERIAL1",
            "switch": {
                "name": "switch1",
                "connections": {"ip": "10.0.0.1"},
            },
            "ports": [
                {
                    "logical_name": "INTERNET4",
                    "name": "sfp2",
                    "logical_interface": "SFP2",
                    "link": "edge-sfp2",
                    "switch_active_port": "Gi1/6",
                }
            ],
        }
    )

    _apply_port_mappings(edge, hardware, "branch1", "edge1")

    assert edge["interfaces"][0]["logical_interface"] == "SFP2"
    assert edge["interfaces"][0]["wanlink"]["name"] == "SFP2_Private"


def test_build_l2_switches_uses_resolved_switch_uplink_for_hypervisor_interface_name():
    hardware = HardwareEdge.model_validate(
        {
            "id": "edge-1",
            "display_name": "edge-1",
            "model": "edge6X0",
            "model_suffix": "680",
            "ha": True,
            "active_serial": "SERIAL1",
            "standby_serial": "SERIAL2",
            "switch": {
                "name": "switch1",
                "connections": {"ip": "10.0.0.1"},
            },
            "ports": [
                {
                    "logical_name": "GE1",
                    "name": "ge1",
                    "logical_interface": "GE1",
                    "link": "edge1-ge1",
                    "switch_name": "switch1",
                    "switch_active_port": "Gi1/3",
                    "switch_vlans": [200],
                    "tagged_vlans": [],
                    "untagged_vlan": 200,
                }
            ],
        }
    )

    l2_switches = _build_l2_switches(hardware, "10.0.0.50", "vmnic0", "Te1/51")

    assert l2_switches[0]["interfaces"][-1]["name"] == "Te1/51"
    assert l2_switches[0]["interfaces"][-1]["link"] == "vmnic0"


def test_build_l2_switches_excludes_unallocated_hardware_ports():
    hardware = HardwareEdge.model_validate(
        {
            "id": "edge-1",
            "display_name": "edge-1",
            "model": "edge6X0",
            "model_suffix": "680",
            "ha": True,
            "active_serial": "SERIAL1",
            "standby_serial": "SERIAL2",
            "switch": {
                "name": "switch1",
                "connections": {"ip": "10.0.0.1"},
            },
            "ports": [
                {
                    "logical_name": "B1E1W4",
                    "name": "ge4",
                    "logical_interface": "GE4",
                    "link": "B1E1W4",
                    "switch_name": "switch1",
                    "switch_active_port": "gigabitethernet1/3",
                    "switch_standby_port": "gigabitethernet1/8",
                    "switch_vlans": [105],
                    "tagged_vlans": [],
                    "untagged_vlan": 105,
                },
                {
                    "logical_name": "GE5",
                    "name": "ge5",
                    "logical_interface": "GE5",
                    "link": "ln_ha_a01_327_dgd10q2_a01_328_16c10q2_ge5",
                    "switch_name": "switch1",
                    "switch_active_port": "gigabitethernet1/3",
                    "switch_vlans": [105],
                    "tagged_vlans": [],
                    "untagged_vlan": 105,
                },
            ],
        }
    )

    l2_switches = _build_l2_switches(
        hardware,
        "10.0.0.50",
        "vmnic0",
        included_ports={"GE4"},
    )

    switch_interfaces = l2_switches[0]["interfaces"]
    assert [interface["link"] for interface in switch_interfaces[:-1]] == ["B1E1W4", "standby_B1E1W4"]
    assert all(interface["link"] != "ln_ha_a01_327_dgd10q2_a01_328_16c10q2_ge5" for interface in switch_interfaces)


def test_switch_only_interface_manual_override_accepts_single_native_vlan():
    edge = {
        "interfaces": [
            {
                "name": "eth2",
                "logical_name": "INTERNET1",
                "logical_interface": "GE3",
                "link": "internet1-link",
            },
        ],
        "vlans": [],
    }
    inventory = build_inventory(
        {
            "edge-1-active": {
                "id": "edge-1-active",
                "type": "edge",
                "display_name": "Edge 1",
                "model": "edge6X0",
                "model_suffix": "680",
                "serial_number": "SERIAL1",
                "ha_group_id": "edge-1",
                "ha_role": "active",
                "vlan_range": {"start": 200, "end": 210},
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
                "id": "edge-1-ge3",
                "a": {"device_id": "edge-1-active", "interface": "GE3"},
                "b": {"device_id": "switch-1", "interface": "Gi1/3"},
                "role": "edge-access",
                "vlans": [],
                "tagged_vlans": [],
                "untagged_vlan": None,
            },
        ],
        [],
    )
    hardware = inventory.hardware[0]

    _remote_updates, _dropped_links, _messages, allocation = _apply_port_mappings(
        edge,
        hardware,
        "branch1",
        "edge1",
        [InterfaceOverride(reference_interface="GE3", hardware_interface="GE3", switch_vlans=[205])],
        inventory=inventory,
        reference_topology_id="3-site",
        reference_branch_name="branch1",
        reference_edge_name="edge1",
    )

    assert allocation.reserved_vlans == [205]
    assert allocation.ports[0].switch_vlans == [205]
    assert allocation.ports[0].untagged_vlan == 205


def test_switch_only_interface_tags_remote_peer_with_same_link():
    edge = {
        "name": "edge1",
        "interfaces": [
            {
                "name": "eth2",
                "logical_name": "INTERNET1",
                "logical_interface": "GE3",
                "link": "internet1-link",
                "mode": "routed",
            },
        ],
        "vlans": [],
    }
    inventory = build_inventory(
        {
            "edge-1-active": {
                "id": "edge-1-active",
                "type": "edge",
                "display_name": "Edge 1",
                "model": "edge6X0",
                "model_suffix": "680",
                "serial_number": "SERIAL1",
                "ha_group_id": "edge-1",
                "ha_role": "active",
                "vlan_range": {"start": 200, "end": 210},
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
                "id": "edge-1-ge3",
                "a": {"device_id": "edge-1-active", "interface": "GE3"},
                "b": {"device_id": "switch-1", "interface": "Gi1/3"},
                "role": "edge-access",
                "vlans": [],
                "tagged_vlans": [],
                "untagged_vlan": None,
            },
        ],
        [],
    )
    hardware = inventory.hardware[0]
    config = {
        "topology": {
            "branches": [
                {
                    "name": "branch1",
                    "edges": [edge],
                }
            ]
        },
        "routers": [
            {
                "name": "cr-internet1",
                "interfaces": [
                    {
                        "name": "eth9",
                        "link": "internet1-link",
                        "mode": "routed",
                    }
                ],
            }
        ],
    }

    remote_updates, _dropped_links, _messages, _allocation = _apply_port_mappings(
        edge,
        hardware,
        "branch1",
        "edge1",
        inventory=inventory,
        reference_topology_id="3-site",
        reference_branch_name="branch1",
        reference_edge_name="edge1",
    )
    _apply_remote_updates_to_config(config, edge, remote_updates)

    remote_interface = config["routers"][0]["interfaces"][0]
    assert remote_interface["name"] == "eth9.200"
    assert remote_interface["link"] == "internet1-link"


def test_generate_resolves_switch_config_path_from_generation_hypervisor_ip(tmp_path):
    with INVENTORY_PATH.open() as fh:
        inventory = json.load(fh)

    inventory["devices"]["upstream_switch"] = {
        "id": "upstream_switch",
        "type": "switch",
        "display_name": "a02-agg-switch",
        "model": "Dell-4048",
        "ip_address": "10.68.137.146",
        "switch_metadata": {
            "name": "a02-agg-switch",
            "model": "Dell-4048",
            "connections": {"ip": "10.68.137.146", "port": None},
            "credentials": {"username": "velocloud", "password": "N#1sdwan"},
        },
    }
    inventory["devices"]["esxi_01"] = {
        "id": "esxi_01",
        "type": "hypervisor",
        "display_name": "esxi-01",
        "model": "Dell-R640",
        "ip_address": "10.68.136.221",
    }
    inventory["connections"].extend(
        [
            {
                "id": "b2e1-upstream",
                "a": {"device_id": "b2e1_l2_switch", "interface": "Te1/51"},
                "b": {"device_id": "upstream_switch", "interface": "Te1/43"},
                "role": "switch-uplink",
            },
            {
                "id": "upstream-hypervisor",
                "a": {"device_id": "upstream_switch", "interface": "Te1/10"},
                "b": {"device_id": "esxi_01", "interface": "vmnic0"},
                "role": "hypervisor-access",
            },
        ]
    )
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(json.dumps(inventory))

    result = generate_topology(
        make_request(hypervisor_ip="10.68.136.221", hypervisor_interface="vmnic0"),
        inventory_path=inventory_path,
        outputs_root=tmp_path,
    )

    assert result.can_configure_switches is True
    assert result.mapping_statuses[0].path_resolved is True
    assert result.mapping_statuses[0].auto_config_ready is True
    config = load_config(result)
    branch = next(item for item in config["topology"]["branches"] if item["name"] == "branch2")
    edge = branch["edges"][0]
    assert edge["l2_switches"][0]["interfaces"][-1]["name"] == "Te1/51"
    assert edge["l2_switches"][0]["interfaces"][-1]["link"] == "vmnic0"
    metadata_path = Path(result.topology_path).parent / "run_metadata.json"
    metadata = json.loads(metadata_path.read_text())
    assert metadata["mappings"][0]["path"]["access_switch_id"] == "b2e1_l2_switch"
    assert metadata["mappings"][0]["path"]["hypervisor_ip"] == "10.68.136.221"
    assert metadata["mappings"][0]["path"]["upstream_hypervisor_port"] == "Te1/10"


def test_resolve_mapping_path_uses_hypervisor_interface_to_disambiguate_links():
    inventory = build_inventory(
        {
            "access_sw": {
                "id": "access_sw",
                "type": "switch",
                "display_name": "chn-rnd-sw-3048-J8Y00Q2",
                "model": "Dell-3048",
                "ip_address": "10.68.136.28",
            },
            "upstream_sw": {
                "id": "upstream_sw",
                "type": "switch",
                "display_name": "chn-rnd-sw-4148-F19CV43",
                "model": "Dell-4148",
                "ip_address": "10.68.137.247",
            },
            "hypervisor": {
                "id": "hypervisor",
                "type": "hypervisor",
                "display_name": "chn-rnd-srv-650-G1HVKJ3",
                "model": "Dell-R650",
                "ip_address": "10.68.137.104",
            },
        },
        [
            {
                "id": "access-upstream",
                "a": {"device_id": "access_sw", "interface": "tengigabitethernet1/52"},
                "b": {"device_id": "upstream_sw", "interface": "eth1/1/53"},
                "role": "switch-uplink",
            },
            {
                "id": "hypervisor-vmnic2",
                "a": {"device_id": "upstream_sw", "interface": "eth1/1/54"},
                "b": {"device_id": "hypervisor", "interface": "vmnic2"},
                "role": "hypervisor-access",
            },
            {
                "id": "hypervisor-vmnic3",
                "a": {"device_id": "upstream_sw", "interface": "eth1/1/52"},
                "b": {"device_id": "hypervisor", "interface": "vmnic3"},
                "role": "hypervisor-access",
            },
        ],
    )

    path = resolve_mapping_path(
        inventory,
        ["chn-rnd-sw-3048-J8Y00Q2", "chn-rnd-sw-4148-F19CV43"],
        "10.68.137.104",
        "vmnic2",
    )

    assert path is not None
    assert path.access_switch_name == "chn-rnd-sw-3048-J8Y00Q2"
    assert path.access_uplink_port == "tengigabitethernet1/52"
    assert path.upstream_switch_name == "chn-rnd-sw-4148-F19CV43"
    assert path.upstream_access_port == "eth1/1/53"
    assert path.upstream_hypervisor_port == "eth1/1/54"


def test_generate_resolves_path_when_mapping_spans_access_and_upstream_switches(tmp_path):
    result = generate_topology(
        make_request(
            topology_name="3-site-hw",
            hypervisor_ip="10.68.137.104",
            hypervisor_interface="vmnic2",
            mappings=[
                {
                    "hardware_id": "ln-ha-a03-515-248202197-a03-516-248202193",
                    "branch_name": "branch2",
                    "edge_name": "b2-edge1",
                }
            ],
        ),
        inventory_path=copy_inventory(tmp_path),
        outputs_root=tmp_path,
    )

    assert result.can_configure_switches is True
    assert result.mapping_statuses[0].path_resolved is True
    assert result.mapping_statuses[0].auto_config_ready is True

    metadata_path = Path(result.topology_path).parent / "run_metadata.json"
    metadata = json.loads(metadata_path.read_text())
    path = metadata["mappings"][0]["path"]
    assert path["access_switch_name"] == "chn-rnd-sw-3048-J8Y00Q2"
    assert path["access_uplink_port"] == "tengigabitethernet1/52"
    assert path["upstream_switch_name"] == "chn-rnd-sw-4148-F19CV43"
    assert path["upstream_access_port"] == "eth1/1/53"
    assert path["upstream_hypervisor_port"] == "eth1/1/54"


def test_apply_hardware_to_edge_forces_dpdk_enabled_true():
    edge = {"name": "ref-edge", "ha_enabled": False, "dpdk_enabled": False}
    hardware = HardwareEdge.model_validate(
        {
            "id": "force-dpdk",
            "display_name": "force-dpdk",
            "model": "edge640",
            "model_suffix": "640",
            "ha": True,
            "active_serial": "SERIAL1",
            "standby_serial": "SERIAL2",
            "dpdk_enabled": None,
            "switch": {
                "name": "switch1",
                "connections": {"ip": "10.0.0.1"},
            },
            "ports": [
                {
                    "logical_name": "GE1",
                    "name": "ge1",
                    "logical_interface": "GE1",
                    "link": "force-dpdk_ge1",
                    "switch_active_port": "Gi1/0/1",
                    "switch_vlans": [165],
                    "tagged_vlans": [],
                    "untagged_vlan": 165,
                }
            ],
        }
    )

    _apply_hardware_to_edge(edge, hardware, "b1-edge1-640")

    assert edge["name"] == "b1-edge1-640"
    assert edge["ha_enabled"] is True
    assert edge["dpdk_enabled"] is True
    assert edge["standby_slno"] == "SERIAL2"


def test_standalone_hardware_on_ha_reference_warns_and_drops_extra_interfaces(tmp_path):
    request = make_request(
        topology_name="standalone-caveat",
        mappings=[
            {
                "hardware_id": "ln-a01-318-1kxfxc2",
                "branch_name": "branch2",
                "edge_name": "b2-edge1",
            }
        ],
        hypervisor_interface="vmnic0",
    )
    result = generate_topology(request, inventory_path=copy_inventory(tmp_path), outputs_root=tmp_path)
    config = load_config(result)
    branch = next(item for item in config["topology"]["branches"] if item["name"] == "branch2")
    edge = branch["edges"][0]
    messages = [item.message for item in result.messages]

    assert edge["ha_enabled"] is False
    assert "standby_slno" not in edge
    assert len(edge["interfaces"]) == 6
    assert any(interface.get("type") == "loopback" for interface in edge["interfaces"])
    assert any("converts it to standalone" in message for message in messages)
    assert any("reference edge has 8 physical interface(s)" in message for message in messages)
    assert any("Dropped 3 unassigned reference interface" in message for message in messages)


def test_duplicate_hardware_mapping_rejected():
    request = make_request(
        mappings=[
            {
                "hardware_id": "chn-3800-8-ha",
                "branch_name": "branch1",
                "edge_name": "b1-edge1",
            },
            {
                "hardware_id": "chn-3800-8-ha",
                "branch_name": "branch2",
                "edge_name": "b2-edge1",
            },
        ]
    )
    with pytest.raises(GenerationError, match="hardware inventory item"):
        generate_topology(request)


def test_duplicate_target_edge_rejected():
    request = make_request(
        mappings=[
            {
                "hardware_id": "chn-3800-8-ha",
                "branch_name": "branch2",
                "edge_name": "b2-edge1",
            },
            {
                "hardware_id": "chn-680-5-ha",
                "branch_name": "branch2",
                "edge_name": "b2-edge1",
            },
        ]
    )
    with pytest.raises(GenerationError, match="target branch/edge"):
        generate_topology(request)
