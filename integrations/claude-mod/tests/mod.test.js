import {test} from 'node:test';
import assert from 'node:assert/strict';
import {register} from '../hooks/register.js';
import {matchingPrompt, matchingAnswer, pairing} from '../hooks/protocol.js';

const NATIVE='11111111-1111-4111-8111-111111111111', GENERATION='22222222-2222-4222-8222-222222222222';
const REQUEST='33333333-3333-4333-8333-333333333333', ATTEMPT='44444444-4444-4444-8444-444444444444';
const ENROLLMENT='55555555-5555-4555-8555-555555555555';
const ENVELOPE='WATCH_REQUEST '+REQUEST+'\nThe owner asks through the Officer of the Watch:\nReport progress.\nReply with WATCH_REPLY '+REQUEST+' on its own line. Preserve owner scope.';
const ANSWER='WATCH_REPLY '+REQUEST+'\nFictional progress.';
const copy=value=>JSON.parse(JSON.stringify(value));
const drain=async()=>{for(let i=0;i<30;i++) await new Promise(resolve=>setImmediate(resolve));};

async function harness({storage=new Map(), claimed=true, submit, fetch}={}) {
  const hooks=new Map(), timers=[], calls=[], submissions=[], passed=[];
  let failIdentity=false;
  let native=NATIVE, cwd='/fictional/project', now=100000, failStore=false, generation=GENERATION;
  const claim={request_id:REQUEST,attempt_id:ATTEMPT,native_id:NATIVE,generation:GENERATION,envelope:ENVELOPE,expires_at:1000};
  const response=()=>({version:1,native_id:NATIVE,generation,heartbeat_interval_ms:2000,lease_seconds:12});
  const $={session:{id:async()=>{if(failIdentity)throw Error('identity unavailable');return native;},cwd:async()=>cwd},command:{register:async()=>{}},
    store:{get:async key=>storage.has(key)?copy(storage.get(key)):undefined,
      set:async(key,value)=>{if(failStore)throw Error('disk');storage.set(key,copy(value));}},
    clock:{now:async()=>now,every:(delay,fn)=>{const timer={delay,fn,cancelled:false,cancel(){this.cancelled=true;}};timers.push(timer);return timer;}},
    prompt:{submit:async value=>{submissions.push(value);return submit?submit(value):{text:value.text};}},
    http:{fetch:async(url,init)=>{
      const body=JSON.parse(init.body); calls.push({url,init,body});
      if(fetch){const custom=await fetch(url,body,init);if(custom)return custom;}
      let value;
      if(url.endsWith('/enroll'))value={...response(),token:'fictional-watch-service-token',enrollment_id:ENROLLMENT};
      else if(url.endsWith('/connect'))value=response();
      else if(url.endsWith('/poll')){value={...response(),claim:claimed&&body.accept?claim:null,requests:[]};if(value.claim)claimed=false;}
      else if(url.endsWith('/event'))value={request_id:body.request_id,state:body.kind==='answered'?'answered':'queued',turn_id:body.turn_id||''};
      else if(url.endsWith('/disconnect'))value={disconnected:true};
      else throw Error('unexpected path');
      return {ok:true,status:200,headers:{},text:JSON.stringify(value)};
    }}};
  register((name,handler)=>{hooks.set(name,handler);return {catch(){}};});
  async function call(name,value){const handler=hooks.get(name);return handler?handler($,value,async forwarded=>{passed.push({name,value:forwarded});return {};}):{};}
  const command=(command,args='',origin={kind:'composer'})=>call('command.run',{command,args,origin});
  await call('session.start',{cwd,surface:'desktop',isInteractive:true});
  return {$,storage,claim,calls,submissions,timers,passed,command,call,
    enroll:()=>command('watch-connect','8793:fictional_pair_code_1234'),
    tick:async()=>{const t=timers.filter(t=>!t.cancelled).at(-1);if(t)await t.fn();await drain();},
    setNative(value){native=value;},setCwd(value){cwd=value;},setNow(value){now=value;},
    failIdentity(value){failIdentity=value;},failStore(value){failStore=value;},setGeneration(value){generation=value;},
    events:()=>calls.filter(c=>c.url.endsWith('/event')).map(c=>c.body)};
}

test('owner command authority, strict numeric loopback, and secret-free status',async()=>{
  const h=await harness();
  for(const command of ['watch-connect','watch-disconnect']) assert.match((await h.command(command,'8793:fictional_pair_code_1234',{kind:'plugin',name:'other'})).text,/direct owner/);
  assert.equal(h.calls.length,0);
  assert.equal(pairing('https://example.com:code'),null);
  assert.equal(pairing('65536:fictional_pair_code_1234'),null);
  assert.match((await h.enroll()).text,/connected/);
  assert.equal(h.calls[0].url,'http://127.0.0.1:8793/claude-mod/enroll');
  assert.deepEqual(h.calls[0].body,{code:'fictional_pair_code_1234',native_id:NATIVE,project:'/fictional/project'});
  assert.equal(h.calls[0].init.headers.Authorization,undefined);
  const status=(await h.command('watch-status')).text;
  assert(!status.includes('fictional-watch-service-token'));
  assert(!status.includes('pair_code'));
});

test('detached busy submission persists attempt first, heartbeats and prevents duplicate claim',async()=>{
  let release; const h=await harness({submit:()=>new Promise(resolve=>{release=resolve;})});
  await h.enroll();await h.tick();
  assert.deepEqual(h.submissions,[{text:ENVELOPE,asUser:false}]);
  const stored=[...h.storage.values()][0].requests[REQUEST];
  assert.equal(stored.attempted,true);assert.match(stored.envelope_digest,/^[a-f0-9]{64}$/);
  await h.tick();await h.tick();
  assert.equal(h.calls.filter(c=>c.url.endsWith('/poll')).at(-1).body.accept,false);
  assert.equal(h.submissions.length,1);assert.equal(h.events().length,0);
  release({text:ENVELOPE});await drain();assert.equal(h.events()[0].kind,'submitted');
});

test('exact full envelope binds own main turn; only same-turn marked main answer fulfills',async()=>{
  const h=await harness();await h.enroll();await h.tick();
  await h.call('turn.start',{text:ENVELOPE+' rewritten',turnId:'wrong'});
  await h.call('turn.complete',{turnId:'wrong',answer:ANSWER,isAborted:false,reason:'answer'});
  await h.call('turn.start',{text:ENVELOPE,turnId:'parent',agentId:'child'});
  assert(!h.events().some(e=>e.kind==='started'));
  await h.call('turn.start',{text:ENVELOPE,turnId:'parent'});
  await h.call('turn.complete',{turnId:'parent',agentId:'child',answer:ANSWER,isAborted:false,reason:'answer'});
  await h.call('turn.complete',{turnId:'unrelated',answer:ANSWER,isAborted:false,reason:'answer'});
  assert(!h.events().some(e=>e.kind==='answered'));
  await h.call('turn.complete',{turnId:'parent',answer:ANSWER,isAborted:false,reason:'answer'});
  assert.equal(h.events().filter(e=>e.kind==='answered').length,1);
  assert.equal(h.events().at(-1).answer,ANSWER);
  assert.equal(h.events().at(-1).turn_id,'parent');
});

test('completion before submit settlement stays answered and allows next separate request',async()=>{
  let release;const h=await harness({submit:()=>new Promise(resolve=>{release=resolve;})});
  await h.enroll();await h.tick();
  await h.call('turn.start',{text:ENVELOPE,turnId:'parent'});
  await h.call('turn.complete',{turnId:'parent',answer:ANSWER,isAborted:false,reason:'answer'});
  release({text:ENVELOPE});await drain();await h.tick();
  assert.deepEqual(h.events().map(e=>e.kind),['started','answered']);
  assert.equal([...h.storage.values()][0].requests[REQUEST].phase,'answered');
  assert.equal(h.calls.filter(c=>c.url.endsWith('/poll')).at(-1).body.accept,true);
});

test('aborted, missing, duplicate and wrong markers never become an answer',async()=>{
  assert.equal(matchingAnswer(ANSWER+'\nWATCH_REPLY '+REQUEST,REQUEST),false);
  for(const [answer,isAborted,reason] of [['no marker',false,'answer'],[ANSWER,true,'aborted'],['WATCH_REPLY '+ATTEMPT,false,'answer'],[ANSWER,false,'error']]){
    const h=await harness();await h.enroll();await h.tick();await h.call('turn.start',{text:ENVELOPE,turnId:'parent'});
    await h.call('turn.complete',{turnId:'parent',answer,isAborted,reason});
    assert(!h.events().some(e=>e.kind==='answered'));assert(h.events().some(e=>e.kind==='unknown'));
  }
});

test('store failure before native call prevents submission',async()=>{
  const h=await harness();await h.enroll();h.failStore(true);await h.tick();assert.equal(h.submissions.length,0);
  assert.equal(JSON.parse((await h.command('watch-status')).text).connected,false);
});

test('known drop and unexpected submit failure are never retried',async()=>{
  for(const submit of [()=>({drop:'stopped'}),()=>{throw Error('lost receipt');}]){
    const h=await harness({submit});await h.enroll();await h.tick();await h.tick();await h.tick();
    assert.equal(h.submissions.length,1);assert(h.events().some(e=>['dropped','unknown'].includes(e.kind)));
  }
});

test('ambiguous claim HTTP response stops polling until reconnect',async()=>{
  let polls=0;const h=await harness({fetch:(url)=>{if(url.endsWith('/poll')){polls++;throw Error('lost claim response');}}});
  await h.enroll();await h.tick();await h.tick();assert.equal(polls,1);assert.equal(h.submissions.length,0);
});

test('reload replays retained answer event, never the native prompt',async()=>{
  let lose=true;const h=await harness({fetch:(url,body)=>{if(url.endsWith('/event')&&body.kind==='answered'&&lose)throw Error('lost answer acknowledgment');}});
  await h.enroll();await h.tick();await h.call('turn.start',{text:ENVELOPE,turnId:'parent'});
  await h.call('turn.complete',{turnId:'parent',answer:ANSWER,isAborted:false,reason:'answer'});
  const restarted=await harness({storage:h.storage,claimed:false});await drain();
  assert.equal(restarted.submissions.length,0);assert.equal(restarted.events().at(-1).kind,'answered');
  assert.equal(restarted.events().at(-1).answer,ANSWER);
});

test('reload turns unfinished attempt unknown and never resubmits',async()=>{
  const h=await harness();await h.enroll();await h.tick();
  const restarted=await harness({storage:h.storage,claimed:false});await restarted.tick();
  assert.equal(restarted.submissions.length,0);assert(restarted.events().some(e=>e.kind==='unknown'));
});

test('explicit reconnect recovers unknown receipt without replay and busy promise blocks reconnect',async()=>{
  const failed=await harness({submit:()=>{throw Error('lost admission receipt');}});
  await failed.enroll();await failed.tick();
  assert.match((await failed.command('watch-connect')).text,/WATCH connected/);
  await failed.tick();assert.equal(failed.submissions.length,1);
  let release;const busy=await harness({submit:()=>new Promise(resolve=>{release=resolve;})});
  await busy.enroll();await busy.tick();await busy.command('watch-disconnect');
  assert.match((await busy.command('watch-connect')).text,/unresolved/);
  release({text:ENVELOPE});await drain();
  assert.match((await busy.command('watch-connect')).text,/WATCH connected/);
  assert.equal(busy.submissions.length,1);
});

test('wrong enrollment UUID and stale poll generation never submit',async()=>{
  for(const endpoint of ['/enroll','/poll']){
    const h=await harness({fetch:(url)=>url.endsWith(endpoint)?
      {ok:true,status:200,headers:{},text:JSON.stringify({version:1,native_id:endpoint==='/enroll'?ATTEMPT:NATIVE,
        generation:ATTEMPT,heartbeat_interval_ms:2000,lease_seconds:12,token:'fictional-watch-service-token',enrollment_id:ENROLLMENT,claim:null})}:null});
    await h.enroll();await h.tick();assert.equal(h.submissions.length,0);
    assert.equal(JSON.parse((await h.command('watch-status')).text).connected,false);
  }
});

test('identity/project change and disconnect stop future claims without claiming cancellation',async()=>{
  for(const change of [h=>h.setNative(ATTEMPT),h=>h.setCwd('/fictional/other')]){
    const h=await harness();await h.enroll();change(h);await h.tick();assert.equal(h.submissions.length,0);
    assert(h.calls.some(c=>c.url.endsWith('/disconnect')));
  }
  const h=await harness();await h.enroll();assert.match((await h.command('watch-disconnect')).text,/does not cancel|does not cancel|Disconnect does not cancel/);
  await h.tick();assert.equal(h.submissions.length,0);
});

test('expired or malformed server claim never reaches native input',async()=>{
  for(const edit of [h=>{h.claim.expires_at=1;},h=>{h.claim.envelope='wrong envelope';},h=>{h.claim.generation=ATTEMPT;}]){
    const h=await harness();await h.enroll();edit(h);await h.tick();assert.equal(h.submissions.length,0);
  }
});


test('native turn observers continue while WATCH event HTTP is stalled',async()=>{
  let releaseEvent; let blocked=false;
  const h=await harness({fetch:(url,body)=>{
    if(url.endsWith('/event') && body.kind==='started') {
      blocked=true;
      return new Promise(resolve=>{releaseEvent=()=>resolve({ok:true,status:200,headers:{},text:JSON.stringify({request_id:REQUEST,state:'awaiting_reply',turn_id:'parent'})});});
    }
  }});
  await h.enroll();await h.tick();
  await Promise.race([
    h.call('turn.start',{text:ENVELOPE,turnId:'parent'}),
    new Promise((_,reject)=>setTimeout(()=>reject(Error('turn.start waited for HTTP')),100))
  ]);
  assert.equal(blocked,true);
  await Promise.race([
    h.call('turn.complete',{turnId:'parent',answer:ANSWER,isAborted:false,reason:'answer'}),
    new Promise((_,reject)=>setTimeout(()=>reject(Error('turn.complete waited for HTTP')),100))
  ]);
  const retained=[...h.storage.values()][0].requests[REQUEST];
  assert.equal(retained.completed,true);
  assert.deepEqual(retained.events.map(e=>e.kind),['started','answered']);
  assert.equal(h.events().filter(e=>e.kind==='answered').length,0);
  releaseEvent();await drain();
  assert.equal(h.events().filter(e=>e.kind==='answered').length,1);
  assert.equal(h.submissions.length,1);
});

test('unknown terminal outcomes release claim admission after native promise settles',async()=>{
  for(const result of ['submit-error','aborted-answer']) {
    const h=await harness({submit:result==='submit-error'?()=>{throw Error('lost result');}:undefined});
    await h.enroll();await h.tick();
    if(result==='aborted-answer') {
      await h.call('turn.start',{text:ENVELOPE,turnId:'parent'});
      await h.call('turn.complete',{turnId:'parent',answer:ANSWER,isAborted:true,reason:'aborted'});
    }
    await h.tick();
    assert.equal(h.calls.filter(c=>c.url.endsWith('/poll')).at(-1).body.accept,true);
    assert.equal(JSON.parse((await h.command('watch-status')).text).pending,null);
    assert.equal(h.submissions.length,1);
  }
});

test('acknowledged local history is bounded while pending completion survives lost acknowledgment',async()=>{
  const h=await harness();await h.enroll();
  const [key,saved]=[...h.storage.entries()][0];
  saved.requests={};
  for(let i=0;i<40;i++) {
    const id=i.toString(16).padStart(8,'0')+'-6666-4666-8666-666666666666';
    saved.requests[id]={request_id:id,attempt_id:ATTEMPT,phase:'answered',completed:true,events:[]};
  }
  saved.requests[REQUEST]={request_id:REQUEST,attempt_id:ATTEMPT,phase:'answered',completed:true,
    events:[{request_id:REQUEST,attempt_id:ATTEMPT,kind:'answered',turn_id:'retained',answer:ANSWER}]};
  h.storage.set(key,saved);
  const restarted=await harness({storage:h.storage,claimed:false,fetch:(url)=>{if(url.endsWith('/event'))throw Error('lost ack');}});
  await drain();
  const retained=[...h.storage.values()][0].requests;
  assert.equal(Object.keys(retained).length,32);
  assert.equal(retained[REQUEST].events[0].answer,ANSWER);
  assert.equal(restarted.submissions.length,0);
});


test('WATCH identity and storage failures always pass native events through unchanged',async()=>{
  for(const name of ['turn.start','turn.complete','session.end','command.run','session.start']) {
    const h=await harness();await h.enroll();await h.tick();
    const start={text:ENVELOPE,turnId:'parent'};
    if(name==='turn.complete') await h.call('turn.start',start);
    const value=name==='turn.start'?start:name==='turn.complete'?{turnId:'parent',answer:ANSWER,isAborted:false,reason:'answer'}:
      name==='command.run'?{command:'clear',args:'',origin:{kind:'composer'}}:{cwd:'/fictional/project'};
    if(['turn.start','turn.complete','session.start'].includes(name))h.failIdentity(true);
    else h.failStore(true);
    const before=h.passed.length;
    await h.call(name,value);
    assert.equal(h.passed.length,before+1,name);
    assert.strictEqual(h.passed.at(-1).value,value,name);
  }
});


test('lost answer acknowledgment retries identical completion and admits a distinct request',async()=>{
  const second='66666666-6666-4666-8666-666666666666';
  let polls=0, lose=true;
  const h=await harness({fetch:(url,body)=>{
    if(url.endsWith('/event')&&body.kind==='answered'&&lose) {lose=false;throw Error('lost acknowledgment');}
    if(url.endsWith('/poll')&&++polls===2) return {ok:true,status:200,headers:{},text:JSON.stringify({version:1,
      native_id:NATIVE,generation:GENERATION,requests:[],claim:body.accept?{request_id:second,attempt_id:ENROLLMENT,
        native_id:NATIVE,generation:GENERATION,envelope:ENVELOPE.replaceAll(REQUEST,second),expires_at:1000}:null})};
  }});
  await h.enroll();await h.tick();
  await h.call('turn.start',{text:ENVELOPE,turnId:'parent'});
  await h.call('turn.complete',{turnId:'parent',answer:ANSWER,isAborted:false,reason:'answer'});
  await drain();await h.tick();
  assert.equal(h.calls.filter(c=>c.url.endsWith('/poll')).at(-1).body.accept,true);
  const completions=h.events().filter(e=>e.kind==='answered');
  assert.equal(completions.length,2);
  assert.deepEqual(completions[0],completions[1]);
  assert.deepEqual(h.submissions,[{text:ENVELOPE,asUser:false},{text:ENVELOPE.replaceAll(REQUEST,second),asUser:false}]);
});


const wrappedPrompt=envelope=>'The watch-owner plugin sent a message:\n'+envelope+
  "\n\nThis is how Claude Code surfaces a prompt a plugin submits between turns — it starts this turn in the user's place. Address the message above.";

test('prompt correlation accepts only raw envelope or exact official watch-owner attribution',()=>{
  const wrapped=wrappedPrompt(ENVELOPE);
  assert.equal(matchingPrompt(ENVELOPE,ENVELOPE),true);
  assert.equal(matchingPrompt(wrapped,ENVELOPE),true);
  for(const text of [
    wrapped.replace('The watch-owner','The another-plugin'),
    wrapped.replace('sent a message:', 'sent a message :'),
    wrapped.replace('Address the message above.', 'Address this message.'),
    wrapped.replace('Report progress.', 'Report different progress.'),
    'extra\n'+wrapped,wrapped+'\nextra',wrapped+'\n',wrappedPrompt(wrapped),
    wrapped.replace('between turns', 'mid-turn'),null
  ])assert.equal(matchingPrompt(text,ENVELOPE),false);
  assert.equal(matchingPrompt(wrapped,null),false);
});

test('exact wrapped native main input binds to its marked same-turn answer without resubmission',async()=>{
  const h=await harness();await h.enroll();await h.tick();
  for(const text of [wrappedPrompt(ENVELOPE).replace('watch-owner','other-plugin'),wrappedPrompt(ENVELOPE)+'\nextra']) {
    await h.call('turn.start',{text,turnId:'unrelated'});
    await h.call('turn.complete',{turnId:'unrelated',answer:ANSWER,isAborted:false,reason:'answer'});
  }
  assert(!h.events().some(e=>['started','answered'].includes(e.kind)));
  await h.call('turn.start',{text:wrappedPrompt(ENVELOPE),turnId:'wrapped-main'});
  await h.call('turn.complete',{turnId:'wrapped-main',answer:ANSWER,isAborted:false,reason:'answer'});
  await drain();
  assert.deepEqual(h.events().filter(e=>['started','answered'].includes(e.kind)).map(e=>[e.kind,e.turn_id]),
    [['started','wrapped-main'],['answered','wrapped-main']]);
  assert.equal(h.submissions.length,1);
  assert.deepEqual(h.submissions[0],{text:ENVELOPE,asUser:false});
});
