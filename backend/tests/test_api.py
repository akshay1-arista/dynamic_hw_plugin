from fastapi.testclient import TestClient

from app.inventory import load_inventory
from app.main import app


client = TestClient(app)


def test_reference_topologies_include_nested_id():
    response = client.get("/api/reference-topologies")
    assert response.status_code == 200
    ids = {item["id"] for item in response.json()}
    assert "3-site-scale/spirent" in ids
    assert "5-site-cluster/hitless" in ids


def test_hardware_inventory_endpoint():
    response = client.get("/api/hardware")
    assert response.status_code == 200
    data = response.json()
    assert data["hardware"][0]["id"] == "chn-3800-8-ha"


def test_a02_720_pair_is_derived_as_ha():
    inventory = load_inventory()
    hardware = next(
        item
        for item in inventory.hardware
        if item.id == "ln-ha-a02-312-246218457-a02-313-246218453"
    )

    assert hardware.ha is True
    assert hardware.active_serial == "246218457"
    assert hardware.standby_serial == "246218453"

    ports = {port.logical_interface: port for port in hardware.ports}
    assert ports["GE2"].switch_vlans == [372, 373, 374]
    assert ports["GE2"].tagged_vlans == [373, 374]
    assert ports["GE2"].switch_standby_port == "gigabitethernet1/42"
    assert ports["GE5"].switch_vlans == [377, 378, 379]
    assert ports["GE5"].tagged_vlans == [378, 379]
    assert ports["GE5"].switch_standby_port == "gigabitethernet1/45"


def test_a02_710_pair_keeps_no_vlan_sfp1_connection():
    inventory = load_inventory()
    hardware = next(
        item
        for item in inventory.hardware
        if item.id == "ln-ha-a02-314-236254370-a02-315-236254372"
    )

    ports = {port.logical_interface: port for port in hardware.ports}
    assert ports["SFP1"].switch_active_port == "gigabitethernet1/20"
    assert ports["SFP1"].switch_standby_port == "gigabitethernet1/25"
    assert ports["SFP1"].switch_vlans == []
    assert ports["SFP1"].untagged_vlan is None


def test_connected_ports_without_vlans_are_kept_in_inventory(tmp_path):
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(
        """
{
  "devices": {
    "edge-1": {
      "id": "edge-1",
      "type": "edge",
      "display_name": "Edge 1",
      "model": "edge7X0",
      "model_suffix": "720",
      "serial_number": "SERIAL1",
      "ha_group_id": "edge-1",
      "ha_role": "active"
    },
    "switch-1": {
      "id": "switch-1",
      "type": "switch",
      "display_name": "Switch 1",
      "model": "Dell-3048",
      "ip_address": "192.0.2.10"
    }
  },
  "connections": [
    {
      "id": "edge-1-ge1-switch-1",
      "a": {"device_id": "edge-1", "interface": "GE1"},
      "b": {"device_id": "switch-1", "interface": "Gi1/1"},
      "vlans": [101],
      "tagged_vlans": [],
      "untagged_vlan": 101
    },
    {
      "id": "edge-1-ge2-switch-1",
      "a": {"device_id": "edge-1", "interface": "GE2"},
      "b": {"device_id": "switch-1", "interface": "Gi1/2"},
      "vlans": [],
      "tagged_vlans": [],
      "untagged_vlan": null
    }
  ]
}
""".strip()
    )

    hardware = load_inventory(inventory_path).hardware[0]
    ports = {port.logical_interface: port for port in hardware.ports}

    assert set(ports) == {"GE1", "GE2"}
    assert ports["GE1"].switch_vlans == [101]
    assert ports["GE2"].switch_active_port == "Gi1/2"
    assert ports["GE2"].switch_vlans == []


def test_invalid_reference_generation_rejected():
    response = client.post(
        "/api/generate",
        json={
            "topology_name": "bad-ref",
            "reference_topology_id": "not-allowlisted",
            "hypervisor_ip": "10.1.1.1",
            "hypervisor_interface": "vmnic0",
            "mappings": [
                {
                    "hardware_id": "chn-3800-8-ha",
                    "branch_name": "branch2",
                    "edge_name": "b2-edge1",
                }
            ],
        },
    )
    assert response.status_code == 400


def test_generate_and_download_zip():
    response = client.post(
        "/api/generate",
        json={
            "topology_name": "api-3-site-3800",
            "reference_topology_id": "3-site",
            "hypervisor_ip": "10.68.136.50",
            "hypervisor_interface": "vmnic0",
            "mappings": [
                {
                    "hardware_id": "chn-3800-8-ha",
                    "branch_name": "branch2",
                    "edge_name": "b2-edge1",
                }
            ],
        },
    )
    assert response.status_code == 200
    result = response.json()
    download = client.get(result["download_url"])
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/zip"
