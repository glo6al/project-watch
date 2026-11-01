#!/usr/bin/env node
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync(path.join(__dirname, '../src/web/index.html'), 'utf8');
const start = html.indexOf('/* Claude connection helpers:');
const end = html.indexOf('/* officer context helpers:', start);
assert(start >= 0 && end > start);
const source = html.slice(start, end);
const agent = 'c:11111111-1111-4111-8111-111111111111';
const other = 'c:22222222-2222-4222-8222-222222222222';
const pair = () => ({agent, port:8793, pair_code:'fictional_pair_code_123456', expires_at:Date.now()/1000+300,
  command:'/watch-connect 8793:fictional_pair_code_123456'});

function page() {
  const nodes = new Map();
  const context = {DEMO:false, KEY:'fictional-owner-key', claiming:false, locked:false, connected:true,
    OFUI:{recipient:'agent', epoch:1}, CU:{capability:null}, A:{[agent]:{name:'Atlas',runtime:'Claude'}},
    target:agent, Date, AbortSignal:{timeout:()=>null}, cuQueueReady:()=>false,
    $(id){if(!nodes.has(id))nodes.set(id,{hidden:false,disabled:false,textContent:''});return nodes.get(id)},
    cuTarget(){return context.target}, fetch:async()=>({ok:true,json:async()=>pair()})};
  vm.createContext(context);
  const api = vm.runInContext(source+';({CCM,cuClaudeConnect,cuClaudeSetupPaint,cuClaudePair,cuClaudeEligible})', context);
  return {context,nodes,...api};
}

(async () => {
  const basic = page();
  await basic.cuClaudeConnect();
  assert.equal(basic.nodes.get('#claudeConnectCommand').textContent,pair().command);
  assert.equal(basic.nodes.get('#claudeConnectCommand').hidden,false);
  assert.equal(basic.cuClaudePair({...pair(),command:'javascript:unexpected'},agent,Date.now()/1000),null);
  assert.equal(basic.cuClaudePair({...pair(),agent:other},agent,Date.now()/1000),null);
  assert.equal(basic.cuClaudePair({...pair(),expires_at:1},agent,Date.now()/1000),null);
  assert.equal(basic.cuClaudeEligible(agent,{runtime:'Claude',observed:true},true),false);
  assert.equal(basic.cuClaudeEligible(agent,{runtime:'Claude',sub:true},true),false);
  assert.equal(basic.cuClaudeEligible(agent,{runtime:'Claude'},false),false);

  for(const change of [p=>{p.context.KEY='different-key'},p=>{p.context.OFUI.epoch++},p=>{p.context.locked=true}]) {
    const resolved=page();await resolved.cuClaudeConnect();
    assert(resolved.CCM.pair,'the setup code was initially resolved');
    change(resolved);resolved.cuClaudeSetupPaint();
    assert.equal(resolved.CCM.pair,null,'auth/context changes discard a resolved pairing code');
    assert.equal(resolved.nodes.get('#claudeConnectCommand').hidden,true);
    assert.equal(resolved.nodes.get('#claudeConnectCommand').textContent,'');
  }

  const pending = page();let release;let calls=0;
  pending.context.fetch=()=>{calls++;return new Promise(resolve=>{release=resolve})};
  const first = pending.cuClaudeConnect();
  await pending.cuClaudeConnect();
  assert.equal(calls,1,'double click creates one pairing request');
  pending.context.target=other;pending.context.OFUI.epoch++;
  pending.context.A[other]={name:'Bram',runtime:'Claude'};
  release({ok:true,json:async()=>pair()});await first;
  assert.equal(pending.CCM.pair,null,'late result cannot pair a newly selected chat');
  assert.equal(pending.nodes.get('#claudeConnectCommand').textContent,'');

  const failed = page();
  failed.context.fetch=async()=>({ok:false,status:403});
  await failed.cuClaudeConnect();
  assert.equal(failed.CCM.pair,null);
  assert.match(failed.nodes.get('#claudeConnectNote').textContent,/could not be prepared/);
  assert.equal(failed.nodes.get('#claudeConnect').disabled,false,'failed pairing remains recoverable');
  failed.context.KEY='';failed.cuClaudeSetupPaint();
  assert.equal(failed.nodes.get('#claudeConnect').disabled,true,'locked credentials cannot pair');
  console.log('Claude connection page checks passed');
})().catch(error=>{console.error(error);process.exitCode=1});
