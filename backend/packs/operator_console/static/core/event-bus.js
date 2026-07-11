// Minimal pub/sub event bus.
const handlers = new Map();

export function initEventBus() { handlers.clear(); }

export function on(event, fn) {
  if (!handlers.has(event)) handlers.set(event, new Set());
  handlers.get(event).add(fn);
  return () => off(event, fn);
}

export function off(event, fn) {
  const set = handlers.get(event);
  if (set) set.delete(fn);
}

export function emit(event, payload) {
  const set = handlers.get(event);
  if (!set) return;
  for (const fn of set) {
    try { fn(payload); } catch (err) { console.error(`handler for ${event} threw:`, err); }
  }
}
