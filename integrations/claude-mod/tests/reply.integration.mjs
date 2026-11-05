// Real MCP, signed hook HTTP, SQLite and plugin; native turns are simulated.
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {register} from '../hooks/register.js';
import {replyNoticeText} from '../hooks/protocol.js';
const {Client}=await import('../../claude/node_modules/@modelcontextprotocol/sdk/dist/esm/client/index.js');
const {StdioClientTransport}=await import('../../claude/node_modules/@modelcontextprotocol/sdk/dist/esm/client/stdio.js');
let input='';for await(const part of process.stdin)input+=part;
const config=JSON.parse(input),url='http://127.0.0.1:'+config.port,storage=new Map();
const copy=value=>JSON.parse(JSON.stringify(value));
const pause=()=>new Promise(resolve=>setTimeout(resolve,10));
const client=new Client({name:'fictional reply integration client',version:'1'});
let current=null;

async function rpc(name,args={}) {
 const raw=await client.callTool({name,arguments:args});
 assert(!raw.isError,JSON.stringify(raw));
 return {raw,value:JSON.parse(raw.content[0].text).untrusted_peer_data};
}
async function receiveNotice(text,provider) {
 assert(current,'a notice must belong to a live fixture case');
 assert.equal(current.source_provider,provider);
 assert.equal(text,replyNoticeText(current.public_request_id),'notice must use the public lookup ID');
 assert(current.source_finished,'native return must follow completion of the asking turn');
 current.events.push('native_notice');
 current.notice_submissions++;
 assert.equal(current.notice_submissions,1,'only one native return admission');
 current.get_reply_calls++;
 const {value}=await rpc('get_reply',{request_id:current.public_request_id});
 current.events.push('get_reply');
 assert.equal(value.request_id,current.public_request_id);
 assert.equal(value.state,'answered');
 assert.equal(value.reply,'Fictional '+provider+' answer.');
 current.reply=value.reply;
}
async function runtime(native,index) {
 const hooks=new Map(),timers=[],submissions=[],polls=[];
 let api,surfaces=['desktop'];
 const $={
  session:{id:async()=>native,cwd:async()=>config.project,surfaces:async()=>copy(surfaces)},
  store:{keys:async()=>[...storage.keys()],get:async key=>storage.has(key)?copy(storage.get(key)):undefined,
   set:async(key,value)=>storage.set(key,copy(value))},
  command:{register:async()=>{}},
  clock:{now:async()=>Date.now(),every:(_ms,fn)=>{
   const timer={fn,cancelled:false,cancel(){this.cancelled=true}};timers.push(timer);return timer;
  }},
  http:{fetch:async(address,init)=>{
   const response=await fetch(address,init),text=await response.text();
   if(response.ok&&address.endsWith('/poll'))polls.push(JSON.parse(text));
   return {ok:response.ok,status:response.status,headers:{},text};
  }},
  prompt:{submit:async value=>{
   assert.equal(value.asUser,false);
   assert.deepEqual(surfaces,[],'background chats must remain usable without a room tile');
   submissions.push(value.text);
   const turn='fictional-'+crypto.randomUUID();
   if(value.text.startsWith('WATCH_PEER_REPLY ')) {
    assert.equal(index,1,'same-project sibling cannot receive a return notice');
    await api.call('turn.start',{text:value.text,turnId:turn});
    await receiveNotice(value.text,'claude');
    // A notice's main turn is not a new peer request to fulfill.
    await api.call('turn.complete',{turnId:turn,answer:current.reply,isAborted:false,reason:'answer'});
   }else{
    assert.equal(index,0,'only the pinned target receives the question');
    const match=/^WATCH_PEER_REQUEST ([0-9a-f-]+)\n/.exec(value.text);
    assert(match);
    assert(current.source_finished,'target answer is deliberately released after asking turn completion');
    assert.notEqual(match[1],current.public_request_id,'transport and public IDs must stay distinct');
    current.question_submissions++;
    assert.equal(current.question_submissions,1);
    current.events.push('target_answer');
    const wrapped='The watch-owner plugin sent a message:\n'+value.text+
     "\n\nThis is how Claude Code surfaces a prompt a plugin submits between turns — it starts this turn in the user's place. Address the message above.";
    await api.call('turn.start',{text:wrapped,turnId:turn});
    await api.call('turn.complete',{turnId:turn,
     answer:'WATCH_REPLY '+match[1]+'\nFictional '+current.source_provider+' answer.',isAborted:false,reason:'answer'});
   }
   return {text:value.text};
  }}
 };
 register((name,fn)=>{hooks.set(name,fn);return {catch(){}}});
 api={submissions,polls,background:()=>{surfaces=[]},call:async(name,value)=>hooks.get(name)?.($,value,async()=>({})),
  tick:async()=>{for(const timer of timers.filter(t=>!t.cancelled))await timer.fn()},
  status:async()=>JSON.parse((await api.call('command.run',{command:'watch-status',args:'',origin:{kind:'composer'}})).text)};
 await api.call('session.start',{cwd:config.project,surface:null,isInteractive:false});
 return api;
}
const runtimes=await Promise.all(config.native.map(runtime));
async function until(check) {
 for(let n=0;n<500;n++) {
  await Promise.all(runtimes.map(r=>r.tick()));
  if(await check())return;
  await pause();
 }
 throw Error('Reply integration did not settle: '+JSON.stringify(current));
}
try {
 await client.connect(new StdioClientTransport({command:process.execPath,
  args:[fileURLToPath(new URL('../../claude/peer.mjs',import.meta.url)),'--credential',config.credential]}));
 await runtimes[0].call('command.run',{command:'watch-setup',args:config.port+':'+config.code,origin:{kind:'composer'}});
 await until(async()=>(await Promise.all(runtimes.map(r=>r.status()))).every(s=>s.connected));
 await until(()=>runtimes.every(r=>r.polls.some(p=>p.reply_notice_protocol===1)));
 for(const runtime of runtimes)runtime.background();
 let directory;
 await until(async()=>{
  directory=(await rpc('list_agents',{provider:'claude'})).value;
  return directory.directory.complete&&directory.agents.length===3&&directory.agents.every(a=>a.capability==='reachable');
 });
 const target=directory.agents.find(a=>a.agent_id==='c:'+config.native[0]);assert(target);
 const cases=[];
 for(const provider of ['codex','claude']) {
  current={source_provider:provider,public_request_id:null,source_finished:false,events:[],
   question_submissions:0,notice_submissions:0,get_reply_calls:0,reply:null};
  const args={agent_id:target.agent_id,identity_revision:target.identity_revision,
   text:'Fictional '+provider+' return test. Reply only; do not start work.'};
  const sent=await rpc('send_message',args);
  assert.equal(sent.value.duplicate,false);
  current.public_request_id=sent.value.request_id;
  current.events.push('send_complete');
  const observed=spawnSync(config.python,[config.hook_script,'--provider',provider,'--credential-file',config.hook_credential],{
   input:JSON.stringify({hook_event_name:'PostToolUse',session_id:provider==='codex'?config.codex_source:config.native[1],
    tool_use_id:'fictional-'+crypto.randomUUID(),tool_name:provider==='codex'?'mcp__watch_peer__send_message':'mcp__watch-peer__send_message',
    tool_input:args,tool_response:sent.raw}),encoding:'utf8',timeout:6000});
  assert.equal(observed.status,0);assert.equal(observed.stdout,'');assert.equal(observed.stderr,'');
  current.source_finished=true;
  current.events.push('source_turn_complete');
  let readCodexNotice=false;
  await until(async()=>{
   if(provider==='codex'&&!readCodexNotice) {
    let data='';try{data=await fs.readFile(config.codex_notice_file,'utf8')}catch(error){if(error.code!=='ENOENT')throw error;}
    if(data.trim()) {
     const messages=data.trim().split('\n').map(JSON.parse);
     assert.equal(messages.length,1);
     assert.equal(messages[0].thread,config.codex_source);
     readCodexNotice=true;
     await receiveNotice(messages[0].message,'codex');
    }
   }
   const noticeReceipts=[...storage.values()].filter(v=>v?.kind==='peer_reply_notice').flatMap(v=>Object.values(v.requests));
   return !!current.reply&&(provider==='codex'||noticeReceipts.some(r=>r.public_request_id===current.public_request_id&&r.phase==='submitted'&&r.events.length===0));
  });
  assert.deepEqual(current.events,['send_complete','source_turn_complete','target_answer','native_notice','get_reply']);
  assert.equal(current.get_reply_calls,1);
  assert.equal(runtimes[2].submissions.length,0);
  cases.push({source_provider:provider,public_request_id:current.public_request_id,reply:current.reply,
   question_submissions:current.question_submissions,notice_submissions:current.notice_submissions,
   get_reply_calls:current.get_reply_calls,source_turn_finished_before_answer:true,sibling_notice_submissions:runtimes[2].submissions.length});
 }
 assert.equal(runtimes[0].submissions.length,2);
 assert.equal(runtimes[1].submissions.length,1);
 assert.equal(runtimes[2].submissions.length,0);
 console.log(JSON.stringify({cases}));
}finally{await client.close()}
