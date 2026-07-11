// CRM view -- best-effort snapshot of the CRM workcell.
export async function render(root) {
  root.innerHTML = `<section><h2>CRM snapshot</h2><div id="crm-mount"></div></section>`;
  const mount = document.getElementById('crm-mount');
  const candidates = [
    '/api/crm/opportunities',
    '/api/crm/list',
    '/api/crm/state',
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
  mount.innerHTML = '<p class="muted">No CRM endpoint responded.</p>';
}

function escapeHtml(s) {
  return String(s ?? '')
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
}
