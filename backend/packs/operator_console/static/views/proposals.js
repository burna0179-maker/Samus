// Proposals view -- best-effort snapshot of the proposal workcell.
export async function render(root) {
  root.innerHTML = `<section><h2>Proposal pipeline</h2><div id="prop-mount"></div></section>`;
  const mount = document.getElementById('prop-mount');
  const candidates = [
    '/api/proposals/list',
    '/api/proposal/list',
    '/api/proposal/state',
  ];
  for (const path of candidates) {
    try {
      const res = await fetch(path, { headers: { 'Accept': 'application/json' } });
      if (res.status === 404) continue;
      const data = await res.json();
      mount.innerHTML =
        `<p class="muted">Source: <code>${escapeHtml(path)}</code></p>` +
        `<pre class="code-block">${escapeHtml(JSON.stringify(data, null, 2))}</pre>`;
      return;
    } catch {
      // try next
    }
  }
  mount.innerHTML = '<p class="muted">No proposal endpoint responded.</p>';
}

function escapeHtml(s) {
  return String(s ?? '')
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
}
