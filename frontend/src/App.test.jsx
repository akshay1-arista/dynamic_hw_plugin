import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { App } from './App.jsx';

const references = [
  {
    id: '3-site',
    exists: true,
    branches: [
      {
        name: 'branch1',
        edges: [
          {
            name: 'b1-edge1',
            model: 'virtual',
            interfaces: [
              { name: 'eth0', logical_name: 'LAN1', logical_interface: 'GE1', mode: 'switched', vlans: [1] }
            ]
          },
          {
            name: 'b1-edge2',
            model: 'virtual',
            interfaces: [
              { name: 'eth0', logical_name: 'LAN1', logical_interface: 'GE1', mode: 'switched', vlans: [1] }
            ]
          }
        ]
      },
      {
        name: 'branch2',
        edges: [
          {
            name: 'b2-edge1',
            model: 'virtual',
            ha_enabled: true,
            interfaces: [
              { name: 'eth0', logical_name: 'LAN1', logical_interface: 'GE1', mode: 'switched', vlans: [1, 100] },
              { name: 'eth1', logical_name: 'LAN2', logical_interface: 'GE2', mode: 'switched', vlans: [1] },
              { name: 'lo', logical_interface: 'lo', type: 'loopback' }
            ]
          }
        ]
      },
      {
        name: 'branch3',
        edges: [
          {
            name: 'b3-edge1',
            model: 'virtual',
            interfaces: [
              { name: 'eth2', logical_name: 'INTERNET1', logical_interface: 'GE3' },
              { name: 'eth3', logical_name: 'INTERNET2', logical_interface: 'GE4' }
            ]
          }
        ]
      }
    ]
  }
];

const inventory = {
  devices: {
    'switch-a01': {
      id: 'switch-a01',
      type: 'switch',
      display_name: 'a01-core-switch',
      model: 'Dell-4148',
      ip_address: '10.68.136.10',
      available: true
    },
    'hyp-1': {
      id: 'hyp-1',
      type: 'hypervisor',
      display_name: 'chn-rnd-srv-640-298VF33',
      model: 'Dell-R640',
      serial_number: '298VF33',
      ip_address: '10.68.136.50',
      available: true
    },
    'hyp-2': {
      id: 'hyp-2',
      type: 'hypervisor',
      display_name: 'chn-rnd-srv-640-8FYS6T2',
      model: 'Dell-R640',
      serial_number: '8FYS6T2',
      ip_address: '10.68.137.162',
      available: true
    }
  },
  connections: [
    {
      id: 'hyp-1-vmnic0',
      a: { device_id: 'switch-a01', interface: 'eth1/1/1' },
      b: { device_id: 'hyp-1', interface: 'vmnic0' },
      role: 'hypervisor-access',
      vlans: [1],
      tagged_vlans: [],
      untagged_vlan: 1
    },
    {
      id: 'hyp-1-vmnic2',
      a: { device_id: 'switch-a01', interface: 'eth1/1/2' },
      b: { device_id: 'hyp-1', interface: 'vmnic2' },
      role: 'hypervisor-access',
      vlans: [3009],
      tagged_vlans: [],
      untagged_vlan: 3009
    },
    {
      id: 'hyp-1-idrac',
      a: { device_id: 'switch-a01', interface: 'eth1/1/3' },
      b: { device_id: 'hyp-1', interface: 'iDRAC' },
      role: 'hypervisor-access',
      vlans: [3007],
      tagged_vlans: [],
      untagged_vlan: 3007
    },
    {
      id: 'hyp-2-eno1np0',
      a: { device_id: 'switch-a01', interface: 'eth1/1/4' },
      b: { device_id: 'hyp-2', interface: 'eno1np0' },
      role: 'hypervisor-access',
      vlans: [1],
      tagged_vlans: [],
      untagged_vlan: 1
    },
    {
      id: 'hyp-2-eno2np1',
      a: { device_id: 'switch-a01', interface: 'eth1/1/5' },
      b: { device_id: 'hyp-2', interface: 'eno2np1' },
      role: 'hypervisor-access',
      vlans: [1],
      tagged_vlans: [],
      untagged_vlan: 1
    },
    {
      id: 'hyp-2-eno3',
      a: { device_id: 'switch-a01', interface: 'eth1/1/6' },
      b: { device_id: 'hyp-2', interface: 'eno3' },
      role: 'hypervisor-access',
      vlans: [3009],
      tagged_vlans: [],
      untagged_vlan: 3009
    }
  ],
  hardware: [
    {
      id: 'chn-3800-8-ha',
      short_name: 'chn-3800-ha-8',
      display_name: 'CHN 3800 HA Pair 8',
      model: 'edge3X00',
      model_suffix: '3800',
      ha: true,
      active_serial: '13WR363',
      standby_serial: '47YP363',
      available: true,
      switch: { name: 'b2e1-l2-switch', model: 'Dell-3048', connections: { ip: '10.68.136.67' } },
      ports: [
        {
          logical_name: 'LAN1',
          logical_interface: 'GE1',
          switch_active_port: 'gigabitethernet1/1',
          switch_standby_port: 'gigabitethernet1/7',
          switch_vlans: [1504],
          tagged_vlans: [],
          untagged_vlan: 1504
        },
        {
          logical_name: 'LAN2',
          logical_interface: 'GE2',
          switch_active_port: 'gigabitethernet1/2',
          switch_standby_port: 'gigabitethernet1/8',
          switch_vlans: [1501, 1502, 1503],
          tagged_vlans: [1502, 1503],
          untagged_vlan: 1501
        }
      ],
      notes: 'reference inventory entry'
    },
    {
      id: 'a01-680-standalone',
      short_name: 'a01-680-solo',
      display_name: 'A01 680 Standalone',
      model: 'edge6X0',
      model_suffix: '680',
      ha: false,
      active_serial: '1KXFXC2',
      standby_serial: '',
      available: true,
      switch: { name: 'a01-access-switch', model: 'Dell-3048', connections: { ip: '10.68.136.70' } },
      ports: [
        {
          logical_name: 'LAN1',
          logical_interface: 'GE1',
          switch_active_port: 'gigabitethernet1/11',
          switch_vlans: [1510],
          tagged_vlans: [],
          untagged_vlan: 1510
        }
      ],
      notes: 'standalone inventory entry'
    },
    {
      id: 'internet-dynamic-680',
      short_name: 'internet-dyn-680',
      display_name: 'Internet Dynamic 680',
      model: 'edge6X0',
      model_suffix: '680',
      ha: false,
      active_serial: 'DYN6801',
      standby_serial: '',
      available: true,
      free_vlans: [200, 201, 202],
      vlan_range: { start: 200, end: 202 },
      switch: { name: 'a02-access-switch', model: 'Dell-3048', connections: { ip: '10.68.136.80' } },
      ports: [
        {
          logical_name: 'INTERNET1',
          logical_interface: 'GE3',
          switch_active_port: 'gigabitethernet1/21',
          switch_vlans: [],
          tagged_vlans: [],
          untagged_vlan: null
        },
        {
          logical_name: 'INTERNET2',
          logical_interface: 'GE4',
          switch_active_port: 'gigabitethernet1/22',
          switch_vlans: [],
          tagged_vlans: [],
          untagged_vlan: null
        }
      ],
      notes: 'dynamic internet ports'
    }
  ]
};

const defaultUser = {
  name: 'Test User',
  email: 'test@example.com'
};

beforeEach(() => {
  const storage = new Map();
  Object.defineProperty(window, 'localStorage', {
    configurable: true,
    value: {
      getItem: vi.fn((key) => (storage.has(key) ? storage.get(key) : null)),
      setItem: vi.fn((key, value) => storage.set(key, String(value))),
      removeItem: vi.fn((key) => storage.delete(key)),
      clear: vi.fn(() => storage.clear())
    }
  });
  window.confirm = vi.fn(() => true);
  window.localStorage.setItem('dynamic-topology-user', JSON.stringify(defaultUser));
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: {
      writeText: vi.fn().mockResolvedValue(undefined)
    }
  });
  let inventoryState = JSON.parse(JSON.stringify(inventory));
  let privateBranches = [];
  let auditTrail = [];
  global.fetch = vi.fn(async (url, options = {}) => {
    if (url === '/api/reference-topologies') {
      return Response.json(references);
    }
    if (url === '/api/hardware' && !options.method) {
      return Response.json(inventoryState);
    }
    if (url === '/api/hapy/private-branches') {
      return Response.json({ branches: privateBranches });
    }
    if (url === '/api/audit-trail') {
      return Response.json({ events: auditTrail });
    }
    if (url === '/api/hardware' && options.method === 'PUT') {
      const payload = JSON.parse(options.body);
      inventoryState = payload.inventory;
      auditTrail = [
        {
          id: 'audit-save',
          action: 'inventory_saved',
          actor: payload.requested_by,
          target_type: 'inventory',
          target_id: 'hardware_inventory',
          summary: 'Saved inventory updates.',
          details: {},
          created_at: '2026-07-12T00:00:00+00:00'
        },
        ...auditTrail
      ];
      return Response.json(payload.inventory);
    }
    if (/^\/api\/hardware\/[^/]+\/availability$/.test(url) && options.method === 'POST') {
      const hardwareId = url.split('/')[3];
      const payload = JSON.parse(options.body);
      inventoryState = {
        ...inventoryState,
        hardware: inventoryState.hardware.map((hardware) =>
          hardware.id === hardwareId
            ? {
                ...hardware,
                available: payload.available,
                reservation: payload.available
                  ? null
                  : {
                      actor: payload.requested_by,
                      reserved_at: '2026-07-12T00:00:00+00:00',
                      reason: 'manual-unavailable'
                    }
              }
            : hardware
        )
      };
      auditTrail = [
        {
          id: `audit-${hardwareId}`,
          action: payload.available ? 'hardware_released' : 'hardware_marked_unavailable',
          actor: payload.requested_by,
          target_type: 'hardware',
          target_id: hardwareId,
          summary: payload.available ? 'Marked hardware as available.' : 'Marked hardware as unavailable.',
          details: { hardware_id: hardwareId },
          created_at: '2026-07-12T00:00:00+00:00'
        },
        ...auditTrail
      ];
      return Response.json(inventoryState);
    }
    if (url === '/api/generate') {
      const payload = JSON.parse(options.body);
      inventoryState = {
        ...inventoryState,
        hardware: inventoryState.hardware.map((hardware) =>
          payload.mappings.some((mapping) => mapping.hardware_id === hardware.id)
            ? {
                ...hardware,
                available: false,
                reservation: {
                  actor: payload.requested_by,
                  reserved_at: '2026-07-12T00:00:00+00:00',
                  reason: 'topology-generation',
                  run_id: 'abc123',
                  topology_name: '3-site-hw-a1b2c3'
                }
              }
            : hardware
        )
      };
      auditTrail = [
        {
          id: 'audit-generate',
          action: 'hardware_reserved',
          actor: payload.requested_by,
          target_type: 'hardware',
          target_id: payload.mappings[0].hardware_id,
          summary: 'Reserved hardware for generated topology.',
          details: { run_id: 'abc123', topology_name: '3-site-hw-a1b2c3' },
          created_at: '2026-07-12T00:00:00+00:00'
        },
        ...auditTrail
      ];
      if (payload.mappings?.[0]?.hardware_id === 'a01-680-standalone') {
        return Response.json({
          run_id: 'abc123',
          topology_name: '3-site-hw-a1b2c3',
          topology_path: '/tmp/3-site-hw-a1b2c3',
          zip_path: '/tmp/3-site-hw-a1b2c3.zip',
          download_url: '/api/runs/abc123/download',
          can_configure_switches: true,
          mapping_statuses: [
            {
              hardware_id: 'a01-680-standalone',
              hardware_display_name: 'A01 680 Standalone',
              branch_name: payload.mappings[0].branch_name,
              edge_name: payload.mappings[0].edge_name,
              path_resolved: true,
              auto_config_ready: true,
              path: {
                access_switch_name: 'a01-access-switch',
                upstream_switch_name: 'a01-core-switch',
                hypervisor_name: 'chn-rnd-srv-640-298VF33'
              }
            }
          ],
          messages: [{ level: 'info', message: 'All generated JSON files parsed successfully' }]
        });
      }
      return Response.json({
        run_id: 'abc123',
        topology_name: '3-site-hw-a1b2c3',
        topology_path: '/tmp/3-site-hw-a1b2c3',
        zip_path: '/tmp/3-site-hw-a1b2c3.zip',
        download_url: '/api/runs/abc123/download',
        can_configure_switches: false,
        mapping_statuses: [
          {
            hardware_id: 'chn-3800-8-ha',
            hardware_display_name: 'CHN 3800 HA Pair 8',
            branch_name: 'branch2',
            edge_name: 'b2-edge1',
            path_resolved: false,
            auto_config_ready: false,
            reason: 'Could not resolve a unique imported path from the selected access switch to hypervisor 10.68.136.50.'
          }
        ],
        messages: [{ level: 'info', message: 'All generated JSON files parsed successfully' }]
      });
    }
    if (url === '/api/runs/abc123/publish-private-branch' && options.method === 'POST') {
      const payload = JSON.parse(options.body);
      const response = {
        run_id: 'abc123',
        topology_name: '3-site-hw-a1b2c3',
        reference_topology_id: '3-site',
        repo_path: '/repo/velocloud.src',
        destination_path: `/repo/velocloud.src/hapy/hapy/testbed/configs/3-site-hw-a1b2c3`,
        destination_relative_path: '3-site-hw-a1b2c3',
        base_branch: payload.base_branch,
        private_branch_name: 'hw_topo_gen_private_abc123',
        commit_sha: 'deadbeef1234',
        commit_message: 'VLDT-None: add topology 3-site-hw-a1b2c3',
        private_branch_pushed: true,
        remote_name: 'origin',
        remote_branch_ref: 'refs/heads/hw_topo_gen_private_abc123',
        fetch_command:
          'git fetch origin refs/heads/hw_topo_gen_private_abc123 && git checkout -b hw_topo_gen_private_abc123 FETCH_HEAD',
        created_at: '2026-07-11T00:00:00+00:00',
        updated_at: '2026-07-11T00:01:00+00:00',
        messages: [{ level: 'info', message: 'Committed and pushed private branch to origin.' }]
      };
      privateBranches = [
        {
          run_id: response.run_id,
          topology_name: response.topology_name,
          reference_topology_id: response.reference_topology_id,
          repo_path: response.repo_path,
          destination_path: response.destination_path,
          destination_relative_path: response.destination_relative_path,
          base_branch: response.base_branch,
          private_branch_name: response.private_branch_name,
          commit_sha: response.commit_sha,
          commit_message: response.commit_message,
          private_branch_pushed: response.private_branch_pushed,
          remote_name: response.remote_name,
          remote_branch_ref: response.remote_branch_ref,
          fetch_command: response.fetch_command,
          created_at: response.created_at,
          updated_at: response.updated_at
        }
      ];
      auditTrail = [
        {
          id: 'audit-publish',
          action: 'private_branch_published',
          actor: payload.requested_by,
          target_type: 'private_branch',
          target_id: response.private_branch_name,
          summary: 'Pushed Gerrit private branch.',
          details: { run_id: 'abc123' },
          created_at: '2026-07-12T00:00:00+00:00'
        },
        ...auditTrail
      ];
      return Response.json(response);
    }
    if (url === '/api/hapy/private-branches/delete' && options.method === 'POST') {
      const payload = JSON.parse(options.body);
      const deletedNames = payload.delete_all
        ? privateBranches.map((branch) => branch.private_branch_name)
        : payload.private_branch_names;
      privateBranches = privateBranches.filter(
        (branch) => !deletedNames.includes(branch.private_branch_name)
      );
      auditTrail = [
        ...deletedNames.map((branchName, index) => ({
          id: `audit-delete-${index}`,
          action: 'private_branch_deleted',
          actor: payload.requested_by,
          target_type: 'private_branch',
          target_id: branchName,
          summary: `Deleted Gerrit private branch ${branchName}.`,
          details: {},
          created_at: '2026-07-12T00:00:00+00:00'
        })),
        ...auditTrail
      ];
      return Response.json({
        results: deletedNames.map((branchName) => ({
          private_branch_name: branchName,
          run_id: 'abc123',
          deleted_local_paths: ['/repo/velocloud.src'],
          deleted_remote: true,
          registry_removed: true,
          success: true,
          messages: []
        })),
        messages: [{ level: 'info', message: `Deleted ${deletedNames.length} Gerrit private branches.` }]
      });
    }
    if (url === '/api/runs/abc123/configure-switches' && options.method === 'POST') {
      const payload = JSON.parse(options.body);
      const commands =
        payload.command_overrides?.[0]?.commands || [
          'interface gigabitethernet1/11',
          ' switchport mode access',
          ' switchport access vlan 1510',
          ' exit'
        ];
      return Response.json({
        run_id: 'abc123',
        applied: !payload.dry_run,
        devices: [
          {
            device_id: 'access_sw',
            device_name: 'a01-access-switch',
            device_ip: '10.68.136.70',
            interface: 'multiple',
            commands
          }
        ],
        messages: [
          {
            level: 'info',
            message: payload.dry_run
              ? 'Generated switch configuration preview.'
              : 'Applied switch configuration.'
          }
        ]
      });
    }
    return new Response(null, { status: 404 });
  });
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  vi.restoreAllMocks();
});

async function chooseHardware(user, query, optionName, index = 0) {
  const comboboxes = screen.getAllByRole('combobox', { name: 'Hardware' });
  const target = comboboxes[index];
  await user.clear(target);
  await user.type(target, query);
  await user.click(screen.getByRole('option', { name: optionName }));
}

describe('App', () => {
  test('shows the homepage user form when no stored user exists', async () => {
    window.localStorage.clear();
    const user = userEvent.setup();
    render(<App />);

    expect(screen.getByText('User Session')).toBeInTheDocument();
    await user.type(screen.getByLabelText('User name'), 'Another User');
    await user.type(screen.getByLabelText('User email'), 'another@example.com');
    await user.click(screen.getByRole('button', { name: 'Continue' }));

    expect(await screen.findByRole('img', { name: 'Dynamic Topology Engine' })).toBeInTheDocument();
  });

  test('loads references and inventory', async () => {
    render(<App />);
    expect((await screen.findAllByText('branch2')).length).toBeGreaterThan(0);
    expect(screen.getAllByText('CHN 3800 HA Pair 8').length).toBeGreaterThan(0);
    expect(screen.getByText('chn-3800-ha-8')).toBeInTheDocument();
  });

  test('filters hardware combobox options while typing', async () => {
    const user = userEvent.setup();
    render(<App />);

    await screen.findAllByText('CHN 3800 HA Pair 8');
    await user.type(screen.getByRole('combobox', { name: 'Hardware' }), '680');

    expect(screen.getByRole('option', { name: /A01 680 Standalone/i })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: /CHN 3800 HA Pair 8/i })).not.toBeInTheDocument();
  });

  test('selects hypervisor ip from inventory without prefilling an interface', async () => {
    const user = userEvent.setup();
    render(<App />);

    await screen.findAllByText('CHN 3800 HA Pair 8');
    const hypervisorIpInput = screen.getByRole('combobox', { name: 'Hypervisor IP' });
    await user.type(hypervisorIpInput, '137.162');
    await user.click(
      screen.getByRole('option', { name: /10\.68\.137\.162 - chn-rnd-srv-640-8FYS6T2/i })
    );

    expect(hypervisorIpInput).toHaveValue('10.68.137.162');
    expect(screen.getByRole('combobox', { name: 'Hypervisor interface' })).toHaveValue('');
    expect(
      screen.queryByText(/Rename mapped branches with hardware model suffix/i)
    ).not.toBeInTheDocument();
  });

  test('filters hypervisor interface choices for the selected hypervisor', async () => {
    const user = userEvent.setup();
    render(<App />);

    await screen.findAllByText('CHN 3800 HA Pair 8');
    await user.type(screen.getByRole('combobox', { name: 'Hypervisor IP' }), '10.68.136.50');
    const interfaceInput = screen.getByRole('combobox', { name: 'Hypervisor interface' });
    await user.clear(interfaceInput);
    await user.type(interfaceInput, 'vmnic2');

    expect(screen.getByRole('option', { name: 'vmnic2' })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'eno1np0' })).not.toBeInTheDocument();
  });

  test('searches inventory by short name and expands details', async () => {
    const user = userEvent.setup();
    render(<App />);

    await screen.findByLabelText('Search hardware');
    await user.type(screen.getByLabelText('Search hardware'), 'chn-3800-ha-8');
    await user.click(screen.getByRole('button', { name: /chn-3800-ha-8/i }));

    expect(screen.getByText('Active serial')).toBeInTheDocument();
    expect(screen.getByText('47YP363')).toBeInTheDocument();
    expect(screen.getByText(/LAN2 GE2/)).toBeInTheDocument();
    expect(screen.getByText('reference inventory entry')).toBeInTheDocument();
  });

  test('filters inventory by available and reserved quick filters', async () => {
    const user = userEvent.setup();
    render(<App />);

    await screen.findAllByText('CHN 3800 HA Pair 8');
    await chooseHardware(user, '3800', /CHN 3800 HA Pair 8/i);
    await user.selectOptions(screen.getByLabelText('Branch'), 'branch2');
    await user.type(screen.getByRole('combobox', { name: 'Hypervisor IP' }), '10.68.136.50');
    await user.type(screen.getByRole('combobox', { name: 'Hypervisor interface' }), 'vmnic0');
    await user.click(screen.getByRole('button', { name: /generate zip/i }));

    await screen.findByText('By Test User');
    const inventoryPanel = screen.getByRole('heading', { name: 'Inventory' }).closest('.panel');

    await user.click(screen.getByRole('button', { name: 'Reserved' }));
    expect(within(inventoryPanel).getAllByText('CHN 3800 HA Pair 8').length).toBeGreaterThan(0);
    expect(within(inventoryPanel).queryByText('A01 680 Standalone')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Available' }));
    expect(within(inventoryPanel).queryByText('CHN 3800 HA Pair 8')).not.toBeInTheDocument();
    expect(within(inventoryPanel).getByText('A01 680 Standalone')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'All' }));
    expect(within(inventoryPanel).getAllByText('CHN 3800 HA Pair 8').length).toBeGreaterThan(0);
    expect(within(inventoryPanel).getByText('A01 680 Standalone')).toBeInTheDocument();
  });

  test('adds mapping preview and generates download result', async () => {
    const user = userEvent.setup();
    render(<App />);

    await screen.findAllByText('CHN 3800 HA Pair 8');
    await chooseHardware(user, '3800', /CHN 3800 HA Pair 8/i);
    await user.selectOptions(screen.getByLabelText('Branch'), 'branch2');
    await user.type(screen.getByRole('combobox', { name: 'Hypervisor IP' }), '10.68.136.50');
    await user.type(screen.getByRole('combobox', { name: 'Hypervisor interface' }), 'vmnic0');

    expect(screen.getByText('branch2 -> branch2')).toBeInTheDocument();
    expect(screen.getByText('b2-edge1 -> b2-edge1-3800')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /generate zip/i }));
    await waitFor(() => expect(screen.getByText('/tmp/3-site-hw-a1b2c3')).toBeInTheDocument());
    expect(screen.getByText(/branch2\/b2-edge1: path unresolved/i)).toBeInTheDocument();
    const generateCall = global.fetch.mock.calls.find(([url]) => url === '/api/generate');
    const payload = JSON.parse(generateCall[1].body);
    expect(payload.hypervisor_ip).toBe('10.68.136.50');
    expect(payload.hypervisor_interface).toBe('vmnic0');
    expect(payload.requested_by).toEqual(defaultUser);
    expect(payload).not.toHaveProperty('branch_rename');
    expect(payload.mappings[0]).not.toHaveProperty('hypervisor_ip');
    expect(payload.mappings[0]).not.toHaveProperty('hypervisor_interface');
    expect(screen.getByRole('link', { name: /download zip/i })).toHaveAttribute(
      'href',
      '/api/runs/abc123/download'
    );
    expect(screen.getByText('By Test User')).toBeInTheDocument();
  });

  test('filters audit trail events', async () => {
    const user = userEvent.setup();
    render(<App />);

    await screen.findAllByText('CHN 3800 HA Pair 8');
    await chooseHardware(user, '3800', /CHN 3800 HA Pair 8/i);
    await user.selectOptions(screen.getByLabelText('Branch'), 'branch2');
    await user.type(screen.getByRole('combobox', { name: 'Hypervisor IP' }), '10.68.136.50');
    await user.type(screen.getByRole('combobox', { name: 'Hypervisor interface' }), 'vmnic0');
    await user.click(screen.getByRole('button', { name: /generate zip/i }));

    await screen.findByText('Reserved hardware for generated topology.');
    const auditSearch = screen.getByLabelText('Search audit trail');
    await user.type(auditSearch, '3-site-hw-a1b2c3');

    expect(screen.getByText('Reserved hardware for generated topology.')).toBeInTheDocument();
    await user.clear(auditSearch);
    await user.type(auditSearch, 'no-match');

    expect(screen.queryByText('Reserved hardware for generated topology.')).not.toBeInTheDocument();
    expect(screen.getByText('No audit events match the current search.')).toBeInTheDocument();
  });

  test('previews switch config, allows edits, and applies the edited commands', async () => {
    const user = userEvent.setup();
    render(<App />);

    await screen.findAllByText('CHN 3800 HA Pair 8');
    await chooseHardware(user, 'a01 680', /A01 680 Standalone/i);
    await user.selectOptions(screen.getByLabelText('Branch'), 'branch1');
    await user.type(screen.getByRole('combobox', { name: 'Hypervisor IP' }), '10.68.136.50');
    await user.type(screen.getByRole('combobox', { name: 'Hypervisor interface' }), 'vmnic0');
    await user.click(screen.getByRole('button', { name: /generate zip/i }));

    await waitFor(() => expect(screen.getByText(/path resolved/i)).toBeInTheDocument());

    await user.click(screen.getByRole('button', { name: /preview config/i }));

    const editor = await screen.findByLabelText('Switch commands for a01-access-switch');
    expect(editor.value).toContain('interface gigabitethernet1/11');

    await user.clear(editor);
    await user.type(
      editor,
      'interface gigabitethernet1/11{enter} switchport mode access{enter} switchport access vlan 1511{enter} exit'
    );
    await user.click(screen.getByRole('button', { name: /configure switches/i }));

    const configureCall = global.fetch.mock.calls
      .filter(([url]) => url === '/api/runs/abc123/configure-switches')
      .at(-1);
    const payload = JSON.parse(configureCall[1].body);

    expect(payload.command_overrides).toEqual([
      {
        device_id: 'access_sw',
        commands: [
          'interface gigabitethernet1/11',
          ' switchport mode access',
          ' switchport access vlan 1511',
          ' exit'
        ]
      }
    ]);
    expect(window.confirm).toHaveBeenCalled();
  });

  test('publishes generated topology from a selected base branch and copies the branch name', async () => {
    const user = userEvent.setup();
    render(<App />);

    await screen.findAllByText('CHN 3800 HA Pair 8');
    await chooseHardware(user, 'a01 680', /A01 680 Standalone/i);
    await user.selectOptions(screen.getByLabelText('Branch'), 'branch1');
    await user.type(screen.getByRole('combobox', { name: 'Hypervisor IP' }), '10.68.136.50');
    await user.type(screen.getByRole('combobox', { name: 'Hypervisor interface' }), 'vmnic0');
    await user.click(screen.getByRole('button', { name: /generate zip/i }));

    await waitFor(() => expect(screen.getByText(/path resolved/i)).toBeInTheDocument());

    await user.selectOptions(screen.getByLabelText('Base branch for Gerrit private branch'), 'release_6.4');
    await user.click(screen.getByRole('button', { name: /commit and push gerrit private branch/i }));

    await waitFor(() =>
      expect(screen.getByText(/Private branch: hw_topo_gen_private_abc123/)).toBeInTheDocument()
    );

    const publishCall = global.fetch.mock.calls.find(([url]) => url === '/api/runs/abc123/publish-private-branch');
    expect(JSON.parse(publishCall[1].body)).toEqual({
      base_branch: 'release_6.4',
      requested_by: defaultUser
    });
    expect(screen.getByText(/Remote ref: refs\/heads\/hw_topo_gen_private_abc123/)).toBeInTheDocument();
    expect(screen.getByText(/pushed \/ release_6.4 \/ run abc123/i)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /copy branch name/i }));
    expect(await screen.findByText('Copied')).toBeInTheDocument();
  });

  test('marks reserved hardware available again through the inventory toggle', async () => {
    const user = userEvent.setup();
    render(<App />);

    await screen.findAllByText('CHN 3800 HA Pair 8');
    await chooseHardware(user, '3800', /CHN 3800 HA Pair 8/i);
    await user.selectOptions(screen.getByLabelText('Branch'), 'branch2');
    await user.type(screen.getByRole('combobox', { name: 'Hypervisor IP' }), '10.68.136.50');
    await user.type(screen.getByRole('combobox', { name: 'Hypervisor interface' }), 'vmnic0');
    await user.click(screen.getByRole('button', { name: /generate zip/i }));

    await screen.findByText('By Test User');
    await user.click(screen.getByRole('button', { name: /chn-3800-ha-8/i }));
    const availabilityToggle = screen.getAllByRole('checkbox', { name: 'Available' })[0];
    await user.click(availabilityToggle);

    const availabilityCall = global.fetch.mock.calls.find(([url]) =>
      /^\/api\/hardware\/chn-3800-8-ha\/availability$/.test(url)
    );
    expect(JSON.parse(availabilityCall[1].body)).toEqual({
      available: true,
      requested_by: defaultUser
    });
    await waitFor(() => expect(screen.getAllByText('Available').length).toBeGreaterThan(0));
  });

  test('deletes selected Gerrit private branches', async () => {
    const user = userEvent.setup();
    render(<App />);

    await screen.findAllByText('CHN 3800 HA Pair 8');
    await chooseHardware(user, 'a01 680', /A01 680 Standalone/i);
    await user.selectOptions(screen.getByLabelText('Branch'), 'branch1');
    await user.type(screen.getByRole('combobox', { name: 'Hypervisor IP' }), '10.68.136.50');
    await user.type(screen.getByRole('combobox', { name: 'Hypervisor interface' }), 'vmnic0');
    await user.click(screen.getByRole('button', { name: /generate zip/i }));
    await waitFor(() => expect(screen.getByText(/path resolved/i)).toBeInTheDocument());
    await user.click(screen.getByRole('button', { name: /commit and push gerrit private branch/i }));
    await screen.findAllByText(/hw_topo_gen_private_abc123/);

    await user.click(document.querySelector('.branchRegistrySelect input'));
    await user.click(screen.getByRole('button', { name: /delete selected/i }));

    await waitFor(() => expect(document.querySelector('.branchRegistrySelect input')).toBeNull());
  });

  test('shows default interface matches and sends override payload when edited', async () => {
    const user = userEvent.setup();
    render(<App />);

    await screen.findAllByText('CHN 3800 HA Pair 8');
    await chooseHardware(user, '3800', /CHN 3800 HA Pair 8/i);
    await user.selectOptions(screen.getByLabelText('Branch'), 'branch2');
    await user.click(screen.getByRole('button', { name: /optional interface mapping/i }));

    const firstInterface = screen.getByLabelText('Hardware interface for GE1');
    const secondInterface = screen.getByLabelText('Hardware interface for GE2');

    expect(firstInterface).toHaveValue('GE2');
    expect(secondInterface).toHaveValue('GE1');

    await user.selectOptions(firstInterface, 'GE1');
    expect(firstInterface).toHaveValue('GE1');
    expect(secondInterface).toHaveValue('');
    await user.type(screen.getByRole('combobox', { name: 'Hypervisor IP' }), '10.68.136.50');
    await user.type(screen.getByRole('combobox', { name: 'Hypervisor interface' }), 'vmnic0');
    await user.click(screen.getByRole('button', { name: /generate zip/i }));

    const generateCall = global.fetch.mock.calls.find(([url]) => url === '/api/generate');
    const payload = JSON.parse(generateCall[1].body);
    expect(payload.mappings[0].interface_overrides).toEqual([
      { reference_interface: 'GE1', hardware_interface: 'GE1' },
      { reference_interface: 'GE2', hardware_interface: null }
    ]);
  });

  test('does not show loopback interfaces in optional interface mapping', async () => {
    const user = userEvent.setup();
    render(<App />);

    await screen.findAllByText('CHN 3800 HA Pair 8');
    await chooseHardware(user, '3800', /CHN 3800 HA Pair 8/i);
    await user.selectOptions(screen.getByLabelText('Branch'), 'branch2');
    await user.click(screen.getByRole('button', { name: /optional interface mapping/i }));

    expect(screen.getByText('2 reference interface(s), 2 connected hardware port(s)')).toBeInTheDocument();
    expect(screen.queryByLabelText('Hardware interface for LO')).not.toBeInTheDocument();
    expect(screen.queryByText(/^LO$/)).not.toBeInTheDocument();
  });

  test('shows reference topology VLANs in optional interface mapping', async () => {
    const user = userEvent.setup();
    render(<App />);

    await screen.findAllByText('CHN 3800 HA Pair 8');
    await chooseHardware(user, '3800', /CHN 3800 HA Pair 8/i);
    await user.selectOptions(screen.getByLabelText('Branch'), 'branch2');
    await user.click(screen.getByRole('button', { name: /optional interface mapping/i }));

    expect(screen.getByText('Reference VLANs 1, 100')).toBeInTheDocument();
    expect(screen.getByText('Reference VLANs 1')).toBeInTheDocument();
  });

  test('sends optional VLAN overrides from interface mapping', async () => {
    const user = userEvent.setup();
    render(<App />);

    await screen.findAllByText('CHN 3800 HA Pair 8');
    await chooseHardware(user, '3800', /CHN 3800 HA Pair 8/i);
    await user.selectOptions(screen.getByLabelText('Branch'), 'branch2');
    await user.click(screen.getByRole('button', { name: /optional interface mapping/i }));
    await user.type(screen.getByLabelText('Switch VLANs for GE1'), '2200, 2201');
    await user.type(screen.getByRole('combobox', { name: 'Hypervisor IP' }), '10.68.136.50');
    await user.type(screen.getByRole('combobox', { name: 'Hypervisor interface' }), 'vmnic0');
    await user.click(screen.getByRole('button', { name: /generate zip/i }));

    const generateCall = global.fetch.mock.calls.find(([url]) => url === '/api/generate');
    const payload = JSON.parse(generateCall[1].body);
    expect(payload.mappings[0].interface_overrides[0]).toMatchObject({
      reference_interface: 'GE1',
      hardware_interface: 'GE2',
      switch_vlans: [2200, 2201]
    });
  });

  test('shows native VLAN allocation for switch-only internet interfaces on dynamic ports', async () => {
    const user = userEvent.setup();
    render(<App />);

    await screen.findAllByText('CHN 3800 HA Pair 8');
    await chooseHardware(user, 'internet dynamic', /Internet Dynamic 680/i);
    await user.selectOptions(screen.getByLabelText('Branch'), 'branch3');
    await user.click(screen.getByRole('button', { name: /optional interface mapping/i }));

    expect(screen.getAllByText('Needs 1 native VLAN')).toHaveLength(2);
    expect(screen.getByLabelText('Switch VLANs for GE3')).toHaveAttribute('placeholder', 'Auto-allocate 1 from range');
    expect(
      screen.getAllByText('Leave blank to auto-allocate 1 VLAN from the hardware range for the access switch.')
    ).toHaveLength(2);
    expect(
      screen.getByText('GE3 (INTERNET1) on gigabitethernet1/21. Will auto-assign VLAN 200 from hardware range.')
    ).toBeInTheDocument();
    expect(
      screen.getByText('GE4 (INTERNET2) on gigabitethernet1/22. Will auto-assign VLAN 201 from hardware range.')
    ).toBeInTheDocument();
    expect(screen.queryByText(/No fixed VLAN metadata on this port/)).not.toBeInTheDocument();
  });

  test('leaves hypervisor interface empty and keeps it editable', async () => {
    const user = userEvent.setup();
    render(<App />);

    const input = await screen.findByRole('combobox', { name: 'Hypervisor interface' });
    expect(input).toHaveValue('');

    await user.type(input, 'vmnic7');

    expect(input).toHaveValue('vmnic7');
  });

  test('removes extra mapping row', async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findAllByText('CHN 3800 HA Pair 8');
    await user.click(screen.getByRole('button', { name: /add mapping/i }));
    expect(screen.getAllByRole('combobox', { name: 'Hardware' })).toHaveLength(2);
    expect(screen.getAllByRole('combobox', { name: 'Hypervisor IP' })).toHaveLength(1);
    expect(screen.getAllByRole('combobox', { name: 'Hypervisor interface' })).toHaveLength(1);
    await user.click(screen.getAllByLabelText('Remove mapping')[0]);
    expect(screen.getAllByRole('combobox', { name: 'Hardware' })).toHaveLength(1);
  });

  test('does not allow selecting the same reference edge in multiple mapping rows', async () => {
    const user = userEvent.setup();
    render(<App />);

    await screen.findAllByText('CHN 3800 HA Pair 8');
    await user.selectOptions(screen.getByLabelText('Branch'), 'branch1');
    await user.click(screen.getByRole('button', { name: /add mapping/i }));

    const branchInputs = screen.getAllByLabelText('Branch');
    const edgeInputs = screen.getAllByLabelText('Edge');

    await user.selectOptions(branchInputs[1], 'branch1');

    expect(edgeInputs[0]).toHaveValue('b1-edge1');
    expect(edgeInputs[1]).toHaveValue('b1-edge2');
    expect(screen.getAllByRole('option', { name: 'b1-edge1' }).some((option) => option.disabled)).toBe(true);
  });

  test('shows a clear validation message when mapping fields are missing', async () => {
    const user = userEvent.setup();
    render(<App />);

    await screen.findAllByText('CHN 3800 HA Pair 8');
    await chooseHardware(user, '3800', /CHN 3800 HA Pair 8/i);
    await user.selectOptions(screen.getByLabelText('Branch'), 'branch2');
    await user.type(screen.getByRole('combobox', { name: 'Hypervisor interface' }), 'vmnic0');
    await user.click(screen.getByRole('button', { name: /generate zip/i }));

    expect(screen.getByText('Select Hypervisor IP, Hypervisor interface, hardware, branch, and edge before generating.')).toBeInTheDocument();
  });

  test('warns immediately when standalone hardware maps to an HA reference edge', async () => {
    const user = userEvent.setup();
    render(<App />);

    await screen.findAllByText('A01 680 Standalone');
    await chooseHardware(user, '680', /A01 680 Standalone/i);
    await user.selectOptions(screen.getByLabelText('Branch'), 'branch2');

    expect(screen.getByText(/Reference edge is HA enabled, but selected hardware is standalone/i)).toBeInTheDocument();
  });
});
