import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { DiscoveryProgress } from './DiscoveryProgress.jsx';
import { fetchDiscoveryActive, fetchDiscoveryRequest } from './api.js';

vi.mock('./api.js', () => ({ fetchDiscoveryActive: vi.fn(), fetchDiscoveryRequest: vi.fn() }));

beforeEach(() => { vi.useFakeTimers(); vi.resetAllMocks(); });
afterEach(() => { cleanup(); vi.useRealTimers(); });

test('shows another user’s request, incremental logs, and completion', async () => {
  const onBusyChange = vi.fn();
  const requestId = 'another-users-request';
  const event = (seq, message) => ({ seq, message, timestamp: '2026-11-27T12:00:00Z' });
  fetchDiscoveryActive.mockResolvedValueOnce({ active: { request_id: requestId } }).mockResolvedValue({ active: null });
  fetchDiscoveryRequest.mockResolvedValueOnce({ request_id: requestId, state: 'running', stage: 'switches',
    completed_roots: 4, total_roots: 4, completed_switches: 3, discovered_switches: 8,
    upstream_calls: 8, elapsed_seconds: 2, last_seq: 1, logs: [event(1, 'Discovering shared switches')] })
    .mockResolvedValue({ request_id: requestId, state: 'succeeded', stage: 'finished', last_seq: 2,
      logs: [event(2, 'Discovery succeeded')] });
  await act(async () => { render(<DiscoveryProgress onBusyChange={onBusyChange} />); });
  expect(onBusyChange).toHaveBeenLastCalledWith(true);
  expect(screen.getByText(requestId)).toBeInTheDocument();
  expect(screen.getByText('Switches: 3/8 discovered')).toBeInTheDocument();
  await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
  expect(onBusyChange).toHaveBeenLastCalledWith(false);
  expect(fetchDiscoveryRequest).toHaveBeenLastCalledWith(requestId, 1);
  expect(screen.getByText('Discovery succeeded')).toBeInTheDocument();
  expect(screen.getAllByText('Discovering shared switches')).toHaveLength(1);
});

test('tracks a local ID before the POST registers and follows the active ID on rejection', async () => {
  fetchDiscoveryActive.mockResolvedValue({ active: null });
  fetchDiscoveryRequest.mockRejectedValue(new Error('Not registered yet'));
  const onBusyChange = vi.fn();
  await act(async () => { render(<DiscoveryProgress onBusyChange={onBusyChange} />); });
  await act(async () => { window.dispatchEvent(new CustomEvent('discovery-request', {
    detail: { phase: 'start', request_id: 'local-request' }
  })); });
  expect(screen.getByText('local-request')).toBeInTheDocument();
  expect(onBusyChange).toHaveBeenLastCalledWith(true);
  fetchDiscoveryActive.mockResolvedValue({ active: { request_id: 'active-request' } });
  fetchDiscoveryRequest.mockResolvedValue({ request_id: 'active-request', state: 'running', stage: 'metadata', logs: [] });
  await act(async () => { window.dispatchEvent(new CustomEvent('discovery-request', {
    detail: { phase: 'end', request_id: 'local-request', state: 'failed', active_request_id: 'active-request' }
  })); });
  expect(screen.getByText('active-request')).toBeInTheDocument();
  expect(onBusyChange).toHaveBeenLastCalledWith(true);
});
