import {UUID, LIMITATION, TRANSITIONS, pairing, sessionResponse, validClaim, matchingPrompt, matchingAnswer, publicStatus} from './protocol.js';

const keyFor = id => 'watch-owner-v1:' + id;
const HISTORY_LIMIT = 32;

function trimHistory(state) {
  const requests = Object.values(state.data.requests);
  let excess = requests.length - HISTORY_LIMIT;
  for (const request of requests) {
    if (excess <= 0) break;
    // Only acknowledged terminal receipts can go. Pending events retain recovery data.
    if (request !== state.flight && !request.events.length &&
        ['answered','dropped','unknown'].includes(request.phase)) {
      delete state.data.requests[request.request_id];
      excess -= 1;
    }
  }
}

// Only top-level functions receive $: official mods discover API calls statically.
async function save($, state, change) {
  const operation = state.writes.then(async () => {
    if (change) change();
    trimHistory(state);
    await $.store.set(keyFor(state.data.native_id), JSON.parse(JSON.stringify(state.data)));
  });
  state.writes = operation.catch(() => {});
  return operation;
}

async function post($, state, path, body, bearer = true) {
  const headers = {'Content-Type':'application/json'};
  if (bearer) headers.Authorization = 'Bearer ' + state.data.token;
  // No response can choose a host, path, port or command. Official fetch follows redirects;
  // WATCH's fixed loopback service never redirects. No unsupported redirect option is used.
  const response = await $.http.fetch('http://127.0.0.1:' + state.data.port + '/claude-mod/' + path,
    {method:'POST', headers, body:JSON.stringify(body)});
  if (!response.ok) throw new Error('WATCH service unavailable (' + response.status + ').');
  if (typeof response.text !== 'string' || response.text.length > 100000) throw new Error('Invalid WATCH response.');
  return JSON.parse(response.text);
}

function stop(state) {
  state.connected = false;
  state.epoch += 1;
  state.timer?.cancel();
  state.timer = null;
}

async function disconnect($, state) {
  stop(state);
  if (!state.data) return;
  await save($, state, () => {state.data.active = false;});
  try { await post($, state, 'disconnect', {generation:state.generation, native_id:state.data.native_id}); }
  catch { state.error = 'WATCH disconnect could not be confirmed; readiness expires when its heartbeat stops.'; }
}

async function event($, state, request, kind, extras = {}) {
  // Completion wins over a late submit settlement. Persist status/reply before network delivery.
  await save($, state, () => {
    if (request.completed && kind !== 'answered') return;
    if (kind === 'submitted' && ['started','answered','unknown','dropped'].includes(request.phase)) return;
    if (kind === 'answered') request.completed = true;
    request.phase = kind;
    const value = {request_id:request.request_id, attempt_id:request.attempt_id, kind, ...extras};
    if (!request.events.some(item => item.kind === kind)) request.events.push(value);
  });
  // Native hooks await durable storage only. WATCH being offline must never hold
  // turn.start/complete (and thus the native turn) behind an HTTP request.
  flush($, state).catch(() => {
    state.error = 'WATCH event delivery interrupted; retained receipts will be retried without resubmitting.';
  });
}

async function flush($, state) {
  if (state.flushing || !state.connected) return;
  state.flushing = true;
  try {
    for (const request of Object.values(state.data.requests)) {
      while (request.events.length && state.connected) {
        const value = request.events[0];
        await post($, state, 'event', {...value, generation:state.generation, native_id:state.data.native_id});
        await save($, state, () => {if (request.events[0] === value) request.events.shift();});
      }
    }
  } finally { state.flushing = false; }
}

async function failRequest($, state, request) {
  try { await event($, state, request, 'unknown'); } catch { /* Durable server claim remains attempted. */ }
}

async function submit($, state, request, envelope, epoch) {
  try {
    const id = await $.session.id();
    const now = await $.clock.now();
    if (!state.connected || state.epoch !== epoch || id !== state.data.native_id) {
      await failRequest($, state, request); return;
    }
    if (now / 1000 >= request.expires_at) {await event($, state, request, 'dropped'); return;}
    // This durable record precedes the side effect, and is never submitted again after reload.
    await save($, state, () => {request.attempted = true; request.phase = 'pending';});
    if (!state.connected || state.epoch !== epoch || await $.session.id() !== state.data.native_id) {
      await failRequest($, state, request); return;
    }
    state.envelope = envelope;
    const result = await $.prompt.submit({text:envelope, asUser:false});
    if (typeof result?.drop === 'string') await event($, state, request, 'dropped');
    else await event($, state, request, 'submitted');
  } catch {
    await failRequest($, state, request);
  } finally {
    state.submitting = false;
    if (['answered','dropped','unknown'].includes(request.phase)) {state.flight = null; state.envelope = null;}
  }
}

async function tick($, state) {
  if (!state.connected || state.polling) return;
  state.polling = true;
  const epoch = state.epoch;
  try {
    if (await $.session.id() !== state.data.native_id || await $.session.cwd() !== state.data.project) {
      await disconnect($, state); return;
    }
    await flush($, state);
    const response = await post($, state, 'poll', {generation:state.generation,
      native_id:state.data.native_id, project:state.data.project, accept:!state.flight && !state.submitting});
    if (!sessionResponse({...response, heartbeat_interval_ms:2000, lease_seconds:12}, state.data.native_id) ||
        response.generation !== state.generation) throw new Error('Invalid WATCH poll identity.');
    if (!state.connected || state.epoch !== epoch || !response.claim) return;
    const claim = response.claim;
    if (state.data.requests[claim.request_id]) {
      // A duplicate response can only recover status. It is never permission to submit.
      await failRequest($, state, state.data.requests[claim.request_id]); return;
    }
    const request = {request_id:claim.request_id, attempt_id:claim.attempt_id,
      expires_at:claim.expires_at, generation:state.generation, attempted:false, phase:'claimed', events:[]};
    if (!validClaim(claim, {...state.data, generation:state.generation}, await $.clock.now())) {
      if (UUID.test(claim.request_id) && UUID.test(claim.attempt_id)) {
        await save($, state, () => {state.data.requests[request.request_id] = request;});
        await event($, state, request, 'dropped');
      }
      return;
    }
    const bytes = new TextEncoder().encode(claim.envelope);
    const digest = await crypto.subtle.digest('SHA-256', bytes);
    request.envelope_digest = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2,'0')).join('');
    state.flight = request;
    state.submitting = true;
    await save($, state, () => {state.data.requests[request.request_id] = request;});
    // Detached timer path, never awaited inside a working turn hook or the heartbeat loop.
    submit($, state, request, claim.envelope, epoch).catch(() => {});
  } catch {
    state.error = 'WATCH connection interrupted; existing attempts are never resubmitted.';
    // Do not repeat an ambiguous claim HTTP call: reconnect establishes a fresh generation.
    stop(state);
  } finally { state.polling = false; }
}

async function activate($, state, response) {
  if (!sessionResponse(response, state.data.native_id)) throw new Error('Invalid WATCH connection identity.');
  if (await $.session.id() !== state.data.native_id || await $.session.cwd() !== state.data.project) throw new Error('Native conversation changed.');
  state.generation = response.generation;
  await save($, state, () => {state.data.active = true; state.data.generation = response.generation;});
  state.connected = true;
  state.error = '';
  // Replay retained completions; unfinished attempts become unknown, never a new prompt.
  for (const request of Object.values(state.data.requests)) {
    if (!['answered','dropped'].includes(request.phase)) await event($, state, request, 'unknown');
  }
  await flush($, state);
  state.timer = $.clock.every(2000, () => tick($, state));
}

export function register(on) {
  const state = {data:null, generation:null, connected:false, epoch:0, timer:null, writes:Promise.resolve(),
    polling:false, flushing:false, submitting:false, flight:null, envelope:null, error:'', connecting:false};
  on('session.start', async ($, e, next) => {
    try {
      const id = await $.session.id();
      const stored = UUID.test(id) ? await $.store.get(keyFor(id)) : null;
      if (stored?.native_id === id && stored.active && stored.project === await $.session.cwd()) {
        state.data = stored;
        await activate($, state, await post($, state, 'connect', {native_id:id, project:stored.project}));
      }
    } catch {stop(state); state.error = 'WATCH reconnect failed; use /watch-connect to reconnect.';}
    try {
      await $.command.register({name:'watch-connect', description:'Enroll this exact chat with WATCH, or reconnect its saved enrollment.', argumentHint:'[port:pair-code]', immediate:true});
      await $.command.register({name:'watch-status', description:'Show WATCH connection status without secrets.', immediate:true});
      await $.command.register({name:'watch-disconnect', description:'Disconnect WATCH; does not cancel native queued input.', immediate:true});
    } catch {state.error = 'WATCH command registration failed.';}
    return next(e);
  });
  on('command.run', async ($, e, next) => {
    if (TRANSITIONS.has(e.command)) {
      try {await disconnect($, state);} catch {stop(state);}
      return next(e);
    }
    if (e.command === 'watch-status') return {text:JSON.stringify(publicStatus(state))};
    if (!['watch-connect','watch-disconnect'].includes(e.command)) return next(e);
    if (!['composer','bridge'].includes(e.origin?.kind)) return {text:'WATCH connection commands require direct owner input.'};
    if (e.command === 'watch-disconnect') {await disconnect($, state); return {text:'WATCH disconnected. ' + LIMITATION};}
    if (state.connecting || state.submitting || (state.connected && state.flight &&
        ['pending','claimed','submitted','started'].includes(state.flight.phase))) return {text:'An existing WATCH attempt is unresolved; nothing was resubmitted.'};
    state.connecting = true;
    try {
      const id = await $.session.id(), project = await $.session.cwd();
      if (!UUID.test(id)) throw new Error('Native conversation identity unavailable.');
      stop(state);
      state.flight = null; state.envelope = null;
      const pair = e.args.trim() ? pairing(e.args) : null;
      if (e.args.trim() && !pair) return {text:'Use the exact port:pair-code command from WATCH.'};
      if (pair) {
        state.data = {native_id:id, project, port:pair.port, requests:{}, active:false};
        const response = await post($, state, 'enroll', {code:pair.code,native_id:id,project}, false);
        if (typeof response.token !== 'string' || response.token.length < 16 || !UUID.test(response.enrollment_id)) throw new Error('Invalid enrollment.');
        state.data.token = response.token; state.data.enrollment_id = response.enrollment_id;
        await activate($, state, response);
      } else {
        state.data = await $.store.get(keyFor(id));
        if (!state.data || state.data.native_id !== id || state.data.project !== project) return {text:'Use the pairing command from WATCH for this exact chat.'};
        await activate($, state, await post($, state, 'connect', {native_id:id,project}));
      }
      return {text:'WATCH connected to this existing chat. ' + LIMITATION};
    } catch {stop(state); return {text:'WATCH connection failed; get a fresh pairing command or reconnect. No request was resubmitted.'};}
    finally {state.connecting = false;}
  }).catch(($, e, next) => {
    stop(state);
    if (['watch-connect','watch-disconnect','watch-status'].includes(e.command)) return {text:'WATCH command failed; no automatic request retry.'};
    return next(e);
  });
  on('turn.start', async ($, e, next) => {
    try {
      const request = state.flight;
      if (request && !request.turn_id && e.agentId === undefined && matchingPrompt(e.text, state.envelope) &&
          await $.session.id() === state.data.native_id) {
        request.turn_id = e.turnId;
        await event($, state, request, 'started', {turn_id:e.turnId});
      }
    } catch { /* WATCH failure must not block native turn admission. */ }
    return next(e);
  });
  on('turn.complete', async ($, e, next) => {
    try {
      const request = state.flight;
      if (request && e.agentId === undefined && e.turnId === request.turn_id &&
          await $.session.id() === state.data.native_id) {
        if (!e.isAborted && e.reason === 'answer' && matchingAnswer(e.answer, request.request_id)) {
          await event($, state, request, 'answered', {turn_id:e.turnId,answer:e.answer});
        } else await event($, state, request, 'unknown');
        if (!state.submitting) {state.flight = null; state.envelope = null;}
      }
    } catch { /* Retained completion is replayable; never block native completion. */ }
    return next(e);
  });
  on('session.end', async ($, e, next) => {
    try {await disconnect($, state);} catch {stop(state);}
    return next(e);
  });
}
