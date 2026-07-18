from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class ActorIdentity(BaseModel):
    name: str
    email: str

    @field_validator("name", "email")
    @classmethod
    def require_value(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("name and email are required")
        return cleaned

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        lowered = value.strip().lower()
        if "@" not in lowered or lowered.startswith("@") or lowered.endswith("@"):
            raise ValueError("email must be a valid email address")
        return lowered


class HardwareReservation(BaseModel):
    actor: ActorIdentity
    reserved_at: str
    reason: Literal["topology-generation", "manual-unavailable"]
    run_id: Optional[str] = None
    topology_name: Optional[str] = None


class HardwareLocalState(BaseModel):
    available: bool = True
    reservation: Optional[HardwareReservation] = None

    @model_validator(mode="after")
    def normalize_reservation(self) -> "HardwareLocalState":
        if self.available:
            self.reservation = None
        return self


class VlanRange(BaseModel):
    start: int
    end: int

    @field_validator("start", "end")
    @classmethod
    def validate_vlan(cls, value: int) -> int:
        if value < 1 or value > 4094:
            raise ValueError("VLAN values must be between 1 and 4094")
        return value

    @model_validator(mode="after")
    def validate_order(self) -> "VlanRange":
        if self.start > self.end:
            raise ValueError("vlan_range start must be less than or equal to end")
        return self


class SwitchConnection(BaseModel):
    ip: str
    port: Optional[int] = None


class SwitchCredentials(BaseModel):
    username: str = "velo"
    password: str = "Velocloud@123"


class SwitchMetadata(BaseModel):
    name: str
    device_type: str = "DELL"
    model: str = "Dell-3048"
    os_family: Optional[Literal["os9", "os10"]] = None
    connections: SwitchConnection
    credentials: SwitchCredentials = Field(default_factory=SwitchCredentials)


class EdgePortMapping(BaseModel):
    logical_name: str
    name: str
    logical_interface: str
    link: str
    switch_name: Optional[str] = None
    switch_active_port: Optional[str] = None
    switch_standby_port: Optional[str] = None
    switch_vlans: list[int] = Field(default_factory=list)
    tagged_vlans: list[int] = Field(default_factory=list)
    untagged_vlan: Optional[int] = None
    edge_vlans: Optional[list[int]] = None
    segment_vlans: dict[str, int] = Field(default_factory=dict)
    wanlink_name: Optional[str] = None
    manual_mapping_required: bool = False
    port_warning: Optional[str] = None

    @model_validator(mode="after")
    def validate_switch_ports(self) -> "EdgePortMapping":
        if not self.switch_active_port and not self.switch_standby_port:
            raise ValueError("edge port mapping must define at least one switch member port")
        return self

    @property
    def global_vlan(self) -> Optional[int]:
        return self.switch_vlans[0] if self.switch_vlans else None


class HardwarePortAllocation(BaseModel):
    reference_interface: str
    logical_interface: str
    link: Optional[str] = None
    switch_name: str
    switch_active_port: Optional[str] = None
    switch_standby_port: Optional[str] = None
    switch_vlans: list[int] = Field(default_factory=list)
    tagged_vlans: list[int] = Field(default_factory=list)
    untagged_vlan: Optional[int] = None
    segment_vlans: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_switch_ports(self) -> "HardwarePortAllocation":
        if not self.switch_active_port and not self.switch_standby_port:
            raise ValueError("hardware port allocation must define at least one switch member port")
        return self


class HardwareAllocation(BaseModel):
    hardware_id: str
    branch_name: str
    edge_name: str
    reference_topology_id: Optional[str] = None
    interface_fingerprint: str
    reserved_vlans: list[int] = Field(default_factory=list)
    ports: list[HardwarePortAllocation] = Field(default_factory=list)


class HardwarePathSummary(BaseModel):
    access_switch_id: Optional[str] = None
    access_switch_name: Optional[str] = None
    access_switch_ip: Optional[str] = None
    access_uplink_port: Optional[str] = None
    upstream_switch_id: Optional[str] = None
    upstream_switch_name: Optional[str] = None
    upstream_switch_model: Optional[str] = None
    upstream_switch_ip: Optional[str] = None
    upstream_access_port: Optional[str] = None
    upstream_hypervisor_port: Optional[str] = None
    hypervisor_id: Optional[str] = None
    hypervisor_name: Optional[str] = None
    hypervisor_ip: Optional[str] = None
    complete: bool = False


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
    vlan_range: Optional[VlanRange] = None
    switch: Optional[SwitchMetadata] = None
    switches: list[SwitchMetadata] = Field(default_factory=list)
    ports: list[EdgePortMapping] = Field(default_factory=list)
    allocations: list[HardwareAllocation] = Field(default_factory=list)
    path: Optional[HardwarePathSummary] = None
    path_complete: bool = False
    auto_config_ready: bool = False
    hypervisor_ip: Optional[str] = None
    available: bool = True
    reservation: Optional[HardwareReservation] = None
    notes: Optional[str] = None

    @model_validator(mode="after")
    def validate_ha_serials(self) -> "HardwareEdge":
        if self.ha and not self.standby_serial:
            raise ValueError("HA hardware requires standby_serial")
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
    vlan_range: Optional[VlanRange] = None
    switch_metadata: Optional[SwitchMetadata] = None
    lab_navigator_id: Optional[int] = None
    hypervisor_ip: Optional[str] = None
    reservation: Optional[HardwareReservation] = None
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
    role: Optional[Literal["edge-access", "switch-uplink", "hypervisor-access"]] = None
    notes: Optional[str] = None


class InventoryFile(BaseModel):
    devices: dict[str, InventoryDevice] = Field(default_factory=dict)
    connections: list[InventoryConnection] = Field(default_factory=list)
    allocations: list[HardwareAllocation] = Field(default_factory=list)
    hardware: list[HardwareEdge]


class InventoryStateFile(BaseModel):
    hardware: dict[str, HardwareLocalState] = Field(default_factory=dict)


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
    saved_hardware: Optional[HardwareEdge] = None


class InterfaceOverride(BaseModel):
    reference_interface: str
    hardware_interface: Optional[str] = None
    switch_vlans: list[int] = Field(default_factory=list)

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

    @field_validator("switch_vlans")
    @classmethod
    def validate_switch_vlans(cls, value: list[int]) -> list[int]:
        cleaned: list[int] = []
        seen: set[int] = set()
        for vlan in value:
            if vlan < 1 or vlan > 4094:
                raise ValueError("switch_vlans values must be between 1 and 4094")
            if vlan not in seen:
                cleaned.append(vlan)
                seen.add(vlan)
        return cleaned

    @model_validator(mode="after")
    def validate_switch_vlan_usage(self) -> "InterfaceOverride":
        if self.hardware_interface is None and self.switch_vlans:
            raise ValueError("switch_vlans require hardware_interface")
        return self


class GenerateRequest(BaseModel):
    topology_name: str
    reference_topology_id: str
    hypervisor_ip: str
    hypervisor_interface: str
    branch_rename: bool = False
    mappings: list[MappingRequest]
    requested_by: ActorIdentity

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


class GenerateMappingStatus(BaseModel):
    hardware_id: str
    hardware_display_name: str
    branch_name: str
    edge_name: str
    path_resolved: bool = False
    auto_config_ready: bool = False
    reason: Optional[str] = None
    path: Optional[HardwarePathSummary] = None


class GenerateResult(BaseModel):
    run_id: str
    topology_name: str
    topology_path: str
    zip_path: str
    download_url: str
    can_configure_switches: bool = False
    mapping_statuses: list[GenerateMappingStatus] = Field(default_factory=list)
    messages: list[ValidationMessage] = Field(default_factory=list)


class SavedGenerateRequest(BaseModel):
    topology_name: str
    reference_topology_id: str
    hypervisor_ip: str = ""
    hypervisor_interface: str = ""
    mappings: list[MappingRequest] = Field(default_factory=list)


class InventoryRefreshRequest(BaseModel):
    hardware_ids: list[str] = Field(default_factory=list)

    @field_validator("hardware_ids")
    @classmethod
    def require_hardware_ids(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item and item.strip()]
        if not cleaned:
            raise ValueError("hardware_ids is required")
        return cleaned


class InventoryRefreshChange(BaseModel):
    change_type: Literal["add-device", "update-device", "add-connection", "update-connection"]
    target: str
    summary: str


class InventoryRefreshResult(BaseModel):
    hardware_ids: list[str]
    changes: list[InventoryRefreshChange] = Field(default_factory=list)
    inventory: InventoryFile
    messages: list[ValidationMessage] = Field(default_factory=list)


BaseBranchName = Literal["release_5.2", "release_6.1", "release_6.4", "release_7.0", "master"]


class HapyCommitRequest(BaseModel):
    base_branch: BaseBranchName = "master"
    requested_by: Optional[ActorIdentity] = None


class HapyPublishMetadata(BaseModel):
    run_id: str
    topology_name: str
    reference_topology_id: str
    repo_path: str
    destination_path: str
    destination_relative_path: str
    base_branch: BaseBranchName
    private_branch_name: str
    commit_sha: str
    commit_message: str
    private_branch_pushed: bool = False
    remote_name: str = "origin"
    remote_branch_ref: Optional[str] = None
    fetch_command: Optional[str] = None
    workspace_path: Optional[str] = None
    created_at: str
    updated_at: str


class HapyCommitResult(BaseModel):
    run_id: str
    topology_name: Optional[str] = None
    reference_topology_id: Optional[str] = None
    repo_path: str
    destination_path: str
    destination_relative_path: str
    base_branch: BaseBranchName
    private_branch_name: str
    commit_sha: str
    commit_message: str
    private_branch_pushed: bool = False
    remote_name: str = "origin"
    remote_branch_ref: Optional[str] = None
    fetch_command: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    messages: list[ValidationMessage] = Field(default_factory=list)


class HapyPrivateBranchRecord(BaseModel):
    run_id: str
    topology_name: str
    reference_topology_id: str
    repo_path: str
    destination_path: str
    destination_relative_path: str
    base_branch: BaseBranchName
    private_branch_name: str
    commit_sha: str
    commit_message: str
    private_branch_pushed: bool = False
    remote_name: str = "origin"
    remote_branch_ref: Optional[str] = None
    fetch_command: Optional[str] = None
    workspace_path: Optional[str] = None
    created_at: str
    updated_at: str


class HapyPrivateBranchListResult(BaseModel):
    branches: list[HapyPrivateBranchRecord] = Field(default_factory=list)


class HapyPrivateBranchDeleteRequest(BaseModel):
    private_branch_names: list[str] = Field(default_factory=list)
    delete_all: bool = False
    requested_by: ActorIdentity

    @field_validator("private_branch_names")
    @classmethod
    def clean_branch_names(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in value:
            branch_name = item.strip()
            if not branch_name or branch_name in seen:
                continue
            cleaned.append(branch_name)
            seen.add(branch_name)
        return cleaned

    @model_validator(mode="after")
    def validate_targets(self) -> "HapyPrivateBranchDeleteRequest":
        if not self.delete_all and not self.private_branch_names:
            raise ValueError("private_branch_names is required unless delete_all is true")
        return self


class HapyPrivateBranchDeleteStatus(BaseModel):
    private_branch_name: str
    run_id: str
    deleted_local_paths: list[str] = Field(default_factory=list)
    deleted_remote: bool = False
    registry_removed: bool = False
    success: bool = True
    messages: list[ValidationMessage] = Field(default_factory=list)


class HapyPrivateBranchDeleteResult(BaseModel):
    results: list[HapyPrivateBranchDeleteStatus] = Field(default_factory=list)
    messages: list[ValidationMessage] = Field(default_factory=list)


class SwitchCommandPlan(BaseModel):
    device_id: str
    device_name: str
    device_ip: str
    interface: str
    commands: list[str] = Field(default_factory=list)


class SwitchCommandOverride(BaseModel):
    device_id: str
    commands: list[str] = Field(default_factory=list)

    @field_validator("device_id")
    @classmethod
    def require_device_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("device_id is required")
        return cleaned

    @field_validator("commands")
    @classmethod
    def validate_commands(cls, value: list[str]) -> list[str]:
        cleaned = [str(command).rstrip() for command in value if str(command).strip()]
        if not cleaned:
            raise ValueError("commands are required")
        return cleaned


class SwitchConfigureRequest(BaseModel):
    dry_run: bool = False
    command_overrides: list[SwitchCommandOverride] = Field(default_factory=list)


class SwitchConfigureResult(BaseModel):
    run_id: str
    applied: bool
    devices: list[SwitchCommandPlan] = Field(default_factory=list)
    messages: list[ValidationMessage] = Field(default_factory=list)


class RunMappingMetadata(BaseModel):
    hardware_id: str
    branch_name: str
    edge_name: str
    path: Optional[HardwarePathSummary] = None
    allocations: list[HardwarePortAllocation] = Field(default_factory=list)


class RunMetadata(BaseModel):
    run_id: str
    topology_name: str
    reference_topology_id: str
    requested_by: Optional[ActorIdentity] = None
    request: Optional[SavedGenerateRequest] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    can_configure_switches: bool = False
    mapping_statuses: list[GenerateMappingStatus] = Field(default_factory=list)
    messages: list[ValidationMessage] = Field(default_factory=list)
    mappings: list[RunMappingMetadata] = Field(default_factory=list)
    hapy_publishes: list[HapyPublishMetadata] = Field(default_factory=list)


class SavedRunSummary(BaseModel):
    run_id: str
    topology_name: str
    requested_topology_name: str
    reference_topology_id: str
    requested_by: Optional[ActorIdentity] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    private_branch_name: Optional[str] = None
    private_branch_pushed: bool = False


class SavedRunListResult(BaseModel):
    runs: list[SavedRunSummary] = Field(default_factory=list)


class SavedRunLoadResult(BaseModel):
    request: SavedGenerateRequest
    result: GenerateResult
    publish_result: Optional[HapyCommitResult] = None


class InventoryUpdateRequest(BaseModel):
    inventory: InventoryFile
    requested_by: Optional[ActorIdentity] = None


class HardwareAvailabilityUpdateRequest(BaseModel):
    available: bool
    requested_by: ActorIdentity


AuditActionName = Literal[
    "hardware_reserved",
    "hardware_released",
    "hardware_marked_unavailable",
    "inventory_saved",
    "private_branch_published",
    "private_branch_deleted",
]

AuditTargetType = Literal["hardware", "private_branch", "inventory"]


class AuditEvent(BaseModel):
    id: str
    action: AuditActionName
    actor: ActorIdentity
    target_type: AuditTargetType
    target_id: str
    summary: str
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class AuditTrailResult(BaseModel):
    events: list[AuditEvent] = Field(default_factory=list)


JsonObject = dict[str, Any]
