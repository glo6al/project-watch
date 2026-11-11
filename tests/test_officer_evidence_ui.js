#!/usr/bin/env node
/* Actual Officer history and late-arrival handlers; no browser, server or owner data. */
'use strict';
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(path.join(__dirname,'../src/web/index.html'),'utf8');
const helpers=html.slice(html.indexOf('function ofReceipt('),html.indexOf('function cuTarget('));
const receipt={payload:{version:1,evidence:{refs:['deck:'+'a'.repeat(64)]}},signature:'b'.repeat(64)};
const h=vm.runInNewContext(helpers+';({ofReceipt,ofHistory,ofRequestHistory})',{ofContextValid:()=>null,TextEncoder});
const saved=h.ofHistory([{role:'assistant',content:'x'.repeat(13000),receipt},{role:'user',content:'y'.repeat(5000),receipt}]);
assert.equal(saved[0].content.length,12000);assert.equal(saved[1].content.length,4000);
assert.equal(saved[0].receipt.signature,receipt.signature);assert.equal(saved[1].receipt,undefined);
receipt.payload.evidence.refs.push('forged-after-copy');assert.equal(saved[0].receipt.payload.evidence.refs.length,1);
for(const bad of [null,[],{signature:'x',payload:{}},{signature:'a'.repeat(64),payload:{text:'x'.repeat(30001)}}])assert.equal(h.ofReceipt(bad),null);
const longHistory=Array.from({length:20},(_,i)=>({role:i===19?'user':'assistant',content:i===19?'Newest owner question':'界'.repeat(12000),receipt:saved[0].receipt}));
const bounded=h.ofRequestHistory(longHistory);assert.ok(new TextEncoder().encode(JSON.stringify(bounded)).length<151000);assert.equal(bounded.at(-1).content,'Newest owner question');assert.ok(bounded.length<20);assert.equal(longHistory.length,20);

const handler=html.match(/^async function ofAcceptEvidence\(r\).*$/m)[0];
async function checks(code=handler){
  const ctx={DECK:{readEpoch:0,deleting:0},ofReceipt:h.ofReceipt,post:async()=>({ok:true,json:async()=>({valid:true})})};
  const accept=vm.runInNewContext(code+';ofAcceptEvidence',ctx);
  const r={text:'Fictional supported answer',receipt:saved[0].receipt,evidence_checked:true};
  assert.equal(await accept(r),true);
  ctx.post=async()=>({ok:true,json:async()=>({valid:false})});assert.equal(await accept(r),false);
  ctx.post=async()=>{ctx.DECK.readEpoch++;return {ok:true,json:async()=>({valid:true})}};assert.equal(await accept(r),false,'deletion during validation withholds late response');
  ctx.DECK.deleting=1;let calls=0;ctx.post=async()=>{calls++;throw Error()};assert.equal(await accept(r),false);assert.equal(calls,0);
  ctx.DECK.deleting=0;assert.equal(await accept(r),false,'offline validation fails closed');
  assert.equal(await accept({text:'Legacy display'}),true);assert.equal(await accept({evidence_checked:true}),false,'checked answer cannot omit receipt');
}
(async()=>{await checks();for(const [name,from,to] of [
  ['generation','epoch===DECK.readEpoch','true'],['validity','state?.valid===true','true'],['missing receipt','!r.evidence_checked','true']
]){assert.ok(handler.includes(from));let failed=false;try{await checks(handler.replace(from,to))}catch(err){failed=true}assert.ok(failed,name+' mutation was detected')}
assert.ok(html.indexOf('if(!await ofAcceptEvidence(r))')<html.indexOf("request.replyUI=cuTurnMeta();OF.turns.push"),'validation precedes display and persistence');
assert.ok(html.includes('ofSavedSources(t,d)'),'saved references remain inspectable after reload');
assert.ok(html.includes("post('/officer/evidence',{receipt:answer.receipt,text:answer.text,ref})"),'source viewer uses authenticated proof, not a raw path');
const notice=html.match(/^function ofEvidenceChanged\(\).*$/m)[0];
function noticeCheck(code){let saved=0,cleared=0;const ctx={DECK:{readEpoch:2,token:3},OF:{turns:[{role:'user',content:'Owner question'},{role:'assistant',content:'Already published answer',receipt:{signed:true}}]},deckClearSource(){cleared++},ofSave(){saved++}};const run=vm.runInNewContext(code+';ofEvidenceChanged',ctx);run();assert.equal(ctx.DECK.readEpoch,3);assert.equal(ctx.DECK.token,4);assert.equal(ctx.OF.turns[0].content,'Owner question');assert.equal(ctx.OF.turns[1].content,'Already published answer');assert.equal(ctx.OF.turns[1].receipt,undefined);assert.equal(saved,1);assert.equal(cleared,1)}
noticeCheck(notice);let caught=false;try{noticeCheck(notice.replace('DECK.readEpoch++','void 0'))}catch(err){caught=true}assert.ok(caught,'cross-browser erasure notification mutation detected');
assert.ok(html.includes("if(e.kind==='memory_changed'){ofEvidenceChanged();return}"),'streamed erasure invalidates delayed validation and future replay');
console.log('ok Officer evidence UI: bounded signed history, owner isolation, late deletion, fail-closed arrival, source continuity and erasure notifications; four causal mutations detected');
})().catch(err=>{console.error(err);process.exitCode=1});
