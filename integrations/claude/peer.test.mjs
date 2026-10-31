import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import {randomUUID,createHash,createHmac} from 'node:crypto';
import {spawn} from 'node:child_process';
import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StdioClientTransport} from '@modelcontextprotocol/sdk/client/stdio.js';
import {readCredential,validArgs,safeResult,validRequestId,instructions} from './peer.mjs';
const here=path.dirname(new URL(import.meta.url).pathname),script=process.env.WATCH_PEER_TEST_SCRIPT||path.join(here,'peer.mjs');
const {PeerClient}=await import(new URL(script,'file://').href);
const hash=v=>createHash('sha256').update(v).digest('hex');
const hmac=(secret,lines)=>createHmac('sha256',Buffer.from(secret,'hex')).update(lines.join('\n')).digest('hex');
async function fixture(){
 const root=await fs.mkdtemp(path.join(await fs.realpath(os.tmpdir()),'watch-peer-'));await fs.chmod(root,0o700);
 const config=path.join(root,'peer-credentials');await fs.mkdir(config,{mode:0o700});
 const credential={version:2,credential_id:randomUUID(),secret:'a'.repeat(64),port:1};
 const file=path.join(config,credential.credential_id+'.json');
 const native=randomUUID(),agent='x:'+native,target={agent_id:agent,name:'Worker',provider:'codex',project_id:'c'.repeat(64),project_label:'Example / Project',identity_revision:'d'.repeat(64)};
 let mode='normal';const seen=[],run=randomUUID(),sockets=new Set();
 const server=http.createServer(async(req,res)=>{
  let raw='';for await(const b of req)raw+=b;seen.push({path:req.url,raw,headers:req.headers,socket:req.socket});
  if(mode==='hang')return;
  if(req.url==='/bridge/v2/challenge'){
   const p=JSON.parse(raw);assert.deepEqual(Object.keys(p),['credential_id','nonce']);assert.equal(p.credential_id,credential.credential_id);assert.match(p.nonce,/^[a-f0-9]{64}$/);
   const body=JSON.stringify({run_id:run,proof:mode==='squatter'?'b'.repeat(64):hmac(credential.secret,['watch-bridge-server-v2',String(credential.port),credential.credential_id,p.nonce,run])});
   res.writeHead(200,{'Content-Length':Buffer.byteLength(body),...(mode==='close'?{Connection:'close'}:{})});res.end(body);return;
  }
  assert.equal(req.url,'/bridge/v2/rpc');const p=JSON.parse(raw),n=req.headers['x-watch-nonce'];
  assert.equal(req.headers['x-watch-bridge'],credential.credential_id);assert.equal(req.headers['x-watch-run'],run);assert.equal(req.headers['x-watch-signature'],hmac(credential.secret,['watch-bridge-request-v2',String(credential.port),'POST','/bridge/v2/rpc',req.headers['x-watch-time'],n,run,hash(raw)]));
  if(mode==='loss'){req.socket.destroy();return}
  const record={origin:'peer',request_id:mode==='mismatched'?randomUUID():p.params.request_id,agent_id:p.params.agent_id||agent,state:'queued',reply:'<untrusted>',error:'',created_at:Date.now()/1000,target:{...target,agent_id:p.params.agent_id||agent},duplicate:false,text_preview:'Please inspect',secret:'SHOULD NOT DISPLAY'};
  const result=p.method==='list_agents'?{agents:[{...target,native_id:native,capability:'unavailable',reason:'No transport',last_seen:Date.now()/1000,supported_transport:'codex_queue',secret:'SHOULD NOT DISPLAY'}],next_cursor:null,directory:{complete:false,reasons:['Scan bounded'],refreshed_at:Date.now()/1000}}:p.method==='list_requests'?{requests:[{...record,request_id:randomUUID()}],next_cursor:null}:record;
  const status=mode==='reject409'||mode==='reject-long'||mode==='reject-control'?409:mode==='reject429'?429:mode==='error500'?500:200;
  const reason=mode==='reject-long'?'x'.repeat(201):mode==='reject-control'?'Invalid\nreason':status===409?'Answer your incoming peer request first.':status===429?'Peer delivery capacity is full.':'Peer service failed.';
  const body=mode==='huge'?'x'.repeat(65537):JSON.stringify(status===200?{result}:{error:reason}),signature=hmac(credential.secret,['watch-bridge-response-v2',String(credential.port),run,n,String(status),hash(body)]);
  res.writeHead(status,{'Content-Length':Buffer.byteLength(body),'X-Watch-Signature':mode==='altered'?'b'.repeat(64):signature});res.end(body);
 });
 server.on('connection',s=>{sockets.add(s);s.on('close',()=>sockets.delete(s))});await new Promise(r=>server.listen(0,'127.0.0.1',r));credential.port=server.address().port;await fs.writeFile(file,JSON.stringify(credential),{mode:0o600});
 return {root,file,credential,seen,target,setMode(v){mode=v},async close(){for(const s of sockets)s.destroy();await new Promise(r=>server.close(r));await fs.rm(root,{recursive:true,force:true})}};
}
test('real signed HTTP authenticates before body on the same socket, filters output and never retries',async()=>{
 const f=await fixture();try{
  const c=new PeerClient(f.credential),params={agent_id:'x:'+randomUUID(),identity_revision:'d'.repeat(64),text:'Sensitive message',request_id:randomUUID()};
  const result=await c.call('send_message',params);assert.equal(result.origin,'peer');assert.equal(result.reply,'<untrusted>');assert.equal(result.secret,undefined);assert.equal(f.seen.length,2);assert.equal(f.seen[0].socket,f.seen[1].socket,'proof and sensitive request share TCP socket');assert.equal(f.seen[0].raw.includes(params.text),false);assert.equal(f.seen[0].raw.includes(f.credential.secret),false);assert.equal(JSON.stringify(f.seen.map(r=>r.headers)).includes(f.credential.secret),false);
  assert.equal((await c.call('send_message',{...params,text:'\u0001'.repeat(2000)})).state,'queued','valid maximum escaped message fits RPC bound');
  for(const mode of ['squatter','close','altered','huge','loss','mismatched']){
   f.setMode(mode);const before=f.seen.length;await assert.rejects(c.call('send_message',params),/No request was retried/);const calls=f.seen.slice(before);assert.equal(calls.filter(r=>r.path==='/bridge/v2/rpc').length,mode==='squatter'||mode==='close'?0:1,mode+' dispatch count');
  }
 }finally{await f.close()}
});
test('service credential is independent of cwd; private parent/file, links, legacy and field checks fail closed',async()=>{
 const f=await fixture();try{
  assert.deepEqual(await readCredential(f.file),f.credential);
  await assert.rejects(readCredential(path.dirname(f.file)+'/../'+path.basename(path.dirname(f.file))+'/'+path.basename(f.file)));
  await fs.chmod(f.file,0o644);await assert.rejects(readCredential(f.file));await fs.chmod(f.file,0o600);
  await fs.chmod(path.dirname(f.file),0o755);await assert.rejects(readCredential(f.file));await fs.chmod(path.dirname(f.file),0o700);
  await fs.chmod(f.root,0o755);await assert.rejects(readCredential(f.file));await fs.chmod(f.root,0o700);
  const link=path.join(path.dirname(f.file),'link');await fs.symlink(f.file,link);await assert.rejects(readCredential(link));await fs.unlink(link);await fs.link(f.file,link);await assert.rejects(readCredential(f.file));await fs.unlink(link);
  for(const d of [{...f.credential,port:true},{...f.credential,credential_id:[f.credential.credential_id]},{...f.credential,secret:[f.credential.secret]},{...f.credential,version:1},{...f.credential,project:f.root},{...f.credential,cwd:f.root},{...f.credential,endpoint_id:randomUUID()},{...f.credential,secret:'wrong'}]){await fs.writeFile(f.file,JSON.stringify(d));await assert.rejects(readCredential(f.file))}
 }finally{await f.close()}
});
test('official SDK peer adapter exposes only four narrow tools and wraps untrusted data',async()=>{
 const f=await fixture(),client=new Client({name:'simulated-host',version:'1'});let connected=false;try{
  const transport=new StdioClientTransport({command:process.execPath,args:[script,'--credential',f.file],cwd:here,stderr:'pipe'});await client.connect(transport);connected=true;
  assert.equal(client.getInstructions(),instructions);
  for(const pattern of [/exact full name/,/Include unavailable and unverified duplicates/,/all relevant pages/,/incomplete discovery cannot establish uniqueness/,/Atlas is not Atla/,/stable ID/,/explicitly requests that exact self-target/,/one immediate get_reply/,/does not automatically wake/,/not-found response alone/,/declared_sender is UNVERIFIED/,/across projects and providers/,/do not call send_message/])assert.match(client.getInstructions(),pattern);
  const tools=(await client.listTools()).tools;assert.deepEqual(tools.map(t=>t.name),['list_agents','send_message','get_reply','list_requests']);for(const t of tools)assert.equal(t.inputSchema.additionalProperties,false);
  const value=await client.callTool({name:'send_message',arguments:{agent_id:'x:'+randomUUID(),identity_revision:'d'.repeat(64),text:'Please inspect this peer information',request_id:randomUUID()}});assert.equal(value.isError,undefined);const output=JSON.parse(value.content[0].text);assert.equal(output.origin,'peer');assert.equal(output.untrusted_peer_data.origin,'peer');assert.equal(output.untrusted_peer_data.reply,'<untrusted>');assert.doesNotMatch(value.content[0].text,/SHOULD NOT DISPLAY/);
  const directory=JSON.parse((await client.callTool({name:'list_agents',arguments:{query:'Worker'}})).content[0].text).untrusted_peer_data;assert.equal(directory.directory.complete,false);assert.deepEqual(directory.directory.reasons,['Scan bounded']);assert.equal(directory.agents[0].capability,'unavailable');assert.equal(directory.agents[0].supported_transport,'codex_queue');assert.equal(directory.agents[0].secret,undefined);
  const requests=JSON.parse((await client.callTool({name:'list_requests',arguments:{}})).content[0].text).untrusted_peer_data;assert.equal(requests.requests[0].reply,undefined);assert.equal(requests.requests[0].text_preview,'Please inspect');assert.equal(requests.requests[0].target.project_label,'Example / Project');assert.ok(requests.requests[0].created_at>0);
  const before=f.seen.length;for(const [name,args] of [['owner_send',{}],['list_agents',{project:f.root}],['send_message',{agent_id:'x:'+randomUUID(),identity_revision:'d'.repeat(64),text:'hi',request_id:randomUUID(),sender:randomUUID()}],['get_reply',{request_id:'bad'}]])assert.equal((await client.callTool({name,arguments:args})).isError,true);assert.equal(f.seen.length,before,'invalid tools do not reach HTTP');
  f.setMode('altered');const invalid=await client.callTool({name:'list_agents',arguments:{}});assert.equal(invalid.isError,true);assert.equal(invalid.content[0].text,'Peer operation could not be confirmed. No request was retried.');
 }finally{if(connected)await client.close();await f.close()}
});
test('strict tool validation and response bounds',()=>{
 assert.equal(validArgs('list_agents',{}),true);assert.equal(validArgs('list_requests',{sender:'fake'}),false);assert.equal(validArgs('send_message',{agent_id:'x:'+randomUUID(),identity_revision:'d'.repeat(64),request_id:randomUUID(),text:'😀'.repeat(2000)}),true);assert.equal(validArgs('send_message',{agent_id:'x:'+randomUUID(),identity_revision:'d'.repeat(64),request_id:randomUUID(),text:'x'.repeat(2001)}),false);
 assert.throws(()=>safeResult('list_requests',{requests:Array(51).fill({}),next_cursor:null}));assert.throws(()=>safeResult('get_reply',{request_id:'wrong'}));
});
test('optional request UUIDs are minted once and retained after uncertain delivery and signed rejection',async()=>{
 const f=await fixture(),client=new Client({name:'UUID recovery host',version:'1'});let connected=false;
 try{
  await client.connect(new StdioClientTransport({command:process.execPath,args:[script,'--credential',f.file],cwd:here,stderr:'pipe'}));connected=true;
  const args={agent_id:f.target.agent_id,identity_revision:f.target.identity_revision,text:'One authorized scoped request',declared_sender:'UNVERIFIED Example'};
  const sent=await client.callTool({name:'send_message',arguments:args}),result=JSON.parse(sent.content[0].text).untrusted_peer_data;
  assert.ok(validRequestId(result.request_id));assert.equal(result.agent_id,args.agent_id);assert.equal(result.target.identity_revision,args.identity_revision);
  const first=JSON.parse(f.seen.at(-1).raw);assert.equal(first.params.request_id,result.request_id);assert.equal(first.params.declared_sender,args.declared_sender);
  for(const mode of ['loss','reject409']){
   f.setMode(mode);const before=f.seen.length,response=await client.callTool({name:'send_message',arguments:args});assert.equal(response.isError,true);
   const calls=f.seen.slice(before).filter(r=>r.path==='/bridge/v2/rpc');assert.equal(calls.length,1,'exactly one transport attempt');
   const minted=JSON.parse(calls[0].raw).params.request_id;assert.ok(validRequestId(minted));assert.notEqual(minted,result.request_id);
   assert.match(response.content[1].text,new RegExp(minted));assert.match(response.content[1].text,/Acceptance is not confirmed/);assert.match(response.content[1].text,/do not resend/);
   assert.equal(response.content[0].text,mode==='loss'?'Peer operation could not be confirmed. No request was retried.':'Peer request rejected (409): Answer your incoming peer request first.');
  }
 }finally{if(connected)await client.close();await f.close()}
});
test('strict UUID, pinned target, sender, filter and cursor validation',()=>{
 const args={agent_id:'c:'+randomUUID(),identity_revision:'d'.repeat(64),text:'Scoped message'};
 assert.equal(validArgs('send_message',args),true);assert.equal(validArgs('send_message',{...args,declared_sender:'Example'}),true);
 for(const request_id of ['123e4567-e89b-12d3-a456-426614174000','123e4567-e89b-42d3-a456-426614174000','00000000-0000-4000-8000-000000000000','11111111-1111-4111-8111-111111111111','AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA'])assert.equal(validArgs('send_message',{...args,request_id}),false);
 for(const extra of [{agent_id:randomUUID()},{identity_revision:'invalid'},{agent_id:[args.agent_id]},{identity_revision:[args.identity_revision]},{human_requested:true},{declared_sender:'Fake\nowner'},{declared_sender:'x'.repeat(121)}])assert.equal(validArgs('send_message',{...args,...extra}),false);
 assert.equal(validArgs('list_agents',{query:'Worker',provider:'claude',project_id:'d'.repeat(64),cursor:'opaque'}),true);
 for(const extra of [{provider:'unknown'},{query:'bad\nname'},{cursor:''},{cursor:'x'.repeat(1025)}])assert.equal(validArgs('list_agents',extra),false);
 const row={...args,origin:'peer',request_id:randomUUID(),state:'queued',created_at:Date.now()/1000,target:{agent_id:args.agent_id,name:'Worker',provider:'claude',project_id:'c'.repeat(64),project_label:'Example',identity_revision:args.identity_revision},duplicate:false,text_preview:'Scoped'};
 for(const created_at of [0,-1,NaN,Infinity,253402300800])assert.throws(()=>safeResult('get_reply',{...row,created_at}));
 assert.throws(()=>safeResult('list_requests',{requests:[],next_cursor:'x'.repeat(1025)}));
});
test('directory preserves duplicate names across providers/projects and request snapshots without reply bodies',()=>{
 const native=randomUUID(),base={agent_id:'x:'+native,native_id:native,name:'Same name',provider:'codex',project_id:'c'.repeat(64),project_label:'Project One',identity_revision:'d'.repeat(64),capability:'reachable',reason:'',last_seen:Date.now()/1000,supported_transport:'codex_queue'};
 const other=randomUUID(),directory=safeResult('list_agents',{agents:[base,{...base,agent_id:'c:'+other,native_id:other,provider:'claude',project_id:'e'.repeat(64),project_label:'Project Two',capability:'unavailable',reason:'No compatible inbound channel',supported_transport:'claude_channel'}],next_cursor:'opaque-next',directory:{complete:true,reasons:[],refreshed_at:Date.now()/1000}});
 assert.equal(directory.agents.length,2);assert.deepEqual(directory.agents.map(r=>r.name),['Same name','Same name']);assert.deepEqual(directory.agents.map(r=>r.capability),['reachable','unavailable']);assert.equal(directory.next_cursor,'opaque-next');
 const row={origin:'peer',request_id:randomUUID(),agent_id:base.agent_id,state:'answered',created_at:Date.now()/1000,target:base,duplicate:true,text_preview:'Original message',reply:'Shared answer',error:''};
 assert.equal(safeResult('send_message',row).duplicate,true);assert.equal(safeResult('send_message',row).target.name,'Same name');
 assert.equal(safeResult('list_requests',{requests:[row],next_cursor:null}).requests[0].reply,undefined);
});
test('adapter sensitivity: server proof and signed response protections are required',async()=>{
 const original=await fs.readFile(path.join(here,'peer.mjs'),'utf8');
 for(const [name,from,to] of [
  ['proof',"!equal(proof.proof,mac(key,['watch-bridge-server-v2',String(d.port),d.credential_id,nonce,proof.run_id]))",'false'],
  ['response',"!equal(response.headers['x-watch-signature'],mac(key,['watch-bridge-response-v2',String(d.port),proof.run_id,nonce,String(response.status),digest(response.body)]))",'false']
 ]){
  assert.ok(original.includes(from));const dir=await fs.mkdtemp(path.join(here,'.peer-mutation-')),target=path.join(dir,'peer.mjs');try{
   await fs.writeFile(target,original.replace(from,to));const result=await new Promise(resolve=>{const env={...process.env,WATCH_PEER_TEST_SCRIPT:target};delete env.NODE_TEST_CONTEXT;const child=spawn(process.execPath,['--test','--test-name-pattern','real signed HTTP',path.join(here,'peer.test.mjs')],{env,stdio:['ignore','pipe','pipe']});let output='';child.stdout.on('data',b=>output+=b);child.stderr.on('data',b=>output+=b);child.on('exit',code=>resolve({code,output}))});assert.notEqual(result.code,0,name+' mutation must fail');assert.match(result.output,/Missing expected rejection/);
  }finally{await fs.rm(dir,{recursive:true,force:true})}
 }
});

test('peer HTTP has a total ten-second deadline without a redispatch',{timeout:12000},async()=>{
 const f=await fixture();try{f.setMode('hang');const started=Date.now();await assert.rejects(new PeerClient(f.credential).call('list_requests',{}),/No request was retried/);assert.ok(Date.now()-started>=9500);assert.ok(Date.now()-started<11500);assert.equal(f.seen.length,1);assert.equal(f.seen[0].path,'/bridge/v2/challenge');}finally{await f.close()}
});
test('signed bounded server rejections are distinct from uncertain transport failures',async()=>{
 const f=await fixture(),client=new Client({name:'signed rejection host',version:'1'});let connected=false;try{
  const broker=new PeerClient(f.credential);
  for(const [mode,message] of [['reject409','Peer request rejected (409): Answer your incoming peer request first.'],['reject429','Peer request rejected (429): Peer delivery capacity is full.'],['error500','Peer server error (500): Peer service failed. Delivery may be uncertain; check list_requests or get_reply before sending again.']]){
   f.setMode(mode);const before=f.seen.length;await assert.rejects(broker.call('list_requests',{}),error=>error.message===message);assert.equal(f.seen.length-before,2,'one challenge and one RPC only');
  }
  for(const mode of ['reject-long','reject-control']){f.setMode(mode);await assert.rejects(broker.call('list_requests',{}),/could not be confirmed/)}
  await client.connect(new StdioClientTransport({command:process.execPath,args:[script,'--credential',f.file],cwd:here,stderr:'pipe'}));connected=true;
  f.setMode('reject409');const result=await client.callTool({name:'list_requests',arguments:{}});assert.equal(result.isError,true);assert.equal(result.content[0].text,'Peer request rejected (409): Answer your incoming peer request first.');
  f.setMode('altered');const invalid=await client.callTool({name:'list_requests',arguments:{}});assert.equal(invalid.isError,true);assert.equal(invalid.content[0].text,'Peer operation could not be confirmed. No request was retried.');
 }finally{if(connected)await client.close();await f.close()}
});
test('unsafe service credential startup prints no paths, credentials or stdout',async()=>{
 const f=await fixture();try{
  await fs.chmod(f.file,0o644);
  const result=await new Promise(resolve=>{const p=spawn(process.execPath,[script,'--credential',f.file],{cwd:here,stdio:['ignore','pipe','pipe']});let stdout='',stderr='';p.stdout.on('data',b=>stdout+=b);p.stderr.on('data',b=>stderr+=b);p.on('exit',code=>resolve({code,stdout,stderr}))});
  assert.notEqual(result.code,0);assert.equal(result.stdout,'');assert.equal(result.stderr,'WATCH shared bridge adapter could not start.\n');
  for(const privateValue of [f.root,f.file,f.credential.secret,f.credential.credential_id])assert.equal(result.stderr.includes(privateValue),false);
 }finally{await f.close()}
});
test('a relay on another port cannot forward the real proof and obtain a sensitive RPC body',async()=>{
 const f=await fixture(),seen=[],sockets=new Set();
 const relay=http.createServer(async(req,res)=>{
  let raw='';for await(const b of req)raw+=b;seen.push({path:req.url,raw});
  const upstream=http.request({host:'127.0.0.1',port:f.credential.port,path:req.url,method:'POST',headers:{'Content-Type':'application/json','Content-Length':Buffer.byteLength(raw)}},response=>{const chunks=[];response.on('data',b=>chunks.push(b));response.on('end',()=>{const body=Buffer.concat(chunks);res.writeHead(response.statusCode,{'Content-Length':body.length});res.end(body)})});upstream.on('error',()=>res.destroy());upstream.end(raw);
 });
 relay.on('connection',s=>{sockets.add(s);s.on('close',()=>sockets.delete(s))});await new Promise(r=>relay.listen(0,'127.0.0.1',r));
 try{
  const params={agent_id:'x:'+randomUUID(),identity_revision:'d'.repeat(64),request_id:randomUUID(),text:'Sensitive body must never reach this relay'};
  await assert.rejects(new PeerClient({...f.credential,port:relay.address().port}).call('send_message',params),/could not be confirmed/);
  assert.equal(seen.length,1);assert.equal(seen[0].path,'/bridge/v2/challenge');assert.equal(seen[0].raw.includes(params.text),false);assert.equal(f.seen.length,1);assert.equal(f.seen[0].path,'/bridge/v2/challenge');
 }finally{for(const s of sockets)s.destroy();await new Promise(r=>relay.close(r));await f.close()}
});
