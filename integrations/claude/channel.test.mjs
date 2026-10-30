import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import http from 'node:http';
import { randomUUID } from 'node:crypto';
import { spawn } from 'node:child_process';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import { z } from 'zod';
const here=path.dirname(new URL(import.meta.url).pathname),script=process.env.WATCH_CHANNEL_TEST_SCRIPT||path.join(here,'channel.mjs');
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function eventually(fn){for(let i=0;i<100;i++){const result=await fn();if(result)return result;await sleep(20)}throw new Error('Timed out waiting for channel state')}
async function fixture(){const parent=await fs.mkdtemp(path.join(await fs.realpath(os.tmpdir()),'watch-channel-'));await fs.chmod(parent,0o700);const session=randomUUID(),dir=path.join(parent,session);await fs.mkdir(dir,{mode:0o700});return {parent,session,dir}}
async function connect(f,options={}){
 const instance=options.instance||randomUUID(),events=[],client=new Client({name:'simulated-claude',version:'1.0'});
 client.setNotificationHandler(z.object({method:z.literal('notifications/claude/channel'),params:z.object({content:z.string(),meta:z.record(z.string(),z.string())})}),e=>{events.push(e);if(options.autoReply)client.callTool({name:'reply',arguments:{request_id:e.params.meta.request_id,text:'Immediate reply'}}).catch(()=>{})});
 const transport=new StdioClientTransport({command:process.execPath,args:[options.script||script],env:{...process.env,WATCH_CLAUDE_SESSION_ID:f.session,WATCH_CLAUDE_INSTANCE_ID:instance,WATCH_CLAUDE_STATE_DIR:f.dir},stderr:'pipe'});
 await client.connect(transport);const connection=await eventually(async()=>{try{const value=JSON.parse(await fs.readFile(path.join(f.dir,'connection.json'),'utf8'));assert.equal((await fs.stat(path.join(f.dir,'connection.json'))).nlink,1,'descriptor single-link at first parse');return value}catch(error){if(error.code==='ERR_ASSERTION')throw error;return null}});
 const headers={authorization:'Bearer '+connection.token,'Content-Type':'application/json'},url=`http://127.0.0.1:${connection.port}`;
 const payload=(id,content='How is the work progressing?')=>({session_id:f.session,instance_id:instance,request_id:id,content});
 const post=(body,extra={})=>fetch(url+'/requests',{method:'POST',headers:{...headers,...extra},body:typeof body==='string'?body:JSON.stringify(body)});
 const get=id=>fetch(url+'/requests/'+id,{headers});
 const reply=(id,text)=>client.callTool({name:'reply',arguments:{request_id:id,text}});
 async function close(){await client.close();await eventually(async()=>{try{await fs.stat(path.join(f.dir,'connection.json'));return false}catch{return true}})}
 return {client,transport,connection,events,instance,headers,url,payload,post,get,reply,close};
}
test('official SDK simulated Claude: real HTTP, notification, reply, identity, safety and cleanup',async()=>{
 const f=await fixture();let c;
 try{
 c=await connect(f);assert.equal(c.connection.version,1);assert.equal(c.connection.session_id,f.session);assert.equal(c.connection.instance_id,c.instance);assert.equal((await fs.stat(path.join(f.dir,'connection.json'))).mode&0o777,0o600);
 assert.equal((await fetch(c.url+'/requests/'+randomUUID())).status,401,'authentication rejects unauthenticated HTTP');
 assert.equal((await c.post(c.payload(randomUUID()),{origin:'null'})).status,403);
 assert.equal(await new Promise(resolve=>{const req=http.request(c.url+'/requests',{method:'POST',headers:{...c.headers,host:'localhost:'+c.connection.port}},res=>{res.resume();resolve(res.statusCode)});req.end(JSON.stringify(c.payload(randomUUID())))}),403);
 assert.equal((await c.post({...c.payload(randomUUID()),session_id:randomUUID()})).status,409);
 assert.equal((await c.post({...c.payload(randomUUID()),instance_id:randomUUID()})).status,409);
 assert.equal((await c.post(c.payload('not-a-uuid'))).status,400);
 assert.equal((await c.post(c.payload(randomUUID(),'x'.repeat(4001)))).status,400);
 assert.equal((await c.post('x'.repeat(16385))).status,413);
 const id=randomUUID(),payload=c.payload(id,'Implement the requested fix under existing permissions. '+ '🛠'.repeat(2000));assert.equal((await c.post(payload)).status,202);
 await eventually(()=>c.events.length===1);assert.equal(c.events[0].params.meta.request_id,id);assert.equal(c.events[0].params.meta.session_id,f.session);assert.equal(c.events[0].params.meta.instance_id,c.instance);assert.equal(c.events[0].params.content,payload.content,'explicit work instruction stays intact without restrictive suffix');assert.doesNotMatch(c.events[0].params.content,/grants no new execution authority/);
 assert.equal((await c.post(payload)).status,200,'identical request reuses durable acceptance');assert.equal((await c.post({...payload,content:'Different'})).status,409);await sleep(30);assert.equal(c.events.length,1,'duplicates never notify');
 assert.equal((await c.post(c.payload(randomUUID()))).status,429,'one pending question');
 assert.equal((await c.get(id)).status,200);assert.equal((await (await c.get(id)).json()).state,'transport_written');
 assert.equal((await c.client.listTools()).tools.length,1);assert.equal((await c.reply(randomUUID(),'wrong')).isError,true);assert.equal((await c.reply(id,'x'.repeat(16001))).isError,true);
 assert.equal((await c.reply(id,'Progress report')).isError,undefined);assert.equal((await c.reply(id,'Progress report')).isError,undefined);assert.equal((await c.reply(id,'Different')).isError,true);
 const answered=await (await c.get(id)).json();assert.equal(answered.reply,'Progress report','reply durable before tool acknowledgment');assert.equal(answered.state,'answered');assert.equal((await fs.stat(path.join(f.dir,id+'.json'))).mode&0o777,0o600);assert.equal(JSON.parse(await fs.readFile(path.join(f.dir,id+'.json'),'utf8')).reply,'Progress report','reply persisted before acknowledgment');
 // Busy agents receive the same notifications; these deterministic tests make no live busy claim.
 for(let i=0;i<9;i++){const next=randomUUID();assert.equal((await c.post(c.payload(next))).status,202);assert.equal((await c.reply(next,'Done')).isError,undefined)}
 assert.equal((await c.post(c.payload(randomUUID()))).status,429,'ten per minute');
 await c.close();c=null;assert.equal((await fs.stat(path.join(f.dir,id+'.json'))).isFile(),true,'cleanup preserves durable requests');
 }finally{if(c)await c.close();await fs.rm(f.parent,{recursive:true,force:true})}
});
test('restart reads completed replies, marks pending unknown and never re-notifies',async()=>{
 const f=await fixture();let c;
 try{
 c=await connect(f);const answeredID=randomUUID();await c.post(c.payload(answeredID));await c.reply(answeredID,'Durable answer');const old=c.instance,id=randomUUID();await c.post(c.payload(id));await eventually(()=>c.events.length===2);await c.close();
 c=await connect(f);assert.equal((await (await c.get(answeredID)).json()).reply,'Durable answer');assert.notEqual(c.instance,old);const recovered=await (await c.get(id)).json();assert.equal(recovered.state,'unknown','restart never claims delivery');assert.equal(recovered.instance_id,old);assert.equal(c.events.length,0);assert.equal((await c.post(c.payload(id))).status,409);assert.equal((await c.reply(id,'not mine')).isError,true);assert.equal((await c.post(c.payload(randomUUID()))).status,429);await c.close();c=null;
 }finally{if(c)await c.close();await fs.rm(f.parent,{recursive:true,force:true})}
});
async function exits(f,{script:target=script,instance=randomUUID()}={}){return await new Promise(resolve=>{const p=spawn(process.execPath,[target],{env:{...process.env,WATCH_CLAUDE_SESSION_ID:f.session,WATCH_CLAUDE_INSTANCE_ID:instance,WATCH_CLAUDE_STATE_DIR:f.dir},stdio:['pipe','pipe','pipe']});let output='';p.stdout.on('data',v=>output+=v);p.stderr.resume();p.on('exit',code=>resolve({code,output}));setTimeout(()=>p.kill(),3000).unref()})}
test('unsafe paths and preexisting discovery fail closed; nothing secret on stdout',async()=>{
 const f=await fixture();
 try{await fs.writeFile(path.join(f.dir,'connection.json'),'existing',{mode:0o600});assert.notEqual((await exits(f)).code,0);assert.equal(await fs.readFile(path.join(f.dir,'connection.json'),'utf8'),'existing');await fs.unlink(path.join(f.dir,'connection.json'));
 await fs.chmod(f.dir,0o755);assert.notEqual((await exits(f)).code,0);await fs.chmod(f.dir,0o700);
 await fs.chmod(f.parent,0o755);assert.notEqual((await exits(f)).code,0);await fs.chmod(f.parent,0o700);
 const link=path.join(f.parent,'linked');await fs.symlink(f.parent,link);assert.notEqual((await exits({...f,dir:path.join(link,f.session)})).code,0);
 const bad=await exits({...f,session:'invalid'});assert.notEqual(bad.code,0);assert.equal(bad.output,'');
 }finally{await fs.rm(f.parent,{recursive:true,force:true})}
});
test('256-record cap and one-day retention use durable files',async()=>{
 const f=await fixture();let c;
 try{for(let i=0;i<256;i++){const id=randomUUID();await fs.writeFile(path.join(f.dir,id+'.json'),JSON.stringify({session_id:f.session,instance_id:randomUUID(),request_id:id,content:'Old',reply:'Done',error:'',state:'answered',created_at:Date.now()-120000}),{mode:0o600})}
 c=await connect(f);assert.equal((await c.post(c.payload(randomUUID()))).status,429);await c.close();c=null;
 for(const file of await fs.readdir(f.dir)){if(file.endsWith('.json')){const r=JSON.parse(await fs.readFile(path.join(f.dir,file),'utf8'));r.created_at=Date.now()-86400001;await fs.writeFile(path.join(f.dir,file),JSON.stringify(r))}}
 c=await connect(f);assert.equal((await c.post(c.payload(randomUUID()))).status,202);await c.close();c=null;
 }finally{if(c)await c.close();await fs.rm(f.parent,{recursive:true,force:true})}
});
test('no discovery before initialized; disconnect shuts down; immediate reply cannot be overwritten',async()=>{
 const f=await fixture();let c,child;
 try{
 child=spawn(process.execPath,[script],{env:{...process.env,WATCH_CLAUDE_SESSION_ID:f.session,WATCH_CLAUDE_INSTANCE_ID:randomUUID(),WATCH_CLAUDE_STATE_DIR:f.dir},stdio:['pipe','pipe','pipe']});let output='';child.stdout.on('data',v=>output+=v);child.stderr.resume();
 child.stdin.write(JSON.stringify({jsonrpc:'2.0',id:1,method:'initialize',params:{protocolVersion:'2024-11-05',capabilities:{},clientInfo:{name:'handshake-test',version:'1'}}})+'\n');await eventually(()=>output.includes('"id":1'));await assert.rejects(fs.stat(path.join(f.dir,'connection.json')));assert.doesNotMatch(output,/"token"/);assert.match(output,/explicit work instructions authorize that work/);
 const ended=new Promise(resolve=>child.once('exit',resolve));child.stdin.end();await ended;child=null;
 c=await connect(f,{autoReply:true});const id=randomUUID();await c.post(c.payload(id));await eventually(async()=>(await (await c.get(id)).json()).state==='answered');await sleep(30);assert.equal((await (await c.get(id)).json()).reply,'Immediate reply');
 const url=c.url;await c.close();c=null;await assert.rejects(fetch(url+'/requests/'+id));
 }finally{if(child)child.kill();if(c)await c.close();await fs.rm(f.parent,{recursive:true,force:true})}
});
test('sensitivity: auth, duplicate suppression, persistence and restart mutations are detected',async()=>{
 const original=await fs.readFile(path.join(here,'channel.mjs'),'utf8');
 for(const [name,from,to,pattern,expected] of [
 ['auth','provided.length!==expected.length||!timingSafeEqual(provided,expected)','false','official SDK simulated','authentication rejects unauthenticated HTTP'],
 ['duplicates','if(existing) return','if(false) return','official SDK simulated','identical request reuses durable acceptance'],
 ['durability','await persist(answered);','/* omitted */','official SDK simulated','reply persisted before acknowledgment'],
 ['recovery',"record.state='unknown';","record.state='transport_written';",'restart reads','restart never claims delivery'],
 ['readiness','ready=true;await fd.writeFile','ready=false;await fd.writeFile','descriptor publication','descriptor must already accept requests']
 ]){
 assert.ok(original.includes(from));const dir=await fs.mkdtemp(path.join(here,'.mutation-')),target=path.join(dir,'channel.mjs');
 try{await fs.writeFile(target,original.replace(from,to));const result=await new Promise(resolve=>{const env={...process.env,WATCH_CHANNEL_TEST_SCRIPT:target};delete env.NODE_TEST_CONTEXT;const child=spawn(process.execPath,['--test','--test-name-pattern',pattern,path.join(here,'channel.test.mjs')],{env,stdio:['ignore','pipe','pipe']});let output='';child.stdout.on('data',b=>output+=b);child.stderr.on('data',b=>output+=b);child.on('exit',code=>resolve({code,output}))});assert.notEqual(result.code,0,name+' mutation was not detected');assert.match(result.output,/ERR_ASSERTION/);assert.ok(result.output.includes(expected),'mutation failed for wrong reason: '+result.output)}finally{await fs.rm(dir,{recursive:true,force:true})}
 }
});
test('descriptor publication is immediately usable, and shutdown preserves replacement discovery',async()=>{
 const f=await fixture();let c;
 try{
 c=await connect(f);assert.equal((await fs.stat(path.join(f.dir,'connection.json'))).nlink,1,'published descriptor has exactly one hard link');assert.equal((await c.post(c.payload(randomUUID()))).status,202,'descriptor must already accept requests');
 const replacement={...c.connection,instance_id:randomUUID()};await fs.writeFile(path.join(f.dir,'connection.json'),JSON.stringify(replacement),{mode:0o600});
 await c.client.close();c=null;assert.equal(JSON.parse(await fs.readFile(path.join(f.dir,'connection.json'),'utf8')).instance_id,replacement.instance_id,'cleanup never deletes another instance discovery');
 }finally{if(c)await c.close();await fs.rm(f.parent,{recursive:true,force:true})}
});
