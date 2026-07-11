// Peers view -- Samus's heartbeat / federation surface.
export async function render(root) {
  root.innerHTML = `<section><h2>Peer agents</h2><div id="peers-mount"></div></section>`;
  const mount = document.getElementById('peers-mount');
  const candidates = [
    '/api/peers',
    '/api/federation/peers',
    '/api/admin/peers',
  ];
  for (const path of candidates) {
    try {
      const res = await fetch(path, { headers: { 'Accept': 'application/json' } });
      if (res.status === 404) continue;
      const data = await res.json();
      const peers = data.peers || data.agents || data.entries || [];
      if (!Array.isArray(peers) || peers.length === 0) {
        mount.innerHTML = `<p class="muted">No peers recorded at <code>${escapeHtml(path)}</code>.</p>`;
        return;
      }
      const headers = Object.keys(peers[0]);
      const head = headers.map((h) => `<th>${escapeHtml(h)}</th>`).join('');
      const rows = peers.map((p) =>
        `<tr>${headers.map((h) => `<td>${escapeHtml(_trunc(p[h]))}</td>`).join('')}</tr>`
      ).join('');
      mount.innerHTML =
        `<p class="muted">Source: <code>${escapeHtml(path)}</code></p>` +
        `<table class="console"><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table>`;
      return;
    } catch {
      // try next
    }
  }
  mount.innerHTML = '<p class="muted">No peer endpoint responded.</p>';
}

function _trunc(v) { const s = String(v ?? ''); return s.length > 48 ? s.slice(0, 44) + '…' : s; }
function escapeHtml(s) {
  return String(s ?? '')
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
}
