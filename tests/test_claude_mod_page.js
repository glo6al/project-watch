#!/usr/bin/env node
'use strict';
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(path.join(__dirname,'../src/web/index.html'),'utf8');
const start=html.indexOf('/* Claude connection helpers:'),end=html.indexOf('/* officer context helpers:',start);
assert(start>=0&&end>start);const source=html.slice(start,end);
const refreshSource=html.slice(html.indexOf('function cuCapabilityRefresh(){'),html.indexOf('function cuSendReason('));
const agent='c:11111111-1111-4111-8111-111111111111',other='c:22222222-2222-4222-8222-222222222222';
const pair=()=>({version:1,peer_receive:true,port:8793,pair_code:'fictional_setup_code_123456',expires_at:Date.now()/1000+300,command:'/watch-setup 8793:fictional_setup_code_123456'});
function page(code=source,refresh=refreshSource){
 const nodes=new Map(),calls=[];
 const context={DEMO:false,KEY:'fictional-owner-key',claiming:false,locked:false,connected:true,OFUI:{recipient:'agent',epoch:1},CU:{capability:null},A:{[agent]:{name:'Atlas',runtime:'Claude'}},target:agent,Date,encodeURIComponent,cuDirectReason:()=>'',ofContextPaint(){},AbortSignal:{timeout:()=>null},
  $(id){if(!nodes.has(id))nodes.set(id,{hidden:false,disabled:false,textContent:''});return nodes.get(id)},cuTarget(){return context.target},fetch:async(url,opts)=>{calls.push({url,opts});return {ok:true,json:async()=>url==='/claude-mod-revoke'?{version:1,revoked:true}:pair()}}};
 function capability(state='setup_needed',authorized=false,peer_receive=authorized){context.CU.capability={agent:context.target,key:context.KEY,epoch:context.OFUI.epoch,status:'ready',expires:Date.now()+15000,queue:{available:state==='connected',claude_setup:{authorized,state,peer_receive}}};}
 capability();vm.createContext(context);const api=vm.runInContext(code+refresh+';({CCM,cuCapabilityRefresh,cuClaudeConnect,cuClaudeRevoke,cuClaudeSetupPaint,cuClaudePair,cuClaudeEligible})',context);
 return {context,nodes,calls,capability,...api};
}
async function checks(code=source,refresh=refreshSource){
 const basic=page(code);await basic.cuClaudeConnect();
 assert.equal(basic.calls[0].url,'/claude-mod-setup');assert.deepEqual(JSON.parse(basic.calls[0].opts.body),{peer_receive:true},'explicit peer consent contains no selected session identity');
 assert.equal(basic.calls[0].opts.headers['X-Live-Room-Key'],'fictional-owner-key');assert.equal(basic.nodes.get('#claudeConnectCommand').textContent,pair().command);
 for(const bad of [{...pair(),peer_receive:false},{...pair(),peer_receive:undefined},{...pair(),command:'/watch-connect 8793:fictional_setup_code_123456'},{...pair(),command:'javascript:unexpected'},{...pair(),version:2},{...pair(),expires_at:1},{...pair(),port:65536}])assert.equal(basic.cuClaudePair(bad,Date.now()/1000),null);
 for(const a of [{runtime:'Claude',observed:true},{runtime:'Claude',sub:true},{runtime:'Claude',parent:other}])assert.equal(basic.cuClaudeEligible(agent,a,true),false);
 assert.equal(basic.cuClaudeEligible(agent,{runtime:'Claude'},false),false);
 for(const change of [p=>p.context.KEY='new-owner',p=>p.context.OFUI.epoch++,p=>p.context.locked=true,p=>p.context.connected=false]){
  const p=page(code);await p.cuClaudeConnect();assert(p.CCM.pair);change(p);p.cuClaudeSetupPaint();assert.equal(p.CCM.pair,null);assert.equal(p.nodes.get('#claudeConnectCommand').textContent,'');
 }
 const pending=page(code);let release,count=0;pending.context.fetch=()=>{count++;return new Promise(resolve=>release=resolve)};
 const first=pending.cuClaudeConnect();await pending.cuClaudeConnect();assert.equal(count,1);
 pending.context.target=other;pending.context.OFUI.epoch++;pending.context.A[other]={name:'Bram',runtime:'Claude'};release({ok:true,json:async()=>pair()});await first;
 assert.equal(pending.CCM.pair,null);assert.equal(pending.nodes.get('#claudeConnectCommand').textContent,'');
 const connected=page(code);await connected.cuClaudeConnect();connected.capability('waiting',true);connected.cuClaudeSetupPaint();
 assert.equal(connected.CCM.pair,null,'successful global authorization clears the one-use command');assert.equal(connected.nodes.get('#claudeConnect').hidden,true);assert.equal(connected.nodes.get('#claudeRevoke').hidden,false);assert.match(connected.nodes.get('#claudeConnectNote').textContent,/no separate installation or pairing/);
 connected.capability('connected',true);connected.cuClaudeSetupPaint();assert.equal(connected.nodes.get('#claudeSetup').hidden,false,'connected status and revocation stay visible');assert.match(connected.nodes.get('#claudeConnectNote').textContent,/this exact Claude chat/);
 connected.capability('disconnected',true);connected.cuClaudeSetupPaint();assert.match(connected.nodes.get('#claudeConnectNote').textContent,/watch-connect/);
 await connected.cuClaudeRevoke();assert.equal(connected.calls.at(-1).url,'/claude-mod-revoke');assert.equal(connected.context.CU.capability,null,'revoke discards previous readiness proof');assert.match(connected.nodes.get('#claudeConnectNote').textContent,/Already queued native input may still run/);
 const upgrade=page(code);upgrade.capability('connected',true,false);upgrade.cuClaudeSetupPaint();
 assert.equal(upgrade.nodes.get('#claudeConnect').hidden,false);assert.equal(upgrade.nodes.get('#claudeConnect').textContent,'Enable agent messaging');assert.equal(upgrade.nodes.get('#claudeRevoke').hidden,false);assert.match(upgrade.nodes.get('#claudeConnectNote').textContent,/all connected clients/);
 await upgrade.cuClaudeConnect();assert.equal(upgrade.nodes.get('#claudeConnectCommand').textContent,pair().command,'owner-only grant must not erase an upgrade code');assert.match(upgrade.nodes.get('#claudeConnectNote').textContent,/replaces previous automatic authorization/);
 upgrade.capability('waiting',true,true);upgrade.cuClaudeSetupPaint();assert.equal(upgrade.CCM.pair,null);assert.equal(upgrade.nodes.get('#claudeConnectCommand').textContent,'');
 const parsed=page(code,refresh);await parsed.cuClaudeConnect();parsed.context.CU.capability=null;
 parsed.context.fetch=async()=>({ok:true,json:async()=>({agent,actions:{queue:{available:true,mode:'queue'}},claude_setup:{authorized:true,state:'connected',peer_receive:true}})});
 parsed.cuCapabilityRefresh();await new Promise(resolve=>setImmediate(resolve));parsed.cuClaudeSetupPaint();
 assert.equal(parsed.context.CU.capability.queue.claude_setup.peer_receive,true,'production capability parsing retains peer authorization');assert.equal(parsed.nodes.get('#claudeConnect').hidden,true);assert.equal(parsed.CCM.pair,null,'real server refresh clears consumed peer setup code');
 parsed.context.CU.capability=null;parsed.context.fetch=async()=>({ok:true,json:async()=>({agent,actions:{queue:{available:true,mode:'queue'}},claude_setup:{authorized:true,state:'connected'}})});
 parsed.cuCapabilityRefresh();await new Promise(resolve=>setImmediate(resolve));parsed.cuClaudeSetupPaint();assert.equal(parsed.nodes.get('#claudeConnect').hidden,false,'older capability response remains owner-only');
 const malformed=page(code);malformed.context.fetch=async()=>({ok:true,json:async()=>({})});await malformed.cuClaudeRevoke();assert.match(malformed.nodes.get('#claudeConnectNote').textContent,/could not be confirmed/);
 const failed=page(code);failed.context.fetch=async()=>({ok:false});await failed.cuClaudeConnect();assert.equal(failed.CCM.pair,null);assert.match(failed.nodes.get('#claudeConnectNote').textContent,/could not be prepared/);assert.equal(failed.nodes.get('#claudeConnect').disabled,false);failed.context.KEY='';failed.cuClaudeSetupPaint();assert.equal(failed.nodes.get('#claudeConnect').disabled,true);
 const independent=page(code);independent.capability('waiting',true);independent.context.ofIn={value:'Keep this draft'};independent.cuClaudeSetupPaint();assert.equal(independent.context.ofIn.value,'Keep this draft');assert.equal(independent.context.CU.capability.queue.available,false,'global authorization does not enable send to an unregistered chat');
}
(async()=>{
 await checks();
 // A precise mutation must be caught by the authorization/context regression.
 let caught=false;try{await checks(source.replace('context.key===key&&','true&&'))}catch{caught=true}assert(caught,'old owner-key setup response must be rejected');
 for(const [label,from,to] of [['peer consent','JSON.stringify({peer_receive:true})',"'{}'"],['upgrade code retention','pair&&info.authorized&&info.peer_receive','pair&&info.authorized'],['upgrade action','button.hidden=info.authorized&&info.peer_receive===true','button.hidden=info.authorized']]){assert(source.includes(from),label+' mutation site exists');let failed=false;try{await checks(source.replace(from,to))}catch{failed=true}assert(failed,label+' mutation must fail')}
 let parserCaught=false;try{await checks(source,refreshSource.replace('peer_receive:setup.peer_receive===true','peer_receive:false'))}catch{parserCaught=true}assert(parserCaught,'discarding peer authorization in the production capability parser must fail');
 console.log('Global Claude setup page checks passed; stale authorization, consent and upgrade mutations detected');
})().catch(e=>{console.error(e);process.exitCode=1});
