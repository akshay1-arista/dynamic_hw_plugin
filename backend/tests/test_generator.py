import json
import re
import zipfile
from pathlib import Path

import pytest

from app.generator import GenerationError, _apply_hardware_to_edge, _apply_port_mappings, generate_topology
from app.config import INVENTORY_PATH
from app.models import GenerateRequest, HardwareEdge, InterfaceOverride


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


def test_generate_3_site_hardware_branch(tmp_path):
    result = generate_topology(make_request(), outputs_root=tmp_path)
    config = load_config(result)
    branch = next(item for item in config["topology"]["branches"] if item["name"] == "branch2")
    edge = branch["edges"][0]

    assert re.fullmatch(r"unit-3-site-3800-[0-9a-f]{6}", config["testbed"]["name"])
    assert config["testbed"]["description"] == "Generated from 3-site topology"
    assert edge["name"] == "b2-edge1-3800"
    assert edge["model"] == "edge3X00"
    assert edge["slno"] == "13WR363"
    assert edge["standby_slno"] == "47YP363"
    assert edge["dpdk_enabled"] is True
    assert edge["vlans"][1]["vlan"] == 1502
    assert edge["interfaces"][4]["logical_interface"] == "SFP1"
    assert edge["interfaces"][4]["subinterfaces"][0]["name"] == "SFP1.1508"
    assert edge["l2_switches"][0]["interfaces"][-1]["link"] == "vmnic7"
    assert "ip" not in edge["l2_switches"][0]["interfaces"][-1]
    assert edge["l2_switches"][0]["interfaces"][-1]["default_gateway"] == "10.68.136.50"
    assert edge["direct_clients"][0]["interfaces"][1]["name"] == "eth1.1501"
    assert edge["direct_clients"][0]["interfaces"][1]["segments"][0]["vlan"] == 1502
    assert branch["l3switches"][0]["interfaces"][1]["name"] == "eth1.1507"
    assert branch["CEs"][0]["interfaces"][2]["name"] == "eth2.1510"
    assert Path(result.zip_path).exists()
    with zipfile.ZipFile(result.zip_path) as archive:
        assert "unit-3-site-3800/config.json" in archive.namelist()


def test_generated_json_files_parse(tmp_path):
    result = generate_topology(make_request(topology_name="parse-check"), outputs_root=tmp_path)
    for path in Path(result.topology_path).rglob("*.json"):
        with path.open() as fh:
            json.load(fh)


def test_connected_ports_without_vlans_are_excluded_from_generated_topology(tmp_path):
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

    assert any("without VLAN metadata" in message and "GE99" in message for message in messages)
    assert all(interface["name"] != "gigabitethernet1/99" for interface in switch_interfaces)
    assert all(interface.get("vlans") != [] for interface in switch_interfaces)


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

    _apply_port_mappings(
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
    result = generate_topology(request, outputs_root=tmp_path)
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
