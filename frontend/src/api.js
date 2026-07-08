const API_BASE = '';

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

export function saveInventory(inventory) {
  return request('/api/hardware', {
    method: 'PUT',
    body: JSON.stringify(inventory)
  });
}

export function generateTopology(payload) {
  return request('/api/generate', {
    method: 'POST',
    body: JSON.stringify(payload)
  });
}
