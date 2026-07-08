from __future__ import annotations

import json
from pathlib import Path

from .config import REFERENCE_CONFIG_ROOT, REFERENCE_TOPOLOGIES
from .models import (
    BranchSummary,
    EdgeSummary,
    ReferenceInterfaceSummary,
    ReferenceSubinterfaceSummary,
    ReferenceTopologySummary,
)


def resolve_reference_path(reference_id: str, root: Path = REFERENCE_CONFIG_ROOT) -> Path:
    if reference_id not in REFERENCE_TOPOLOGIES:
        raise ValueError(f"Reference topology is not allowlisted: {reference_id}")
    path = (root / reference_id).resolve()
    root_resolved = root.resolve()
    if root_resolved not in path.parents and path != root_resolved:
        raise ValueError(f"Reference topology resolves outside config root: {reference_id}")
    return path


def summarize_reference(reference_id: str, root: Path = REFERENCE_CONFIG_ROOT) -> ReferenceTopologySummary:
    path = resolve_reference_path(reference_id, root)
    if not path.exists():
        return ReferenceTopologySummary(id=reference_id, path=str(path), exists=False)

    config_path = path / "config.json"
    if not config_path.exists():
        return ReferenceTopologySummary(id=reference_id, path=str(path), exists=True)

    with config_path.open() as fh:
        config = json.load(fh)

    branches = []
    for branch in config.get("topology", {}).get("branches", []):
        edges = [
            EdgeSummary(
                name=edge.get("name", ""),
                model=edge.get("model"),
                management_ip=edge.get("management_ip"),
                ha_enabled=edge.get("ha_enabled"),
                interfaces=[
                    ReferenceInterfaceSummary(
                        name=str(interface.get("name", "")),
                        logical_name=interface.get("logical_name"),
                        logical_interface=interface.get("logical_interface"),
                        mode=interface.get("mode"),
                        vlans=[vlan for vlan in interface.get("vlans", []) if isinstance(vlan, int)],
                        subinterfaces=[
                            ReferenceSubinterfaceSummary(
                                name=subinterface.get("name"),
                                segment_name=subinterface.get("segment_name"),
                                vlan=subinterface.get("vlan"),
                            )
                            for subinterface in interface.get("subinterfaces", [])
                            if isinstance(subinterface, dict)
                        ],
                    )
                    for interface in edge.get("interfaces", [])
                    if isinstance(interface, dict)
                ],
            )
            for edge in branch.get("edges", [])
        ]
        branches.append(BranchSummary(name=branch.get("name", ""), type=branch.get("type"), edges=edges))

    return ReferenceTopologySummary(
        id=reference_id,
        path=str(path),
        exists=True,
        testbed_name=config.get("testbed", {}).get("name"),
        testbed_type=config.get("testbed", {}).get("type"),
        branches=branches,
    )


def list_references(root: Path = REFERENCE_CONFIG_ROOT) -> list[ReferenceTopologySummary]:
    return [summarize_reference(reference_id, root) for reference_id in REFERENCE_TOPOLOGIES]
