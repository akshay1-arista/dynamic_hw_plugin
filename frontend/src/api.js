const API_BASE = (import.meta.env.VITE_API_BASE_URL || '').replace(/\/$/, '');

async function request(path, options = {}) {
  const discovery = /^\/api\/(hardware\/(refresh|import)-(preview|apply)|lab-navigator\/search|generate$|switch-config-runs$)/.test(path);
  const requestId = discovery ? newRequestId() : null;
  const signalProgress = (phase, detail = {}) => {
    if (discovery) window.dispatchEvent(new CustomEvent('discovery-request', {
      detail: { phase, request_id: requestId, ...detail }
    }));
  };
  signalProgress('start');
  try {
    const response = await fetch(`${API_BASE}${path}`, {
      ...options,
      headers: {
        'Content-Type': 'application/json',
        ...(options.headers || {}),
        ...(requestId ? { 'X-Request-ID': requestId } : {})
      }
    });

    if (!response.ok) {
      let message = response.statusText;
      let detail;
      try {
        const body = await response.json();
        detail = body.detail;
        message = formatErrorDetail(body.detail) || message;
      } catch {
        // Keep status text when the server does not return JSON.
      }
      const identifier = detail?.request_id || requestId;
      const error = new Error(`${message || 'Request failed'}${identifier ? ` (Request ID: ${identifier})` : ''}`);
      error.status = response.status;
      error.detail = detail;
      throw error;
    }

    const result = await response.json();
    signalProgress('end', { state: result.summary?.status === 'partial' ? 'partial' : 'succeeded' });
    return result;
  } catch (error) {
    signalProgress('end', { state: 'failed', active_request_id: error.detail?.active_request_id });
    if (requestId && !error.message.includes(requestId)) error.message += ` (Request ID: ${requestId})`;
    throw error;
  }
}

function newRequestId() {
  if (globalThis.crypto.randomUUID) return globalThis.crypto.randomUUID();
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export function fetchDiscoveryActive() {
  return request('/api/discovery/active');
}

export function fetchDiscoveryRequest(requestId, afterSeq = 0) {
  return request(`/api/discovery/requests/${encodeURIComponent(requestId)}?after_seq=${afterSeq}`);
}

function formatErrorDetail(detail) {
  if (!detail) {
    return '';
  }
  if (typeof detail === 'string') {
    return detail;
  }
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (typeof item === 'string') {
          return item;
        }
        const location = Array.isArray(item.loc) ? item.loc.filter((part) => part !== 'body').join('.') : '';
        return [location, item.msg].filter(Boolean).join(': ');
      })
      .filter(Boolean)
      .join('\n');
  }
  if (typeof detail === 'object') {
    return detail.message || JSON.stringify(detail);
  }
  return String(detail);
}

export function fetchReferences() {
  return request('/api/reference-topologies');
}

export function fetchInventory() {
  return request('/api/hardware');
}

export function fetchPrivateBranches() {
  return request('/api/hapy/private-branches');
}

export function fetchGeneratedRuns() {
  return request('/api/runs');
}

export function fetchGeneratedRun(runId) {
  return request(`/api/runs/${runId}`);
}

export function fetchAuditTrail() {
  return request('/api/audit-trail');
}

export function saveInventory(inventory, requestedBy = null) {
  return request('/api/hardware', {
    method: 'PUT',
    body: JSON.stringify({
      inventory,
      requested_by: requestedBy
    })
  });
}

export function previewInventoryRefresh(hardwareIds) {
  return request('/api/hardware/refresh-preview', {
    method: 'POST',
    body: JSON.stringify({ hardware_ids: hardwareIds })
  });
}

export function applyInventoryRefresh(hardwareIds, previewId) {
  return request('/api/hardware/refresh-apply', {
    method: 'POST',
    body: JSON.stringify({ hardware_ids: hardwareIds, ...(previewId ? { preview_id: previewId } : {}) })
  });
}

export function searchLabNavigatorDevices(query) {
  return request(`/api/lab-navigator/search?q=${encodeURIComponent(query)}`);
}

export function previewInventoryImport(payload) {
  return request('/api/hardware/import-preview', {
    method: 'POST',
    body: JSON.stringify(payload)
  });
}

export function applyInventoryImport(payload) {
  return request('/api/hardware/import-apply', {
    method: 'POST',
    body: JSON.stringify(payload)
  });
}

export function generateTopology(payload) {
  return request('/api/generate', {
    method: 'POST',
    body: JSON.stringify(payload)
  });
}

export function createSwitchConfigRun(payload) {
  return request('/api/switch-config-runs', {
    method: 'POST',
    body: JSON.stringify(payload)
  });
}

export function updateHardwareAvailability(hardwareId, available, requestedBy) {
  return request(`/api/hardware/${hardwareId}/availability`, {
    method: 'POST',
    body: JSON.stringify({
      available,
      requested_by: requestedBy
    })
  });
}

export function publishPrivateBranch(runId, payload) {
  return request(`/api/runs/${runId}/publish-private-branch`, {
    method: 'POST',
    body: JSON.stringify(payload)
  });
}

export function deletePrivateBranches(payload) {
  return request('/api/hapy/private-branches/delete', {
    method: 'POST',
    body: JSON.stringify(payload)
  });
}

export function configureSwitches(runId, payload = {}) {
  return request(`/api/runs/${runId}/configure-switches`, {
    method: 'POST',
    body: JSON.stringify(payload)
  });
}
