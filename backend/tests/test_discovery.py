import json

import httpx
import pytest

from app.discovery import DiscoveryError, LabNavigatorClient, apply_inventory_refresh, preview_inventory_refresh
from app.models import InventoryRefreshRequest


class StubLabNavigatorClient:
    def close(self):
        return None

    def search(self, query):
        if query == "10.0.0.10":
            return [{"id": 11, "name": "access-sw", "ip_address": "10.0.0.10", "device_model": "Dell-3048", "device_type": "switch"}]
        if query == "access-sw":
            return [{"id": 11, "name": "access-sw", "ip_address": "10.0.0.10", "device_model": "Dell-3048", "device_type": "switch"}]
        if query == "10.0.0.20":
            return [{"id": 33, "name": "esxi-01", "ip_address": "10.0.0.20", "device_model": "ESXi", "device_type": "server"}]
        if query == "agg-sw":
            return [{"id": 22, "name": "agg-sw", "ip_address": "10.0.0.11", "device_model": "Dell-4048", "device_type": "switch"}]
        if query == "esxi-01":
            return [{"id": 33, "name": "esxi-01", "ip_address": "10.0.0.20", "device_model": "ESXi", "device_type": "server"}]
        return []

    def get_wiremap(self, device_id):
        if device_id == 101:
            return {
                "connections": [
                    {
                        "interface_name": "GE1",
                        "remote_device": {
                            "id": 11,
                            "name": "access-sw",
                            "ip_address": "10.0.0.10",
                            "device_type": "switch",
                            "device_model": "Dell-3048",
                        },
                        "remote_interface_name": "Gi1/10",
                        "remote_vlans": "Untagged: 300|Tagged: 301",
                    }
                ]
            }
        if device_id == 11:
            return {
                "connections": [
                    {
                        "interface_name": "Te1/1",
                        "remote_device": "agg-sw",
                        "remote_interface": "Te1/49",
                    }
                ]
            }
        if device_id == 22:
            return {
                "connections": [
                    {
                        "interface_name": "Te1/49",
                        "remote_device": "access-sw",
                        "remote_interface": "Te1/1",
                    },
                    {
                        "interface_name": "Te1/50",
                        "remote_device": "esxi-01",
                        "remote_interface": "vmnic0",
                    },
                ]
            }
        return {"connections": []}


def test_inventory_refresh_preview_adds_upstream_path(tmp_path):
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(
        json.dumps(
            {
                "devices": {
                    "edge-1-active": {
                        "id": "edge-1-active",
                        "type": "edge",
                        "display_name": "Edge 1",
                        "model": "edge6X0",
                        "model_suffix": "680",
                        "serial_number": "SERIAL1",
                        "lab_navigator_id": 101,
                        "ha_group_id": "edge-1",
                        "ha_role": "active",
                        "hypervisor_ip": "10.0.0.20",
                        "vlan_range": {"start": 200, "end": 210},
                    },
                    "access_sw": {
                        "id": "access_sw",
                        "type": "switch",
                        "display_name": "access-sw",
                        "model": "Dell-3048",
                        "ip_address": "10.0.0.10",
                        "switch_metadata": {
                            "name": "access-sw",
                            "model": "Dell-3048",
                            "connections": {"ip": "10.0.0.10", "port": None},
                            "credentials": {"username": "velocloud", "password": "N#1sdwan"},
                        },
                    },
                },
                "connections": [
                    {
                        "id": "edge-1-ge1-access",
                        "a": {"device_id": "edge-1-active", "interface": "GE1"},
                        "b": {"device_id": "access_sw", "interface": "Gi1/1"},
                        "role": "edge-access",
                    }
                ],
            }
        )
    )

    result = preview_inventory_refresh(
        InventoryRefreshRequest(hardware_ids=["edge-1"]),
        inventory_path=inventory_path,
        client=StubLabNavigatorClient(),
    )

    summaries = [item.summary for item in result.changes]
    assert any("Add switch agg-sw" in summary for summary in summaries)
    assert any("Add hypervisor esxi-01" in summary for summary in summaries)
    assert result.inventory.hardware[0].path_complete is True
    assert result.inventory.hardware[0].path.upstream_switch_name == "agg-sw"


def test_inventory_refresh_apply_persists_graph_updates(tmp_path):
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(
        json.dumps(
            {
                "devices": {
                    "edge-1-active": {
                        "id": "edge-1-active",
                        "type": "edge",
                        "display_name": "Edge 1",
                        "model": "edge6X0",
                        "model_suffix": "680",
                        "serial_number": "SERIAL1",
                        "lab_navigator_id": 101,
                        "ha_group_id": "edge-1",
                        "ha_role": "active",
                        "hypervisor_ip": "10.0.0.20",
                        "vlan_range": {"start": 200, "end": 210},
                    },
                    "access_sw": {
                        "id": "access_sw",
                        "type": "switch",
                        "display_name": "access-sw",
                        "model": "Dell-3048",
                        "ip_address": "10.0.0.10",
                        "switch_metadata": {
                            "name": "access-sw",
                            "model": "Dell-3048",
                            "connections": {"ip": "10.0.0.10", "port": None},
                            "credentials": {"username": "velocloud", "password": "N#1sdwan"},
                        },
                    },
                },
                "connections": [
                    {
                        "id": "edge-1-ge1-access",
                        "a": {"device_id": "edge-1-active", "interface": "GE1"},
                        "b": {"device_id": "access_sw", "interface": "Gi1/1"},
                        "role": "edge-access",
                    }
                ],
            }
        )
    )

    result = apply_inventory_refresh(
        InventoryRefreshRequest(hardware_ids=["edge-1"]),
        inventory_path=inventory_path,
        client=StubLabNavigatorClient(),
    )

    device_ids = set(result.inventory.devices)
    assert "agg_sw" in device_ids
    assert "esxi_01" in device_ids
    assert result.inventory.hardware[0].auto_config_ready is True


def test_inventory_refresh_apply_updates_edge_access_connections_from_wiremap(tmp_path):
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(
        json.dumps(
            {
                "devices": {
                    "edge-1-active": {
                        "id": "edge-1-active",
                        "type": "edge",
                        "display_name": "Edge 1",
                        "model": "edge6X0",
                        "model_suffix": "680",
                        "serial_number": "SERIAL1",
                        "lab_navigator_id": 101,
                        "ha_group_id": "edge-1",
                        "ha_role": "active",
                        "hypervisor_ip": "10.0.0.20",
                    },
                    "access_sw": {
                        "id": "access_sw",
                        "type": "switch",
                        "display_name": "access-sw",
                        "model": "Dell-3048",
                        "ip_address": "10.0.0.10",
                        "switch_metadata": {
                            "name": "access-sw",
                            "model": "Dell-3048",
                            "connections": {"ip": "10.0.0.10", "port": None},
                            "credentials": {"username": "velocloud", "password": "N#1sdwan"},
                        },
                    },
                },
                "connections": [
                    {
                        "id": "ln-edge-1-old-wiremap",
                        "a": {"device_id": "edge-1-active", "interface": "GE1"},
                        "b": {"device_id": "access_sw", "interface": "gigabitethernet1/1"},
                        "role": "edge-access",
                        "notes": "Imported from Lab Navigator wiremap.",
                        "vlans": [200],
                        "tagged_vlans": [],
                        "untagged_vlan": 200,
                    }
                ],
            }
        )
    )

    result = apply_inventory_refresh(
        InventoryRefreshRequest(hardware_ids=["edge-1"]),
        inventory_path=inventory_path,
        client=StubLabNavigatorClient(),
    )

    refreshed_ports = {port.logical_interface: port for port in result.inventory.hardware[0].ports}
    assert refreshed_ports["GE1"].switch_active_port == "gigabitethernet1/10"
    assert refreshed_ports["GE1"].switch_vlans == [300, 301]
    assert refreshed_ports["GE1"].tagged_vlans == [301]
    assert refreshed_ports["GE1"].untagged_vlan == 300
    connection_ids = {connection.id for connection in result.inventory.connections}
    assert "ln-edge-1-old-wiremap" not in connection_ids


def test_inventory_refresh_apply_does_not_require_hypervisor_ip(tmp_path):
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(
        json.dumps(
            {
                "devices": {
                    "edge-1-active": {
                        "id": "edge-1-active",
                        "type": "edge",
                        "display_name": "Edge 1",
                        "model": "edge6X0",
                        "model_suffix": "680",
                        "serial_number": "SERIAL1",
                        "lab_navigator_id": 101,
                        "ha_group_id": "edge-1",
                        "ha_role": "active",
                    },
                    "access_sw": {
                        "id": "access_sw",
                        "type": "switch",
                        "display_name": "access-sw",
                        "model": "Dell-3048",
                        "ip_address": "10.0.0.10",
                        "switch_metadata": {
                            "name": "access-sw",
                            "model": "Dell-3048",
                            "connections": {"ip": "10.0.0.10", "port": None},
                            "credentials": {"username": "velocloud", "password": "N#1sdwan"},
                        },
                    },
                },
                "connections": [
                    {
                        "id": "edge-1-ge1-access",
                        "a": {"device_id": "edge-1-active", "interface": "GE1"},
                        "b": {"device_id": "access_sw", "interface": "Gi1/1"},
                        "role": "edge-access",
                    }
                ],
            }
        )
    )

    result = apply_inventory_refresh(
        InventoryRefreshRequest(hardware_ids=["edge-1"]),
        inventory_path=inventory_path,
        client=StubLabNavigatorClient(),
    )

    assert result.inventory.hardware[0].hypervisor_ip is None
    assert result.inventory.hardware[0].path_complete is False
    device_ids = set(result.inventory.devices)
    assert {"access_sw", "agg_sw", "esxi_01"} <= device_ids
    roles = {(connection.role, connection.a.device_id, connection.b.device_id) for connection in result.inventory.connections}
    assert ("switch-uplink", "access_sw", "agg_sw") in roles
    assert ("hypervisor-access", "agg_sw", "esxi_01") in roles


def test_lab_navigator_client_allows_anonymous_reads():
    client = LabNavigatorClient(api_key="")
    try:
        assert "Authorization" not in client.client.headers
    finally:
        client.close()


def test_lab_navigator_client_reports_anonymous_auth_rejection():
    client = LabNavigatorClient(api_key="", base_url="https://lab-navigator.example.com")
    client.close()
    client.client = httpx.Client(
        base_url=client.base_url,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(401, request=request, json={"detail": "unauthorized"})
        ),
    )
    try:
        with pytest.raises(DiscoveryError, match="configure LN_PROD_API_KEY"):
            client.search("access-sw")
    finally:
        client.close()
