import json

from app.config import INVENTORY_PATH
from app.run_history import load_saved_run


MISSING_HARDWARE_ID = "ln-ha-a02-314-236254370-a02-315-236254372"


def test_load_saved_run_attaches_saved_hardware_snapshot_for_missing_derived_inventory(tmp_path):
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(INVENTORY_PATH.read_text())
    outputs_root = tmp_path / "outputs"
    run_root = outputs_root / "run123-saved01"
    topology_path = run_root / "saved-topology-saved01"
    topology_path.mkdir(parents=True)

    metadata = {
        "run_id": "run123",
        "topology_name": "saved-topology-saved01",
        "reference_topology_id": "5-site-cluster/spirent",
        "requested_by": {"name": "Test User", "email": "test@example.com"},
        "mappings": [
            {
                "hardware_id": MISSING_HARDWARE_ID,
                "branch_name": "branch2",
                "edge_name": "b2-edge1",
                "path": {
                    "access_switch_id": "chn_rnd_sw_3048_fctd9z2",
                    "access_switch_name": "chn-rnd-sw-3048-FCTD9Z2",
                    "access_switch_ip": "10.68.136.111",
                    "access_uplink_port": "tengigabitethernet1/51",
                    "upstream_switch_id": "ln_a01_340_switch_hfcqxc2",
                    "upstream_switch_name": "chn-rnd-sw-4048-HFCQXC2",
                    "upstream_switch_model": "Dell-4048",
                    "upstream_switch_ip": "10.68.137.146",
                    "upstream_access_port": "tengigabitethernet1/45",
                    "upstream_hypervisor_port": "tengigabitethernet1/10",
                    "hypervisor_id": "ln_a02_347_hypervisor_3p83s63",
                    "hypervisor_name": "chn-rnd-srv-640-3P83S63",
                    "hypervisor_ip": "10.68.136.221",
                    "complete": True,
                },
                "allocations": [
                    {
                        "reference_interface": "GE1",
                        "logical_interface": "GE1",
                        "link": "B2E1_HA",
                        "switch_name": "chn-rnd-sw-3048-FCTD9Z2",
                        "switch_active_port": "gigabitethernet1/16",
                        "switch_standby_port": None,
                        "switch_vlans": [361],
                        "tagged_vlans": [],
                        "untagged_vlan": 361,
                        "segment_vlans": {},
                    }
                ],
            }
        ],
        "messages": [{"level": "info", "message": "Loaded saved topology run run123."}],
    }
    (run_root / "run_metadata.json").write_text(json.dumps(metadata))
    (topology_path / "config.json").write_text(
        json.dumps(
            {
                "testbed": {"name": "saved-topology-saved01"},
                "topology": {
                    "branches": [
                        {
                            "name": "branch2",
                            "edges": [
                                {
                                    "name": "b2-edge1-710",
                                    "model": "edge710",
                                    "ha_enabled": True,
                                    "slno": "236254370",
                                    "standby_slno": "236254372",
                                }
                            ],
                        }
                    ]
                },
            }
        )
    )

    loaded = load_saved_run("run123", inventory_path=inventory_path, outputs_root=outputs_root)

    mapping = loaded.request.mappings[0]
    assert mapping.hardware_id == MISSING_HARDWARE_ID
    assert mapping.saved_hardware is not None
    assert mapping.saved_hardware.id == MISSING_HARDWARE_ID
    assert mapping.saved_hardware.ports[0].switch_active_port == "gigabitethernet1/16"
    assert mapping.saved_hardware.ports[0].manual_mapping_required is True
