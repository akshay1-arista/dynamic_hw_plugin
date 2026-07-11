const API_BASE = (import.meta.env.VITE_API_BASE_URL || '').replace(/\/$/, '');

async function request(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: {
      'Content-Type': 'application/json',
      ...(options.headers || {})
    },
    ...options
  });

  if (!response.ok) {
    let message = response.statusText;
    try {
      const body = await response.json();
      message = formatErrorDetail(body.detail) || message;
    } catch {
      // Keep status text when the server does not return JSON.
    }
    throw new Error(message);
  }

  return response.json();
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

export function applyInventoryRefresh(hardwareIds) {
  return request('/api/hardware/refresh-apply', {
    method: 'POST',
    body: JSON.stringify({ hardware_ids: hardwareIds })
  });
}

export function generateTopology(payload) {
  return request('/api/generate', {
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
