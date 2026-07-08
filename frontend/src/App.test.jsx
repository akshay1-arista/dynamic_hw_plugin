import { cleanup, render, screen, waitFor } from '@testing-library/react';
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
      }
    ]
  }
];

const inventory = {
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
    }
  ]
};

beforeEach(() => {
  global.fetch = vi.fn(async (url, options = {}) => {
    if (url === '/api/reference-topologies') {
      return Response.json(references);
    }
    if (url === '/api/hardware' && !options.method) {
      return Response.json(inventory);
    }
    if (url === '/api/hardware' && options.method === 'PUT') {
      return Response.json(JSON.parse(options.body));
    }
    if (url === '/api/generate') {
      return Response.json({
        run_id: 'abc123',
        topology_name: '3-site-hw-a1b2c3',
        topology_path: '/tmp/3-site-hw',
        zip_path: '/tmp/3-site-hw.zip',
        download_url: '/api/runs/abc123/download',
        messages: [{ level: 'info', message: 'All generated JSON files parsed successfully' }]
      });
    }
    return new Response(null, { status: 404 });
  });
});

afterEach(() => {
  cleanup();
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

  test('adds mapping preview and generates download result', async () => {
    const user = userEvent.setup();
    render(<App />);

    await screen.findAllByText('CHN 3800 HA Pair 8');
    await chooseHardware(user, '3800', /CHN 3800 HA Pair 8/i);
    await user.selectOptions(screen.getByLabelText('Branch'), 'branch2');
    await user.type(screen.getByLabelText('Hypervisor IP'), '10.68.136.50');

    expect(screen.getByText('branch2 -> branch2')).toBeInTheDocument();
    expect(screen.getByText('b2-edge1 -> b2-edge1-3800')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /generate zip/i }));
    await waitFor(() => expect(screen.getByText('/tmp/3-site-hw')).toBeInTheDocument());
    const generateCall = global.fetch.mock.calls.find(([url]) => url === '/api/generate');
    const payload = JSON.parse(generateCall[1].body);
    expect(payload.hypervisor_ip).toBe('10.68.136.50');
    expect(payload.hypervisor_interface).toBe('vmnic0');
    expect(payload.mappings[0]).not.toHaveProperty('hypervisor_ip');
    expect(payload.mappings[0]).not.toHaveProperty('hypervisor_interface');
    expect(screen.getByRole('link', { name: /download zip/i })).toHaveAttribute(
      'href',
      '/api/runs/abc123/download'
    );
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
    await user.type(screen.getByLabelText('Hypervisor IP'), '10.68.136.50');
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

    expect(screen.getByText('2 reference interface(s), 2 VLAN-backed hardware port(s)')).toBeInTheDocument();
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

  test('prefills hypervisor interface and keeps it editable', async () => {
    const user = userEvent.setup();
    render(<App />);

    const input = await screen.findByLabelText('Hypervisor interface');
    expect(input).toHaveValue('vmnic0');

    await user.clear(input);
    await user.type(input, 'vmnic7');

    expect(input).toHaveValue('vmnic7');
  });

  test('removes extra mapping row', async () => {
    const user = userEvent.setup();
    render(<App />);
    await screen.findAllByText('CHN 3800 HA Pair 8');
    await user.click(screen.getByRole('button', { name: /add mapping/i }));
    expect(screen.getAllByRole('combobox', { name: 'Hardware' })).toHaveLength(2);
    expect(screen.getAllByLabelText('Hypervisor IP')).toHaveLength(1);
    expect(screen.getAllByLabelText('Hypervisor interface')).toHaveLength(1);
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
    await user.type(screen.getByLabelText('Hypervisor interface'), 'vmnic0');
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
