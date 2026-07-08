import { useEffect, useMemo, useState } from 'react';
import {
  Archive,
  CheckCircle2,
  ChevronDown,
  Download,
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
import { fetchInventory, fetchReferences, generateTopology, saveInventory } from './api.js';

const emptyMapping = {
  hardware_id: '',
  branch_name: '',
  edge_name: '',
  target_branch_name: '',
  target_edge_name: '',
  interface_overrides: []
};

export function App() {
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
  const [inventorySearch, setInventorySearch] = useState('');
  const [expandedHardwareId, setExpandedHardwareId] = useState('');

  useEffect(() => {
    loadData();
  }, []);

  const selectedReference = useMemo(
    () => references.find((reference) => reference.id === selectedReferenceId),
    [references, selectedReferenceId]
  );

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
        const vlanBackedPorts = hardware.ports.filter((port) => port.switch_vlans?.length).length;
        return {
          hardware: hardware.display_name,
          branch: `${mapping.branch_name} -> ${branchName}`,
          edge: `${mapping.edge_name} -> ${edgeName}`,
          ports: hardware.ports.length,
          vlanBackedPorts
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
      hardware: current.hardware.map((hardware) =>
        hardware.id === hardwareId ? { ...hardware, available } : hardware
      )
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

  async function submitGenerate() {
    setGenerating(true);
    setResult(null);
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
                interface_overrides: mapping.interface_overrides.map((override) => ({
                  reference_interface: override.reference_interface,
                  hardware_interface: override.hardware_interface || null
                }))
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

          <label>
            <RequiredLabel>Hypervisor IP</RequiredLabel>
            <input
              aria-label="Hypervisor IP"
              required
              value={hypervisorIp}
              onChange={(event) => setHypervisorIp(event.target.value)}
            />
          </label>

          <label>
            <RequiredLabel>Hypervisor interface</RequiredLabel>
            <input
              aria-label="Hypervisor interface"
              required
              placeholder="vmnic0"
              value={hypervisorInterface}
              onChange={(event) => setHypervisorInterface(event.target.value)}
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
                {expandedHardwareId === hardware.id && <HardwareDetails hardware={hardware} />}
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
                    {row.vlanBackedPorts}/{row.ports} VLAN links
                  </span>
                </div>
              ))}
            </div>
          )}

          {result && (
            <div className="resultBox">
              <strong>{result.topology_name}</strong>
              <span>{result.topology_path}</span>
              <div className="messageList">
                {result.messages.map((message, index) => (
                  <small className={`message ${message.level}`} key={index}>
                    {message.level}: {message.message}
                  </small>
                ))}
              </div>
              <a className="download" href={result.download_url}>
                <Download size={16} />
                Download zip
              </a>
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

function hardwareInterfaceOptionLabel(port) {
  const vlanSummary = port.switch_vlans?.length ? `VLANs ${port.switch_vlans.join(', ')}` : 'no VLAN metadata';
  return `${port.logical_interface} - ${port.logical_name} / ${port.switch_active_port} / ${vlanSummary}`;
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
  return [...hardware.ports]
    .filter((port) => port.switch_vlans?.length)
    .sort((left, right) => compareInterfaceNames(left.logical_interface, right.logical_interface));
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
    hardware_interface: matchedPortByInterface.get(getReferenceInterfaceKey(item)) || ''
  }));
}

function mergeInterfaceAssignments(defaultAssignments, overrideAssignments) {
  if (!overrideAssignments?.length) {
    return defaultAssignments;
  }
  const overrideMap = new Map(
    overrideAssignments
      .filter((item) => item.reference_interface)
      .map((item) => [item.reference_interface.trim().toUpperCase(), item.hardware_interface?.trim().toUpperCase() || ''])
  );
  return defaultAssignments.map((assignment) => ({
    ...assignment,
    hardware_interface: overrideMap.has(assignment.reference_interface)
      ? overrideMap.get(assignment.reference_interface)
      : assignment.hardware_interface
  }));
}

function normalizeInterfaceAssignments(assignments) {
  return assignments.map((assignment) => ({
    reference_interface: assignment.reference_interface.trim().toUpperCase(),
    hardware_interface: assignment.hardware_interface?.trim().toUpperCase() || ''
  }));
}

function interfaceAssignmentsEqual(left, right) {
  if (left.length !== right.length) {
    return false;
  }
  return left.every(
    (assignment, index) =>
      assignment.reference_interface === right[index].reference_interface &&
      assignment.hardware_interface === right[index].hardware_interface
  );
}

function HardwareDetails({ hardware }) {
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
            {port.switch_vlans?.length ? `VLANs ${port.switch_vlans.join(', ')}` : 'no VLAN metadata'}
          </small>
        ))}
      </div>

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

  useEffect(() => {
    if (!selectedHardware || !selectedEdge) {
      setInterfaceOverridesOpen(false);
    }
  }, [selectedEdge, selectedHardware]);

  function updateInterfaceOverrides(referenceInterface, hardwareInterface) {
    const nextAssignments = displayedInterfaceAssignments.map((assignment) => {
      if (assignment.reference_interface === referenceInterface) {
        return { ...assignment, hardware_interface: hardwareInterface };
      }
      if (hardwareInterface && assignment.hardware_interface === hardwareInterface) {
        return { ...assignment, hardware_interface: '' };
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
                {referenceInterfaces.length} reference interface(s), {hardwarePorts.length} VLAN-backed hardware port(s)
              </small>
            </span>
            <ChevronDown className={interfaceOverridesOpen ? 'open' : ''} size={16} aria-hidden="true" />
          </button>
          {interfaceOverridesOpen && (
            <div className="interfaceOverrideEditor">
              <p className="muted">
                Defaults follow the internal VLAN/profile matching rules. Change a dropdown only when you need to pin or drop a specific reference interface.
              </p>
              <div className="interfaceOverrideList">
                {referenceInterfaces.map((interfaceSummary) => {
                  const referenceInterface = getReferenceInterfaceKey(interfaceSummary);
                  const assignment = displayedInterfaceAssignments.find(
                    (item) => item.reference_interface === referenceInterface
                  );
                  return (
                    <label className="interfaceOverrideRow" key={referenceInterface}>
                      <span className="interfaceOverrideLabel">
                        <strong>{referenceInterfaceLabel(interfaceSummary)}</strong>
                        <small>Reference interface</small>
                        {referenceInterfaceVlanSummary(interfaceSummary) && (
                          <small>{referenceInterfaceVlanSummary(interfaceSummary)}</small>
                        )}
                      </span>
                      <select
                        aria-label={`Hardware interface for ${referenceInterface}`}
                        value={assignment?.hardware_interface || ''}
                        onChange={(event) => updateInterfaceOverrides(referenceInterface, event.target.value)}
                      >
                        <option value="">Drop interface</option>
                        {hardwarePorts.map((port) => (
                          <option key={port.logical_interface} value={port.logical_interface}>
                            {hardwareInterfaceOptionLabel(port)}
                          </option>
                        ))}
                      </select>
                    </label>
                  );
                })}
              </div>
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
