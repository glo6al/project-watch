import {UUID, LIMITATION, TRANSITIONS, pairing, sessionResponse, validClaim, matchingPrompt, matchingAnswer, publicStatus, globalConfiguration, receiptRecord, surfaceRoster} from './protocol.js';

const GLOBAL_KEY = 'watch-owner-global-v1';
const legacyKey = id => 'watch-owner-v1:' + id;
const preferenceKey = id => 'watch-owner-preferences-v1:' + id;
const shardPrefix = id => 'watch-owner-runtime-v2:' + id + ':';
const MAX_DATA = 4 * 1024 * 1024;
const copy = value => JSON.parse(JSON.stringify(value));
const configurationId = value => value ? JSON.stringify([value.grant_id,value.grant,value.port]) : '';

function trimHistory(state, data) {
  let excess = Object.keys(data.requests).length - 32;
  for (const request of Object.values(data.requests)) {
    if (excess <= 0) break;
    if (request !== state.flight && !request.events.length && ['answered','dropped','unknown'].includes(request.phase)) {
      delete data.requests[request.request_id]; excess -= 1;
    }
  }
}

// Keep every official API call in a top-level $ function: the native compiler scans them.
async function save($, state, change) {
  const data = state.data, key = state.key;
  if (!data || !key) throw Error('Missing WATCH receipt context.');
  const operation = state.writes.then(async () => {
    if (change) change(data);
    trimHistory(state, data);
    if (JSON.stringify(data).length > MAX_DATA) throw Error('WATCH recovery data limit.');
    await $.store.set(key, copy(data));
  });
  state.writes = operation.catch(() => {});
  return operation;
}

async function post($, state, path, body, options = {}) {
  const port = options.port ?? state.data?.port;
  const token = options.anonymous ? null : (options.token ?? state.data?.token);
  const headers = {'Content-Type':'application/json'};
  if (token) headers.Authorization = 'Bearer ' + token;
  // Serialize network requests, including detached event/disconnect calls. The official
  // host bounds one fetch (redirects and body included) to 30 seconds.
  const operation = state.network.then(async () => {
    state.networkBusy = true;
    try {
      const response = await $.http.fetch('http://127.0.0.1:' + port + '/claude-mod/' + path,
        {method:'POST',headers,body:JSON.stringify(body)});
      if (typeof response.text !== 'string' || response.text.length > 100000) throw Error('Invalid WATCH response.');
      const value = JSON.parse(response.text);
      if (!response.ok) {
        const error = Error('WATCH service unavailable.');
        error.code = typeof value?.error_code === 'string' ? value.error_code : '';
        error.status = response.status;
        throw error;
      }
      return value;
    } finally {state.networkBusy = false;}
  });
  state.network = operation.catch(() => {});
  return operation;
}

function identityBody(state, data = state.data, generation = state.generation) {
  return {generation,native_id:data.native_id,...data.mode === 'derived' ? {client_id:state.clientId} : {}};
}

function stop(state) {
  if (state.connected) state.sequence += 1;
  state.connected = false;
  state.peerProtocol = 0;
  state.epoch += 1;
}

function backoff(state, now) {
  state.failures += 1;
  state.retryAt = now + Math.min(30000, 2000 * 2 ** Math.min(state.failures - 1,4));
}

function suspend($, state) {
  const wasConnected = state.connected;
  const data = state.data;
  const body = data ? identityBody(state) : null;
  stop(state);
  if (wasConnected && data) {
    post($, state, 'disconnect', body, {port:data.port,token:data.token}).catch(() => {});
  }
}

async function fingerprint(value) {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify(value)));
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2,'0')).join('');
}

async function event($, state, request, kind, extras = {}) {
  const data = state.data;
  await save($, state, () => {
    if (request.completed && kind !== 'answered') return;
    if (kind === 'submitted' && ['started','answered','unknown','dropped'].includes(request.phase)) return;
    if (kind === 'answered') request.completed = true;
    request.phase = kind;
    const value = {request_id:request.request_id,attempt_id:request.attempt_id,kind,...extras};
    if (!request.events.some(item => JSON.stringify(item) === JSON.stringify(value))) request.events.push(value);
  });
  if (state.data === data) flush($, state).catch(() => {state.error = 'WATCH event delivery interrupted; retained receipts will be retried without resubmitting.';});
}

async function flush($, state) {
  if (state.flushing || !state.connected) return;
  const data = state.data, epoch = state.epoch;
  state.flushing = true;
  try {
    for (const request of Object.values(data.requests)) {
      while (request.events.length && state.connected && state.data === data && state.epoch === epoch) {
        const value = request.events[0];
        await post($, state, 'event', {...value,...identityBody(state)});
        if (state.data !== data || state.epoch !== epoch) return;
        const hash = data.mode === 'derived' ? await fingerprint(value) : null;
        await save($, state, () => {
          if (hash) data.acknowledged[hash] = true;
          if (request.events[0] === value) request.events.shift();
        });
      }
    }
  } finally {state.flushing = false;}
}

async function recover($, state) {
  const data = state.data;
  if (data.mode !== 'derived') return;
  const keys = await $.store.keys();
  if (!Array.isArray(keys) || keys.length > 4096) throw Error('WATCH recovery data limit.');
  let total = JSON.stringify(data).length;
  const shards = [];
  for (const key of keys) {
    if (typeof key !== 'string' || key === state.key || !key.startsWith(shardPrefix(data.native_id))) continue;
    if (!UUID.test(key.slice(shardPrefix(data.native_id).length))) throw Error('Invalid WATCH receipt namespace.');
    const shard = await $.store.get(key);
    total += JSON.stringify(shard ?? null).length;
    if (total > MAX_DATA) throw Error('WATCH recovery data limit.');
    if (!shard || shard.mode !== 'derived' || shard.native_id !== data.native_id ||
        shard.project !== data.project || shard.grant_id !== data.grant_id || shard.enrollment_id !== data.enrollment_id) continue;
    if (!shard.requests || typeof shard.requests !== 'object' || Array.isArray(shard.requests)) throw Error('Invalid WATCH receipt data.');
    shards.push(shard);
  }
  // Validate before writing; foreign runtime keys are read-only, even after lease takeover.
  for (const shard of shards) for (const [id,record] of Object.entries(shard.requests)) {
    if (!receiptRecord(record,id)) throw Error('Invalid WATCH receipt data.');
    const current = data.requests[id];
    if (current && (current.attempt_id !== record.attempt_id || current.envelope_digest !== record.envelope_digest)) throw Error('Conflicting WATCH receipt identity.');
    if (current?.completed && record.completed) {
      const own = current.events.find(value => value.kind === 'answered');
      const incoming = record.events.find(value => value.kind === 'answered');
      if (own && incoming && JSON.stringify(own) !== JSON.stringify(incoming)) throw Error('Conflicting WATCH answer.');
    }
  }
  for (const shard of shards) for (const [id,record] of Object.entries(shard.requests)) {
    let current = data.requests[id];
    if (!current) {
      current = {...copy(record),events:[],phase:record.completed?'answered':'unknown'};
      data.requests[id] = current;
    }
    if (record.completed) {current.completed = true; current.phase = 'answered';}
    for (const value of record.events) {
      const hash = await fingerprint(value);
      if (data.acknowledged[hash] || current.events.some(item => JSON.stringify(item) === JSON.stringify(value))) continue;
      if (current.completed && value.kind !== 'answered') continue;
      current.events.push(copy(value));
    }
    if (!current.completed && !['dropped','unknown'].includes(record.phase)) {
      const value = {request_id:id,attempt_id:record.attempt_id,kind:'unknown'};
      const hash = await fingerprint(value);
      if (!data.acknowledged[hash] && !current.events.some(item => item.kind === 'unknown')) current.events.push(value);
    }
  }
  await save($, state);
}

async function failRequest($, state, request) {
  try {await event($,state,request,'unknown');} catch { /* Server claim remains attempted. */ }
}

async function submit($, state, request, envelope, epoch) {
  try {
    const id = await $.session.id(), now = await $.clock.now();
    if (!state.connected || state.epoch !== epoch || id !== state.data.native_id || await $.session.cwd() !== state.data.project) {await failRequest($,state,request); return;}
    if (now / 1000 >= request.expires_at) {await event($,state,request,'dropped'); return;}
    await save($,state,()=>{request.attempted = true; request.phase = 'pending';});
    if (!state.connected || state.epoch !== epoch || await $.session.id() !== state.data.native_id || await $.session.cwd() !== state.data.project) {await failRequest($,state,request); return;}
    state.envelope = envelope;
    const result = await $.prompt.submit({text:envelope,asUser:false});
    if (typeof result?.drop === 'string') await event($,state,request,'dropped');
    else await event($,state,request,'submitted');
  } catch {await failRequest($,state,request);}
  finally {
    state.submitting = false;
    if (['answered','dropped','unknown'].includes(request.phase)) {state.flight = null; state.envelope = null;}
  }
}

async function poll($, state) {
  const epoch = state.epoch;
  const response = await post($,state,'poll',{...identityBody(state),project:state.data.project,accept:!state.flight && !state.submitting});
  if (!sessionResponse({...response,heartbeat_interval_ms:2000,lease_seconds:12},state.data.native_id) || response.generation !== state.generation) throw Error('Invalid WATCH poll identity.');
  if (!state.connected || state.epoch !== epoch || !response.claim) return;
  const claim = response.claim;
  if (state.data.requests[claim.request_id]) {await failRequest($,state,state.data.requests[claim.request_id]); return;}
  const request = {request_id:claim.request_id,attempt_id:claim.attempt_id,expires_at:claim.expires_at,
    generation:state.generation,attempted:false,phase:'claimed',events:[]};
  if (!validClaim(claim,{...state.data,generation:state.generation,peer_protocol:state.peerProtocol},await $.clock.now())) {
    if (UUID.test(claim.request_id) && UUID.test(claim.attempt_id)) {
      request.envelope_digest = await fingerprint(claim.envelope ?? '');
      await save($,state,data=>{data.requests[request.request_id] = request;});
      await event($,state,request,'dropped');
    }
    return;
  }
  request.envelope_digest = await fingerprint(claim.envelope);
  state.flight = request;
  await save($,state,data=>{data.requests[request.request_id] = request;});
  state.submitting = true;
  submit($,state,request,claim.envelope,epoch).catch(()=>{});
}

async function activate($, state, response) {
  if (!sessionResponse(response,state.data.native_id)) throw Error('Invalid WATCH connection identity.');
  if (await $.session.id() !== state.data.native_id || await $.session.cwd() !== state.data.project) throw Error('Native conversation changed.');
  state.generation = response.generation;
  // Only this current derived connection can negotiate peer input. Saved receipts and
  // manual owner enrollments cannot enable it after reload or through a service echo.
  state.peerProtocol = state.data.mode === 'derived' && response.peer_protocol === 1 ? 1 : 0;
  await save($,state,data=>{data.active = true; data.generation = response.generation; data.peer_protocol = state.peerProtocol;});
  state.connected = true; state.error = ''; state.failures = 0; state.retryAt = 0;
  if (state.data.mode === 'derived') await recover($,state);
  for (const request of Object.values(state.data.requests)) {
    if (!['answered','dropped','unknown'].includes(request.phase)) await event($,state,request,'unknown');
  }
  if (!state.submitting && state.flight && ['unknown','dropped','answered'].includes(state.flight.phase)) {state.flight = null; state.envelope = null;}
  state.recoverAt = (await $.clock.now()) + 10000;
  await flush($,state);
}

async function connectDerived($, state, id, project, config) {
  state.config = config;
  const response = await post($,state,'register',{native_id:id,project,client_id:state.clientId,connection_seq:state.sequence,peer_protocol:1},
    {port:config.port,token:config.grant});
  if (!sessionResponse(response,id) || response.grant_id !== config.grant_id || !UUID.test(response.enrollment_id) ||
      !/^[a-f0-9]{64}$/.test(response.token) || response.client_id !== state.clientId || response.connection_seq !== state.sequence) throw Error('Invalid WATCH registration.');
  if (await $.session.id() !== id || await $.session.cwd() !== project) throw Error('Native conversation changed.');
  const same = state.data?.mode === 'derived' && state.data.native_id === id && state.data.project === project && state.data.enrollment_id === response.enrollment_id;
  if (!same) {
    await state.writes;
    const key = shardPrefix(id) + state.clientId;
    // Returning to A after visiting B reuses this runtime's A namespace. Read
    // its retained receipts before any activation write can replace that row.
    const stored = await $.store.get(key);
    let requests = {}, acknowledged = {};
    if (stored?.mode === 'derived' && stored.native_id === id && stored.project === project &&
        stored.grant_id === config.grant_id && stored.enrollment_id === response.enrollment_id) {
      if (JSON.stringify(stored).length > MAX_DATA || !stored.requests || typeof stored.requests !== 'object' || Array.isArray(stored.requests) ||
          !stored.acknowledged || typeof stored.acknowledged !== 'object' || Array.isArray(stored.acknowledged) ||
          !Object.entries(stored.requests).every(([requestId,record])=>receiptRecord(record,requestId)) ||
          !Object.entries(stored.acknowledged).every(([hash,value])=>/^[a-f0-9]{64}$/.test(hash) && value === true)) throw Error('Invalid WATCH receipt data.');
      requests = copy(stored.requests); acknowledged = copy(stored.acknowledged);
    }
    state.key = key;
    state.data = {version:2,mode:'derived',native_id:id,project,port:config.port,grant_id:config.grant_id,
      enrollment_id:response.enrollment_id,token:response.token,requests,acknowledged,active:true};
    // Persisted receipts are recovery evidence, never a restored native flight.
    state.flight = null; state.envelope = null;
  }
  state.config = config;
  await activate($,state,response);
}

async function readSurfaces($, state) {
  try {state.currentSurfaces = surfaceRoster(await $.session.surfaces());}
  catch {state.currentSurfaces = null;}
  return state.currentSurfaces;
}

async function supervisor($, state) {
  if (state.supervising || state.connecting || state.networkBusy || state.flushing) return;
  state.supervising = true;
  let now = 0;
  try {
    // SDK-hosted Desktop is not a REPL. The current public roster also covers
    // a Desktop frontend attached after a null startup surface; headless SDK stays off.
    if (!state.interactive && !(await readSurfaces($,state))?.includes('desktop')) return;
    now = await $.clock.now();
    const id = await $.session.id(), project = await $.session.cwd();
    const raw = await $.store.get(GLOBAL_KEY);
    const config = globalConfiguration(raw) ? raw : null;
    const preference = UUID.test(id) ? await $.store.get(preferenceKey(id)) : null;
    const changed = state.data && (id !== state.data.native_id || project !== state.data.project ||
      (state.data.mode === 'derived' && configurationId(config) !== configurationId(state.config)));
    if (changed || preference?.disconnected === true || !UUID.test(id)) {
      suspend($,state);
      if (state.submitting) return;
      if (state.flight) await failRequest($,state,state.flight);
      state.flight = null; state.envelope = null;
      state.data = null; state.key = null;
      state.retryAt = 0; state.failures = 0;
      if (preference?.disconnected === true || !UUID.test(id)) return;
    }
    if (now < state.retryAt) return;
    if (!state.connected) {
      if (state.submitting) return;
      if (!state.data) {
        const manual = await $.store.get(legacyKey(id));
        if (manual?.native_id === id && manual.project === project && manual.active) {state.data = manual; state.key = legacyKey(id);}
      }
      if (state.data?.mode === 'derived' || (!state.data && config)) {
        if (!config || state.rejectedGrant === configurationId(config) || state.rejectedIdentity === JSON.stringify([id,project,configurationId(config)])) return;
        await connectDerived($,state,id,project,config);
      } else {
        const stored = state.data ?? await $.store.get(legacyKey(id));
        if (!stored || stored.native_id !== id || stored.project !== project || !stored.active) return;
        state.key = legacyKey(id); state.data = stored;
        await activate($,state,await post($,state,'connect',{native_id:id,project}));
      }
      return;
    }
    if (state.data.mode === 'derived' && now >= state.recoverAt) {
      await recover($,state); state.recoverAt = now + 10000;
    }
    await flush($,state);
    await poll($,state);
  } catch (error) {
    stop(state);
    if (error.status === 403 && state.config) state.rejectedGrant = configurationId(state.config);
    if (error.code === 'identity_rejected') {
      const id = await $.session.id().catch(()=>null), project = await $.session.cwd().catch(()=>null);
      state.rejectedIdentity = JSON.stringify([id,project,configurationId(state.config)]);
    }
    state.error = error.message === 'WATCH recovery data limit.' ? 'WATCH recovery data limit reached; automatic claims stopped.' :
      'WATCH connection interrupted; existing attempts are never resubmitted.';
    backoff(state,now);
  } finally {state.supervising = false;}
}

async function explicitDisconnect($, state) {
  const id = await $.session.id();
  if (UUID.test(id)) await $.store.set(preferenceKey(id),{version:1,disconnected:true});
  const data = state.data;
  suspend($,state);
  if (data && data.mode !== 'derived') await save($,state,()=>{data.active = false;});
}

export function register(on) {
  const state = {clientId:crypto.randomUUID(),sequence:0,data:null,key:null,config:null,generation:null,connected:false,epoch:0,
    timer:null,peerProtocol:0,writes:Promise.resolve(),network:Promise.resolve(),networkBusy:false,flushing:false,supervising:false,
    submitting:false,flight:null,envelope:null,error:'',connecting:false,interactive:false,failures:0,retryAt:0,recoverAt:0,
    rejectedGrant:'',rejectedIdentity:'',startSeen:false,startupInteractive:null,startupSurface:null,timerTicks:0,currentSurfaces:null};
  on('session.start',async($,e,next)=>{
    state.interactive = e.isInteractive === true;
    state.startSeen = true;
    state.startupInteractive = typeof e.isInteractive === 'boolean' ? e.isInteractive : null;
    state.startupSurface = surfaceRoster([e.surface])?.[0] ?? null;
    state.currentSurfaces = null;
    state.timerTicks = 0;
    try {
      state.timer?.cancel();
      state.timer = null;
      await $.command.register({name:'watch-setup',description:'Authorize automatic WATCH connections once for your local Claude chats.',argumentHint:'<port:setup-code>',immediate:true});
      await $.command.register({name:'watch-connect',description:'Reconnect this chat, or use a legacy WATCH pairing code.',argumentHint:'[port:pair-code]',immediate:true});
      await $.command.register({name:'watch-status',description:'Show WATCH connection status without secrets.',immediate:true});
      await $.command.register({name:'watch-disconnect',description:'Keep this chat disconnected until explicit reconnect.',immediate:true});
      state.timer = $.clock.every(2000,()=>{
        // Count actual dispatch before any supervisor gate, never imply installation is dispatch.
        state.timerTicks = Math.min(Number.MAX_SAFE_INTEGER,state.timerTicks + 1);
        return supervisor($,state);
      });
    } catch {state.error = 'WATCH command registration failed.';}
    return next(e);
  });
  on('command.run',async($,e,next)=>{
    if (TRANSITIONS.has(e.command)) {suspend($,state); return next(e);}
    if (e.command === 'watch-status') {await readSurfaces($,state); return {text:JSON.stringify(publicStatus(state))};}
    if (!['watch-connect','watch-disconnect','watch-setup'].includes(e.command)) return next(e);
    if (!['composer','bridge'].includes(e.origin?.kind)) return {text:'WATCH connection commands require direct owner input.'};
    if (e.command === 'watch-disconnect') {
      try {await explicitDisconnect($,state); return {text:'WATCH disconnected. ' + LIMITATION};}
      catch {return {text:'WATCH disconnect could not be saved; readiness expires when its heartbeat stops.'};}
    }
    if (state.connecting || state.submitting || (state.connected && state.flight && ['pending','claimed','submitted','started'].includes(state.flight.phase))) return {text:'An existing WATCH attempt is unresolved; nothing was resubmitted.'};
    state.connecting = true;
    try {
      const id = await $.session.id(), project = await $.session.cwd();
      if (!UUID.test(id)) throw Error('Native identity unavailable.');
      const pair = e.args.trim() ? pairing(e.args) : null;
      if (e.args.trim() && !pair) return {text:'Use the exact port:code command from WATCH.'};
      if (e.command === 'watch-setup') {
        if (!pair) return {text:'Use the exact /watch-setup command from WATCH.'};
        const config = await post($,state,'setup',{code:pair.code},{port:pair.port,anonymous:true});
        if (!globalConfiguration(config) || config.port !== pair.port) throw Error('Invalid WATCH setup response.');
        await $.store.set(GLOBAL_KEY,config);
        state.rejectedGrant = ''; state.rejectedIdentity = '';
        await $.store.set(preferenceKey(id),{version:1,disconnected:false});
        suspend($,state);
        state.data = null; state.key = null; state.config = config;
        state.retryAt = 0; state.failures = 0;
        return {text:'WATCH automatic connections authorized. Loaded eligible chats will connect automatically. ' + LIMITATION};
      }
      await $.store.set(preferenceKey(id),{version:1,disconnected:false});
      state.rejectedIdentity = ''; state.retryAt = 0; state.failures = 0;
      stop(state);
      if (pair) {
        state.key = legacyKey(id);
        state.data = {native_id:id,project,port:pair.port,requests:{},active:false};
        const response = await post($,state,'enroll',{code:pair.code,native_id:id,project},{anonymous:true});
        if (typeof response.token !== 'string' || response.token.length < 16 || !UUID.test(response.enrollment_id)) throw Error('Invalid enrollment.');
        state.data.token = response.token; state.data.enrollment_id = response.enrollment_id;
        await activate($,state,response);
        return {text:'WATCH connected to this existing chat. ' + LIMITATION};
      }
      if (state.data?.mode !== 'derived') {
        state.data = await $.store.get(legacyKey(id)); state.key = state.data ? legacyKey(id) : null;
        if (state.data?.native_id === id && state.data.project === project) {
          state.data.active = true;
          await activate($,state,await post($,state,'connect',{native_id:id,project}));
          return {text:'WATCH connected to this existing chat. ' + LIMITATION};
        }
        state.data = null; state.key = null;
      }
      return {text:'WATCH reconnect scheduled for this existing chat. ' + LIMITATION};
    } catch {stop(state); return {text:'WATCH connection failed. If setup was interrupted, get a fresh setup command. No request was resubmitted.'};}
    finally {state.connecting = false;}
  }).catch(($,e,next)=>{stop(state); return ['watch-connect','watch-disconnect','watch-status','watch-setup'].includes(e.command)?{text:'WATCH command failed; no automatic request retry.'}:next(e);});
  on('turn.start',async($,e,next)=>{
    try {
      const request = state.flight;
      if (request && !request.turn_id && e.agentId === undefined && matchingPrompt(e.text,state.envelope) && await $.session.id() === state.data.native_id) {
        request.turn_id = e.turnId;
        await event($,state,request,'started',{turn_id:e.turnId});
      }
    } catch { /* WATCH cannot block native admission. */ }
    return next(e);
  });
  on('turn.complete',async($,e,next)=>{
    try {
      const request = state.flight;
      if (request && e.agentId === undefined && e.turnId === request.turn_id && await $.session.id() === state.data.native_id) {
        if (!e.isAborted && e.reason === 'answer' && matchingAnswer(e.answer,request.request_id)) await event($,state,request,'answered',{turn_id:e.turnId,answer:e.answer});
        else await event($,state,request,'unknown');
        if (!state.submitting) {state.flight = null; state.envelope = null;}
      }
    } catch { /* Retained completion can replay without a new prompt. */ }
    return next(e);
  });
  on('session.end',async($,e,next)=>{suspend($,state); return next(e);});
}
