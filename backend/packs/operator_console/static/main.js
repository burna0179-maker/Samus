// Operator console SPA entrypoint -- vanilla ESM, no bundler.
import { initEventBus, on, emit } from 'core/event-bus.js';
import { initApi, fetchState } from 'core/api.js';
import { initNav, switchView } from 'core/router.js';
import { initSidebar } from 'features/sidebar.js';

initEventBus();
initApi();
initNav();
initSidebar();

document.addEventListener('DOMContentLoaded', async () => {
  try {
    const state = await fetchState();
    document.getElementById('health-dot').dataset.ok = '1';
    emit('console:state', state);
  } catch (err) {
    document.getElementById('health-dot').dataset.ok = '0';
    document.getElementById('status-text').textContent =
      `state fetch failed (${err.message || err})`;
  }
  switchView('chat');
});

on('view:enter', ({ name }) => {
  document.querySelectorAll('.navlink').forEach((b) =>
    b.classList.toggle('is-active', b.dataset.view === name)
  );
});
