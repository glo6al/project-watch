/* Our adapter to Antigravity's installed native sidecar UI contract.
 * This is not the vendor SDK and never installs a replacement window.sidecar.
 */
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const validUUID = value => typeof value === 'string' && UUID.test(value);
const exact = (value, keys) => value !== null && typeof value === 'object' &&
  !Array.isArray(value) && Object.keys(value).sort().join('|') === keys.slice().sort().join('|');

function createConnector({request, invoke, pageId, now = Date.now, onState = () => {}}) {
  if (!validUUID(pageId) || typeof request !== 'function' || typeof invoke !== 'function') {
    throw new Error('Invalid connector configuration.');
  }
  let lease = null, closed = false, busy = false;
  const seen = new Map();
  async function tick() {
    if (closed || busy) return;
    busy = true;
    try {
      if (!lease) {
        const hello = await request('/ui/hello', {page_id: pageId});
        if (closed) return;
        if (!hello || hello.page_id !== pageId || hello.lease_ms !== 8000) throw new Error('Invalid native lease.');
        if (hello.passive === true && exact(hello, ['page_id', 'passive', 'lease_ms'])) {
          onState('Connected through another panel');
          return;
        }
        if (hello.passive !== false || !exact(hello, ['page_id', 'passive', 'lease_ms', 'lease']) ||
            typeof hello.lease !== 'string' || !/^[0-9a-f]{64}$/.test(hello.lease)) throw new Error('Invalid native lease.');
        lease = hello.lease;
      }
      const started = now();
      const response = await request('/ui/poll', {page_id: pageId, lease});
      if (closed) return;
      const command = response?.command;
      if (!command) { onState('Connected'); return; }
      for (const [id, expires] of seen) if (now() >= expires + 30000) seen.delete(id);
      if (!exact(command, ['request_id', 'conversation_id', 'expires_at_ms', 'remaining_ms']) ||
          !validUUID(command.request_id) || !validUUID(command.conversation_id) ||
          !Number.isSafeInteger(command.expires_at_ms) || !Number.isInteger(command.remaining_ms) ||
          command.remaining_ms < 1 || command.remaining_ms > 5000 ||
          command.expires_at_ms <= now() || command.expires_at_ms > now() + 5000 || now() - started < 0 ||
          now() - started > Math.min(1500, command.remaining_ms) ||
          seen.has(command.request_id) || seen.size >= 2048) {
        onState('Opening was not requested');
        return;
      }
      // Consume before touching the native API; uncertain delivery never retries.
      seen.set(command.request_id, command.expires_at_ms);
      let state = 'called';
      try { invoke(command.conversation_id); }
      catch (_) { state = 'failed'; }
      onState(state === 'called' ? 'Opening requested' : 'Opening was not requested');
      await request('/ui/receipt', {page_id: pageId, lease,
        request_id: command.request_id, conversation_id: command.conversation_id, state});
    } catch (_) {
      lease = null;
      onState('Connection unavailable');
    } finally { busy = false; }
  }
  async function close() {
    closed = true;
    const previous = lease;
    lease = null;
    if (previous) {
      try { await request('/ui/bye', {page_id: pageId, lease: previous}); }
      catch (_) { /* Server-side lease expiry is the remaining boundary. */ }
    }
  }
  return {tick, close};
}

function boot() {
  const status = document.getElementById('status');
  status.textContent = 'Connecting…';
  const parentOrigin = document.querySelector('meta[name="watch-native-parent"]')?.content;
  const uiOrigin = document.querySelector('meta[name="watch-native-origin"]')?.content;
  const actualOrigin = `${window.location.protocol}//${window.location.host}`;
  const ancestors = window.location.ancestorOrigins;
  let observedParent = ancestors && ancestors.length ? ancestors[0] : '';
  if (!observedParent && document.referrer) {
    try { observedParent = new URL(document.referrer).origin; } catch (_) {}
  }
  const params = new URLSearchParams(window.location.search);
  const tokens = params.getAll('token');
  if (window.parent === window || !/^https:\/\/127\.0\.0\.1:[1-9][0-9]{0,4}$/.test(parentOrigin || '') ||
      observedParent !== parentOrigin || actualOrigin !== uiOrigin ||
      window.location.protocol !== 'plugin:' || tokens.length !== 1 || !tokens[0]) {
    status.textContent = 'Open WATCH Navigation inside Antigravity to connect.';
    return;
  }
  const token = tokens[0];
  const request = async (path, body) => {
    if (path === '/ui/hello') body = {...body, parent_origin: parentOrigin};
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 1500);
    try {
      const response = await fetch(path, {method: 'POST', redirect: 'error', cache: 'no-store',
        credentials: 'omit', referrerPolicy: 'no-referrer', signal: controller.signal,
        headers: {'Content-Type': 'application/json', 'X-Sidecar-Token': token},
        body: JSON.stringify(body), keepalive: path === '/ui/bye'});
      if (!response.ok) throw new Error('Native connection unavailable.');
      return await response.json();
    } finally { clearTimeout(timer); }
  };
  const connector = createConnector({request, pageId: crypto.randomUUID(),
    invoke: conversationId => {
      // Exactly the native SDK's navigation payload, from a legitimately owned
      // sidecar frame. Neither a credential nor an arbitrary method crosses it.
      window.parent.postMessage({type: 'toggle-conversation', payload: {conversationId}}, parentOrigin);
    }, onState: value => { status.textContent = value; }});
  let stopped = false;
  async function loop() {
    await connector.tick();
    if (!stopped) setTimeout(loop, 1000);
  }
  window.addEventListener('pagehide', () => { stopped = true; void connector.close(); }, {once: true});
  void loop();
}

if (typeof window !== 'undefined' && typeof document !== 'undefined') {
  try { boot(); }
  catch (_) {
    const status = document.getElementById('status');
    if (status) status.textContent = 'Connection unavailable. Reopen WATCH Navigation to try again.';
  }
}
