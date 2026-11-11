#!/usr/bin/env node
/* Actual page handlers, fictional packets and deferred local transports only. */
'use strict';
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(path.join(__dirname,'../src/web/index.html'),'utf8');
const helpers=html.slice(html.indexOf('function ofReceipt('),html.indexOf('function cuTarget('));
const acceptance=html.slice(html.indexOf('/* officer evidence acceptance:'),html.indexOf('/* end officer evidence acceptance */'));
const send=html.slice(html.indexOf('async function ofSend(){'),html.indexOf('/* officer evidence acceptance:'));
const navigation=['deckOpen','deckClose','deckLoad'].map(name=>html.split('\n').find(x=>x.startsWith('function '+name+'(')||x.startsWith('async function '+name+'('))).join('\n');
const receipt={payload:{version:1,evidence:{refs:['deck:'+'a'.repeat(64)]}},signature:'b'.repeat(64)};
const clone=v=>JSON.parse(JSON.stringify(v));
const packet=(changes={})=>({text:'Original supported information',receipt:clone(receipt),cites:[{ref:'deck:'+'a'.repeat(64)}],actions:[{do:'filter',project:'Orchard'}],evidence_checked:true,verification:'supported',identity:'boot-a',erasure_generation:0,publication:{valid:true},...changes});
const assessment=(answer,status=answer.verification)=>({status,valid:['supported','stale'].includes(status),answer:clone(answer),warnings:[],identity:answer.identity,erasure_generation:answer.erasure_generation,removed:status==='filtered'});
const response=state=>({ok:true,status:200,json:async()=>state});
function harness(code=acceptance){
  const nodes=new Map(),document={activeElement:{}};
  const node=id=>{if(!nodes.has(id))nodes.set(id,{hidden:true,value:'',focus(){document.activeElement=this},setAttribute(){},getClientRects:()=>[{}]});return nodes.get(id)};
  const ctx={DECK:{readEpoch:0,token:0,loadToken:0,deleting:0,mode:null,mutations:Promise.resolve()},KEY:'fictional-key',locked:false,claiming:false,
    OF:{turns:[],busy:false},document,$:node,txt:(n,v)=>n.textContent=v,deckClearSource(){},deckFocusBack(){return false},deckForeground(){return !node('#deckLog').hidden},deckOptions(){},deckSettings(){},deckRows:v=>v,deckNotice(){},ofSave(){},
    deckRequest:async()=>({settings:{}}),post:async()=>response(assessment(packet())),setTimeout,clearTimeout,AbortController,TextEncoder,
    ofContextValid:c=>c||null,esc:s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))};
  const api=vm.runInNewContext(helpers+'\n'+code+'\n'+navigation+';({OFE,ofAcceptEvidence,ofEvidenceChanged,ofEvidenceSource,ofEvidenceObserve,ofHistory,ofRequestHistory,ofWarnings,ofWarningMarkup,ofReceipt,deckOpen,deckClose,deckLoad})',ctx);
  return {ctx,api,node};
}
async function navigationChecks(code=acceptance){
  for(const action of ['none','settings','source','close','load']){
    const {ctx,api}=harness(code);let release;
    ctx.post=()=>new Promise(done=>release=done);
    const pending=api.ofAcceptEvidence(packet());
    if(action==='settings')api.deckOpen('settings');
    if(action==='source')api.deckOpen('source');
    if(action==='close')api.deckClose();
    if(action==='load'){ctx.DECK.mode='settings';await api.deckLoad()}
    release(response(assessment(packet())));
    const value=await pending;
    assert.equal(value.text,'Original supported information',action+' preserves valid answer');
    assert.equal(value.verification,'supported');assert.ok(value.receipt);
  }
}
async function failures(){
  for(const kind of ['network','http403','json','schema','timeout']){
    const {ctx,api}=harness();let calls=0;
    ctx.post=async()=>{calls++;if(kind==='network')throw Error('offline');if(kind==='http403')return {ok:false,status:403,json:async()=>{throw Error('not JSON')}};if(kind==='json')return {ok:true,json:async()=>{throw Error('bad JSON')}};if(kind==='schema')return response({valid:true});return new Promise(()=>{})};
    if(kind==='timeout'){ctx.setTimeout=fn=>setTimeout(fn,0);ctx.clearTimeout=clearTimeout}
    const value=await api.ofAcceptEvidence(packet());
    assert.equal(value.text,'Original supported information',kind+' preserves server-published information');
    assert.equal(value.verification,'unavailable');assert.equal(value.receipt,undefined);
    assert.equal(value.cites.length,0);assert.equal(value.actions.length,0);assert.ok(value.warnings.length);
    assert.equal(calls,1,'no paid call or repeated ordinary validation');
    if(kind==='http403')assert.ok(value.warnings.some(x=>x.includes('Unlock')));
  }
  const {ctx,api}=harness();let release;ctx.post=()=>new Promise(done=>release=done);
  const pending=api.ofAcceptEvidence(packet());ctx.locked=true;release(response(assessment(packet())));assert.equal(await pending,null,'room lock suppresses delayed answer');
  ctx.locked=false;ctx.post=async()=>{ctx.KEY='different-room-key';return response(assessment(packet()))};assert.equal(await api.ofAcceptEvidence(packet()),null,'changed authorization suppresses delayed answer');
}
async function erasureChecks(code=acceptance){
  const {ctx,api}=harness(code);const original=packet();let first,reads=0;
  const safe=packet({text:'Surviving source only',verification:'filtered',identity:'boot-a',erasure_generation:1,warnings:['Removed material is excluded.']});
  ctx.post=()=>{reads++;return reads===1?new Promise(done=>first=done):Promise.resolve(response(assessment(safe)))};
  const pending=api.ofAcceptEvidence(original);api.ofEvidenceChanged({identity:'boot-a',erasure_generation:1});first(response(assessment(original)));
  const prepared=await pending;assert.equal(reads,2,'one bounded read-only retry after actual erasure');assert.equal(prepared.text,'Surviving source only');assert.equal(prepared.verification,'filtered');
  ctx.post=async()=>{throw Error('later network failure')};const later=await api.ofAcceptEvidence(original);
  assert.equal(later.text,'Surviving source only','a filtered packet must never restore original text');assert.equal(later.receipt,undefined);
  api.ofEvidenceChanged({identity:'boot-a',erasure_generation:2});const erasedAgain=await api.ofAcceptEvidence(original);
  assert.ok(!erasedAgain.text.includes('Original supported')&&!erasedAgain.text.includes('Surviving source'));assert.equal(erasedAgain.receipt,undefined);
}
/* Frozen pre-repair handler: an existing open tab must never display the old
   prose when a repaired server returns a different safe answer. No git needed. */
const baselineAcceptance="async function ofAcceptEvidence(r){if(!r.receipt)return !r.evidence_checked;const epoch=DECK.readEpoch;if(DECK.deleting||!ofReceipt(r.receipt))return false;try{const res=await post('/officer/validate',{receipt:r.receipt,text:r.text});const state=res.ok?await res.json():null;return state?.valid===true&&epoch===DECK.readEpoch&&!DECK.deleting}catch(err){return false}}";
async function legacyCompatibilityCheck(){
  const original=packet(),safe=packet({text:'Fresh surviving source excerpt',verification:'filtered',erasure_generation:1});
  const state=assessment(safe);assert.equal(state.valid,false,'replacement is not unchanged original-text approval');
  const legacyCtx={DECK:{readEpoch:0,deleting:0},ofReceipt:value=>value,post:async()=>response(state)};
  const oldAccept=vm.runInNewContext(baselineAcceptance+';ofAcceptEvidence',legacyCtx);
  assert.equal(await oldAccept(original),false,'old tab must reject original prose');
  const {ctx,api}=harness();ctx.post=async()=>response(state);
  const prepared=await api.ofAcceptEvidence(original);assert.equal(prepared.text,safe.text);assert.equal(prepared.verification,'filtered');assert.ok(prepared.receipt);
  assert.notEqual(prepared.text,original.text,'new tab displays only server-prepared replacement');
}
async function identityChecks(){
  const {ctx,api}=harness();api.ofEvidenceObserve({identity:'boot-a',erasure_generation:8});
  const fresh=packet({identity:'boot-b',erasure_generation:0});ctx.post=async()=>response(assessment(fresh));const value=await api.ofAcceptEvidence(fresh);
  assert.equal(value.verification,'supported');assert.equal(api.OFE.identity,'boot-b');assert.equal(api.OFE.generation,0);
  const before=api.OFE.eraseEpoch;api.ofEvidenceChanged({identity:'boot-a',erasure_generation:99});assert.equal(api.OFE.identity,'boot-b');assert.equal(api.OFE.eraseEpoch,before,'retired stream identity cannot reset new server state');
  const unknown=packet({identity:'boot-b',verification:'unavailable',receipt:undefined,cites:[],warnings:['Storage is temporarily unavailable.']});assert.equal((await api.ofAcceptEvidence(unknown)).text,unknown.text,'qualified /ask packet without proof stays visible');
  const freshHarness=harness(),live=packet({identity:'live-only',erasure_generation:null});
  freshHarness.ctx.post=async()=>response(assessment(live));
  const accepted=await freshHarness.api.ofAcceptEvidence(live);assert.equal(accepted.verification,'supported');assert.ok(accepted.receipt);assert.equal(freshHarness.api.OFE.identity,'live-only');
  const oldEpoch=freshHarness.api.OFE.eraseEpoch;
  freshHarness.api.ofEvidenceObserve({identity:'live-only',erasure_generation:0});
  assert.equal(freshHarness.api.OFE.eraseEpoch,oldEpoch,'archive initialization is not erasure');
  const initialized=packet({identity:'live-only',erasure_generation:0});freshHarness.ctx.post=async()=>response(assessment(initialized));
  const afterInit=await freshHarness.api.ofAcceptEvidence(initialized);assert.equal(afterInit.verification,'supported');assert.ok(afterInit.receipt);
}
function historyChecks(){
  const {api}=harness();const rows=[{role:'user',content:'Owner question',receipt},{role:'assistant',content:'Qualified answer',receipt,verification:'unavailable',warnings:['A source cannot be checked.']},{role:'assistant',content:'Stale observation',receipt,verification:'stale',warnings:['The value may have changed.']},{role:'assistant',content:'Safe filtered answer',receipt,verification:'filtered',warnings:['Removed content is excluded.']}];
  const saved=api.ofHistory(rows);assert.equal(saved[0].receipt,undefined);assert.equal(saved[1].receipt,undefined);assert.ok(saved[2].receipt);assert.ok(saved[3].receipt);assert.equal(saved[1].warnings[0],rows[1].warnings[0]);
  const replay=api.ofRequestHistory(saved);assert.equal(replay.length,3);assert.equal(replay[0].content,'Owner question');assert.equal(replay[1].content,'Stale observation');assert.equal(replay[2].content,'Safe filtered answer');assert.ok(api.ofWarningMarkup(['<script>unsafe</script>']).includes('&lt;script&gt;'));
  for(const bad of [null,[],{signature:'x',payload:{}},{signature:'a'.repeat(64),payload:{text:'x'.repeat(30001)}}])assert.equal(api.ofReceipt(bad),null);
  const long=Array.from({length:20},(_,i)=>({role:i===19?'user':'assistant',content:i===19?'Newest owner question':'界'.repeat(12000),receipt}));const bounded=api.ofRequestHistory(long);assert.ok(new TextEncoder().encode(JSON.stringify(bounded)).length<151000);assert.equal(bounded.at(-1).content,'Newest owner question');
}
async function sourceChecks(){
  for(const removed of [false,true]){const {ctx,api,node}=harness();ctx.post=async()=>({ok:false,status:removed?409:503,json:async()=>({status:removed?'removed':'unavailable',removed,error:'fixed server message'})});await api.ofEvidenceSource(packet(),'deck:'+'a'.repeat(64),{});assert.ok(node('#deckSourceState').textContent.includes(removed?'was removed':'not been identified as deleted'));assert.equal(node('#deckEvidence').hidden,true)}
  const {ctx,api,node}=harness(),saved={role:'assistant',content:'Surviving B observation',receipt:clone(receipt),verification:'supported'};
  ctx.OF.turns=[{role:'user',content:'Owner question'},saved];api.ofEvidenceObserve({identity:'boot-a',erasure_generation:0});
  api.ofEvidenceChanged({identity:'boot-a',erasure_generation:1});
  assert.ok(saved.receipt,'unrelated erasure keeps authentic lineage for server reassessment');assert.equal(saved.verification,'stale');assert.equal(api.ofRequestHistory(ctx.OF.turns).length,2);
  ctx.post=async()=>response({status:'supported',event:{chat_name:'Bram',provider:'Codex',kind:'message',text:'Surviving B source'},warnings:[]});ctx.deckEventText=e=>e.text;ctx.deckSpeaker=e=>e.chat_name;
  await api.ofEvidenceSource({text:saved.content,receipt:saved.receipt},'deck:'+'b'.repeat(64),{});
  assert.equal(node('#deckEvidence').textContent,'Surviving B source');assert.equal(node('#deckEvidence').hidden,false);
}
async function ownerQuestionCheck(){
  const {ctx}=harness();let displayed;Object.assign(ctx,{DEMO:false,ofIn:{value:'Keep my question'},ofCurrent:()=>({context:{scope:'room'},history:{scope:'room'}}),cuTurnMeta:()=>({ts:1}),cuVisible:()=>true,ofAttributed:(c,t)=>t,ofAdd:()=>({}),cuDraftSave(){},ofContextPaint(){},ofStatus(){},ofDay(){},ofReplyNotice(){},ofSt:{textContent:''},ofShow:r=>displayed=r,ofSave(){},crypto:{randomUUID:()=> 'fictional-request'}});
  ctx.post=async path=>path==='/ask'?response(packet({verification:'unavailable',receipt:undefined,cites:[],warnings:['Storage temporarily unavailable.']})):Promise.reject(Error('offline'));
  const run=vm.runInNewContext(send+';ofSend',ctx);await run();assert.equal(ctx.OF.turns[0].content,'Keep my question');assert.equal(ctx.OF.turns[0].role,'user');assert.equal(ctx.OF.turns[1].content,'Original supported information');assert.equal(ctx.OF.turns[1].verification,'unavailable');assert.equal(ctx.OF.turns[1].receipt,undefined);assert.equal(displayed.text,'Original supported information');
}
(async()=>{await navigationChecks();await failures();await erasureChecks();await identityChecks();await legacyCompatibilityCheck();historyChecks();await sourceChecks();await ownerQuestionCheck();
  for(const [name,from,to] of [['sticky filtered','OFE.filtered.get(original)||original','original'],['later erasure','OFE.generation>packet.erasure_generation','false']]){assert.ok(acceptance.includes(from));let failed=false;try{await erasureChecks(acceptance.replace(from,to))}catch(err){failed=true}assert.ok(failed,name+' mutation must be caught')}
  assert.ok(html.includes("if(e.kind==='memory_changed'){ofEvidenceChanged(e);return}"));console.log('ok Officer repair: real navigation/erasure races, bounded validation failures, sticky filtering, identity/auth, qualified history and owner question; two mutations detected');
})().catch(err=>{console.error(err);process.exitCode=1});
