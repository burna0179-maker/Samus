// Chat view -- list of chats + active stream + compose box.
import { emit, on } from 'core/event-bus.js';
import { listChats, createChat, readChat, respond } from 'core/api.js';

let activeChat = null;
let chats = [];

const aiName = () => document.querySelector('meta[name="ai-name"]')?.content || 'Samus';

export async function render(root) {
  root.innerHTML = `
    <section class="chat-shell">
      <div class="chat-list" id="chat-list"></div>
      <div class="chat-stream" id="chat-stream"></div>
      <form class="chat-compose" id="chat-form">
        <textarea id="chat-input" placeholder="ask ${escapeHtml(aiName())} about workcells, CRM, proposals…"></textarea>
        <button type="submit">Send</button>
      </form>
    </section>
  `;
  document.getElementById('chat-form').addEventListener('submit', onSubmit);
  await refreshList();
  if (chats.length > 0) {
    await selectChat(chats[0].chat_id);
  } else {
    await selectChat(null);
  }
}

async function refreshList() {
  const data = await listChats();
  chats = data.chats || [];
  const ul = document.getElementById('chat-list');
  ul.innerHTML = '';
  const newBtn = document.createElement('button');
  newBtn.textContent = '+ new chat';
  newBtn.className = 'new-chat';
  newBtn.addEventListener('click', onNewChat);
  ul.appendChild(newBtn);
  for (const c of chats) {
    const b = document.createElement('button');
    b.textContent = c.name;
    if (activeChat && c.chat_id === activeChat.chat_id) b.classList.add('is-active');
    b.addEventListener('click', () => selectChat(c.chat_id));
    ul.appendChild(b);
  }
}

async function selectChat(id) {
  if (!id) {
    activeChat = null;
    document.getElementById('chat-stream').innerHTML =
      '<div class="chat-msg system"><span class="role">empty</span>\nNo chats yet — click <strong>+ new chat</strong>.</div>';
    emit('chat:active', null);
    return;
  }
  const data = await readChat(id);
  activeChat = data;
  paintMessages(data.messages || []);
  emit('chat:active', data);
  refreshList();
}

function paintMessages(messages) {
  const stream = document.getElementById('chat-stream');
  stream.innerHTML = '';
  for (const m of messages) {
    const el = document.createElement('div');
    el.className = `chat-msg ${m.role}`;
    el.innerHTML =
      `<span class="role">${m.role}${m.metadata?.spice_turn ? ` · turn ${m.metadata.spice_turn}` : ''}</span>\n` +
      escapeHtml(m.content);
    stream.appendChild(el);
  }
  stream.scrollTop = stream.scrollHeight;
}

async function onNewChat() {
  const name = prompt('Chat name?', `console-${new Date().toISOString().slice(0, 16)}`);
  if (!name) return;
  const personaId = document.querySelector('meta[name="default-persona"]').content;
  const c = await createChat({ name, persona_id: personaId });
  await refreshList();
  await selectChat(c.chat_id);
}

async function onSubmit(e) {
  e.preventDefault();
  if (!activeChat) return;
  const input = document.getElementById('chat-input');
  const content = input.value.trim();
  if (!content) return;
  input.value = '';
  const placeholder = document.createElement('div');
  placeholder.className = 'chat-msg user';
  placeholder.innerHTML = `<span class="role">user</span>\n${escapeHtml(content)}`;
  document.getElementById('chat-stream').appendChild(placeholder);
  try {
    await respond(activeChat.chat_id, content);
    const data = await readChat(activeChat.chat_id);
    activeChat = data;
    paintMessages(data.messages || []);
  } catch (err) {
    const stream = document.getElementById('chat-stream');
    const div = document.createElement('div');
    div.className = 'chat-msg system';
    div.textContent = `respond failed: ${err.message || err}`;
    stream.appendChild(div);
  }
}

on('chat:updated', (chat) => {
  if (activeChat && chat && chat.chat_id === activeChat.chat_id) {
    activeChat = chat;
  }
});

function escapeHtml(s) {
  return String(s)
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');
}
