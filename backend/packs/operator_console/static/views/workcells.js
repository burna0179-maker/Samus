// Workcells view -- per-workcell health.
const WORKCELLS = [
  'gateway', 'crm', 'feedback', 'finance', 'fulfillment',
  'intake', 'leadgen', 'outreach', 'products', 'proposal',
  'prospecting', 'retainer', 'seo', 'voice',
];

export async function render(root) {
  root.innerHTML = `
    <section>
      <h2>Workcell health</h2>
      <p class="muted">Best-effort /health probe to each workcell. Adjust /etc/hosts or use a proxy if probing from outside the cluster.</p>
      <table class="console">
        <thead><tr><th>workcell</th><th>status</th><th>detail</th></tr></thead>
        <tbody id="wc-mount"></tbody>
      </table>
    </section>
  `;
  const tbody = document.getElementById('wc-mount');
  for (const wc of WORKCELLS) {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${escapeHtml(wc)}</td><td><span class="tag">…</span></td><td>—</td>`;
    tbody.appendChild(tr);
    fetch(`/health`, { headers: { 'Accept': 'application/json' } })
      .then((r) => r.json().then((d) => ({ ok: r.ok, status: r.status, data: d })))
      .then(({ ok, status, data }) => {
        const tag = ok ? 'ok' : 'danger';
        tr.children[1].innerHTML = `<span class="tag ${tag}">${status}</span>`;
        tr.children[2].textContent = JSON.stringify(data).slice(0, 80);
      })
      .catch((err) => {
        tr.children[1].innerHTML = '<span class="tag warn">err</span>';
        tr.children[2].textContent = err.message || String(err);
      });
  }
}

function escapeHtml(s) {
  return String(s ?? '')
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
}
