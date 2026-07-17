from app.edge_models import normalize_edge_model
from app.inventory import build_inventory


def test_normalize_edge_model_maps_velocloud_7105g_to_supported_edge710():
    model, suffix = normalize_edge_model("VeloCloud-7105g")

    assert model == "edge710"
    assert suffix == "7105g"


def test_build_inventory_normalizes_unsupported_edge_models_from_saved_inventory():
    inventory = build_inventory(
        {
            "edge-7105g": {
                "id": "edge-7105g",
                "type": "edge",
                "display_name": "Edge 7105g",
                "model": "VeloCloud-7105g",
                "model_suffix": "7105g",
                "serial_number": "SERIAL7105G",
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
                "id": "edge-7105g-ge1",
                "a": {"device_id": "edge-7105g", "interface": "GE1"},
                "b": {"device_id": "switch-1", "interface": "Gi1/1"},
                "vlans": [101],
                "tagged_vlans": [],
                "untagged_vlan": 101,
                "role": "edge-access",
            }
        ],
    )

    device = inventory.devices["edge-7105g"]
    hardware = inventory.hardware[0]

    assert device.model == "edge710"
    assert device.model_suffix == "7105g"
    assert hardware.model == "edge710"
    assert hardware.model_suffix == "7105g"
