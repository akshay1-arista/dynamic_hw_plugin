from pathlib import Path

from fastapi.testclient import TestClient

from app import main as app_main
from app.config import INVENTORY_PATH
from app.generator import generate_topology as real_generate_topology
from app.inventory import load_inventory, save_inventory, save_inventory_hardware_edits as real_save_inventory_hardware_edits
from app.main import app


client = TestClient(app)


def test_reference_topologies_include_nested_id():
    response = client.get("/api/reference-topologies")
    assert response.status_code == 200
    ids = {item["id"] for item in response.json()}
    assert "3-site/spirent" in ids
    assert "3-site-scale/spirent" in ids
    assert "5-site-cluster/hitless" in ids


def test_hardware_inventory_endpoint():
    response = client.get("/api/hardware")
    assert response.status_code == 200
    data = response.json()
    assert any(item["id"] == "ln-ha-a01-327-dgd10q2-a01-328-16c10q2" for item in data["hardware"])


def test_a02_720_pair_is_still_visible_without_imported_connections():
    inventory = load_inventory()
    hardware = next(
        item
        for item in inventory.hardware
        if item.id == "ln-ha-a02-312-246218457-a02-313-246218453"
    )

    assert hardware.ha is True
    assert hardware.active_serial == "246218457"
    assert hardware.standby_serial == "246218453"
    if not hardware.ports:
        assert hardware.switch is None
        assert hardware.switches == []


def test_a02_710_pair_is_still_visible_without_imported_connections():
    inventory = load_inventory()
    hardware = next(
        item
        for item in inventory.hardware
        if item.id == "ln-ha-a02-314-236254370-a02-315-236254372"
    )

    assert hardware.ha is True
    assert hardware.active_serial == "236254370"
    assert hardware.standby_serial == "236254372"
    if not hardware.ports:
        assert hardware.switch is None
        assert hardware.switches == []


def test_a01_3800_pair_keeps_standby_connection_for_ge5():
    inventory = load_inventory()
    hardware = next(
        item
        for item in inventory.hardware
        if item.id == "ln-ha-a01-327-dgd10q2-a01-328-16c10q2"
    )

    ports = {port.logical_interface: port for port in hardware.ports}
    assert hardware.active_serial == "DGD10Q2"
    assert hardware.standby_serial == "16C10Q2"
    assert ports["GE5"].switch_standby_port == "gigabitethernet1/25"


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


def test_save_inventory_route_preserves_existing_graph(tmp_path, monkeypatch):
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
      "free_vlans": [101, 102, 103],
      "vlan_range": {"start": 101, "end": 103}
    },
    "edge-2": {
      "id": "edge-2",
      "type": "edge",
      "display_name": "Edge 2",
      "model": "edge5X0",
      "model_suffix": "520",
      "serial_number": "SERIAL2",
      "ha_group_id": "edge-2",
      "ha_role": "active",
      "free_vlans": [201, 202, 203],
      "vlan_range": {"start": 201, "end": 203}
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
    },
    {
      "id": "edge-2-ge1-switch-1",
      "a": {"device_id": "edge-2", "interface": "GE1"},
      "b": {"device_id": "switch-1", "interface": "Gi1/2"},
      "role": "edge-access"
    }
  ],
  "allocations": []
}
""".strip()
    )

    inventory = load_inventory(inventory_path)
    edited_hardware = inventory.hardware[0].model_dump(mode="json")
    edited_hardware["vlan_range"] = {"start": 111, "end": 113}

    def save_with_temp_inventory(request_inventory, **kwargs):
        return real_save_inventory_hardware_edits(request_inventory, inventory_path, **kwargs)

    monkeypatch.setattr(app_main, "save_inventory_hardware_edits", save_with_temp_inventory)

    response = client.put(
        "/api/hardware",
        json={
            "inventory": {
                "devices": {},
                "connections": [],
                "allocations": [],
                "hardware": [edited_hardware],
            },
            "requested_by": {"name": "Test User", "email": "test@example.com"},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["hardware"]) == 2

    persisted = load_inventory(inventory_path)
    assert len(persisted.hardware) == 2
    assert len(persisted.connections) == 2
    edge_1 = persisted.devices["edge-1"]
    edge_2 = persisted.devices["edge-2"]
    assert edge_1.vlan_range.model_dump(mode="json") == {"start": 111, "end": 113}
    assert edge_2.vlan_range.model_dump(mode="json") == {"start": 201, "end": 203}


def test_invalid_reference_generation_rejected():
    response = client.post(
        "/api/generate",
        json={
            "topology_name": "bad-ref",
            "reference_topology_id": "not-allowlisted",
            "hypervisor_ip": "10.1.1.1",
            "hypervisor_interface": "vmnic0",
            "requested_by": {
                "name": "Test User",
                "email": "test@example.com",
            },
            "mappings": [
                {
                    "hardware_id": "ln-ha-a01-327-dgd10q2-a01-328-16c10q2",
                    "branch_name": "branch2",
                    "edge_name": "b2-edge1",
                }
            ],
        },
    )
    assert response.status_code == 400


def test_generate_and_download_zip(tmp_path, monkeypatch):
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(Path(INVENTORY_PATH).read_text())
    inventory = load_inventory(inventory_path)
    target = next(
        item
        for item in inventory.hardware
        if item.id == "ln-ha-a01-327-dgd10q2-a01-328-16c10q2"
    )
    target.available = True
    target.reservation = None
    save_inventory(inventory, inventory_path)
    outputs_root = tmp_path / "outputs"
    outputs_root.mkdir()

    def generate_with_temp_inventory(request):
        return real_generate_topology(request, inventory_path=inventory_path, outputs_root=outputs_root)

    monkeypatch.setattr(app_main, "generate_topology", generate_with_temp_inventory)
    monkeypatch.setattr(app_main, "OUTPUTS_ROOT", outputs_root)
    response = client.post(
        "/api/generate",
        json={
            "topology_name": "api-3-site-3800",
            "reference_topology_id": "3-site",
            "hypervisor_ip": "10.68.136.50",
            "hypervisor_interface": "vmnic0",
            "requested_by": {
                "name": "Test User",
                "email": "test@example.com",
            },
            "mappings": [
                {
                    "hardware_id": "ln-ha-a01-327-dgd10q2-a01-328-16c10q2",
                    "branch_name": "branch2",
                    "edge_name": "b2-edge1",
                }
            ],
        },
    )
    assert response.status_code == 200
    result = response.json()
    assert Path(result["topology_path"]).name == result["topology_name"]
    assert Path(result["zip_path"]).name == f'{result["topology_name"]}.zip'
    suffix = result["topology_name"].rsplit("-", 1)[-1]
    assert Path(result["topology_path"]).parent.name.endswith(f"-{suffix}")
    download = client.get(result["download_url"])
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/zip"


def test_commit_hapy_route(monkeypatch):
    monkeypatch.setattr(
        app_main,
        "publish_run_private_branch",
        lambda run_id, request: {
            "run_id": run_id,
            "topology_name": "demo-topology",
            "reference_topology_id": "3-site",
            "repo_path": "/repo/velocloud.src",
            "destination_path": "/repo/velocloud.src/hapy/hapy/testbed/configs/demo-topology",
            "destination_relative_path": "demo-topology",
            "base_branch": request.base_branch,
            "private_branch_name": "hw_topo_gen_private_run123",
            "commit_sha": "deadbeef",
            "commit_message": "VLDT-None: add topology demo-topology",
            "private_branch_pushed": True,
            "remote_name": "origin",
            "remote_branch_ref": "refs/heads/hw_topo_gen_private_run123",
            "fetch_command": "git fetch origin refs/heads/hw_topo_gen_private_run123 && git checkout -b hw_topo_gen_private_run123 FETCH_HEAD",
            "created_by": {"name": "Test User", "email": "test@example.com"},
            "pushed_by": {"name": "Test User", "email": "test@example.com"},
            "created_at": "2026-07-11T00:00:00+00:00",
            "updated_at": "2026-07-11T00:01:00+00:00",
            "messages": [{"level": "info", "message": "Committed and pushed private branch."}],
        },
    )

    response = client.post("/api/runs/run123/publish-private-branch", json={"base_branch": "release_6.4"})

    assert response.status_code == 200
    body = response.json()
    assert body["base_branch"] == "release_6.4"
    assert body["private_branch_name"] == "hw_topo_gen_private_run123"
    assert body["created_by"]["email"] == "test@example.com"
    assert body["pushed_by"]["email"] == "test@example.com"


def test_list_private_branches_route(monkeypatch):
    monkeypatch.setattr(
        app_main,
        "list_private_branches",
        lambda: {
            "branches": [
                {
                    "run_id": "run123",
                    "topology_name": "demo-topology",
                    "reference_topology_id": "3-site",
                    "repo_path": "/repo/velocloud.src",
                    "destination_path": "/repo/velocloud.src/hapy/hapy/testbed/configs/demo-topology",
                    "destination_relative_path": "demo-topology",
                    "base_branch": "master",
                    "private_branch_name": "hw_topo_gen_private_run123",
                    "commit_sha": "deadbeef",
                    "commit_message": "VLDT-None: add topology demo-topology",
                    "private_branch_pushed": False,
                    "remote_name": "origin",
                    "remote_branch_ref": None,
                    "fetch_command": None,
                    "created_by": {"name": "Test User", "email": "test@example.com"},
                    "pushed_by": None,
                    "created_at": "2026-07-11T00:00:00+00:00",
                    "updated_at": "2026-07-11T00:00:00+00:00",
                }
            ]
        },
    )

    response = client.get("/api/hapy/private-branches")

    assert response.status_code == 200
    assert response.json()["branches"][0]["private_branch_name"] == "hw_topo_gen_private_run123"
    assert response.json()["branches"][0]["created_by"]["email"] == "test@example.com"


def test_list_saved_runs_route(monkeypatch):
    monkeypatch.setattr(
        app_main,
        "list_saved_runs",
        lambda: {
            "runs": [
                {
                    "run_id": "run123",
                    "topology_name": "demo-topology-a1b2c3",
                    "requested_topology_name": "demo-topology",
                    "reference_topology_id": "3-site",
                    "requested_by": {"name": "Test User", "email": "test@example.com"},
                    "created_at": "2026-07-11T00:00:00+00:00",
                    "updated_at": "2026-07-11T00:01:00+00:00",
                    "private_branch_name": "hw_topo_gen_private_run123",
                    "private_branch_pushed": True,
                }
            ]
        },
    )

    response = client.get("/api/runs")

    assert response.status_code == 200
    body = response.json()
    assert body["runs"][0]["run_id"] == "run123"
    assert body["runs"][0]["requested_topology_name"] == "demo-topology"


def test_load_saved_run_route(monkeypatch):
    monkeypatch.setattr(
        app_main,
        "load_saved_run",
        lambda run_id: {
            "request": {
                "topology_name": "demo-topology",
                "reference_topology_id": "3-site",
                "hypervisor_ip": "10.68.136.50",
                "hypervisor_interface": "vmnic0",
                "mappings": [
                    {
                        "hardware_id": "demo-hw",
                        "branch_name": "branch1",
                        "edge_name": "edge1",
                        "target_branch_name": None,
                        "target_edge_name": "edge1-680",
                        "interface_overrides": [
                            {
                                "reference_interface": "GE1",
                                "hardware_interface": "GE1",
                                "switch_vlans": [1510],
                            }
                        ],
                    }
                ],
            },
            "result": {
                "run_id": run_id,
                "topology_name": "demo-topology-a1b2c3",
                "topology_path": "/tmp/demo-topology-a1b2c3",
                "zip_path": "/tmp/demo-topology-a1b2c3.zip",
                "download_url": f"/api/runs/{run_id}/download",
                "can_configure_switches": True,
                "mapping_statuses": [],
                "messages": [{"level": "info", "message": "Loaded saved topology run."}],
            },
            "publish_result": None,
        },
    )

    response = client.get("/api/runs/run123")

    assert response.status_code == 200
    body = response.json()
    assert body["request"]["hypervisor_interface"] == "vmnic0"
    assert body["result"]["run_id"] == "run123"


def test_hardware_availability_route(monkeypatch):
    monkeypatch.setattr(
        app_main,
        "update_hardware_availability",
        lambda hardware_id, available, requested_by: (
            {
                "devices": {},
                "connections": [],
                "allocations": [],
                "hardware": [
                    {
                        "id": hardware_id,
                        "display_name": "Demo Hardware",
                        "model": "edge6X0",
                        "model_suffix": "680",
                        "active_serial": "SERIAL1",
                        "ha": False,
                        "available": available,
                        "reservation": None,
                        "ports": [
                            {
                                "logical_name": "GE1",
                                "name": "ge1",
                                "logical_interface": "GE1",
                                "link": "demo_ge1",
                                "switch_name": "switch1",
                                "switch_active_port": "Gi1/1",
                                "switch_vlans": [101],
                                "tagged_vlans": [],
                                "untagged_vlan": 101,
                            }
                        ],
                        "switch": {
                            "name": "switch1",
                            "connections": {"ip": "10.0.0.1"},
                        },
                    }
                ],
            },
            [],
        ),
    )

    response = client.post(
        "/api/hardware/demo-hw/availability",
        json={
            "available": True,
            "requested_by": {"name": "Test User", "email": "test@example.com"},
        },
    )

    assert response.status_code == 200
    assert response.json()["hardware"][0]["available"] is True


def test_audit_trail_route(monkeypatch):
    monkeypatch.setattr(
        app_main,
        "list_audit_events",
        lambda: {
            "events": [
                {
                    "id": "audit123",
                    "action": "hardware_reserved",
                    "actor": {"name": "Test User", "email": "test@example.com"},
                    "target_type": "hardware",
                    "target_id": "hw-1",
                    "summary": "Reserved hardware.",
                    "details": {"run_id": "run123"},
                    "created_at": "2026-07-11T00:00:00+00:00",
                }
            ]
        },
    )

    response = client.get("/api/audit-trail")

    assert response.status_code == 200
    assert response.json()["events"][0]["action"] == "hardware_reserved"
