// Thin fetch wrapper that auto-propagates the trace-id header.
const BASE = '/api/console';
const trace = () => {
  if (!window.__traceId) {
    window.__traceId = crypto.randomUUID().replaceAll('-', '');
    const el = document.getElementById('trace-id');
    if (el) el.textContent = `trace=${window.__traceId.slice(0, 12)}`;
  }
  return window.__traceId;
};

export function initApi() { trace(); }

async function request(method, path, body) {
  const init = {
    method,
    headers: { 'Accept': 'application/json', 'X-Trace-Id': trace() },
  };
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  const res = await fetch(BASE + path, init);
  if (res.status === 204) return null;
  const text = await res.text();
  let parsed = null;
  if (text) { try { parsed = JSON.parse(text); } catch { /* keep */ } }
  if (!res.ok) {
    const err = new Error(parsed?.title || parsed?.detail || `HTTP ${res.status}`);
    err.status = res.status; err.problem = parsed; throw err;
  }
  return parsed;
}

export const get  = (p)    => request('GET', p);
export const post = (p, b) => request('POST', p, b ?? {});
export const patch = (p, b) => request('PATCH', p, b ?? {});
export const del  = (p)    => request('DELETE', p);

export const fetchState     = ()        => get('/state');
export const fetchPersonas  = ()        => get('/personas');
export const fetchPresets   = ()        => get('/presets');
export const fetchPieces    = ()        => get('/pieces');
export const listChats      = ()        => get('/chats');
export const createChat     = (body)    => post('/chats', body);
export const readChat       = (id)      => get(`/chats/${encodeURIComponent(id)}`);
export const patchChat      = (id, b)   => patch(`/chats/${encodeURIComponent(id)}`, b);
export const deleteChat     = (id)      => del(`/chats/${encodeURIComponent(id)}`);
export const assemblePrompt = (id)      => post(`/chats/${encodeURIComponent(id)}/assemble`, {});
export const respond        = (id, c)   => post(`/chats/${encodeURIComponent(id)}/respond`, { content: c });
