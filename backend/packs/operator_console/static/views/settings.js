// Settings view -- console state + Samus health snapshot.
export async function render(root) {
  root.innerHTML = `
    <section>
      <h2>Settings</h2>
      <div id="set-state"></div>
      <h3>Health</h3>
      <div id="set-health"></div>
    </section>
  `;
  try {
    const state = await (await fetch('/api/console/state')).json();
    document.getElementById('set-state').innerHTML =
      `<h3>Console state</h3><pre class="code-block">${escapeHtml(JSON.stringify(state, null, 2))}</pre>`;
  } catch (err) {
    document.getElementById('set-state').textContent = `state fetch failed: ${err.message || err}`;
  }
  try {
    const health = await (await fetch('/health')).json();
    document.getElementById('set-health').innerHTML =
      `<pre class="code-block">${escapeHtml(JSON.stringify(health, null, 2))}</pre>`;
  } catch (err) {
    document.getElementById('set-health').textContent = `health fetch failed: ${err.message || err}`;
  }
}

function escapeHtml(s) {
  return String(s ?? '')
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
}
