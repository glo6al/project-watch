import {UUID, LIMITATION, TRANSITIONS, pairing, sessionResponse, validClaim, matchingPrompt, matchingAnswer, publicStatus, globalConfiguration, receiptRecord, surfaceRoster, validNoticeClaim, noticeReceipt, lifecycleReceipt} from './protocol.js';

const GLOBAL_KEY = 'watch-owner-global-v1';
const legacyKey = id => 'watch-owner-v1:' + id;
const preferenceKey = id => 'watch-owner-preferences-v1:' + id;
const legacyShardPrefix = id => 'watch-owner-runtime-v2:' + id + ':';
const shardPrefix = id => 'watch-owner-runtime-v3:' + id + ':';
const noticePrefix = id => 'watch-reply-notice-runtime-v1:' + id + ':';
const MAX_DATA = 4 * 1024 * 1024;
const copy = value => JSON.parse(JSON.stringify(value));
const configurationId = value => value ? JSON.stringify([value.grant_id,value.grant,value.port]) : '';

function trimHistory(state, data) {
  let excess = Object.keys(data.requests).length - 32;
  for (const request of Object.values(data.requests)) {
    if (excess <= 0) break;
    if (request !== state.flight && !request.events.length && (request.execution_completed || ['answered','dropped'].includes(request.phase))) {
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
      if (typeof response.text !== 'string' || response.text.length > 650000) throw Error('Invalid WATCH response.');
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
  state.connected = false;
  state.peerProtocol = 0;
  state.noticeProtocol = 0;
  state.lifecycleProtocol = 0;
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

async function event($, state, request, kind, extras = {}, reason = '') {
  const data = state.data, at = await $.clock.now();
  if (state.data !== data || data.requests[request.request_id] !== request) throw Error('WATCH receipt identity changed.');
  await save($, state, () => {
    if (request.execution_completed || (request.completed && kind !== 'answered')) return;
    if (kind === 'submitted' && ['started','answered','unknown','dropped'].includes(request.phase)) return;
    if (kind === 'answered') {request.completed = true; request.execution_completed = true; request.capture = 'answered';}
    if (kind === 'dropped') request.execution_completed = true;
    if (kind === 'completed') {
      request.execution_completed = true; request.capture = extras.outcome;
      request.completed = extras.outcome === 'answered';
    }
    request.phase = kind;
    request.journal = [...(request.journal || []),{at,kind,reason:kind==='completed'?extras.outcome:reason}].slice(-16);
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
        if (value.kind === 'completed' && state.lifecycleProtocol !== 2) throw Error('WATCH lifecycle recovery requires a compatible bridge.');
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
    if (typeof key !== 'string' || key === state.key) continue;
    const prefix = [shardPrefix(data.native_id),legacyShardPrefix(data.native_id)].find(prefix=>key.startsWith(prefix));
    if (!prefix) continue;
    if (!UUID.test(key.slice(prefix.length))) throw Error('Invalid WATCH receipt namespace.');
    const shard = await $.store.get(key);
    total += JSON.stringify(shard ?? null).length;
    if (total > MAX_DATA) throw Error('WATCH recovery data limit.');
    if (!shard || shard.mode !== 'derived' || shard.native_id !== data.native_id ||
        shard.project !== data.project || shard.grant_id !== data.grant_id || shard.enrollment_id !== data.enrollment_id) continue;
    if (!shard.requests || typeof shard.requests !== 'object' || Array.isArray(shard.requests)) throw Error('Invalid WATCH receipt data.');
    shards.push({...shard, legacy:prefix===legacyShardPrefix(data.native_id)});
  }
  // Validate before writing; foreign runtime keys are read-only, even after lease takeover.
  for (const shard of shards) for (const [id,record] of Object.entries(shard.requests)) {
    if (!(shard.legacy?receiptRecord(record,id):lifecycleReceipt(record,id))) throw Error('Invalid WATCH receipt data.');
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
      current = {...copy(record),events:[],phase:record.execution_completed?'completed':record.completed?'answered':'unknown'};
      data.requests[id] = current;
    }
    if (record.execution_completed && !current.execution_completed) {current.execution_completed=true; current.capture=record.capture; current.phase='completed';}
    if (record.completed && current.capture!=='withheld') {current.completed = true; current.phase = record.lifecycle_protocol===2?'completed':'answered';}
    for (const value of record.events) {
      const hash = await fingerprint(value);
      if (data.acknowledged[hash] || current.events.some(item => JSON.stringify(item) === JSON.stringify(value))) continue;
      if ((current.execution_completed || current.completed) && !['answered','completed'].includes(value.kind)) continue;
      current.events.push(copy(value));
    }
    if (!current.execution_completed && !current.completed && current !== state.flight && !['dropped','unknown'].includes(record.phase)) {
      const value = {request_id:id,attempt_id:record.attempt_id,kind:'unknown'};
      const hash = await fingerprint(value);
      if (!data.acknowledged[hash] && !current.events.some(item => item.kind === 'unknown')) current.events.push(value);
    }
  }
  await save($, state);
}

async function saveNotices($,state,change) {
  const data=state.noticeData,key=state.noticeKey;
  if (!data || !key) throw Error('Missing WATCH notice context.');
  const operation=state.writes.then(async()=>{
    if(change)change(data);
    for(const [id,r] of Object.entries(data.requests))if(Object.keys(data.requests).length>32 && !r.events.length && r!==state.flight && ['submitted','dropped','unknown'].includes(r.phase))delete data.requests[id];
    if(JSON.stringify(data).length>MAX_DATA)throw Error('WATCH recovery data limit.');
    await $.store.set(key,copy(data));
  });
  state.writes=operation.catch(()=>{});return operation;
}
async function noticeEvent($,state,request,kind) {
  await saveNotices($,state,()=>{
    if(['submitted','dropped','unknown'].includes(request.phase))return;
    request.phase=kind;
    request.events.push({request_id:request.request_id,attempt_id:request.attempt_id,kind});
  });
  flushNotices($,state).catch(()=>{});
}
async function flushNotices($,state) {
  if(state.noticeFlushing || !state.connected || state.noticeProtocol!==1 || !state.noticeData)return;
  const data=state.noticeData,epoch=state.epoch;state.noticeFlushing=true;
  try {
    for(const request of Object.values(data.requests))while(request.events.length && state.connected && state.noticeProtocol===1 && state.noticeData===data && state.epoch===epoch){
      const value=request.events[0];await post($,state,'event',{...value,...identityBody(state)});
      if(state.noticeData!==data || state.epoch!==epoch || state.noticeProtocol!==1)return;
      const hash=await fingerprint(value);
      await saveNotices($,state,()=>{data.acknowledged[hash]=true;if(request.events[0]===value)request.events.shift();});
    }
  } finally {state.noticeFlushing=false;}
}
async function recoverNotices($,state) {
  if(state.noticeProtocol!==1)return;
  if(state.noticeData && (state.submitting || state.flight?.kind==='peer_reply_notice' || state.noticeFlushing))return;
  const ordinary=state.data,prefix=noticePrefix(ordinary.native_id),key=prefix+state.clientId;
  const keys=await $.store.keys();if(!Array.isArray(keys)||keys.length>4096)throw Error('WATCH recovery data limit.');
  let total=0;const records={},acknowledged={};
  for(const k of keys){
    if(typeof k!=='string'||!k.startsWith(prefix))continue;
    if(!UUID.test(k.slice(prefix.length)))throw Error('Invalid WATCH notice namespace.');
    const shard=await $.store.get(k);total+=JSON.stringify(shard??null).length;
    if(total>MAX_DATA)throw Error('WATCH recovery data limit.');
    if(shard?.kind!=='peer_reply_notice'||shard.native_id!==ordinary.native_id||shard.project!==ordinary.project||shard.grant_id!==ordinary.grant_id||shard.enrollment_id!==ordinary.enrollment_id)continue;
    if(!shard.requests||!shard.acknowledged||typeof shard.requests!=='object'||Array.isArray(shard.requests)||typeof shard.acknowledged!=='object'||Array.isArray(shard.acknowledged))throw Error('Invalid WATCH notice receipts.');
    for(const [hash,v] of Object.entries(shard.acknowledged)){if(!/^[a-f0-9]{64}$/.test(hash)||v!==true)throw Error('Invalid WATCH notice ACK.');acknowledged[hash]=true;}
    for(const [id,r] of Object.entries(shard.requests)){
      if(!noticeReceipt(r,id))throw Error('Invalid WATCH notice receipt.');
      if(records[id] && (records[id].attempt_id!==r.attempt_id||records[id].envelope_digest!==r.envelope_digest||records[id].public_request_id!==r.public_request_id))throw Error('Conflicting WATCH notice receipt.');
      if(!records[id])records[id]=copy(r);
      else for(const e of r.events)if(!records[id].events.some(v=>JSON.stringify(v)===JSON.stringify(e)))records[id].events.push(copy(e));
    }
  }
  for(const r of Object.values(records)){
    const terminal=r.events.find(e=>['submitted','dropped','unknown'].includes(e.kind));
    if(terminal)r.phase=terminal.kind;
    if(!['submitted','dropped','unknown'].includes(r.phase)){
      r.phase='unknown';r.events.push({request_id:r.request_id,attempt_id:r.attempt_id,kind:'unknown'});
    }
    const pending=[];for(const e of r.events)if(!acknowledged[await fingerprint(e)])pending.push(e);r.events=pending;
  }
  state.noticeKey=key;state.noticeData={kind:'peer_reply_notice',native_id:ordinary.native_id,project:ordinary.project,grant_id:ordinary.grant_id,enrollment_id:ordinary.enrollment_id,requests:records,acknowledged};
  await saveNotices($,state);await flushNotices($,state);
}
async function submitNotice($,state,request,envelope,epoch) {
  try {
    if(!state.connected||state.noticeProtocol!==1||state.epoch!==epoch||await $.session.id()!==state.data.native_id||await $.session.cwd()!==state.data.project) {await noticeEvent($,state,request,'unknown');return;}
    if((await $.clock.now())/1000>=request.expires_at){await noticeEvent($,state,request,'dropped');return;}
    await saveNotices($,state,()=>{request.attempted=true;request.phase='pending';});
    if(!state.connected||state.noticeProtocol!==1||state.epoch!==epoch||await $.session.id()!==state.data.native_id||await $.session.cwd()!==state.data.project){await noticeEvent($,state,request,'unknown');return;}
    const result=await $.prompt.submit({text:envelope,asUser:false});
    await noticeEvent($,state,request,typeof result?.drop==='string'?'dropped':'submitted');
  }catch{try{await noticeEvent($,state,request,'unknown');}catch{}}
  finally{state.submitting=false;if(state.flight===request){state.flight=null;state.envelope=null;}}
}

async function failRequest($, state, request) {
  try {if(request.kind==='peer_reply_notice')await noticeEvent($,state,request,'unknown');else await event($,state,request,'unknown');} catch { /* Server claim remains attempted. */ }
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
    if (state.flight === request && (request.execution_completed || ['answered','dropped','unknown'].includes(request.phase))) {state.flight = null; state.envelope = null;}
  }
}

async function poll($, state) {
  const epoch = state.epoch;
  const response = await post($,state,'poll',{...identityBody(state),project:state.data.project,accept:!state.flight && !state.submitting});
  if (!sessionResponse({...response,heartbeat_interval_ms:2000,lease_seconds:12},state.data.native_id) || response.generation !== state.generation) throw Error('Invalid WATCH poll identity.');
  if (!state.connected || state.epoch !== epoch || !response.claim) return;
  const claim = response.claim;
  if(claim.kind!==undefined){
    if(!validNoticeClaim(claim,{...state.data,generation:state.generation,reply_notice_protocol:state.noticeProtocol},await $.clock.now())||!state.noticeData)return;
    if(state.noticeData.requests[claim.request_id])return;
    const request={kind:'peer_reply_notice',request_id:claim.request_id,public_request_id:claim.public_request_id,attempt_id:claim.attempt_id,expires_at:claim.expires_at,phase:'claimed',events:[],envelope_digest:await fingerprint(claim.envelope)};
    await saveNotices($,state,data=>{data.requests[request.request_id]=request;});
    state.flight=request;
    state.submitting=true;submitNotice($,state,request,claim.envelope,epoch).catch(()=>{});return;
  }
  if (state.data.requests[claim.request_id]) {await failRequest($,state,state.data.requests[claim.request_id]); return;}
  const request = {request_id:claim.request_id,attempt_id:claim.attempt_id,expires_at:claim.expires_at,lifecycle_protocol:state.lifecycleProtocol,origin:claim.origin??'owner',
    generation:state.generation,attempted:false,phase:'claimed',events:[]};
  if (!validClaim(claim,{...state.data,generation:state.generation,peer_protocol:state.peerProtocol,peer_envelope_limit:state.peerEnvelopeLimit},await $.clock.now())) {
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

async function activate($, state, response, eligibilityEpoch) {
  checkEligibility(state,eligibilityEpoch);
  if (!sessionResponse(response,state.data.native_id)) throw Error('Invalid WATCH connection identity.');
  if (await $.session.id() !== state.data.native_id || await $.session.cwd() !== state.data.project) throw Error('Native conversation changed.');
  checkEligibility(state,eligibilityEpoch);
  state.generation = response.generation;
  // Only this current derived connection can negotiate peer input. Saved receipts and
  // manual owner enrollments cannot enable it after reload or through a service echo.
  state.noticeProtocol = state.data.mode==='derived' && response.reply_notice_protocol===1 ? 1 : 0;
  state.noticeData=null;state.noticeKey=null;
  state.peerProtocol = state.data.mode === 'derived' && response.peer_protocol === 1 ? 1 : 0;
  state.peerEnvelopeLimit = state.data.mode === 'derived' && response.peer_envelope_limit === 100000 ? 100000 : 4000;
  state.lifecycleProtocol = state.data.mode === 'derived' && response.lifecycle_protocol === 2 ? 2 : 0;
  await save($,state,data=>{checkEligibility(state,eligibilityEpoch); data.active = true; data.generation = response.generation; data.peer_protocol = state.peerProtocol;});
  checkEligibility(state,eligibilityEpoch);
  state.connected = true; state.error = ''; state.failures = 0; state.retryAt = 0;
  if (state.data.mode === 'derived') await recover($,state);
  checkEligibility(state,eligibilityEpoch);
  for (const request of Object.values(state.data.requests)) {
    if (request !== state.flight && !request.execution_completed && !['answered','dropped','unknown'].includes(request.phase)) await event($,state,request,'unknown',{},'reconnect');
  }
  if (!state.submitting && state.flight && (state.flight.execution_completed || ['dropped','answered'].includes(state.flight.phase))) {state.flight = null; state.envelope = null;}
  state.recoverAt = (await $.clock.now()) + 10000;
  await flush($,state);
  if(state.noticeProtocol===1)await recoverNotices($,state);
}

async function connectDerived($, state, id, project, config, eligibilityEpoch) {
  state.config = config;
  async function registration(notices=false,lifecycle=false,largePeer=false){
    // Reserve before I/O, including while not connected: lost commits never reuse a tuple's episode.
    if(!Number.isSafeInteger(state.sequence+1))throw Error('WATCH connection sequence exhausted.');
    const sequence=++state.sequence;
    const body={native_id:id,project,client_id:state.clientId,connection_seq:sequence,peer_protocol:1,...notices?{reply_notice_protocol:1}:{},...lifecycle?{lifecycle_protocol:2}:{},...largePeer?{peer_envelope_limit:100000}:{}};
    const response=await post($,state,'register',body,{port:config.port,token:config.grant});
    checkEligibility(state,eligibilityEpoch);
    if(!sessionResponse(response,id)||response.grant_id!==config.grant_id||!UUID.test(response.enrollment_id)||
       !/^[a-f0-9]{64}$/.test(response.token)||response.client_id!==state.clientId||response.connection_seq!==sequence)throw Error('Invalid WATCH registration.');
    if(notices && response.reply_notice_protocol!==1)throw Error('Invalid WATCH notice negotiation.');
    if(lifecycle && response.lifecycle_protocol!==2)throw Error('Invalid WATCH lifecycle negotiation.');
    if(largePeer && response.peer_envelope_limit!==100000)throw Error('Invalid WATCH peer capacity negotiation.');
    return response;
  }
  let response=await registration();
  if(response.supported_reply_notice_protocol===1 || response.supported_lifecycle_protocol===2 || response.supported_peer_envelope_limit===100000)response=await registration(response.supported_reply_notice_protocol===1,response.supported_lifecycle_protocol===2,response.supported_peer_envelope_limit===100000);
  if (await $.session.id() !== id || await $.session.cwd() !== project) throw Error('Native conversation changed.');
  checkEligibility(state,eligibilityEpoch);
  const same = state.data?.mode === 'derived' && state.data.native_id === id && state.data.project === project && state.data.enrollment_id === response.enrollment_id;
  if (!same) {
    await state.writes;
    const key = shardPrefix(id) + state.clientId;
    // Returning to A after visiting B reuses this runtime's A namespace. Read
    // its retained receipts before any activation write can replace that row.
    const stored = await $.store.get(key);
    checkEligibility(state,eligibilityEpoch);
    let requests = {}, acknowledged = {};
    if (stored?.mode === 'derived' && stored.native_id === id && stored.project === project &&
        stored.grant_id === config.grant_id && stored.enrollment_id === response.enrollment_id) {
      if (JSON.stringify(stored).length > MAX_DATA || !stored.requests || typeof stored.requests !== 'object' || Array.isArray(stored.requests) ||
          !stored.acknowledged || typeof stored.acknowledged !== 'object' || Array.isArray(stored.acknowledged) ||
          !Object.entries(stored.requests).every(([requestId,record])=>lifecycleReceipt(record,requestId)) ||
          !Object.entries(stored.acknowledged).every(([hash,value])=>/^[a-f0-9]{64}$/.test(hash) && value === true)) throw Error('Invalid WATCH receipt data.');
      requests = copy(stored.requests); acknowledged = copy(stored.acknowledged);
    }
    state.key = key;
    state.data = {version:3,mode:'derived',native_id:id,project,port:config.port,grant_id:config.grant_id,
      enrollment_id:response.enrollment_id,token:response.token,requests,acknowledged,active:true};
    // Persisted receipts are recovery evidence, never a restored native flight.
    state.flight = null; state.envelope = null;
  }
  state.config = config;
  await activate($,state,response,eligibilityEpoch);
}

// Desktop evidence belongs only to this loaded runtime's exact native identity.
function invalidateEligibility(state) {
  state.desktopBinding = null;
  state.eligibilityEpoch += 1;
  return state.eligibilityEpoch;
}

function checkEligibility(state, expected) {
  if (expected !== undefined && state.eligibilityEpoch !== expected) throw Error('WATCH eligibility changed.');
}

async function readSurfaces($, state, eligibilityEpoch) {
  let surfaces = null;
  try {surfaces = surfaceRoster(await $.session.surfaces());} catch { /* Unknown stays fail-closed. */ }
  checkEligibility(state,eligibilityEpoch);
  state.currentSurfaces = surfaces;
  return surfaces;
}

async function supervisor($, state) {
  if (state.supervising || state.connecting || state.networkBusy || state.flushing || state.disconnecting) return;
  state.supervising = true;
  let now = 0, eligibilityEpoch = state.eligibilityEpoch;
  try {
    now = await $.clock.now();
    const id = await $.session.id(), project = await $.session.cwd();
    checkEligibility(state,eligibilityEpoch);
    if (state.desktopBinding && (state.desktopBinding.native_id !== id || state.desktopBinding.project !== project)) {
      // Refresh only at our synchronous invalidation, never after awaited cleanup.
      eligibilityEpoch = invalidateEligibility(state);
    }
    const raw = await $.store.get(GLOBAL_KEY);
    checkEligibility(state,eligibilityEpoch);
    const config = globalConfiguration(raw) ? raw : null;
    const preference = UUID.test(id) ? await $.store.get(preferenceKey(id)) : null;
    checkEligibility(state,eligibilityEpoch);
    const changed = state.data && (id !== state.data.native_id || project !== state.data.project ||
      (state.data.mode === 'derived' && configurationId(config) !== configurationId(state.config)));
    if (changed || preference?.disconnected === true || !UUID.test(id)) {
      if (preference?.disconnected === true || !UUID.test(id)) eligibilityEpoch = invalidateEligibility(state);
      suspend($,state);
      if (state.submitting) return;
      if (state.flight) await failRequest($,state,state.flight);
      checkEligibility(state,eligibilityEpoch);
      state.flight = null; state.envelope = null;
      state.data = null; state.key = null; state.bound.clear();
      state.retryAt = 0; state.failures = 0;
      if (preference?.disconnected === true || !UUID.test(id)) return;
    }
    const surfaces = await readSurfaces($,state,eligibilityEpoch);
    const currentId = await $.session.id(), currentProject = await $.session.cwd();
    checkEligibility(state,eligibilityEpoch);
    if (currentId !== id || currentProject !== project) {
      invalidateEligibility(state);
      suspend($,state);
      return;
    }
    if (surfaces === null) return;
    if (surfaces.includes('desktop') && UUID.test(id)) state.desktopBinding = {native_id:id,project};
    if (!state.interactive && !(state.desktopBinding?.native_id === id && state.desktopBinding.project === project)) return;
    if (now < state.retryAt) return;
    if (!state.connected) {
      if (state.submitting) return;
      if (!state.data) {
        const manual = await $.store.get(legacyKey(id));
        checkEligibility(state,eligibilityEpoch);
        if (manual?.native_id === id && manual.project === project && manual.active) {state.data = manual; state.key = legacyKey(id);}
      }
      if (state.data?.mode === 'derived' || (!state.data && config)) {
        if (!config || state.rejectedGrant === configurationId(config) || state.rejectedIdentity === JSON.stringify([id,project,configurationId(config)])) return;
        await connectDerived($,state,id,project,config,eligibilityEpoch);
      } else {
        const stored = state.data ?? await $.store.get(legacyKey(id));
        checkEligibility(state,eligibilityEpoch);
        if (!stored || stored.native_id !== id || stored.project !== project || !stored.active) return;
        state.key = legacyKey(id); state.data = stored;
        const response = await post($,state,'connect',{native_id:id,project});
        checkEligibility(state,eligibilityEpoch);
        await activate($,state,response,eligibilityEpoch);
      }
      return;
    }
    if (state.data.mode === 'derived' && now >= state.recoverAt) {
      await recover($,state);if(state.noticeProtocol===1)await recoverNotices($,state); state.recoverAt = now + 10000;
      checkEligibility(state,eligibilityEpoch);
    }
    await flush($,state);
    checkEligibility(state,eligibilityEpoch);
    await flushNotices($,state);
    await poll($,state);
  } catch (error) {
    if (state.eligibilityEpoch !== eligibilityEpoch) return;
    stop(state);
    if (error.status === 403 && state.config) state.rejectedGrant = configurationId(state.config);
    if (error.code === 'identity_rejected') {
      const id = await $.session.id().catch(()=>null), project = await $.session.cwd().catch(()=>null);
      if (state.eligibilityEpoch !== eligibilityEpoch) return;
      state.rejectedIdentity = JSON.stringify([id,project,configurationId(state.config)]);
    }
    state.error = error.message === 'WATCH recovery data limit.' ? 'WATCH recovery data limit reached; automatic claims stopped.' :
      'WATCH connection interrupted; existing attempts are never resubmitted.';
    backoff(state,now);
  } finally {state.supervising = false;}
}

async function explicitDisconnect($, state) {
  state.disconnecting += 1;
  invalidateEligibility(state);
  try {
    const id = await $.session.id();
    if (UUID.test(id)) await $.store.set(preferenceKey(id),{version:1,disconnected:true});
    const data = state.data;
    suspend($,state);
    if (data && data.mode !== 'derived') await save($,state,()=>{data.active = false;});
  } finally {state.disconnecting -= 1;}
}

async function observeInput($,state,e) {
  if (e.agentId !== undefined || !state.data || !state.bound.size) return;
  const id=state.data.native_id, project=state.data.project;
  for (const bound of state.bound.values()) {
    const request=bound.request;
    if (bound.native_id!==id || bound.project!==project || state.data.native_id!==id || state.data.project!==project ||
        state.data.requests[request.request_id]!==request || request.origin!=='peer' || request.execution_completed || request.mixed_input) continue;
    // Native admissions can be queued or steer the running turn. Conservatively withhold
    // its peer answer in either case; never expose owner input to another provider.
    request.mixed_input=true;
    await save($,state);
  }
}

export function register(on) {
  const state = {clientId:crypto.randomUUID(),sequence:-1,data:null,key:null,config:null,generation:null,connected:false,epoch:0,eligibilityEpoch:0,desktopBinding:null,disconnecting:0,
    timer:null,peerProtocol:0,lifecycleProtocol:0,bound:new Map(),noticeProtocol:0,noticeData:null,noticeKey:null,noticeFlushing:false,writes:Promise.resolve(),network:Promise.resolve(),networkBusy:false,flushing:false,supervising:false,
    submitting:false,flight:null,envelope:null,error:'',connecting:false,interactive:false,failures:0,retryAt:0,recoverAt:0,
    rejectedGrant:'',rejectedIdentity:'',startSeen:false,startupInteractive:null,startupSurface:null,timerTicks:0,currentSurfaces:null};
  on('session.start',async($,e,next)=>{
    invalidateEligibility(state);
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
    if (TRANSITIONS.has(e.command)) {invalidateEligibility(state); suspend($,state); return next(e);}
    if (e.command === 'watch-status') {await readSurfaces($,state); return {text:JSON.stringify(publicStatus(state))};}
    if (!['watch-connect','watch-disconnect','watch-setup'].includes(e.command)) return next(e);
    if (!['composer','bridge'].includes(e.origin?.kind)) return {text:'WATCH connection commands require direct owner input.'};
    if (e.command === 'watch-disconnect') {
      try {await explicitDisconnect($,state); return {text:'WATCH disconnected. ' + LIMITATION};}
      catch {return {text:'WATCH disconnect could not be saved; readiness expires when its heartbeat stops.'};}
    }
    if (state.connecting || state.submitting || (state.connected && state.flight && ['pending','claimed','submitted','started'].includes(state.flight.phase))) return {text:'An existing WATCH attempt is unresolved; nothing was resubmitted.'};
    state.connecting = true;
    invalidateEligibility(state);
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
  on('prompt.submit',async($,e,next)=>{
    try {await observeInput($,state,e);} catch { /* Native input is never blocked. */ }
    return next(e);
  });
  on('session.receive',async($,e,next)=>{
    try {await observeInput($,state,e);} catch { /* Native input is never blocked. */ }
    return next(e);
  });
  on('turn.start',async($,e,next)=>{
    try {
      const request = state.flight;
      if (request && request.kind!=='peer_reply_notice' && !request.turn_id && e.agentId === undefined && matchingPrompt(e.text,state.envelope) && await $.session.id() === state.data.native_id) {
        request.turn_id = e.turnId;
        state.bound.set(e.turnId,{request,native_id:state.data.native_id,project:state.data.project,enrollment_id:state.data.enrollment_id});
        await event($,state,request,'started',{turn_id:e.turnId});
      }
    } catch { /* WATCH cannot block native admission. */ }
    return next(e);
  });
  on('turn.complete',async($,e,next)=>{
    try {
      const bound = state.bound.get(e.turnId), request = bound?.request, data=state.data, eligibility=state.eligibilityEpoch;
      const native=await $.session.id(), project=await $.session.cwd();
      if (state.data!==data || state.eligibilityEpoch!==eligibility) return next(e);
      if (request && !request.execution_completed && e.agentId === undefined && state.data && state.data.requests[request.request_id] === request &&
          bound.native_id === state.data.native_id && bound.project === state.data.project && bound.enrollment_id === state.data.enrollment_id &&
          native === bound.native_id && project === bound.project) {
        const outcome = request.mixed_input ? 'withheld' : e.isAborted ? 'aborted' :
          e.reason === 'answer' && matchingAnswer(e.answer,request.request_id) ? 'answered' : 'unusable';
        if (request.lifecycle_protocol === 2) {
          await event($,state,request,'completed',{turn_id:e.turnId,outcome,answer:outcome==='answered'?e.answer:'',event_id:crypto.randomUUID(),occurred_at:await $.clock.now()});
        } else if (outcome === 'answered') await event($,state,request,'answered',{turn_id:e.turnId,answer:e.answer});
        else {await event($,state,request,'unknown'); await save($,state,()=>{request.execution_completed=true;request.capture=outcome;});}
        state.bound.delete(e.turnId);
        if (!state.submitting && state.flight === request) {state.flight = null; state.envelope = null;}
      }
    } catch { /* Retained completion can replay without a new prompt. */ }
    return next(e);
  });
  on('session.end',async($,e,next)=>{invalidateEligibility(state); suspend($,state); return next(e);});
}
