import { useEffect, useId, useMemo, useState } from 'react';
import headerLogo from './assets/header-logo.png';
import {
  Archive,
  CheckCircle2,
  ChevronDown,
  Copy,
  Download,
  Eye,
  GitBranch,
  HardDrive,
  Loader2,
  Plus,
  RefreshCw,
  Search,
  Server,
  TriangleAlert,
  Trash2
} from 'lucide-react';
import {
  applyInventoryRefresh,
  configureSwitches,
  deletePrivateBranches,
  fetchAuditTrail,
  fetchPrivateBranches,
  fetchInventory,
  fetchReferences,
  generateTopology,
  publishPrivateBranch,
  previewInventoryRefresh,
  saveInventory,
  updateHardwareAvailability
} from './api.js';

const emptyMapping = {
  hardware_id: '',
  branch_name: '',
  edge_name: '',
  target_branch_name: '',
  target_edge_name: '',
  interface_overrides: []
};

const hapyBaseBranches = ['release_5.2', 'release_6.1', 'release_6.4', 'release_7.0', 'master'];
const userStorageKey = 'dynamic-topology-user';

export function App() {
  const hypervisorIpFieldId = useId();
  const hypervisorInterfaceFieldId = useId();
  const [currentUser, setCurrentUser] = useState(() => loadStoredUser());
  const [profileName, setProfileName] = useState(() => loadStoredUser()?.name || '');
  const [profileEmail, setProfileEmail] = useState(() => loadStoredUser()?.email || '');
  const [references, setReferences] = useState([]);
  const [inventory, setInventory] = useState({ hardware: [] });
  const [privateBranches, setPrivateBranches] = useState([]);
  const [auditTrail, setAuditTrail] = useState([]);
  const [selectedPrivateBranchNames, setSelectedPrivateBranchNames] = useState([]);
  const [selectedReferenceId, setSelectedReferenceId] = useState('');
  const [topologyName, setTopologyName] = useState('');
  const [hypervisorIp, setHypervisorIp] = useState('');
  const [hypervisorInterface, setHypervisorInterface] = useState('');
  const [mappings, setMappings] = useState([{ ...emptyMapping }]);
  const [result, setResult] = useState(null);
  const [publishResult, setPublishResult] = useState(null);
  const [publishBaseBranch, setPublishBaseBranch] = useState('master');
  const [publishingAction, setPublishingAction] = useState('');
  const [deletingPrivateBranchNames, setDeletingPrivateBranchNames] = useState([]);
  const [privateBranchFeedback, setPrivateBranchFeedback] = useState(null);
  const [copyState, setCopyState] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(Boolean(loadStoredUser()));
  const [generating, setGenerating] = useState(false);
  const [savingInventory, setSavingInventory] = useState(false);
  const [refreshingHardwareId, setRefreshingHardwareId] = useState('');
  const [updatingAvailabilityId, setUpdatingAvailabilityId] = useState('');
  const [configuringSwitches, setConfiguringSwitches] = useState(false);
  const [previewingSwitches, setPreviewingSwitches] = useState(false);
  const [switchPreview, setSwitchPreview] = useState(null);
  const [inventorySearch, setInventorySearch] = useState('');
  const [inventoryAvailabilityFilter, setInventoryAvailabilityFilter] = useState('all');
  const [auditSearch, setAuditSearch] = useState('');
  const [expandedHardwareId, setExpandedHardwareId] = useState('');
  const [collapsedDataCards, setCollapsedDataCards] = useState({
    inventory: false,
    privateBranches: false,
    auditTrail: false
  });

  useEffect(() => {
    if (!currentUser) {
      setLoading(false);
      return;
    }
    loadData();
  }, [currentUser]);

  const selectedReference = useMemo(
    () => references.find((reference) => reference.id === selectedReferenceId),
    [references, selectedReferenceId]
  );
  const hypervisorOptions = useMemo(() => buildHypervisorOptions(inventory), [inventory]);
  const selectedHypervisorOption = useMemo(
    () => hypervisorOptions.find((option) => option.ip === hypervisorIp.trim()) || null,
    [hypervisorIp, hypervisorOptions]
  );
  const hypervisorIpOptions = useMemo(
    () =>
      hypervisorOptions.map((option) => ({
        value: option.ip,
        label: hypervisorIpOptionLabel(option),
        searchText: hypervisorSearchText(option)
      })),
    [hypervisorOptions]
  );
  const hypervisorInterfaceOptions = useMemo(() => {
    const interfaces = selectedHypervisorOption?.interfaces?.length
      ? selectedHypervisorOption.interfaces
      : [...new Set(hypervisorOptions.flatMap((option) => option.interfaces || []))].sort(
          compareHypervisorInterfaceNames
        );
    return interfaces.map((interfaceName) => ({
      value: interfaceName,
      label: interfaceName,
      searchText: interfaceName.toLowerCase()
    }));
  }, [selectedHypervisorOption, hypervisorOptions]);

  useEffect(() => {
    if (!selectedHypervisorOption?.interfaces?.length || !hypervisorInterface.trim()) {
      return;
    }
    const normalizedCurrent = hypervisorInterface.trim().toLowerCase();
    const matchesSelectedHypervisor = selectedHypervisorOption.interfaces.some(
      (interfaceName) => interfaceName.toLowerCase() === normalizedCurrent
    );
    if (!matchesSelectedHypervisor) {
      setHypervisorInterface('');
    }
  }, [selectedHypervisorOption, hypervisorInterface]);

  const previewRows = useMemo(() => {
    return mappings
      .map((mapping) => {
        const hardware = inventory.hardware.find((item) => item.id === mapping.hardware_id);
        if (!hardware || !mapping.branch_name || !mapping.edge_name) {
          return null;
        }
        const branchName = mapping.target_branch_name || mapping.branch_name;
        const edgeName = mapping.target_edge_name || `${mapping.edge_name}-${hardware.model_suffix}`;
        return {
          hardware: hardware.display_name,
          branch: `${mapping.branch_name} -> ${branchName}`,
          edge: `${mapping.edge_name} -> ${edgeName}`,
          ports: hardware.ports.length,
          configurablePorts: hardware.ports.length
        };
      })
      .filter(Boolean);
  }, [inventory.hardware, mappings]);

  const filteredHardware = useMemo(() => {
    const query = inventorySearch.trim().toLowerCase();
    return inventory.hardware.filter((hardware) => {
      const matchesAvailability =
        inventoryAvailabilityFilter === 'all' ||
        (inventoryAvailabilityFilter === 'available' ? hardware.available : !hardware.available);
      if (!matchesAvailability) {
        return false;
      }
      return !query || hardwareSearchText(hardware).includes(query);
    });
  }, [inventory.hardware, inventoryAvailabilityFilter, inventorySearch]);
  const filteredAuditTrail = useMemo(() => {
    const query = auditSearch.trim().toLowerCase();
    if (!query) {
      return auditTrail;
    }
    return auditTrail.filter((event) => auditSearchText(event).includes(query));
  }, [auditTrail, auditSearch]);

  const configureSwitchState = useMemo(() => {
    if (!result) {
      return { enabled: false, reason: '' };
    }
    if (result.can_configure_switches) {
      return { enabled: true, reason: '' };
    }
    const unresolved = result.mapping_statuses?.find((item) => !item.path_resolved);
    if (unresolved) {
      return {
        enabled: false,
        reason: `Configure switches unavailable: ${unresolved.branch_name}/${unresolved.edge_name} path is unresolved. ${unresolved.reason || ''}`.trim()
      };
    }
    const blockedByCredentials = result.mapping_statuses?.find((item) => item.path_resolved && !item.auto_config_ready);
    if (blockedByCredentials) {
      return {
        enabled: false,
        reason: `Configure switches unavailable: ${blockedByCredentials.branch_name}/${blockedByCredentials.edge_name} is missing switch credentials.`
      };
    }
    return {
      enabled: false,
      reason: 'Configure switches unavailable for one or more generated mappings.'
    };
  }, [result]);
  const existingReferenceCount = useMemo(
    () => references.filter((reference) => reference.exists).length,
    [references]
  );
  const availableHardwareCount = useMemo(
    () => inventory.hardware.filter((hardware) => hardware.available).length,
    [inventory.hardware]
  );
  const selectedMappingCount = useMemo(
    () => mappings.filter((mapping) => mapping.hardware_id && mapping.branch_name && mapping.edge_name).length,
    [mappings]
  );
  const allPrivateBranchesSelected =
    privateBranches.length > 0 && selectedPrivateBranchNames.length === privateBranches.length;
  const deletingPrivateBranches = publishingAction === 'delete-branches';

  async function loadData() {
    setLoading(true);
    setError('');
    try {
      const [referenceData, inventoryData, privateBranchData, auditData] = await Promise.all([
        fetchReferences(),
        fetchInventory(),
        fetchPrivateBranches(),
        fetchAuditTrail()
      ]);
      setReferences(referenceData);
      setInventory(inventoryData);
      setPrivateBranches(privateBranchData.branches || []);
      setAuditTrail(auditData.events || []);
      setSelectedPrivateBranchNames((current) =>
        current.filter((branchName) =>
          (privateBranchData.branches || []).some((branch) => branch.private_branch_name === branchName)
        )
      );
      const firstExisting = referenceData.find((reference) => reference.exists);
      if (firstExisting && !selectedReferenceId) {
        setSelectedReferenceId(firstExisting.id);
        setTopologyName(`${firstExisting.id.replaceAll('/', '-')}-hw`);
      }
    } catch (loadError) {
      setError(loadError.message);
    } finally {
      setLoading(false);
    }
  }

  function updateMapping(index, field, value) {
    setMappings((current) =>
      current.map((mapping, mappingIndex) => {
        if (mappingIndex !== index) {
          return mapping;
        }
        const next = { ...mapping, [field]: value };
        if (field === 'hardware_id' || field === 'branch_name' || field === 'edge_name') {
          next.interface_overrides = [];
        }
        if (field === 'branch_name') {
          const branch = selectedReference?.branches.find((item) => item.name === value);
          const firstAvailableEdge = branch?.edges.find(
            (edge) =>
              !current.some(
                (currentMapping, currentIndex) =>
                  currentIndex !== index &&
                  currentMapping.branch_name === value &&
                  currentMapping.edge_name === edge.name
              )
          );
          next.edge_name = firstAvailableEdge?.name || '';
        }
        return next;
      })
    );
  }

  function addMapping() {
    setMappings((current) => [...current, { ...emptyMapping }]);
  }

  function removeMapping(index) {
    setMappings((current) => current.filter((_, mappingIndex) => mappingIndex !== index));
  }

  function updateHardwareVlanRange(hardwareId, boundary, value) {
    setInventory((current) => ({
      ...current,
      hardware: current.hardware.map((hardware) => {
        if (hardware.id !== hardwareId) {
          return hardware;
        }
        const nextRange = {
          start: hardware.vlan_range?.start ?? '',
          end: hardware.vlan_range?.end ?? ''
        };
        nextRange[boundary] = value === '' ? '' : Number(value);
        return {
          ...hardware,
          vlan_range:
            nextRange.start === '' || nextRange.end === ''
              ? null
              : { start: Number(nextRange.start), end: Number(nextRange.end) }
        };
      })
    }));
  }

  function submitUserProfile(event) {
    event.preventDefault();
    const name = profileName.trim();
    const email = profileEmail.trim().toLowerCase();
    if (!name || !email || !email.includes('@')) {
      setError('Enter a valid name and email address before continuing.');
      return;
    }
    const nextUser = { name, email };
    storeUser(nextUser);
    setCurrentUser(nextUser);
    setError('');
  }

  function clearCurrentUser() {
    clearStoredUser();
    setCurrentUser(null);
    setReferences([]);
    setInventory({ hardware: [] });
    setPrivateBranches([]);
    setAuditTrail([]);
    setSelectedPrivateBranchNames([]);
    setDeletingPrivateBranchNames([]);
    setPrivateBranchFeedback(null);
    setLoading(false);
  }

  async function persistInventory() {
    setSavingInventory(true);
    setError('');
    try {
      const saved = await saveInventory(inventory, currentUser);
      setInventory(saved);
      const auditData = await fetchAuditTrail();
      setAuditTrail(auditData.events || []);
    } catch (saveError) {
      setError(saveError.message);
    } finally {
      setSavingInventory(false);
    }
  }

  async function changeHardwareAvailability(hardwareId, available) {
    setUpdatingAvailabilityId(hardwareId);
    setError('');
    try {
      const updatedInventory = await updateHardwareAvailability(hardwareId, available, currentUser);
      setInventory(updatedInventory);
      const auditData = await fetchAuditTrail();
      setAuditTrail(auditData.events || []);
    } catch (availabilityError) {
      setError(availabilityError.message);
    } finally {
      setUpdatingAvailabilityId('');
    }
  }

  async function refreshHardwareFromLabNavigator(hardwareId) {
    setRefreshingHardwareId(hardwareId);
    setError('');
    try {
      const preview = await previewInventoryRefresh([hardwareId]);
      const summary = preview.changes.length
        ? preview.changes.map((item) => item.summary).join('\n')
        : 'No inventory changes detected.';
      if (!window.confirm(`Apply Lab Navigator refresh for ${hardwareId}?\n\n${summary}`)) {
        return;
      }
      const applied = await applyInventoryRefresh([hardwareId]);
      setInventory(applied.inventory);
    } catch (refreshError) {
      setError(refreshError.message);
    } finally {
      setRefreshingHardwareId('');
    }
  }

  async function submitGenerate() {
    setGenerating(true);
    setResult(null);
    setPublishResult(null);
    setSwitchPreview(null);
    setError('');
    try {
      const duplicateTarget = mappings.find(
        (mapping, index) =>
          mapping.branch_name &&
          mapping.edge_name &&
          mappings.some(
            (otherMapping, otherIndex) =>
              otherIndex !== index &&
              otherMapping.branch_name === mapping.branch_name &&
              otherMapping.edge_name === mapping.edge_name
          )
      );
      const missingMapping = mappings.find(
        (mapping) =>
          !mapping.hardware_id ||
          !mapping.branch_name ||
          !mapping.edge_name
      );
      if (duplicateTarget) {
        throw new Error(
          `Reference edge ${duplicateTarget.branch_name}/${duplicateTarget.edge_name} is already mapped in another row.`
        );
      }
      if (!hypervisorIp.trim() || !hypervisorInterface.trim() || missingMapping) {
        throw new Error('Select Hypervisor IP, Hypervisor interface, hardware, branch, and edge before generating.');
      }
      const payload = {
        topology_name: topologyName,
        reference_topology_id: selectedReferenceId,
        hypervisor_ip: hypervisorIp,
        hypervisor_interface: hypervisorInterface,
        requested_by: currentUser,
        mappings: mappings.map((mapping) => ({
          hardware_id: mapping.hardware_id,
          branch_name: mapping.branch_name,
          edge_name: mapping.edge_name,
          target_branch_name: mapping.target_branch_name || null,
          target_edge_name: mapping.target_edge_name || null,
          ...(mapping.interface_overrides?.length
            ? {
                interface_overrides: mapping.interface_overrides.map((override) => {
                  const switchVlans = parseSwitchVlanOverride(
                    override.switch_vlans_text || '',
                    override.reference_interface
                  );
                  return {
                    reference_interface: override.reference_interface,
                    hardware_interface: override.hardware_interface || null,
                    ...(switchVlans.length ? { switch_vlans: switchVlans } : {})
                  };
                })
              }
            : {})
        }))
      };
      const generated = await generateTopology(payload);
      setResult(generated);
      const [inventoryData, auditData] = await Promise.all([fetchInventory(), fetchAuditTrail()]);
      setInventory(inventoryData);
      setAuditTrail(auditData.events || []);
    } catch (generateError) {
      setError(generateError.message);
    } finally {
      setGenerating(false);
    }
  }

  async function submitPublishPrivateBranch() {
    if (!result?.run_id) {
      return;
    }
    setPublishingAction('publish');
    setError('');
    try {
      const response = await publishPrivateBranch(result.run_id, {
        base_branch: publishBaseBranch,
        requested_by: currentUser
      });
      setPublishResult(response);
      const [refreshed, auditData] = await Promise.all([fetchPrivateBranches(), fetchAuditTrail()]);
      setPrivateBranches(refreshed.branches || []);
      setAuditTrail(auditData.events || []);
      setCopyState('');
    } catch (publishError) {
      setError(publishError.message);
    } finally {
      setPublishingAction('');
    }
  }

  function togglePrivateBranchSelection(privateBranchName) {
    setSelectedPrivateBranchNames((current) =>
      current.includes(privateBranchName)
        ? current.filter((item) => item !== privateBranchName)
        : [...current, privateBranchName]
    );
  }

  function toggleAllPrivateBranches() {
    setSelectedPrivateBranchNames(
      allPrivateBranchesSelected ? [] : privateBranches.map((branch) => branch.private_branch_name)
    );
  }

  function toggleDataCard(cardName) {
    setCollapsedDataCards((current) => ({
      ...current,
      [cardName]: !current[cardName]
    }));
  }

  async function submitDeletePrivateBranches({ deleteAll = false, branchNames = [] } = {}) {
    const targets = deleteAll ? privateBranches.map((branch) => branch.private_branch_name) : branchNames;
    if (!targets.length) {
      return;
    }
    const prompt = deleteAll
      ? `Delete all ${targets.length} Gerrit private branches from local and remote repos?`
      : `Delete ${targets.length} Gerrit private branch${targets.length === 1 ? '' : 'es'} from local and remote repos?`;
    if (!window.confirm(prompt)) {
      return;
    }
    setPublishingAction('delete-branches');
    setDeletingPrivateBranchNames(targets);
    setPrivateBranchFeedback({
      level: 'info',
      message:
        targets.length === 1
          ? `Deleting Gerrit private branch ${targets[0]}...`
          : `Deleting ${targets.length} Gerrit private branches...`
    });
    setError('');
    try {
      const response = await deletePrivateBranches({
        delete_all: deleteAll,
        private_branch_names: deleteAll ? [] : targets,
        requested_by: currentUser
      });
      const [refreshed, auditData] = await Promise.all([fetchPrivateBranches(), fetchAuditTrail()]);
      setPrivateBranches(refreshed.branches || []);
      setAuditTrail(auditData.events || []);
      setSelectedPrivateBranchNames([]);
      const deletedNames = (response.results || [])
        .filter((item) => item.success)
        .map((item) => item.private_branch_name);
      setPrivateBranchFeedback({
        level: 'success',
        message:
          deletedNames.length === 1
            ? `Deleted Gerrit private branch ${deletedNames[0]}.`
            : response.messages?.[0]?.message ||
              `Deleted ${deletedNames.length} Gerrit private branch${deletedNames.length === 1 ? '' : 'es'}.`
      });
    } catch (deleteError) {
      setPrivateBranchFeedback(null);
      setError(deleteError.message);
    } finally {
      setPublishingAction('');
      setDeletingPrivateBranchNames([]);
    }
  }

  async function copyPrivateBranchName() {
    if (!publishResult?.private_branch_name) {
      return;
    }
    try {
      await navigator.clipboard.writeText(publishResult.private_branch_name);
      setCopyState('copied');
      window.setTimeout(() => setCopyState(''), 1500);
    } catch {
      setCopyState('failed');
      window.setTimeout(() => setCopyState(''), 2000);
    }
  }

  async function submitPreviewSwitches() {
    if (!result?.run_id) {
      return;
    }
    setPreviewingSwitches(true);
    setError('');
    try {
      const response = await configureSwitches(result.run_id, { dry_run: true });
      setSwitchPreview({
        devices: response.devices.map((device) => ({
          ...device,
          command_text: commandsToEditorText(device.commands)
        })),
        messages: response.messages || []
      });
    } catch (previewError) {
      setError(previewError.message);
    } finally {
      setPreviewingSwitches(false);
    }
  }

  function updateSwitchPreviewCommand(deviceId, commandText) {
    setSwitchPreview((current) =>
      current
        ? {
            ...current,
            devices: current.devices.map((device) =>
              device.device_id === deviceId ? { ...device, command_text: commandText } : device
            )
          }
        : current
    );
  }

  async function submitConfigureSwitches() {
    if (!result?.run_id) {
      return;
    }
    setConfiguringSwitches(true);
    setError('');
    try {
      const payload = {};
      if (switchPreview?.devices?.length) {
        const commandOverrides = switchPreview.devices.map((device) => ({
          device_id: device.device_id,
          commands: editorTextToCommands(device.command_text)
        }));
        const emptyDevice = commandOverrides.find((device) => device.commands.length === 0);
        if (emptyDevice) {
          const deviceName =
            switchPreview.devices.find((device) => device.device_id === emptyDevice.device_id)?.device_name ||
            emptyDevice.device_id;
          throw new Error(`Switch preview for ${deviceName} is empty. Add at least one command or refresh the preview.`);
        }
        payload.command_overrides = commandOverrides;
      }
      if (
        !window.confirm(
          switchPreview?.devices?.length
            ? 'Apply the current previewed switch configuration for the generated run?'
            : 'Apply switch configuration for the generated run?'
        )
      ) {
        return;
      }
      const response = await configureSwitches(result.run_id, payload);
      setResult((current) =>
        current
          ? {
              ...current,
              messages: [...current.messages, ...response.messages]
            }
          : current
      );
    } catch (configureError) {
      setError(configureError.message);
    } finally {
      setConfiguringSwitches(false);
    }
  }

  if (loading) {
    return (
      <main className="shell center">
        <div className="loadingCard">
          <BrandMark />
          <Loader2 className="spin" aria-hidden="true" />
          <span>Loading Dynamic Topology Engine</span>
        </div>
      </main>
    );
  }

  if (!currentUser) {
    return (
      <main className="shell center">
        <section className="identityCard">
          <div className="panelTitle">
            <GitBranch size={18} />
            <div>
              <h2>User Session</h2>
              <p>Enter your name and email before using shared hardware reservations.</p>
            </div>
          </div>
          {error && <div className="alert inlineAlert">{error}</div>}
          <form className="identityForm" onSubmit={submitUserProfile}>
            <label>
              <RequiredLabel>Name</RequiredLabel>
              <input
                aria-label="User name"
                value={profileName}
                onChange={(event) => setProfileName(event.target.value)}
                placeholder="Your name"
              />
            </label>
            <label>
              <RequiredLabel>Email</RequiredLabel>
              <input
                aria-label="User email"
                type="email"
                value={profileEmail}
                onChange={(event) => setProfileEmail(event.target.value)}
                placeholder="name@example.com"
              />
            </label>
            <button className="primary" type="submit">
              Continue
            </button>
          </form>
        </section>
      </main>
    );
  }

  return (
    <main className="shell">
      <header className="appHeader">
        <div className="brandCluster">
          <BrandMark />
        </div>
        <div className="headerActions">
          <div className="activeUserPill" aria-label="Current user">
            <span>
              <strong>{currentUser.name}</strong>
              <small>{currentUser.email}</small>
            </span>
            <button className="secondary slimButton" onClick={clearCurrentUser} type="button">
              Change user
            </button>
          </div>
          <div className="metricStrip" aria-label="Workspace summary">
            <MetricPill icon={<GitBranch size={14} />} label="References" value={existingReferenceCount} />
            <MetricPill icon={<HardDrive size={14} />} label="Available hardware" value={`${availableHardwareCount}/${inventory.hardware.length}`} />
            <MetricPill icon={<Server size={14} />} label="Private branches" value={privateBranches.length} />
          </div>
          <button className="iconButton refreshButton" onClick={loadData} aria-label="Refresh data" title="Refresh data">
            <RefreshCw size={18} />
          </button>
        </div>
      </header>

      {error && <div className="alert">{error}</div>}

      <section className="workspace appWorkspace">
        <div className="workspaceColumn sideColumn">
          <CollapsiblePanel
            className="inventoryPanel"
            collapsed={collapsedDataCards.inventory}
            description="Search, inspect, and mark physical hardware availability."
            icon={<Server size={18} />}
            onToggle={() => toggleDataCard('inventory')}
            title="Inventory"
          >
            <label className="searchField">
              Search hardware
              <span>
                <Search size={16} aria-hidden="true" />
                <input
                  value={inventorySearch}
                  onChange={(event) => setInventorySearch(event.target.value)}
                  placeholder="device name, short name, serial, model"
                />
              </span>
            </label>
            <div className="quickFilterRow" aria-label="Inventory quick filters" role="group">
              <button
                className={`quickFilterButton ${inventoryAvailabilityFilter === 'all' ? 'active' : ''}`}
                onClick={() => setInventoryAvailabilityFilter('all')}
                type="button"
              >
                All
              </button>
              <button
                className={`quickFilterButton ${inventoryAvailabilityFilter === 'available' ? 'active' : ''}`}
                onClick={() => setInventoryAvailabilityFilter('available')}
                type="button"
              >
                Available
              </button>
              <button
                className={`quickFilterButton ${inventoryAvailabilityFilter === 'reserved' ? 'active' : ''}`}
                onClick={() => setInventoryAvailabilityFilter('reserved')}
                type="button"
              >
                Reserved
              </button>
            </div>
            <div className="inventoryList">
              {filteredHardware.map((hardware) => (
                <div key={hardware.id} className="inventoryItem">
                  <button
                    className="inventorySummary"
                    onClick={() =>
                      setExpandedHardwareId((current) => (current === hardware.id ? '' : hardware.id))
                    }
                    aria-expanded={expandedHardwareId === hardware.id}
                  >
                    <span>
                      <strong>{hardware.short_name || hardware.display_name}</strong>
                      <small>{hardware.display_name}</small>
                      <span className="inventoryBadges">
                        <StatusBadge tone={hardware.available ? 'success' : 'neutral'}>
                          {hardware.available ? 'Available' : 'Reserved'}
                        </StatusBadge>
                        {!hardware.available && hardware.reservation?.actor && (
                          <StatusBadge tone="warning">By {hardware.reservation.actor.name}</StatusBadge>
                        )}
                        <StatusBadge tone={hardware.ha ? 'accent' : 'neutral'}>
                          {hardware.ha ? 'HA' : 'Standalone'}
                        </StatusBadge>
                        {hardware.path_complete && (
                          <StatusBadge tone="success">Path complete</StatusBadge>
                        )}
                      </span>
                      <small>{hardware.model} / {hardware.ports.length} switch links</small>
                    </span>
                    <ChevronDown size={16} aria-hidden="true" />
                  </button>
                  <label className="toggle">
                    <input
                      type="checkbox"
                      checked={hardware.available}
                      disabled={updatingAvailabilityId === hardware.id}
                      onChange={(event) => changeHardwareAvailability(hardware.id, event.target.checked)}
                    />
                    {updatingAvailabilityId === hardware.id ? 'Saving' : 'Available'}
                  </label>
                  {expandedHardwareId === hardware.id && (
                    <HardwareDetails
                      hardware={hardware}
                      refreshing={refreshingHardwareId === hardware.id}
                      onVlanRangeChange={(boundary, value) => updateHardwareVlanRange(hardware.id, boundary, value)}
                      onRefresh={() => refreshHardwareFromLabNavigator(hardware.id)}
                    />
                  )}
                </div>
              ))}
              {filteredHardware.length === 0 && <p className="muted">No hardware matches the current search.</p>}
            </div>
            <button className="secondary" onClick={persistInventory} disabled={savingInventory}>
              {savingInventory ? <Loader2 className="spin" size={16} /> : <CheckCircle2 size={16} />}
              Save inventory
            </button>
          </CollapsiblePanel>

          <div className="operationsGrid">
            <CollapsiblePanel
              className="branchRegistryPanel"
              collapsed={collapsedDataCards.privateBranches}
              description="Recently created private branches for generated topology runs."
              icon={<GitBranch size={18} />}
              onToggle={() => toggleDataCard('privateBranches')}
              title="Gerrit Private Branches"
            >
              {privateBranches.length > 0 && (
                <div className="branchBulkActions">
                  <label className="toggle bulkSelectToggle">
                    <input
                      type="checkbox"
                      checked={allPrivateBranchesSelected}
                      onChange={toggleAllPrivateBranches}
                    />
                    Select all
                  </label>
                  <button
                    className="secondary bulkDeleteButton"
                    onClick={() => submitDeletePrivateBranches({ branchNames: selectedPrivateBranchNames })}
                    disabled={publishingAction !== '' || selectedPrivateBranchNames.length === 0}
                    aria-label={deletingPrivateBranches ? 'Deleting selected branches' : 'Delete selected'}
                    type="button"
                  >
                    {deletingPrivateBranches ? <Loader2 className="spin" size={16} /> : <Trash2 size={16} />}
                    {deletingPrivateBranches ? 'Deleting...' : 'Delete selected'}
                  </button>
                </div>
              )}
              {privateBranchFeedback && (
                <div className="messageList branchFeedback" role="status" aria-live="polite">
                  <small className={`message ${privateBranchFeedback.level}`}>{privateBranchFeedback.message}</small>
                </div>
              )}
              {privateBranches.length === 0 ? (
                <div className="emptyState">
                  <GitBranch size={18} aria-hidden="true" />
                  <p>No private branches created by the tool yet.</p>
                </div>
              ) : (
                <div className="branchRegistryList">
                  {privateBranches.map((branch) => (
                    <div className="branchRegistryItem" key={branch.private_branch_name}>
                      <label className="branchRegistrySelect">
                        <input
                          type="checkbox"
                          checked={selectedPrivateBranchNames.includes(branch.private_branch_name)}
                          onChange={() => togglePrivateBranchSelection(branch.private_branch_name)}
                        />
                        <span>
                          <strong>{branch.private_branch_name}</strong>
                          <small>
                            {branch.private_branch_pushed ? 'pushed' : 'committed only'} / {branch.base_branch} / run{' '}
                            {branch.run_id}
                          </small>
                          <small>
                            {branch.topology_name} from {branch.reference_topology_id}
                          </small>
                          <small>{branch.destination_relative_path}</small>
                          {branch.remote_branch_ref && <small>{branch.remote_branch_ref}</small>}
                        </span>
                      </label>
                      <button
                        className="iconButton dangerButton branchDeleteButton"
                        onClick={() =>
                          submitDeletePrivateBranches({ branchNames: [branch.private_branch_name] })
                        }
                        disabled={publishingAction !== ''}
                        aria-label={
                          deletingPrivateBranchNames.includes(branch.private_branch_name)
                            ? `Deleting ${branch.private_branch_name}`
                            : `Delete ${branch.private_branch_name}`
                        }
                        title={
                          deletingPrivateBranchNames.includes(branch.private_branch_name)
                            ? 'Deleting branch'
                            : 'Delete branch'
                        }
                        type="button"
                      >
                        {deletingPrivateBranchNames.includes(branch.private_branch_name) ? (
                          <Loader2 className="spin" size={16} />
                        ) : (
                          <Trash2 size={16} />
                        )}
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </CollapsiblePanel>

            <CollapsiblePanel
              className="auditPanel"
              collapsed={collapsedDataCards.auditTrail}
              description="Reservation, inventory, publish, and deletion history for shared hardware usage."
              icon={<TriangleAlert size={18} />}
              onToggle={() => toggleDataCard('auditTrail')}
              title="Audit Trail"
            >
              {auditTrail.length === 0 ? (
                <div className="emptyState">
                  <TriangleAlert size={18} aria-hidden="true" />
                  <p>No audit events recorded yet.</p>
                </div>
              ) : (
                <>
                  <label className="searchField">
                    Search audit trail
                    <span>
                      <Search size={16} aria-hidden="true" />
                      <input
                        aria-label="Search audit trail"
                        value={auditSearch}
                        onChange={(event) => setAuditSearch(event.target.value)}
                        placeholder="action, user, topology, run, hardware"
                      />
                    </span>
                  </label>
                  <div className="auditList">
                    {filteredAuditTrail.map((event) => (
                      <div className="auditItem" key={event.id}>
                        <div className="auditItemHeader">
                          <strong>{event.summary}</strong>
                          <StatusBadge tone={auditTone(event.action)}>{formatAuditAction(event.action)}</StatusBadge>
                        </div>
                        <small>
                          {event.actor.name} ({event.actor.email}) / {formatAuditTimestamp(event.created_at)}
                        </small>
                        {event.details?.topology_name && <small>Topology: {event.details.topology_name}</small>}
                        {event.details?.run_id && <small>Run: {event.details.run_id}</small>}
                      </div>
                    ))}
                    {filteredAuditTrail.length === 0 && <p className="muted">No audit events match the current search.</p>}
                  </div>
                </>
              )}
            </CollapsiblePanel>
          </div>
        </div>

        <div className="workspaceColumn mainColumn">
          <div className="panel setupPanel">
            <div className="panelTitle">
              <GitBranch size={18} />
              <div>
                <h2>Topology Setup</h2>
                <p>Choose the virtual reference and target hypervisor context.</p>
              </div>
            </div>
            <label>
              <RequiredLabel>Topology</RequiredLabel>
              <select
                aria-label="Topology"
                required
                value={selectedReferenceId}
                onChange={(event) => {
                  setSelectedReferenceId(event.target.value);
                  setTopologyName(`${event.target.value.replaceAll('/', '-')}-hw`);
                  setMappings([{ ...emptyMapping }]);
                }}
              >
                {references.map((reference) => (
                  <option key={reference.id} value={reference.id} disabled={!reference.exists}>
                    {reference.id}
                    {!reference.exists ? ' unavailable' : ''}
                  </option>
                ))}
              </select>
            </label>

            <label>
              <RequiredLabel>Output topology name</RequiredLabel>
              <input
                aria-label="Output topology name"
                required
                value={topologyName}
                onChange={(event) => setTopologyName(event.target.value)}
              />
            </label>

            <label htmlFor={hypervisorIpFieldId}>
              <RequiredLabel>Hypervisor IP</RequiredLabel>
              <SearchableTextCombobox
                aria-label="Hypervisor IP"
                inputId={hypervisorIpFieldId}
                options={hypervisorIpOptions}
                placeholder="Search and select hypervisor IP"
                value={hypervisorIp}
                onChange={setHypervisorIp}
              />
            </label>

            <label htmlFor={hypervisorInterfaceFieldId}>
              <RequiredLabel>Hypervisor interface</RequiredLabel>
              <SearchableTextCombobox
                aria-label="Hypervisor interface"
                inputId={hypervisorInterfaceFieldId}
                options={hypervisorInterfaceOptions}
                placeholder="Search and select hypervisor interface"
                value={hypervisorInterface}
                onChange={setHypervisorInterface}
              />
            </label>

            <div className="branchList">
              {selectedReference?.branches.map((branch) => (
                <div key={branch.name} className="branchItem">
                  <span>
                    <strong>{branch.name}</strong>
                    <small>{branch.edges.length} edge{branch.edges.length === 1 ? '' : 's'}</small>
                  </span>
                  <small>{branch.edges.map((edge) => `${edge.name} (${edge.model})`).join(', ')}</small>
                </div>
              ))}
            </div>
          </div>

          <div className="panel wide mappingPanel">
          <div className="panelTitle">
            <HardDrive size={18} />
            <div>
              <h2>Hardware Mapping</h2>
              <p>{selectedMappingCount} of {mappings.length} mapping row{mappings.length === 1 ? '' : 's'} ready.</p>
            </div>
          </div>
          {mappings.map((mapping, index) => (
            <MappingRow
              key={index}
              index={index}
              mapping={mapping}
              mappings={mappings}
              reference={selectedReference}
              inventory={inventory}
              onChange={updateMapping}
              onRemove={removeMapping}
              canRemove={mappings.length > 1}
            />
          ))}
          <div className="actionRow">
            <button className="secondary" onClick={addMapping}>
              <Plus size={16} />
              Add mapping
            </button>
            <button className="primary" onClick={submitGenerate} disabled={generating}>
              {generating ? <Loader2 className="spin" size={16} /> : <Archive size={16} />}
              Generate zip
            </button>
          </div>
          </div>

        <div className="panel wide previewPanel">
          <div className="panelTitle">
            <CheckCircle2 size={18} />
            <div>
              <h2>Preview & Delivery</h2>
              <p>Validate generated names, download output, publish branches, and configure switches.</p>
            </div>
          </div>
          {previewRows.length === 0 ? (
            <div className="emptyState">
              <Archive size={18} aria-hidden="true" />
              <p>Select hardware, branch, and edge to preview generated names.</p>
            </div>
          ) : (
            <div className="previewTable">
              <div className="previewRow previewHeader" aria-hidden="true">
                <span>Hardware</span>
                <span>Branch</span>
                <span>Edge</span>
                <span>Links</span>
              </div>
              {previewRows.map((row, index) => (
                <div className="previewRow" key={`${row.hardware}-${index}`}>
                  <span>{row.hardware}</span>
                  <span>{row.branch}</span>
                  <span>{row.edge}</span>
                  <span>
                    {row.configurablePorts}/{row.ports} switch links
                  </span>
                </div>
              ))}
            </div>
          )}

          {result && (
            <div className="resultBox">
              <div className="resultHeader">
                <CheckCircle2 size={18} aria-hidden="true" />
                <span>
                  <strong>{result.topology_name}</strong>
                  <small>{result.topology_path}</small>
                </span>
              </div>
              {result.mapping_statuses?.length > 0 && (
                <div className="messageList">
                  {result.mapping_statuses.map((status, index) => (
                    <small
                      className={`message ${
                        status.auto_config_ready ? 'info' : status.path_resolved ? 'warning' : 'error'
                      }`}
                      key={`${status.hardware_id}-${index}`}
                    >
                      {status.branch_name}/{status.edge_name}: {status.path_resolved ? 'path resolved' : 'path unresolved'}
                      {status.path?.access_switch_name ? ` via ${status.path.access_switch_name}` : ''}
                      {status.path?.upstream_switch_name ? ` -> ${status.path.upstream_switch_name}` : ''}
                      {status.path?.hypervisor_name ? ` -> ${status.path.hypervisor_name}` : ''}
                      {status.auto_config_ready ? ' (switch auto-config ready)' : ''}
                      {status.reason ? ` - ${status.reason}` : ''}
                    </small>
                  ))}
                </div>
              )}
              <div className="messageList">
                {result.messages.map((message, index) => (
                  <small className={`message ${message.level}`} key={index}>
                    {message.level}: {message.message}
                  </small>
                ))}
              </div>
              <div className="publishControls">
                <label className="publishField">
                  Base branch for Gerrit private branch
                  <select
                    aria-label="Base branch for Gerrit private branch"
                    value={publishBaseBranch}
                    onChange={(event) => setPublishBaseBranch(event.target.value)}
                    disabled={publishingAction !== ''}
                  >
                    {hapyBaseBranches.map((branchName) => (
                      <option key={branchName} value={branchName}>
                        {branchName}
                      </option>
                    ))}
                  </select>
                </label>
                <small className="muted">
                  This publishes one Gerrit private branch for the generated run using the selected base branch.
                </small>
              </div>
              {publishResult && (
                <div className="publishBox">
                  <strong>Gerrit private branch details</strong>
                  <small>Repo destination: {publishResult.destination_relative_path}</small>
                  <small>Base branch: {publishResult.base_branch}</small>
                  <small>Private branch: {publishResult.private_branch_name}</small>
                  <small>Commit: {publishResult.commit_sha}</small>
                  {publishResult.remote_branch_ref && <small>Remote ref: {publishResult.remote_branch_ref}</small>}
                  {publishResult.fetch_command && <small>Fetch command: {publishResult.fetch_command}</small>}
                  {publishResult.private_branch_pushed && (
                    <div className="copyRow">
                      <button className="secondary" onClick={copyPrivateBranchName}>
                        <Copy size={16} />
                        Copy branch name
                      </button>
                      {copyState === 'copied' && <small className="muted">Copied</small>}
                      {copyState === 'failed' && <small className="message error">Copy failed</small>}
                    </div>
                  )}
                  {publishResult.messages?.length > 0 && (
                    <div className="messageList">
                      {publishResult.messages.map((message, index) => (
                        <small className={`message ${message.level}`} key={`publish-${index}`}>
                          {message.level}: {message.message}
                        </small>
                      ))}
                    </div>
                  )}
                </div>
              )}
              <div className="resultActions">
                <a className="download" href={result.download_url}>
                  <Download size={16} />
                  Download zip
                </a>
                <button
                  className="secondary"
                  onClick={submitPublishPrivateBranch}
                  disabled={publishingAction !== '' || generating}
                >
                  {publishingAction === 'publish' ? <Loader2 className="spin" size={16} /> : <GitBranch size={16} />}
                  Commit And Push Gerrit Private Branch
                </button>
                <button
                  className="secondary"
                  onClick={submitPreviewSwitches}
                  disabled={!configureSwitchState.enabled || previewingSwitches || configuringSwitches || publishingAction !== ''}
                  title={configureSwitchState.reason}
                >
                  {previewingSwitches ? <Loader2 className="spin" size={16} /> : <Eye size={16} />}
                  {switchPreview ? 'Refresh preview' : 'Preview config'}
                </button>
                <button
                  className="secondary"
                  onClick={submitConfigureSwitches}
                  disabled={
                    !configureSwitchState.enabled || configuringSwitches || previewingSwitches || publishingAction !== ''
                  }
                  title={configureSwitchState.reason}
                >
                  {configuringSwitches ? <Loader2 className="spin" size={16} /> : <Server size={16} />}
                  Configure switches
                </button>
              </div>
              {!configureSwitchState.enabled && configureSwitchState.reason && (
                <small className="muted">{configureSwitchState.reason}</small>
              )}
              {switchPreview && (
                <div className="switchPreviewBox">
                  <div className="switchPreviewHeader">
                    <strong>Switch config preview</strong>
                    <small>Edit the commands below if needed. Configure switches applies the current text.</small>
                  </div>
                  {switchPreview.messages?.length > 0 && (
                    <div className="messageList">
                      {switchPreview.messages.map((message, index) => (
                        <small className={`message ${message.level}`} key={`preview-${index}`}>
                          {message.level}: {message.message}
                        </small>
                      ))}
                    </div>
                  )}
                  {switchPreview.devices.length === 0 ? (
                    <small className="muted">No switch commands were generated for this run.</small>
                  ) : (
                    <div className="switchPreviewList">
                      {switchPreview.devices.map((device) => (
                        <label className="switchPreviewDevice" key={device.device_id}>
                          <span className="switchPreviewDeviceTitle">
                            <strong>{device.device_name}</strong>
                            <small>{device.device_ip}</small>
                          </span>
                          <textarea
                            aria-label={`Switch commands for ${device.device_name}`}
                            className="switchPreviewEditor"
                            rows={Math.min(18, Math.max(6, device.command_text.split('\n').length + 1))}
                            value={device.command_text}
                            onChange={(event) => updateSwitchPreviewCommand(device.device_id, event.target.value)}
                          />
                        </label>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
        </div>

        </div>
      </section>
    </main>
  );
}

function formatSwitchSummary(hardware) {
  const switches = hardware.switches?.length ? hardware.switches : hardware.switch ? [hardware.switch] : [];
  if (switches.length === 0) {
    return 'no switch metadata';
  }
  if (switches.length === 1) {
    return `${switches[0].name} ${switches[0].connections.ip}`;
  }
  return `${switches.length} switches`;
}

function loadStoredUser() {
  try {
    const raw = getSafeStorage()?.getItem(userStorageKey);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function storeUser(user) {
  getSafeStorage()?.setItem(userStorageKey, JSON.stringify(user));
}

function clearStoredUser() {
  getSafeStorage()?.removeItem(userStorageKey);
}

function getSafeStorage() {
  const candidate = typeof window !== 'undefined' ? window.localStorage : null;
  if (!candidate || typeof candidate.getItem !== 'function') {
    return null;
  }
  return candidate;
}

function formatAuditAction(action) {
  return String(action || '')
    .replaceAll('_', ' ')
    .replace(/\b\w/g, (match) => match.toUpperCase());
}

function auditTone(action) {
  if (['hardware_reserved', 'hardware_marked_unavailable'].includes(action)) {
    return 'warning';
  }
  if (['hardware_released', 'private_branch_published'].includes(action)) {
    return 'success';
  }
  if (action === 'private_branch_deleted') {
    return 'neutral';
  }
  return 'accent';
}

function formatAuditTimestamp(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value || '';
  }
  return date.toLocaleString();
}

function auditSearchText(event) {
  return [
    event.id,
    event.action,
    formatAuditAction(event.action),
    event.summary,
    event.target_type,
    event.target_id,
    event.actor?.name,
    event.actor?.email,
    event.created_at,
    formatAuditTimestamp(event.created_at),
    event.details?.topology_name,
    event.details?.run_id,
    event.details?.hardware_id,
    event.details ? JSON.stringify(event.details) : ''
  ]
    .filter(Boolean)
    .join(' ')
    .toLowerCase();
}

function hardwareSearchText(hardware) {
  const switches = hardware.switches?.length ? hardware.switches : hardware.switch ? [hardware.switch] : [];
  return [
    hardware.short_name,
    hardware.display_name,
    hardware.id,
    hardware.model,
    hardware.model_suffix,
    hardware.active_serial,
    hardware.standby_serial,
    hardware.reservation?.actor?.name,
    hardware.reservation?.actor?.email,
    hardware.notes,
    ...switches.flatMap((item) => [item.name, item.connections?.ip]),
    ...hardware.ports.flatMap((port) => [
      port.logical_name,
      port.logical_interface,
      port.switch_name,
      port.switch_active_port,
      port.switch_standby_port,
      ...(port.switch_vlans || [])
    ])
  ]
    .filter(Boolean)
    .join(' ')
    .toLowerCase();
}

function buildHypervisorOptions(inventory) {
  const deviceById = new Map(Object.values(inventory.devices || {}).map((device) => [device.id, device]));
  const optionsById = new Map();

  function ensureOption(deviceId) {
    const device = deviceById.get(deviceId);
    if (!device || device.type !== 'hypervisor') {
      return null;
    }
    if (!optionsById.has(deviceId)) {
      optionsById.set(deviceId, {
        id: device.id,
        ip: typeof device.ip_address === 'string' ? device.ip_address.trim() : '',
        display_name: device.display_name || '',
        model: device.model || '',
        serial_number: device.serial_number || '',
        interfaces: new Set()
      });
    }
    return optionsById.get(deviceId);
  }

  Object.values(inventory.devices || {}).forEach((device) => {
    if (device.type === 'hypervisor') {
      ensureOption(device.id);
    }
  });

  (inventory.connections || []).forEach((connection) => {
    if (connection.role !== 'hypervisor-access') {
      return;
    }
    const hypervisorEndpoint = [connection.a, connection.b].find(
      (endpoint) => deviceById.get(endpoint.device_id)?.type === 'hypervisor'
    );
    if (!hypervisorEndpoint) {
      return;
    }
    const option = ensureOption(hypervisorEndpoint.device_id);
    const interfaceName = hypervisorEndpoint.interface?.trim();
    if (option && interfaceName) {
      option.interfaces.add(interfaceName);
    }
  });

  if (!optionsById.size) {
    (inventory.hardware || []).forEach((hardware) => {
      const ip = hardware.path?.hypervisor_ip?.trim();
      if (!ip || optionsById.has(ip)) {
        return;
      }
      optionsById.set(ip, {
        id: ip,
        ip,
        display_name: hardware.path?.hypervisor_name || '',
        model: '',
        serial_number: '',
        interfaces: new Set()
      });
    });
  }

  const optionsByIp = new Map();
  optionsById.forEach((option) => {
    if (!option.ip) {
      return;
    }
    if (!optionsByIp.has(option.ip)) {
      optionsByIp.set(option.ip, {
        ...option,
        interfaces: new Set(option.interfaces)
      });
      return;
    }
    const existing = optionsByIp.get(option.ip);
    if (!existing.display_name && option.display_name) {
      existing.display_name = option.display_name;
    }
    if (!existing.model && option.model) {
      existing.model = option.model;
    }
    if (!existing.serial_number && option.serial_number) {
      existing.serial_number = option.serial_number;
    }
    option.interfaces.forEach((interfaceName) => existing.interfaces.add(interfaceName));
  });

  return [...optionsByIp.values()]
    .map((option) => ({
      ...option,
      interfaces: [...option.interfaces].sort(compareHypervisorInterfaceNames)
    }))
    .sort((left, right) => left.ip.localeCompare(right.ip, undefined, { numeric: true }));
}

function hypervisorIpOptionLabel(option) {
  return option.display_name ? `${option.ip} - ${option.display_name}` : option.ip;
}

function hypervisorSearchText(option) {
  return [
    option.ip,
    option.display_name,
    option.model,
    option.serial_number,
    ...(option.interfaces || [])
  ]
    .filter(Boolean)
    .join(' ')
    .toLowerCase();
}

function isManagementHypervisorInterface(interfaceName) {
  return /idrac|ilo|bmc/i.test(interfaceName || '');
}

function compareHypervisorInterfaceNames(left, right) {
  const leftRank = isManagementHypervisorInterface(left) ? 1 : 0;
  const rightRank = isManagementHypervisorInterface(right) ? 1 : 0;
  if (leftRank !== rightRank) {
    return leftRank - rightRank;
  }
  return left.localeCompare(right, undefined, { numeric: true, sensitivity: 'base' });
}

function commandsToEditorText(commands) {
  return (commands || []).join('\n');
}

function editorTextToCommands(value) {
  return String(value || '')
    .split('\n')
    .map((command) => command.replace(/\r/g, '').replace(/\s+$/g, ''))
    .filter((command) => command.trim());
}

function hardwareOptionLabel(hardware) {
  const prefix = hardware.short_name ? `${hardware.short_name} - ` : '';
  const state = hardware.ha ? 'HA' : 'standalone';
  const reservation = hardware.available
    ? 'available'
    : `reserved by ${hardware.reservation?.actor?.name || 'unknown user'}`;
  return `${prefix}${hardware.display_name} (${hardware.model}, ${state}, ${reservation})`;
}

function getReferenceInterfaceKey(interfaceSummary) {
  return (
    interfaceSummary?.logical_interface ||
    interfaceSummary?.logical_name ||
    interfaceSummary?.name ||
    ''
  )
    .trim()
    .toUpperCase();
}

function isLoopbackName(value) {
  if (!value || typeof value !== 'string') {
    return false;
  }
  return ['LO', 'LO0', 'LO1'].includes(value.trim().split('.', 1)[0].toUpperCase());
}

function isLoopbackInterface(interfaceSummary) {
  if (String(interfaceSummary?.type || '').toLowerCase() === 'loopback') {
    return true;
  }
  return ['logical_interface', 'logical_name', 'name'].some((key) =>
    isLoopbackName(interfaceSummary?.[key])
  );
}

function referenceInterfaceLabel(interfaceSummary) {
  const logicalInterface = getReferenceInterfaceKey(interfaceSummary);
  const logicalName = interfaceSummary?.logical_name?.trim();
  if (logicalName && logicalName.toUpperCase() !== logicalInterface) {
    return `${logicalName} (${logicalInterface})`;
  }
  return logicalInterface || interfaceSummary?.name || 'Unnamed interface';
}

function referenceInterfaceVlanSummary(interfaceSummary) {
  const baseVlans = Array.isArray(interfaceSummary?.vlans) ? interfaceSummary.vlans : [];
  const subinterfaceVlans = Array.isArray(interfaceSummary?.subinterfaces)
    ? interfaceSummary.subinterfaces
        .map((item) => item?.vlan)
        .filter((value) => Number.isInteger(value))
    : [];
  const vlans = [...new Set([...baseVlans, ...subinterfaceVlans])];
  return vlans.length ? `Reference VLANs ${vlans.join(', ')}` : '';
}

function referenceInterfaceVlanRequirements(interfaceSummary) {
  const vlans = Array.isArray(interfaceSummary?.vlans) ? interfaceSummary.vlans : [];
  const subinterfaces = Array.isArray(interfaceSummary?.subinterfaces) ? interfaceSummary.subinterfaces : [];
  if (interfaceSummary?.mode === 'switched') {
    return {
      nativeCount: 1,
      taggedCount: Math.max(vlans.length - 1, 0)
    };
  }
  if (subinterfaces.length) {
    return {
      nativeCount: 1,
      taggedCount: subinterfaces.length
    };
  }
  if (vlans.length) {
    return {
      nativeCount: 1,
      taggedCount: 0
    };
  }
  return {
    nativeCount: 0,
    taggedCount: 0
  };
}

function referenceInterfaceVlanPlan(interfaceSummary, port) {
  const { nativeCount, taggedCount } = referenceInterfaceVlanRequirements(interfaceSummary);
  const totalCount = nativeCount + taggedCount;
  const inventoryVlans = Array.isArray(port?.switch_vlans)
    ? port.switch_vlans.filter((value) => Number.isInteger(value))
    : [];
  const inventoryNative = Number.isInteger(port?.untagged_vlan) ? port.untagged_vlan : inventoryVlans[0];
  if (totalCount === 0) {
    return {
      totalCount: 1,
      summary: 'Needs 1 native VLAN',
      placeholder: inventoryVlans.length ? 'Reuse inventory access VLAN' : 'Auto-allocate 1 from range',
      autoSummary: inventoryVlans.length
        ? inventoryNative != null
          ? `Leave blank to reuse inventory native VLAN ${inventoryNative}. Enter 1 VLAN to override it.`
          : `Leave blank to reuse inventory VLAN ${inventoryVlans.join(', ')}. Enter 1 VLAN to override the switch access/native VLAN.`
        : port
          ? 'Leave blank to auto-allocate 1 VLAN from the hardware range for the access switch.'
          : 'Select a hardware port to allocate the switch access/native VLAN.'
    };
  }
  const parts = [];
  if (nativeCount) {
    parts.push('1 native');
  }
  if (taggedCount) {
    parts.push(`${taggedCount} tagged`);
  }
  return {
    totalCount,
    summary: `Needs ${parts.join(' + ')} VLAN${totalCount > 1 ? 's' : ''}`,
    placeholder: `Auto-allocate ${totalCount} from range`,
    autoSummary: `Leave blank to auto-allocate ${totalCount} VLAN${totalCount > 1 ? 's' : ''} from the hardware range.`
  };
}

function hardwareVlanPool(hardware) {
  const freeVlans = Array.isArray(hardware?.free_vlans)
    ? hardware.free_vlans.filter((value) => Number.isInteger(value) && value >= 1 && value <= 4094)
    : [];
  if (freeVlans.length) {
    return freeVlans;
  }
  const rangeStart = hardware?.vlan_range?.start;
  const rangeEnd = hardware?.vlan_range?.end;
  if (!Number.isInteger(rangeStart) || !Number.isInteger(rangeEnd) || rangeStart > rangeEnd) {
    return [];
  }
  const start = Math.max(rangeStart, 1);
  const end = Math.min(rangeEnd, 4094);
  if (start > end) {
    return [];
  }
  return Array.from({ length: end - start + 1 }, (_, index) => start + index);
}

function parseVlanPreview(value) {
  const trimmed = String(value || '').trim();
  if (!trimmed) {
    return [];
  }
  return trimmed
    .split(',')
    .map((token) => token.trim())
    .filter((token) => /^\d+$/.test(token))
    .map((token) => Number(token))
    .filter((vlan) => vlan >= 1 && vlan <= 4094);
}

function buildAutoVlanAllocationPreview(referenceInterfaces, assignments, hardware, hardwarePortByInterface) {
  const pool = hardwareVlanPool(hardware);
  const preview = new Map();
  if (!pool.length) {
    return preview;
  }

  const assignmentByReference = new Map(
    assignments.map((assignment) => [assignment.reference_interface, assignment])
  );
  const explicitReserved = new Set();
  const dynamicPairs = [];

  referenceInterfaces.forEach((interfaceSummary) => {
    const referenceInterface = getReferenceInterfaceKey(interfaceSummary);
    const assignment = assignmentByReference.get(referenceInterface);
    const port = hardwarePortByInterface.get(assignment?.hardware_interface || '');
    if (!assignment?.hardware_interface || !port) {
      return;
    }
    if (assignment.switch_vlans_text?.trim()) {
      const manualVlans = parseVlanPreview(assignment.switch_vlans_text);
      manualVlans.forEach((vlan) => explicitReserved.add(vlan));
      return;
    }
    dynamicPairs.push({ referenceInterface, interfaceSummary, port });
  });

  const available = pool.filter((vlan) => !explicitReserved.has(vlan));
  let cursor = 0;

  dynamicPairs.forEach(({ referenceInterface, interfaceSummary, port }) => {
    const { nativeCount, taggedCount } = referenceInterfaceVlanRequirements(interfaceSummary);
    const requiredCount = nativeCount + taggedCount;
    if (requiredCount === 0) {
      if (port.switch_vlans?.length || port.untagged_vlan != null || cursor >= available.length) {
        return;
      }
      preview.set(referenceInterface, [available[cursor]]);
      cursor += 1;
      return;
    }
    if (cursor + requiredCount > available.length) {
      return;
    }
    preview.set(referenceInterface, available.slice(cursor, cursor + requiredCount));
    cursor += requiredCount;
  });

  return preview;
}

function hardwareInterfaceOptionLabel(port) {
  return `${port.logical_interface} / ${port.switch_active_port} / ${hardwarePortModeSummary(port)}`;
}

function hardwarePortModeSummary(port) {
  if (!port.switch_vlans?.length) {
    return 'dynamic';
  }
  if (port.untagged_vlan != null && port.switch_vlans.length === 1) {
    return `untagged ${port.untagged_vlan}`;
  }
  if (port.untagged_vlan != null) {
    const taggedCount = Math.max(port.switch_vlans.length - 1, 0);
    return taggedCount > 0
      ? `native ${port.untagged_vlan} + ${taggedCount} tagged`
      : `native ${port.untagged_vlan}`;
  }
  return `${port.switch_vlans.length} tagged`;
}

function hardwarePortHint(port, plannedVlans = []) {
  if (!port) {
    return 'This reference interface will be dropped from the generated topology.';
  }
  const inventoryVlans = port.switch_vlans?.length
    ? port.switch_vlans
    : Number.isInteger(port.untagged_vlan)
      ? [port.untagged_vlan]
      : [];
  const vlanSummary = inventoryVlans.length
    ? `Current inventory VLANs ${inventoryVlans.join(', ')}`
    : plannedVlans.length
      ? `Will auto-assign VLAN${plannedVlans.length > 1 ? 's' : ''} ${plannedVlans.join(', ')} from hardware range`
      : 'No hardware VLAN range available for auto-allocation';
  return `${port.logical_interface}${port.logical_name ? ` (${port.logical_name})` : ''} on ${port.switch_active_port}${port.switch_standby_port ? ` / standby ${port.switch_standby_port}` : ''}. ${vlanSummary}.`;
}

function parseSwitchVlanOverride(value, referenceInterface) {
  const trimmed = value.trim();
  if (!trimmed) {
    return [];
  }
  return trimmed.split(',').map((token) => {
    const cleaned = token.trim();
    if (!/^\d+$/.test(cleaned)) {
      throw new Error(
        `Optional VLAN override for ${referenceInterface} must be a comma-separated list of VLAN numbers.`
      );
    }
    const vlan = Number(cleaned);
    if (vlan < 1 || vlan > 4094) {
      throw new Error(`Optional VLAN override for ${referenceInterface} must stay between 1 and 4094.`);
    }
    return vlan;
  });
}

function interfaceSortKey(interfaceName) {
  const upper = interfaceName.toUpperCase();
  const geMatch = upper.match(/^GE(\d+)$/);
  if (geMatch) {
    const number = Number(geMatch[1]);
    return [number <= 4 ? 0 : 2, number];
  }
  const sfpMatch = upper.match(/^SFP(\d+)$/);
  if (sfpMatch) {
    return [1, Number(sfpMatch[1])];
  }
  return [9, 999];
}

function compareInterfaceNames(left, right) {
  const [leftTier, leftNumber] = interfaceSortKey(left);
  const [rightTier, rightNumber] = interfaceSortKey(right);
  if (leftTier !== rightTier) {
    return leftTier - rightTier;
  }
  return leftNumber - rightNumber;
}

function topologyHardwarePorts(hardware) {
  if (!hardware) {
    return [];
  }
  return [...hardware.ports].sort((left, right) =>
    compareInterfaceNames(left.logical_interface, right.logical_interface)
  );
}

function referenceVlanProfile(interfaceSummary) {
  const vlans = Array.isArray(interfaceSummary?.vlans) ? interfaceSummary.vlans : [];
  const subinterfaces = Array.isArray(interfaceSummary?.subinterfaces) ? interfaceSummary.subinterfaces : [];
  const taggedCount =
    interfaceSummary?.mode === 'switched' ? Math.max(vlans.length - 1, 0) : subinterfaces.length;
  return {
    hasUntagged: vlans.length > 0,
    hasTagged: taggedCount > 0,
    taggedCount
  };
}

function hardwareVlanProfile(port) {
  let taggedVlans = Array.isArray(port.tagged_vlans) ? [...port.tagged_vlans] : [];
  if (!taggedVlans.length && port.switch_vlans?.length > 1 && port.untagged_vlan != null) {
    taggedVlans = port.switch_vlans.slice(1);
  }
  const taggedCount = taggedVlans.length;
  const hasTagged = taggedCount > 0;
  const hasUntagged = port.untagged_vlan != null || Boolean(port.switch_vlans?.length && !hasTagged);
  return { hasUntagged, hasTagged, taggedCount };
}

function portMatchScore(interfaceSummary, port) {
  const referenceProfile = referenceVlanProfile(interfaceSummary);
  const hardwareProfile = hardwareVlanProfile(port);
  let score = 0;
  if (
    referenceProfile.hasUntagged === hardwareProfile.hasUntagged &&
    referenceProfile.hasTagged === hardwareProfile.hasTagged
  ) {
    score += 100;
  }
  score += referenceProfile.hasTagged === hardwareProfile.hasTagged ? 40 : -60;
  score += referenceProfile.hasUntagged === hardwareProfile.hasUntagged ? 30 : -45;
  score -= Math.abs(referenceProfile.taggedCount - hardwareProfile.taggedCount) * 5;
  return score;
}

function matchPortsToReferenceInterfaces(referenceInterfaces, hardwarePorts) {
  if (!referenceInterfaces.length || !hardwarePorts.length) {
    return [];
  }

  const memo = new Map();

  function assign(referenceIndex, usedMask) {
    const cacheKey = `${referenceIndex}:${usedMask}`;
    if (memo.has(cacheKey)) {
      return memo.get(cacheKey);
    }
    if (referenceIndex === referenceInterfaces.length) {
      const result = { score: 0, order: [] };
      memo.set(cacheKey, result);
      return result;
    }

    let best = { score: Number.NEGATIVE_INFINITY, order: [] };
    hardwarePorts.forEach((port, portIndex) => {
      if (usedMask & (1 << portIndex)) {
        return;
      }
      const next = assign(referenceIndex + 1, usedMask | (1 << portIndex));
      const score = portMatchScore(referenceInterfaces[referenceIndex], port) + next.score;
      if (score > best.score) {
        best = { score, order: [portIndex, ...next.order] };
      }
    });
    memo.set(cacheKey, best);
    return best;
  }

  const { order } = assign(0, 0);
  return order.map((portIndex) => hardwarePorts[portIndex]);
}

function buildDefaultInterfaceAssignments(edge, hardware) {
  const referenceInterfaces = (edge?.interfaces || []).filter(
    (item) => getReferenceInterfaceKey(item) && !isLoopbackInterface(item)
  );
  const hardwarePorts = topologyHardwarePorts(hardware);
  const mappedCount = Math.min(referenceInterfaces.length, hardwarePorts.length);
  const mappedInterfaces = referenceInterfaces.slice(0, mappedCount);
  const matchedPorts = matchPortsToReferenceInterfaces(mappedInterfaces, hardwarePorts);
  const matchedPortByInterface = new Map(
    mappedInterfaces.map((item, index) => [getReferenceInterfaceKey(item), matchedPorts[index]?.logical_interface || ''])
  );

  return referenceInterfaces.map((item) => ({
    reference_interface: getReferenceInterfaceKey(item),
    hardware_interface: matchedPortByInterface.get(getReferenceInterfaceKey(item)) || '',
    switch_vlans_text: ''
  }));
}

function mergeInterfaceAssignments(defaultAssignments, overrideAssignments) {
  if (!overrideAssignments?.length) {
    return defaultAssignments;
  }
  const overrideMap = new Map(
    overrideAssignments
      .filter((item) => item.reference_interface)
      .map((item) => [
        item.reference_interface.trim().toUpperCase(),
        {
          hardware_interface: item.hardware_interface?.trim().toUpperCase() || '',
          switch_vlans_text: item.switch_vlans_text || ''
        }
      ])
  );
  return defaultAssignments.map((assignment) => ({
    ...assignment,
    hardware_interface: overrideMap.has(assignment.reference_interface)
      ? overrideMap.get(assignment.reference_interface).hardware_interface
      : assignment.hardware_interface,
    switch_vlans_text: overrideMap.has(assignment.reference_interface)
      ? overrideMap.get(assignment.reference_interface).switch_vlans_text
      : assignment.switch_vlans_text
  }));
}

function normalizeInterfaceAssignments(assignments) {
  return assignments.map((assignment) => ({
    reference_interface: assignment.reference_interface.trim().toUpperCase(),
    hardware_interface: assignment.hardware_interface?.trim().toUpperCase() || '',
    switch_vlans_text: assignment.switch_vlans_text?.trim() || ''
  }));
}

function interfaceAssignmentsEqual(left, right) {
  if (left.length !== right.length) {
    return false;
  }
  return left.every(
    (assignment, index) =>
      assignment.reference_interface === right[index].reference_interface &&
      assignment.hardware_interface === right[index].hardware_interface &&
      assignment.switch_vlans_text === right[index].switch_vlans_text
  );
}

function HardwareDetails({ hardware, refreshing, onVlanRangeChange, onRefresh }) {
  const switches = hardware.switches?.length ? hardware.switches : hardware.switch ? [hardware.switch] : [];
  return (
    <div className="hardwareDetails">
      <div className="detailGrid">
        <span>
          <small>Active serial</small>
          <strong>{hardware.active_serial}</strong>
        </span>
        <span>
          <small>Standby serial</small>
          <strong>{hardware.standby_serial || 'none'}</strong>
        </span>
        <span>
          <small>Switches</small>
          <strong>{formatSwitchSummary(hardware)}</strong>
        </span>
        <span>
          <small>Path status</small>
          <strong>{hardware.path_complete ? 'complete' : 'incomplete'}</strong>
        </span>
        <span>
          <small>Reservation</small>
          <strong>
            {hardware.available
              ? 'available'
              : hardware.reservation?.actor
                ? `${hardware.reservation.actor.name} (${hardware.reservation.actor.email})`
                : 'reserved'}
          </strong>
        </span>
        <span>
          <small>Reserved for</small>
          <strong>{hardware.reservation?.topology_name || hardware.reservation?.reason || 'n/a'}</strong>
        </span>
      </div>

      <div className="detailGrid">
        <label>
          <small>VLAN range start</small>
          <input
            aria-label={`VLAN range start for ${hardware.id}`}
            type="number"
            value={hardware.vlan_range?.start ?? ''}
            onChange={(event) => onVlanRangeChange('start', event.target.value)}
          />
        </label>
        <label>
          <small>VLAN range end</small>
          <input
            aria-label={`VLAN range end for ${hardware.id}`}
            type="number"
            value={hardware.vlan_range?.end ?? ''}
            onChange={(event) => onVlanRangeChange('end', event.target.value)}
          />
        </label>
      </div>

      <div className="switchList">
        {switches.map((item) => (
          <small key={item.name}>
            {item.name} / {item.model} / {item.connections?.ip || 'no ip'}
          </small>
        ))}
      </div>

      <div className="portList">
        {hardware.ports.map((port) => (
          <small key={`${port.logical_name}-${port.switch_active_port}`}>
            {port.logical_name} {port.logical_interface}: {port.switch_active_port}
            {port.switch_standby_port ? ` / standby ${port.switch_standby_port}` : ''} /{' '}
            {port.switch_vlans?.length ? `VLANs ${port.switch_vlans.join(', ')}` : 'dynamic VLAN allocation'}
          </small>
        ))}
      </div>

      {hardware.path && (
        <div className="switchList">
          <small>
            uplink: {hardware.path.access_switch_name || 'unknown'} {hardware.path.access_uplink_port || 'n/a'} {'->'}{' '}
            {hardware.path.upstream_switch_name || 'unknown'} {hardware.path.upstream_access_port || 'n/a'}
          </small>
          <small>
            hypervisor: {hardware.path.upstream_switch_name || 'unknown'} {hardware.path.upstream_hypervisor_port || 'n/a'} {'->'}{' '}
            {hardware.path.hypervisor_name || hardware.path.hypervisor_ip || 'imported hypervisor'}
          </small>
        </div>
      )}

      <button className="secondary" onClick={onRefresh} disabled={refreshing}>
        {refreshing ? <Loader2 className="spin" size={16} /> : <RefreshCw size={16} />}
        Refresh from Lab Navigator
      </button>

      {hardware.notes && <small className="notes">{hardware.notes}</small>}
    </div>
  );
}

function CollapsiblePanel({ children, className, collapsed, description, icon, onToggle, title }) {
  return (
    <div className={`panel collapsiblePanel${collapsed ? ' collapsedPanel' : ''} ${className || ''}`}>
      <div className="panelTitle collapsiblePanelTitle">
        {icon}
        <div>
          <h2>{title}</h2>
          <p>{description}</p>
        </div>
        <button
          className="iconButton collapseButton"
          onClick={onToggle}
          type="button"
          aria-expanded={!collapsed}
          aria-label={`${collapsed ? 'Expand' : 'Collapse'} ${title}`}
          title={`${collapsed ? 'Expand' : 'Collapse'} ${title}`}
        >
          <ChevronDown className={collapsed ? '' : 'open'} size={16} aria-hidden="true" />
        </button>
      </div>
      {!collapsed && children}
    </div>
  );
}

function BrandMark() {
  return (
    <img src={headerLogo} alt="Dynamic Topology Engine" className="brandMark" />
  );
}

function MetricPill({ icon, label, value }) {
  return (
    <span className="metricPill">
      {icon}
      <span>
        <strong>{value}</strong>
        <small>{label}</small>
      </span>
    </span>
  );
}

function StatusBadge({ children, tone = 'neutral' }) {
  return <span className={`statusBadge ${tone}`}>{children}</span>;
}

function RequiredLabel({ children }) {
  return (
    <span className="labelText">
      {children}
      <span className="requiredMark" aria-hidden="true">
        *
      </span>
    </span>
  );
}

function SearchableTextCombobox({
  ariaLabel,
  inputId,
  options,
  placeholder,
  value,
  onChange,
  disabled = false,
  noResultsText = 'No matches found.'
}) {
  const [isOpen, setIsOpen] = useState(false);
  const [query, setQuery] = useState(value);
  const [highlightedIndex, setHighlightedIndex] = useState(0);
  const listId = `${useId()}-options`;
  const filteredOptions = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    if (!normalizedQuery) {
      return options;
    }
    return options.filter((option) =>
      (option.searchText || option.label || option.value).toLowerCase().includes(normalizedQuery)
    );
  }, [options, query]);

  useEffect(() => {
    if (!isOpen) {
      setQuery(value);
    }
  }, [isOpen, value]);

  useEffect(() => {
    if (highlightedIndex >= filteredOptions.length) {
      setHighlightedIndex(Math.max(filteredOptions.length - 1, 0));
    }
  }, [filteredOptions.length, highlightedIndex]);

  function commitSelection(option) {
    if (!option || option.disabled) {
      return;
    }
    onChange(option.value);
    setQuery(option.value);
    setIsOpen(false);
  }

  function handleBlur(event) {
    if (event.currentTarget.contains(event.relatedTarget)) {
      return;
    }
    setIsOpen(false);
    setQuery(value);
  }

  function handleInputChange(event) {
    const nextQuery = event.target.value;
    setQuery(nextQuery);
    onChange(nextQuery);
    setIsOpen(true);
    setHighlightedIndex(0);
  }

  function handleKeyDown(event) {
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      setIsOpen(true);
      setHighlightedIndex((current) =>
        filteredOptions.length ? Math.min(current + 1, filteredOptions.length - 1) : 0
      );
      return;
    }
    if (event.key === 'ArrowUp') {
      event.preventDefault();
      setIsOpen(true);
      setHighlightedIndex((current) => (filteredOptions.length ? Math.max(current - 1, 0) : 0));
      return;
    }
    if (event.key === 'Enter' && isOpen && filteredOptions.length) {
      event.preventDefault();
      commitSelection(filteredOptions[highlightedIndex]);
      return;
    }
    if (event.key === 'Escape') {
      setIsOpen(false);
      setQuery(value);
    }
  }

  return (
    <div className="combobox" onBlur={handleBlur}>
      <div className={`comboboxField${isOpen ? ' open' : ''}`}>
        <Search size={16} aria-hidden="true" />
        <input
          aria-label={ariaLabel}
          aria-autocomplete="list"
          aria-controls={listId}
          aria-expanded={isOpen}
          aria-haspopup="listbox"
          className="comboboxInput"
          disabled={disabled}
          id={inputId}
          placeholder={placeholder}
          role="combobox"
          value={query}
          onChange={handleInputChange}
          onFocus={() => setIsOpen(true)}
          onKeyDown={handleKeyDown}
        />
        <button
          type="button"
          className="comboboxToggle"
          tabIndex={-1}
          aria-label={`Toggle ${ariaLabel} options`}
          disabled={disabled}
          onMouseDown={(event) => event.preventDefault()}
          onClick={() => setIsOpen((current) => !current)}
        >
          <ChevronDown className={isOpen ? 'open' : ''} size={16} aria-hidden="true" />
        </button>
      </div>
      {isOpen && (
        <div className="comboboxMenu" id={listId} role="listbox">
          {filteredOptions.length ? (
            filteredOptions.map((option, optionIndex) => (
              <button
                key={`${ariaLabel}-${option.value}`}
                type="button"
                role="option"
                aria-selected={option.value === value}
                className={`comboboxOption${optionIndex === highlightedIndex ? ' active' : ''}`}
                disabled={option.disabled}
                onMouseDown={(event) => event.preventDefault()}
                onMouseEnter={() => setHighlightedIndex(optionIndex)}
                onClick={() => commitSelection(option)}
              >
                {option.label}
              </button>
            ))
          ) : (
            <div className="comboboxEmpty">{noResultsText}</div>
          )}
        </div>
      )}
    </div>
  );
}

function HardwareCombobox({ index, hardwareOptions, selectedHardwareId, onSelect }) {
  const [isOpen, setIsOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [highlightedIndex, setHighlightedIndex] = useState(0);
  const selectedHardware = useMemo(
    () => hardwareOptions.find((hardware) => hardware.id === selectedHardwareId),
    [hardwareOptions, selectedHardwareId]
  );
  const selectedLabel = selectedHardware ? hardwareOptionLabel(selectedHardware) : '';
  const listId = `hardware-options-${index}`;
  const filteredHardwareOptions = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    if (!normalizedQuery) {
      return hardwareOptions;
    }
    return hardwareOptions.filter(
      (hardware) =>
        hardware.id === selectedHardwareId || hardwareSearchText(hardware).includes(normalizedQuery)
    );
  }, [hardwareOptions, query, selectedHardwareId]);

  useEffect(() => {
    if (!isOpen) {
      setQuery(selectedLabel);
    }
  }, [isOpen, selectedLabel]);

  useEffect(() => {
    if (highlightedIndex >= filteredHardwareOptions.length) {
      setHighlightedIndex(Math.max(filteredHardwareOptions.length - 1, 0));
    }
  }, [filteredHardwareOptions.length, highlightedIndex]);

  function commitSelection(hardware) {
    if (!hardware || !hardware.available) {
      return;
    }
    onSelect(hardware.id);
    setQuery(hardwareOptionLabel(hardware));
    setIsOpen(false);
  }

  function handleBlur(event) {
    if (event.currentTarget.contains(event.relatedTarget)) {
      return;
    }
    setIsOpen(false);
    if (selectedHardware) {
      setQuery(selectedLabel);
    }
  }

  function handleInputChange(event) {
    const nextQuery = event.target.value;
    setQuery(nextQuery);
    setIsOpen(true);
    setHighlightedIndex(0);
    if (selectedHardwareId && nextQuery !== selectedLabel) {
      onSelect('');
    }
  }

  function handleKeyDown(event) {
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      setIsOpen(true);
      setHighlightedIndex((current) =>
        filteredHardwareOptions.length ? Math.min(current + 1, filteredHardwareOptions.length - 1) : 0
      );
      return;
    }
    if (event.key === 'ArrowUp') {
      event.preventDefault();
      setIsOpen(true);
      setHighlightedIndex((current) => (filteredHardwareOptions.length ? Math.max(current - 1, 0) : 0));
      return;
    }
    if (event.key === 'Enter' && isOpen) {
      event.preventDefault();
      commitSelection(filteredHardwareOptions[highlightedIndex]);
      return;
    }
    if (event.key === 'Escape') {
      setIsOpen(false);
      setQuery(selectedLabel);
    }
  }

  return (
    <div className="combobox" onBlur={handleBlur}>
      <div className={`comboboxField${isOpen ? ' open' : ''}`}>
        <Search size={16} aria-hidden="true" />
        <input
          aria-label="Hardware"
          aria-autocomplete="list"
          aria-controls={listId}
          aria-expanded={isOpen}
          aria-haspopup="listbox"
          className="comboboxInput"
          placeholder="Search and select hardware"
          role="combobox"
          value={query}
          onChange={handleInputChange}
          onFocus={() => setIsOpen(true)}
          onKeyDown={handleKeyDown}
        />
        <button
          type="button"
          className="comboboxToggle"
          tabIndex={-1}
          aria-label={`Toggle hardware options for mapping ${index + 1}`}
          onMouseDown={(event) => event.preventDefault()}
          onClick={() => setIsOpen((current) => !current)}
        >
          <ChevronDown className={isOpen ? 'open' : ''} size={16} aria-hidden="true" />
        </button>
      </div>
      {isOpen && (
        <div className="comboboxMenu" id={listId} role="listbox">
          {filteredHardwareOptions.length ? (
            filteredHardwareOptions.map((hardware, optionIndex) => (
              <button
                key={hardware.id}
                type="button"
                role="option"
                aria-selected={hardware.id === selectedHardwareId}
                className={`comboboxOption${optionIndex === highlightedIndex ? ' active' : ''}`}
                disabled={!hardware.available}
                onMouseDown={(event) => event.preventDefault()}
                onMouseEnter={() => setHighlightedIndex(optionIndex)}
                onClick={() => commitSelection(hardware)}
              >
                {hardwareOptionLabel(hardware)}
              </button>
            ))
          ) : (
            <div className="comboboxEmpty">No hardware matches the current search.</div>
          )}
        </div>
      )}
    </div>
  );
}

function MappingRow({ index, mapping, mappings, reference, inventory, onChange, onRemove, canRemove }) {
  const [interfaceOverridesOpen, setInterfaceOverridesOpen] = useState(false);
  const branch = reference?.branches.find((item) => item.name === mapping.branch_name);
  const selectedHardware = inventory.hardware.find((item) => item.id === mapping.hardware_id);
  const selectedEdge = branch?.edges.find((edge) => edge.name === mapping.edge_name);
  const haMismatch = selectedHardware && selectedEdge?.ha_enabled && !selectedHardware.ha;
  const usedEdgeNames = useMemo(
    () =>
      new Set(
        mappings
          .filter(
            (item, mappingIndex) => mappingIndex !== index && item.branch_name === mapping.branch_name && item.edge_name
          )
          .map((item) => item.edge_name)
      ),
    [index, mapping.branch_name, mappings]
  );
  const referenceInterfaces = useMemo(
    () =>
      (selectedEdge?.interfaces || []).filter(
        (item) => getReferenceInterfaceKey(item) && !isLoopbackInterface(item)
      ),
    [selectedEdge]
  );
  const hardwarePorts = useMemo(() => topologyHardwarePorts(selectedHardware), [selectedHardware]);
  const defaultInterfaceAssignments = useMemo(
    () => buildDefaultInterfaceAssignments(selectedEdge, selectedHardware),
    [selectedEdge, selectedHardware]
  );
  const displayedInterfaceAssignments = useMemo(
    () => mergeInterfaceAssignments(defaultInterfaceAssignments, mapping.interface_overrides || []),
    [defaultInterfaceAssignments, mapping.interface_overrides]
  );
  const hardwarePortByInterface = useMemo(
    () => new Map(hardwarePorts.map((port) => [port.logical_interface, port])),
    [hardwarePorts]
  );
  const autoVlanAllocationPreview = useMemo(
    () =>
      buildAutoVlanAllocationPreview(
        referenceInterfaces,
        displayedInterfaceAssignments,
        selectedHardware,
        hardwarePortByInterface
      ),
    [displayedInterfaceAssignments, hardwarePortByInterface, referenceInterfaces, selectedHardware]
  );

  useEffect(() => {
    if (!selectedHardware || !selectedEdge) {
      setInterfaceOverridesOpen(false);
    }
  }, [selectedEdge, selectedHardware]);

  function updateInterfaceOverrides(referenceInterface, changes) {
    const nextAssignments = displayedInterfaceAssignments.map((assignment) => {
      if (assignment.reference_interface === referenceInterface) {
        const nextAssignment = { ...assignment, ...changes };
        if (!nextAssignment.hardware_interface) {
          nextAssignment.switch_vlans_text = '';
        }
        return nextAssignment;
      }
      if (
        changes.hardware_interface &&
        assignment.hardware_interface === changes.hardware_interface
      ) {
        return { ...assignment, hardware_interface: '', switch_vlans_text: '' };
      }
      return assignment;
    });
    const normalizedNext = normalizeInterfaceAssignments(nextAssignments);
    const normalizedDefaults = normalizeInterfaceAssignments(defaultInterfaceAssignments);
    onChange(
      index,
      'interface_overrides',
      interfaceAssignmentsEqual(normalizedNext, normalizedDefaults) ? [] : normalizedNext
    );
  }

  return (
    <div className="mappingRow">
      <label>
        <RequiredLabel>Hardware</RequiredLabel>
        <HardwareCombobox
          index={index}
          hardwareOptions={inventory.hardware}
          selectedHardwareId={mapping.hardware_id}
          onSelect={(hardwareId) => onChange(index, 'hardware_id', hardwareId)}
        />
      </label>

      <label>
        <RequiredLabel>Branch</RequiredLabel>
        <select
          aria-label="Branch"
          required
          value={mapping.branch_name}
          onChange={(event) => onChange(index, 'branch_name', event.target.value)}
        >
          <option value="">Select branch</option>
          {reference?.branches.map((item) => (
            <option key={item.name} value={item.name}>
              {item.name}
            </option>
          ))}
        </select>
      </label>

      <label>
        <RequiredLabel>Edge</RequiredLabel>
        <select
          aria-label="Edge"
          required
          value={mapping.edge_name}
          onChange={(event) => onChange(index, 'edge_name', event.target.value)}
        >
          <option value="">Select edge</option>
          {branch?.edges.map((edge) => (
            <option key={edge.name} value={edge.name} disabled={usedEdgeNames.has(edge.name)}>
              {edge.name}
            </option>
          ))}
        </select>
      </label>

      <label>
        New branch
        <input
          placeholder={selectedHardware && mapping.branch_name ? `${mapping.branch_name}-${selectedHardware.model_suffix}` : ''}
          value={mapping.target_branch_name}
          onChange={(event) => onChange(index, 'target_branch_name', event.target.value)}
        />
      </label>

      <label>
        New edge
        <input
          placeholder={selectedHardware && mapping.edge_name ? `${mapping.edge_name}-${selectedHardware.model_suffix}` : ''}
          value={mapping.target_edge_name}
          onChange={(event) => onChange(index, 'target_edge_name', event.target.value)}
        />
      </label>

      <button
        type="button"
        className="iconButton danger"
        onClick={() => onRemove(index)}
        disabled={!canRemove}
        aria-label="Remove mapping"
        title="Remove mapping"
      >
        <Trash2 size={16} />
      </button>
      {selectedHardware && selectedEdge && referenceInterfaces.length > 0 && (
        <div className="interfaceOverrideCard">
          <button
            type="button"
            className="interfaceOverrideToggle"
            onClick={() => setInterfaceOverridesOpen((current) => !current)}
            aria-expanded={interfaceOverridesOpen}
          >
            <span>
              <strong>Optional interface mapping</strong>
              <small>
                {referenceInterfaces.length} reference interface(s), {hardwarePorts.length} connected hardware port(s)
              </small>
            </span>
            <ChevronDown className={interfaceOverridesOpen ? 'open' : ''} size={16} aria-hidden="true" />
          </button>
          {interfaceOverridesOpen && (
            <div className="interfaceOverrideEditor">
              <p className="muted">
                Review one row at a time. Keep the suggested hardware port to use automatic matching, change it to pin or drop the interface, and only fill VLANs when you want to override the generated allocation.
              </p>
              <div className="interfaceOverrideList">
                {referenceInterfaces.map((interfaceSummary) => {
                  const referenceInterface = getReferenceInterfaceKey(interfaceSummary);
                  const assignment = displayedInterfaceAssignments.find(
                    (item) => item.reference_interface === referenceInterface
                  );
                  const selectedPort = hardwarePortByInterface.get(assignment?.hardware_interface || '');
                  const vlanPlan = referenceInterfaceVlanPlan(interfaceSummary, selectedPort);
                  const hasManualVlans = Boolean(assignment?.switch_vlans_text?.trim());
                  const statusTone = !assignment?.hardware_interface
                    ? 'warning'
                    : hasManualVlans
                      ? 'accent'
                      : 'neutral';
                  const statusLabel = !assignment?.hardware_interface
                    ? 'Dropped'
                    : hasManualVlans
                      ? 'Manual VLANs'
                      : 'Auto VLANs';
                  return (
                    <div className="interfaceOverrideRow" key={referenceInterface}>
                      <div className="interfaceOverrideMeta">
                        <div className="interfaceOverrideHeader">
                          <strong>{referenceInterfaceLabel(interfaceSummary)}</strong>
                          <span className={`interfacePill ${statusTone}`}>{statusLabel}</span>
                        </div>
                        <small>Reference interface</small>
                        <small>{vlanPlan.summary}</small>
                        {referenceInterfaceVlanSummary(interfaceSummary) && (
                          <small>{referenceInterfaceVlanSummary(interfaceSummary)}</small>
                        )}
                      </div>
                      <label className="interfaceOverrideField">
                        <span className="fieldCaption">Connected hardware port</span>
                        <select
                          aria-label={`Hardware interface for ${referenceInterface}`}
                          value={assignment?.hardware_interface || ''}
                          onChange={(event) =>
                            updateInterfaceOverrides(referenceInterface, {
                              hardware_interface: event.target.value
                            })
                          }
                        >
                          <option value="">Drop interface</option>
                          {hardwarePorts.map((port) => (
                            <option key={port.logical_interface} value={port.logical_interface}>
                              {hardwareInterfaceOptionLabel(port)}
                            </option>
                          ))}
                        </select>
                        <small>{hardwarePortHint(selectedPort, autoVlanAllocationPreview.get(referenceInterface) || [])}</small>
                      </label>
                      <label className="interfaceOverrideField">
                        <span className="fieldCaption">VLAN allocation</span>
                        <input
                          aria-label={`Switch VLANs for ${referenceInterface}`}
                          placeholder={vlanPlan.placeholder || 'Auto-allocate from range'}
                          value={assignment?.switch_vlans_text || ''}
                          disabled={!assignment?.hardware_interface || !vlanPlan.totalCount}
                          onChange={(event) =>
                            updateInterfaceOverrides(referenceInterface, {
                              switch_vlans_text: event.target.value
                            })
                          }
                        />
                        <small>
                          {hasManualVlans
                            ? 'Manual override. First VLAN is native/untagged; remaining VLANs are tagged.'
                            : vlanPlan.autoSummary}
                        </small>
                      </label>
                    </div>
                  );
                })}
              </div>
              <p className="muted">Use comma-separated VLANs only for manual override. Example: `2200,2201,2202`.</p>
            </div>
          )}
        </div>
      )}
      {haMismatch && (
        <div className="mappingCaveat">
          <TriangleAlert size={16} aria-hidden="true" />
          Reference edge is HA enabled, but selected hardware is standalone. Generation will convert this branch edge to standalone.
        </div>
      )}
    </div>
  );
}
