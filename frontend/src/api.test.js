import { afterEach, expect, test, vi } from 'vitest';
import { applyInventoryRefresh, previewInventoryRefresh } from './api.js';

afterEach(() => vi.unstubAllGlobals());

test('generates an ID on HTTP origins without randomUUID and sends the reviewed preview', async () => {
  vi.stubGlobal('crypto', { getRandomValues: (bytes) => bytes.fill(13) });
  const fetch = vi.fn().mockResolvedValue(Response.json({ summary: { status: 'success' } }));
  vi.stubGlobal('fetch', fetch);
  await applyInventoryRefresh(['edge-1'], 'preview-1');
  const options = fetch.mock.calls[0][1];
  expect(options.headers['X-Request-ID']).toMatch(/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/);
  expect(JSON.parse(options.body)).toEqual({ hardware_ids: ['edge-1'], preview_id: 'preview-1' });
});

test('preserves busy metadata and includes the request ID in errors', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json({ detail: {
    message: 'Discovery is already running', code: 'discovery_busy', active_request_id: 'other-request'
  } }, { status: 409 })));
  try {
    await previewInventoryRefresh(['edge-1']);
    throw new Error('Expected busy rejection');
  } catch (error) {
    expect(error.status).toBe(409);
    expect(error.detail.active_request_id).toBe('other-request');
    expect(error.message).toContain('Request ID:');
  }
});
