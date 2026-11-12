// Production MCP adapter, signed HTTP, outbox and plugin. Only native execution is mocked.
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {register} from '../hooks/register.js';
const {Client}=await import('../../claude/node_modules/@modelcontextprotocol/sdk/dist/esm/client/index.js');
const {StdioClientTransport}=await import('../../claude/node_modules/@modelcontextprotocol/sdk/dist/esm/client/stdio.js');
let input='';for await(const part of process.stdin)input+=part;
const config=JSON.parse(input),url='http://127.0.0.1:'+config.port,storage=new Map();
const clone=v=>JSON.parse(JSON.stringify(v)),pause=()=>new Promise(r=>setTimeout(r,10));
async function owner(path,body){const response=await fetch(url+path,{method:'POST',headers:{'Content-Type':'application/json','X-Live-Room-Key':config.owner_key},body:JSON.stringify(body)});const value=await response.json();assert(response.ok,JSON.stringify(value));return value;}
async function runtime(native,index){
 const hooks=new Map(),timers=[],submissions=[],heartbeats=[];let api,roster=['desktop'];
 const $={session:{id:async()=>native,cwd:async()=>config.project,surfaces:async()=>clone(roster)},store:{keys:async()=>[...storage.keys()],get:async k=>storage.has(k)?clone(storage.get(k)):undefined,set:async(k,v)=>storage.set(k,clone(v))},command:{register:async()=>{}},clock:{now:async()=>Date.now(),every:(ms,fn)=>{const t={fn,cancelled:false,cancel(){this.cancelled=true}};timers.push(t);return t}},http:{fetch:async(address,init)=>{const r=await fetch(address,init),text=await r.text();if(r.ok&&address.endsWith('/poll'))heartbeats.push(JSON.parse(init.body));return {ok:r.ok,status:r.status,headers:{},text}}},prompt:{submit:async value=>{
  assert.equal(value.asUser,false);assert.deepEqual(roster,[],'peer and owner input are admitted while both runtimes are backgrounded');submissions.push(value.text);
  const prefix=index===0?'WATCH_PEER_REQUEST':'WATCH_REQUEST';const match=new RegExp('^'+prefix+' ([0-9a-f-]+)\\n').exec(value.text);assert(match,'origin/prefix preserved');
  if(index===0){assert.match(value.text,/Origin: peer/);assert.match(value.text,/UNVERIFIED/);assert.match(value.text,/Do not call send_message/);assert.match(value.text,/Cedar/);assert.doesNotMatch(value.text,/The owner asks/);}
  const turn='fixture-main-'+index,wrapped='The watch-owner plugin sent a message:\n'+value.text+"\n\nThis is how Claude Code surfaces a prompt a plugin submits between turns — it starts this turn in the user's place. Address the message above.";
  await api.call('turn.start',{text:wrapped,turnId:turn});
  await api.call('turn.complete',{turnId:turn,answer:'WATCH_REPLY '+match[1]+'\n'+(index===0?'Cedar is blue.':'Fictional owner reply.'),isAborted:false,reason:'answer'});
  return {text:value.text};
 }}};
 register((name,fn)=>{hooks.set(name,fn);return {catch(){}}});
 api={submissions,heartbeats,setSurfaces:value=>{roster=clone(value)},call:async(name,value)=>hooks.get(name)?.($,value,async()=>({})),tick:async()=>{for(const t of timers.filter(t=>!t.cancelled))await t.fn()},status:async()=>JSON.parse((await api.call('command.run',{command:'watch-status',args:'',origin:{kind:'composer'}})).text)};
 await api.call('session.start',{cwd:config.project,surface:null,isInteractive:false});return api;
}
const runtimes=await Promise.all(config.native.map(runtime));
async function until(check){for(let n=0;n<500;n++){await Promise.all(runtimes.map(r=>r.tick()));if(await check())return;await pause()}throw Error('Fixture did not settle');}
const client=new Client({name:'fictional Codex-side MCP client',version:'1'});
try{
 await client.connect(new StdioClientTransport({command:process.execPath,args:[fileURLToPath(new URL('../../claude/peer.mjs',import.meta.url)),'--credential',config.credential]}));
 async function rpc(name,args={}){const r=await client.callTool({name,arguments:args});assert(!r.isError,JSON.stringify(r));return JSON.parse(r.content[0].text).untrusted_peer_data;}
 await runtimes[0].call('command.run',{command:'watch-setup',args:config.port+':'+config.code,origin:{kind:'composer'}});
 await until(async()=>(await Promise.all(runtimes.map(r=>r.status()))).every(r=>r.connected));
 const foregroundPolls=runtimes.map(r=>r.heartbeats.length);
 for(const r of runtimes)r.setSurfaces([]);
 // Count successful production polls rather than trusting a stale local status.
 await until(async()=>runtimes.every((r,index)=>r.heartbeats.length>=foregroundPolls[index]+3));
 for(const [index,r] of runtimes.entries()){
  assert(r.heartbeats.slice(foregroundPolls[index]).every(body=>body.native_id===config.native[index]&&body.project===config.project));
  assert.deepEqual((await r.status()).current_surfaces,[]);
 }
 assert.equal((await owner('/claude-mod-setup-status',{})).connected_chats,2,'both background enrollments retain live backend leases');
 let directory;await until(async()=>{directory=await rpc('list_agents',{provider:'claude'});return directory.directory.complete&&directory.agents.length===2&&directory.agents.every(a=>a.capability==='reachable')});
 const target=directory.agents.find(a=>a.name==='Atlas');assert(target);assert.equal(target.supported_transport,'claude_mod');
 const request={agent_id:target.agent_id,identity_revision:target.identity_revision,text:'Fictional check: Cedar is blue. What color is Cedar? No tools, files or forwarding.',declared_sender:'UNVERIFIED fictional caller',request_id:crypto.randomUUID()};
 const sent=await rpc('send_message',request);assert.equal(sent.duplicate,false);
 const duplicate=await rpc('send_message',request);assert.equal(duplicate.duplicate,true);assert.equal(duplicate.request_id,sent.request_id);
 await until(async()=>runtimes[0].submissions.length===1&&[...storage.values()].some(v=>v?.requests&&Object.values(v.requests).some(r=>r.phase==='completed'&&r.capture==='answered'&&r.completed===true&&r.events.length===0)));
 const reply=await rpc('get_reply',{request_id:sent.request_id});assert.equal(reply.state,'answered');assert.equal(reply.reply,'Cedar is blue.');
 assert.deepEqual(runtimes.map(r=>r.submissions.length),[1,0],'same-project sibling never receives the peer question');
 const hidden=await fetch(url+'/agent-messages',{headers:{'X-Live-Room-Key':config.owner_key}});assert.deepEqual((await hidden.json()).requests,[],'peer text is absent from owner history');
 await owner('/agent-message',{agent:'c:'+config.native[1],question:'A fictional owner request.',request_id:crypto.randomUUID()});
 let ownerReply;await until(async()=>{const r=await fetch(url+'/agent-messages',{headers:{'X-Live-Room-Key':config.owner_key}});const rows=(await r.json()).requests;ownerReply=rows.find(r=>r.state==='answered')?.reply;return !!ownerReply});
 assert.equal(ownerReply,'Fictional owner reply.');
 assert.equal((await owner('/claude-mod-setup-status',{})).connected_chats,2,'both connections remain active after background peer and owner replies');
 await owner('/claude-mod-revoke',{});
 console.log(JSON.stringify({peer_reply:reply.reply,native_submissions:runtimes.map(r=>r.submissions.length),duplicate_recovered:true,owner_reply:ownerReply}));
}finally{await client.close()}
