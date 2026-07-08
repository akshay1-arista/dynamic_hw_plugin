from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class SwitchConnection(BaseModel):
    ip: str
    port: Optional[int] = None


class SwitchCredentials(BaseModel):
    username: str = "velocloud"
    password: str = "N#1sdwan"


class SwitchMetadata(BaseModel):
    name: str
    device_type: str = "DELL"
    model: str = "Dell-3048"
    connections: SwitchConnection
    credentials: SwitchCredentials = Field(default_factory=SwitchCredentials)


class EdgePortMapping(BaseModel):
    logical_name: str
    name: str
    logical_interface: str
    link: str
    switch_name: Optional[str] = None
    switch_active_port: str
    switch_standby_port: Optional[str] = None
    switch_vlans: list[int] = Field(default_factory=list)
    tagged_vlans: list[int] = Field(default_factory=list)
    untagged_vlan: Optional[int] = None
    edge_vlans: Optional[list[int]] = None
    segment_vlans: dict[str, int] = Field(default_factory=dict)
    wanlink_name: Optional[str] = None

    @property
    def global_vlan(self) -> Optional[int]:
        return self.switch_vlans[0] if self.switch_vlans else None


class HardwareEdge(BaseModel):
    id: str
    short_name: Optional[str] = None
    display_name: str
    model: str
    model_suffix: str
    ha: bool = False
    dpdk_enabled: Optional[bool] = None
    active_serial: str
    standby_serial: Optional[str] = None
    free_vlans: list[int] = Field(default_factory=list)
    switch: Optional[SwitchMetadata] = None
    switches: list[SwitchMetadata] = Field(default_factory=list)
    ports: list[EdgePortMapping]
    available: bool = True
    notes: Optional[str] = None

    @field_validator("ports")
    @classmethod
    def require_ports(cls, value: list[EdgePortMapping]) -> list[EdgePortMapping]:
        if not value:
            raise ValueError("hardware entry must define at least one port mapping")
        return value

    @model_validator(mode="after")
    def validate_ha_serials(self) -> "HardwareEdge":
        if self.ha and not self.standby_serial:
            raise ValueError("HA hardware requires standby_serial")
        if not self.switch and not self.switches:
            raise ValueError("hardware entry must define switch or switches")
        return self


class InventoryDevice(BaseModel):
    id: str
    type: Literal["edge", "switch", "hypervisor"]
    display_name: str
    short_name: Optional[str] = None
    model: Optional[str] = None
    model_suffix: Optional[str] = None
    serial_number: Optional[str] = None
    ip_address: Optional[str] = None
    available: bool = True
    ha_group_id: Optional[str] = None
    ha_role: Optional[Literal["active", "standby"]] = None
    dpdk_enabled: Optional[bool] = None
    free_vlans: list[int] = Field(default_factory=list)
    switch_metadata: Optional[SwitchMetadata] = None
    notes: Optional[str] = None


class InventoryEndpoint(BaseModel):
    device_id: str
    interface: str


class InventoryConnection(BaseModel):
    id: str
    a: InventoryEndpoint
    b: InventoryEndpoint
    vlans: list[int] = Field(default_factory=list)
    tagged_vlans: list[int] = Field(default_factory=list)
    untagged_vlan: Optional[int] = None
    notes: Optional[str] = None


class InventoryFile(BaseModel):
    devices: dict[str, InventoryDevice] = Field(default_factory=dict)
    connections: list[InventoryConnection] = Field(default_factory=list)
    hardware: list[HardwareEdge]


class EdgeSummary(BaseModel):
    name: str
    model: Optional[str] = None
    management_ip: Optional[str] = None
    ha_enabled: Optional[Any] = None
    interfaces: list["ReferenceInterfaceSummary"] = Field(default_factory=list)


class ReferenceSubinterfaceSummary(BaseModel):
    name: Optional[str] = None
    segment_name: Optional[str] = None
    vlan: Optional[int] = None


class ReferenceInterfaceSummary(BaseModel):
    name: str = ""
    logical_name: Optional[str] = None
    logical_interface: Optional[str] = None
    mode: Optional[str] = None
    vlans: list[int] = Field(default_factory=list)
    subinterfaces: list[ReferenceSubinterfaceSummary] = Field(default_factory=list)


class BranchSummary(BaseModel):
    name: str
    type: Optional[str] = None
    edges: list[EdgeSummary]


class ReferenceTopologySummary(BaseModel):
    id: str
    path: str
    exists: bool
    testbed_name: Optional[str] = None
    testbed_type: Optional[str] = None
    branches: list[BranchSummary] = Field(default_factory=list)


class MappingRequest(BaseModel):
    hardware_id: str
    branch_name: str
    edge_name: str
    target_branch_name: Optional[str] = None
    target_edge_name: Optional[str] = None
    interface_overrides: list["InterfaceOverride"] = Field(default_factory=list)


class InterfaceOverride(BaseModel):
    reference_interface: str
    hardware_interface: Optional[str] = None

    @field_validator("reference_interface")
    @classmethod
    def require_reference_interface(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("reference_interface is required")
        return cleaned

    @field_validator("hardware_interface")
    @classmethod
    def clean_hardware_interface(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class GenerateRequest(BaseModel):
    topology_name: str
    reference_topology_id: str
    hypervisor_ip: str
    hypervisor_interface: str
    branch_rename: bool = False
    mappings: list[MappingRequest]

    @field_validator("topology_name")
    @classmethod
    def validate_topology_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("topology_name is required")
        if "/" in cleaned or "\\" in cleaned or cleaned in {".", ".."}:
            raise ValueError("topology_name must be a folder name, not a path")
        return cleaned

    @field_validator("hypervisor_ip", "hypervisor_interface")
    @classmethod
    def require_hypervisor_values(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("hypervisor_ip and hypervisor_interface are required")
        return cleaned

    @field_validator("mappings")
    @classmethod
    def require_mappings(cls, value: list[MappingRequest]) -> list[MappingRequest]:
        if not value:
            raise ValueError("at least one mapping is required")
        return value


class ValidationMessage(BaseModel):
    level: Literal["info", "warning", "error"]
    message: str


class GenerateResult(BaseModel):
    run_id: str
    topology_name: str
    topology_path: str
    zip_path: str
    download_url: str
    messages: list[ValidationMessage] = Field(default_factory=list)


JsonObject = dict[str, Any]
