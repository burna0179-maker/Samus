// Sidebar wiring -- populated when a chat is active, hidden otherwise.
import { emit, on } from 'core/event-bus.js';
import { fetchPersonas, fetchPresets, patchChat } from 'core/api.js';

let activeChat = null;
let personas = [];
let presets = [];

const sidebar = () => document.getElementById('sidebar');
const $ = (id) => document.getElementById(id);

export async function initSidebar() {
  on('chat:active', async (chat) => {
    activeChat = chat;
    if (!chat) { sidebar().hidden = true; return; }
    sidebar().hidden = false;
    if (personas.length === 0) {
      [personas, presets] = await Promise.all([
        fetchPersonas().then((d) => d.personas).catch(() => []),
        fetchPresets().then((d) => d.presets).catch(() => []),
      ]);
    }
    populate();
  });

  $('bag-save').addEventListener('click', save);
}

function populate() {
  const personaPicker = $('persona-picker');
  personaPicker.innerHTML = '';
  for (const p of personas) {
    const opt = document.createElement('option');
    opt.value = p.persona_id;
    opt.textContent = p.display_name;
    if (p.persona_id === activeChat.persona_id) opt.selected = true;
    personaPicker.appendChild(opt);
  }
  $('persona-tagline').textContent =
    personas.find((p) => p.persona_id === activeChat.persona_id)?.tagline || '';

  const presetPicker = $('preset-picker');
  presetPicker.innerHTML = '';
  for (const p of presets) {
    const opt = document.createElement('option');
    opt.value = p.preset_id;
    opt.textContent = p.preset_id;
    if (p.preset_id === activeChat.bag.preset_id) opt.selected = true;
    presetPicker.appendChild(opt);
  }

  $('bag-inject-datetime').checked = !!activeChat.bag.inject_datetime;
  $('bag-inject-evidence').checked = !!activeChat.bag.inject_evidence_tip;
  $('bag-spice-enabled').checked = !!activeChat.bag.spice_enabled;
  $('bag-spice-turns').value = activeChat.bag.spice_turns || 3;
  $('bag-custom-context').value = activeChat.bag.custom_context || '';
  $('bag-trim-color').value = activeChat.bag.trim_color || '#dc2626';
  applyTrim($('bag-trim-color').value);
  $('bag-trim-color').addEventListener('input', (e) => applyTrim(e.target.value));
}

function applyTrim(hex) {
  document.documentElement.style.setProperty('--trim', hex);
  document.documentElement.style.setProperty('--trim-glow', hex + '40');
}

async function save() {
  if (!activeChat) return;
  const newBag = {
    preset_id: $('preset-picker').value,
    inject_datetime: $('bag-inject-datetime').checked,
    inject_evidence_tip: $('bag-inject-evidence').checked,
    spice_enabled: $('bag-spice-enabled').checked,
    spice_turns: parseInt($('bag-spice-turns').value, 10) || 3,
    custom_context: $('bag-custom-context').value,
    trim_color: $('bag-trim-color').value,
  };
  const personaId = $('persona-picker').value;
  try {
    const updated = await patchChat(activeChat.chat_id, {
      persona_id: personaId,
      bag: newBag,
    });
    activeChat = { ...activeChat, ...updated };
    emit('chat:updated', activeChat);
    document.getElementById('status-text').textContent =
      `saved chat ${updated.chat_id.slice(0, 8)}…`;
  } catch (err) {
    document.getElementById('status-text').textContent =
      `save failed: ${err.message || err}`;
  }
}
