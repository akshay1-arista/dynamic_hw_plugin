import { useEffect, useId, useMemo, useState } from 'react';
import {
  Archive,
  CheckCircle2,
  ChevronDown,
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
  fetchInventory,
  fetchReferences,
  generateTopology,
  previewInventoryRefresh,
  saveInventory
} from './api.js';

const emptyMapping = {
  hardware_id: '',
  branch_name: '',
  edge_name: '',
  target_branch_name: '',
  target_edge_name: '',
  interface_overrides: []
};

export function App() {
  const hypervisorIpFieldId = useId();
  const hypervisorInterfaceFieldId = useId();
  const [references, setReferences] = useState([]);
  const [inventory, setInventory] = useState({ hardware: [] });
  const [selectedReferenceId, setSelectedReferenceId] = useState('');
  const [topologyName, setTopologyName] = useState('');
  const [hypervisorIp, setHypervisorIp] = useState('');
  const [hypervisorInterface, setHypervisorInterface] = useState('vmnic0');
  const [branchRename, setBranchRename] = useState(false);
  const [mappings, setMappings] = useState([{ ...emptyMapping }]);
  const [result, setResult] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [generating, setGenerating] = useState(false);
  const [savingInventory, setSavingInventory] = useState(false);
  const [refreshingHardwareId, setRefreshingHardwareId] = useState('');
  const [configuringSwitches, setConfiguringSwitches] = useState(false);
  const [previewingSwitches, setPreviewingSwitches] = useState(false);
  const [switchPreview, setSwitchPreview] = useState(null);
  const [inventorySearch, setInventorySearch] = useState('');
  const [expandedHardwareId, setExpandedHardwareId] = useState('');

  useEffect(() => {
    loadData();
  }, []);

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
    if (!selectedHypervisorOption?.interfaces?.length) {
      return;
    }
    const normalizedCurrent = hypervisorInterface.trim().toLowerCase();
    if (
      normalizedCurrent &&
      selectedHypervisorOption.interfaces.some(
        (interfaceName) => interfaceName.toLowerCase() === normalizedCurrent
      )
    ) {
      return;
    }
    const preferredInterface = pickPreferredHypervisorInterface(selectedHypervisorOption.interfaces);
    if (preferredInterface) {
      setHypervisorInterface(preferredInterface);
    }
  }, [selectedHypervisorOption, hypervisorInterface]);

  const previewRows = useMemo(() => {
    return mappings
      .map((mapping) => {
        const hardware = inventory.hardware.find((item) => item.id === mapping.hardware_id);
        if (!hardware || !mapping.branch_name || !mapping.edge_name) {
          return null;
        }
        const branchName =
          mapping.target_branch_name ||
          (branchRename ? `${mapping.branch_name}-${hardware.model_suffix}` : mapping.branch_name);
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
  }, [branchRename, inventory.hardware, mappings]);

  const filteredHardware = useMemo(() => {
    const query = inventorySearch.trim().toLowerCase();
    if (!query) {
      return inventory.hardware;
    }
    return inventory.hardware.filter((hardware) => hardwareSearchText(hardware).includes(query));
  }, [inventory.hardware, inventorySearch]);

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

  async function loadData() {
    setLoading(true);
    setError('');
    try {
      const [referenceData, inventoryData] = await Promise.all([fetchReferences(), fetchInventory()]);
      setReferences(referenceData);
      setInventory(inventoryData);
      const firstExisting = referenceData.find((reference) => reference.exists);
      if (firstExisting) {
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

  function updateHardwareAvailability(hardwareId, available) {
    setInventory((current) => ({
      ...current,
      hardware: current.hardware.map((hardware) =>
        hardware.id === hardwareId ? { ...hardware, available } : hardware
      )
    }));
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

  async function persistInventory() {
    setSavingInventory(true);
    setError('');
    try {
      const saved = await saveInventory(inventory);
      setInventory(saved);
    } catch (saveError) {
      setError(saveError.message);
    } finally {
      setSavingInventory(false);
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
        branch_rename: branchRename,
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
    } catch (generateError) {
      setError(generateError.message);
    } finally {
      setGenerating(false);
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
        <Loader2 className="spin" aria-hidden="true" />
      </main>
    );
  }

  return (
    <main className="shell">
      <header className="topbar">
        <div>
          <h1>Hardware Topology Generator</h1>
          <p>Phase1 folder generation for Hapy virtual-to-hardware branch mappings.</p>
        </div>
        <button className="iconButton" onClick={loadData} aria-label="Refresh data" title="Refresh data">
          <RefreshCw size={18} />
        </button>
      </header>

      {error && <div className="alert">{error}</div>}

      <section className="workspace">
        <div className="panel">
          <div className="panelTitle">
            <GitBranch size={18} />
            <h2>Reference</h2>
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
              placeholder={selectedHypervisorOption ? 'Search and select hypervisor interface' : 'vmnic0'}
              value={hypervisorInterface}
              onChange={setHypervisorInterface}
            />
          </label>

          <label className="checkboxLine">
            <input
              type="checkbox"
              checked={branchRename}
              onChange={(event) => setBranchRename(event.target.checked)}
            />
            Rename mapped branches with hardware model suffix
          </label>

          <div className="branchList">
            {selectedReference?.branches.map((branch) => (
              <div key={branch.name} className="branchItem">
                <span>{branch.name}</span>
                <small>{branch.edges.map((edge) => `${edge.name} (${edge.model})`).join(', ')}</small>
              </div>
            ))}
          </div>
        </div>

        <div className="panel wide">
          <div className="panelTitle">
            <HardDrive size={18} />
            <h2>Mappings</h2>
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
      </section>

      <section className="workspace lower">
        <div className="panel">
          <div className="panelTitle">
            <Server size={18} />
            <h2>Hardware Inventory</h2>
          </div>
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
                    <small>
                      {hardware.model} / {hardware.ha ? 'HA' : 'standalone'} / {hardware.ports.length} links
                    </small>
                  </span>
                  <ChevronDown size={16} aria-hidden="true" />
                </button>
                <label className="toggle">
                  <input
                    type="checkbox"
                    checked={hardware.available}
                    onChange={(event) => updateHardwareAvailability(hardware.id, event.target.checked)}
                  />
                  Available
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
        </div>

        <div className="panel wide">
          <div className="panelTitle">
            <CheckCircle2 size={18} />
            <h2>Preview And Result</h2>
          </div>
          {previewRows.length === 0 ? (
            <p className="muted">Select hardware, branch, and edge to preview generated names.</p>
          ) : (
            <div className="previewTable">
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
              <strong>{result.topology_name}</strong>
              <span>{result.topology_path}</span>
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
              <div className="resultActions">
                <a className="download" href={result.download_url}>
                  <Download size={16} />
                  Download zip
                </a>
                <button
                  className="secondary"
                  onClick={submitPreviewSwitches}
                  disabled={!configureSwitchState.enabled || previewingSwitches || configuringSwitches}
                  title={configureSwitchState.reason}
                >
                  {previewingSwitches ? <Loader2 className="spin" size={16} /> : <Eye size={16} />}
                  {switchPreview ? 'Refresh preview' : 'Preview config'}
                </button>
                <button
                  className="secondary"
                  onClick={submitConfigureSwitches}
                  disabled={!configureSwitchState.enabled || configuringSwitches || previewingSwitches}
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

function pickPreferredHypervisorInterface(interfaces) {
  return (
    interfaces.find((interfaceName) => interfaceName.toLowerCase() === 'vmnic0') ||
    interfaces.find((interfaceName) => !isManagementHypervisorInterface(interfaceName)) ||
    interfaces[0] ||
    ''
  );
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
  return `${prefix}${hardware.display_name} (${hardware.model}, ${state})`;
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

function referenceInterfaceVlanPlan(interfaceSummary, port) {
  const vlans = Array.isArray(interfaceSummary?.vlans) ? interfaceSummary.vlans : [];
  const subinterfaces = Array.isArray(interfaceSummary?.subinterfaces) ? interfaceSummary.subinterfaces : [];
  const taggedCount =
    interfaceSummary?.mode === 'switched' ? Math.max(vlans.length - 1, 0) : subinterfaces.length;
  const nativeCount = vlans.length || subinterfaces.length ? 1 : 0;
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

function hardwarePortHint(port) {
  if (!port) {
    return 'This reference interface will be dropped from the generated topology.';
  }
  const vlanSummary = port.switch_vlans?.length
    ? `Current inventory VLANs ${port.switch_vlans.join(', ')}`
    : 'No fixed VLAN metadata on this port';
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
                        <small>{hardwarePortHint(selectedPort)}</small>
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
