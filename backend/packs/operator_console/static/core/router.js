// Simple in-document router -- dynamically imports the view module.
import { emit } from 'core/event-bus.js';

const VIEWS = {
  chat:      () => import('views/chat.js'),
  workcells: () => import('views/workcells.js'),
  crm:       () => import('views/crm.js'),
  proposals: () => import('views/proposals.js'),
  peers:     () => import('views/peers.js'),
  settings:  () => import('views/settings.js'),
};

let current = null;

export function initNav() {
  document.querySelectorAll('.navlink').forEach((btn) => {
    btn.addEventListener('click', () => switchView(btn.dataset.view));
  });
}

export async function switchView(name) {
  if (current === name) return;
  current = name;
  const root = document.getElementById('view-root');
  root.innerHTML = '<div class="view-placeholder">Loading view&hellip;</div>';
  emit('view:enter', { name });
  const loader = VIEWS[name];
  if (!loader) {
    root.innerHTML = `<div class="view-placeholder">Unknown view: ${name}</div>`;
    return;
  }
  try {
    const mod = await loader();
    root.innerHTML = '';
    if (typeof mod.render === 'function') {
      await mod.render(root);
    } else {
      root.innerHTML = `<div class="view-placeholder">View ${name} exports no render()</div>`;
    }
  } catch (err) {
    root.innerHTML =
      `<div class="view-placeholder">View ${name} failed to load: ${err.message || err}</div>`;
  }
}
