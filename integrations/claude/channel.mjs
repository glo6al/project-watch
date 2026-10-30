#!/usr/bin/env node
import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';
import { randomBytes, timingSafeEqual } from 'node:crypto';
import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { ListToolsRequestSchema, CallToolRequestSchema } from '@modelcontextprotocol/sdk/types.js';

const UUID=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const session=process.env.WATCH_CLAUDE_SESSION_ID, instance=process.env.WATCH_CLAUDE_INSTANCE_ID;
const directory=process.env.WATCH_CLAUDE_STATE_DIR;
if(!UUID.test(session||'')||!UUID.test(instance||'')||!directory||!path.isAbsolute(directory)) throw new Error('Explicit session, instance and absolute state directory required.');
const root=path.resolve(directory);
if(path.basename(root)!==session) throw new Error('State directory must match session ID.');
const connection=path.join(root,'connection.json'), starting=path.join(root,'.connection-starting'), DAY=86400000;
async function safeDirectory(){
  let current=path.parse(root).root;
  for(const part of root.slice(current.length).split(path.sep)){
    current=path.join(current,part);const st=await fs.lstat(current);
    if(!st.isDirectory()||st.isSymbolicLink()) throw new Error('State path must contain only real directories.');
    if(current===root||current===path.dirname(root)) if(st.uid!==process.getuid()||(st.mode&0o077)) throw new Error('State directory and parent must be private and owned by this user.');
  }
}
await safeDirectory();
try{await fs.lstat(connection);throw new Error('Existing channel discovery must not be replaced.')}catch(error){if(error.code!=='ENOENT')throw error}
const token=randomBytes(32).toString('hex'), records=new Map();let ready=false, stopping=false, published=false, reservation=null, accepted=[];
async function privateRead(file){const st=await fs.lstat(file);if(!st.isFile()||st.isSymbolicLink()||st.uid!==process.getuid()||(st.mode&0o077)||st.size>100000) throw new Error('Unsafe state file.');return JSON.parse(await fs.readFile(file,'utf8'))}
async function syncDirectory(){const fd=await fs.open(root,'r');try{await fd.sync()}finally{await fd.close()}}
async function persist(record){
  await safeDirectory();const file=path.join(root,record.request_id+'.json'),tmp=file+'.'+randomBytes(8).toString('hex')+'.tmp';
  const fd=await fs.open(tmp,'wx',0o600);
  try{await fd.writeFile(JSON.stringify(record));await fd.sync()}finally{await fd.close()}
  try{await fs.rename(tmp,file);await syncDirectory()}finally{await fs.rm(tmp,{force:true})}
}
async function loadRecords(){
for await(const entry of await fs.opendir(root)){
  if(!/^[0-9a-f-]{36}\.json$/i.test(entry.name)) continue;
  if(records.size>=256) throw new Error('Too many retained requests.');
  const record=await privateRead(path.join(root,entry.name));
  if(!UUID.test(record.request_id||'')||entry.name!==record.request_id+'.json'||record.session_id!==session||!UUID.test(record.instance_id||'')||typeof record.content!=='string'||Array.from(record.content).length>4000||!Number.isFinite(record.created_at)||!['unknown','transport_written','answered'].includes(record.state)||typeof record.reply!=='string'||Array.from(record.reply).length>16000) throw new Error('Invalid durable request.');
  if(Date.now()-record.created_at>DAY){await fs.unlink(path.join(root,entry.name));continue}
  if(record.state!=='answered'){record.state='unknown';record.error='Channel restarted; no notification was repeated.';await persist(record)}
  records.set(record.request_id,record);
}
accepted=[...records.values()].map(r=>r.created_at).filter(at=>Date.now()-at<60000);
}
const publicRecord=r=>({session_id:r.session_id,instance_id:r.instance_id,request_id:r.request_id,state:r.state,reply:r.reply,error:r.error});
function bounded(promise,ms){let timer;return Promise.race([promise,new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('Transport write timed out.')),ms)})]).finally(()=>clearTimeout(timer))}
let serial=Promise.resolve();
function exclusive(fn){const next=serial.then(fn);serial=next.catch(()=>{});return next}
const mcp=new Server({name:'watch',version:'1.0.0'},{capabilities:{experimental:{'claude/channel':{}},tools:{}},instructions:'WATCH relays the owner’s request: explicit work instructions authorize that work under existing permissions; status questions ask only for information. Do not add tasks or permissions. Use reply with the exact request_id to report the outcome.'});
mcp.setRequestHandler(ListToolsRequestSchema,async()=>({tools:[{name:'reply',description:'Return the outcome of a known WATCH request using its exact request_id.',inputSchema:{type:'object',properties:{request_id:{type:'string'},text:{type:'string',maxLength:16000}},required:['request_id','text'],additionalProperties:false}}]}));
mcp.setRequestHandler(CallToolRequestSchema,({params})=>exclusive(async()=>{
  const args=params.arguments||{}, r=records.get(args.request_id);
  const fail=text=>({isError:true,content:[{type:'text',text}]});
  if(params.name!=='reply'||!r||r.instance_id!==instance||typeof args.text!=='string'||!args.text.length||Array.from(args.text).length>16000||Object.keys(args).some(k=>!['request_id','text'].includes(k))) return fail('Unknown or invalid request/reply.');
  if(r.state==='answered') return r.reply===args.text?{content:[{type:'text',text:'Reply already recorded.'}]}:fail('A different reply was already recorded.');
  const answered={...r,state:'answered',reply:args.text,error:''};await persist(answered);records.set(r.request_id,answered);
  return {content:[{type:'text',text:'Reply recorded.'}]};
}));
const web=http.createServer({maxHeaderSize:8192},async(req,res)=>{
  const finish=(status,v)=>{res.writeHead(status,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify(v))};
  const provided=Buffer.from(req.headers.authorization||''),expected=Buffer.from('Bearer '+token);
  if(provided.length!==expected.length||!timingSafeEqual(provided,expected)) return finish(401,{error:'Unauthorized.'});
  if(req.headers.origin!==undefined) return finish(403,{error:'Browser origins not accepted.'});
  if(req.headers.host!==`127.0.0.1:${web.address()?.port}`) return finish(403,{error:'Invalid host.'});
  const route=req.url||'';
  if(req.method==='GET'&&/^\/requests\/[0-9a-f-]{36}$/i.test(route)){
    const id=route.slice('/requests/'.length),r=records.get(id);return r?finish(200,publicRecord(r)):finish(404,{error:'Unknown request.'});
  }
  if(req.method!=='POST'||route!=='/requests') return finish(404,{error:'Not found.'});
  try{
    let size=0;const chunks=[];
    for await(const chunk of req){size+=chunk.length;if(size>16384){finish(413,{error:'Request too large.'});req.resume();return}chunks.push(chunk)}
    const value=JSON.parse(Buffer.concat(chunks).toString('utf8'));
    if(!value||typeof value!=='object'||Array.isArray(value)||Object.keys(value).some(k=>!['session_id','instance_id','request_id','content'].includes(k))||!UUID.test(value.request_id||'')||typeof value.content!=='string'||!value.content.trim()||Array.from(value.content).length>4000) return finish(400,{error:'Invalid request.'});
    if(value.session_id!==session||value.instance_id!==instance) return finish(409,{error:'Session or instance mismatch.'});
    const result=await exclusive(async()=>{
      const existing=records.get(value.request_id);
      if(existing) return existing.session_id===value.session_id&&existing.instance_id===value.instance_id&&existing.content===value.content?[200,publicRecord(existing)]:[409,{error:'Request ID already binds a different payload.'}];
      if(!ready||stopping) return [503,{error:'Channel is disconnected.'}];
      const now=Date.now();for(const [id,r] of records) if(now-r.created_at>DAY){await fs.unlink(path.join(root,id+'.json'));records.delete(id)}
      accepted=accepted.filter(at=>now-at<60000);
      if(records.size>=256||accepted.length>=10||[...records.values()].some(r=>r.state!=='answered')) return [429,{error:'Channel request limit reached.'}];
      let r={...value,created_at:now,state:'unknown',reply:'',error:'Notification delivery is not confirmed.'};
      await persist(r);records.set(r.request_id,r);accepted.push(now);
      try{await bounded(mcp.notification({method:'notifications/claude/channel',params:{content:value.content,meta:{request_id:r.request_id,session_id:session,instance_id:instance}}}),5000);r={...r,state:'transport_written',error:''};await persist(r);records.set(r.request_id,r)}catch{ /* Persisted unknown survives restart; never retry notification. */ }
      return [202,publicRecord(records.get(r.request_id))];
    });finish(...result);
  }catch{finish(400,{error:'Request could not be accepted.'})}
});
web.requestTimeout=10000;web.headersTimeout=5000;web.timeout=10000;web.keepAliveTimeout=1000;web.maxConnections=32;
async function stop(){
  if(stopping)return;stopping=true;ready=false;web.close();web.closeAllConnections();
  await serial;
  if(published){try{const c=await privateRead(connection);if(c.instance_id===instance&&c.token===token) await fs.unlink(connection)}catch{}}
  if(reservation){try{const st=await fs.lstat(starting);if(st.ino===reservation.ino&&st.dev===reservation.dev)await fs.unlink(starting)}catch{}}
  await mcp.close();process.exit(0);
}
mcp.oninitialized=()=>{(async()=>{
  if(stopping)return;await safeDirectory();const lock=await fs.open(starting,'wx',0o600);
  try{reservation=await lock.stat();await lock.writeFile(JSON.stringify({instance_id:instance,pid:process.pid}));await lock.sync()}finally{await lock.close()}
  await loadRecords();await new Promise((resolve,reject)=>{web.once('error',reject);web.listen(0,'127.0.0.1',resolve)});
  const temporary=path.join(root,'.connection-'+randomBytes(8).toString('hex')+'.tmp');
  try{
    const fd=await fs.open(temporary,'wx',0o600);
    try{ready=true;await fd.writeFile(JSON.stringify({version:1,session_id:session,instance_id:instance,port:web.address().port,token}));await fd.sync()}finally{await fd.close()}
    // All cooperating channel instances hold the exclusive reservation before publication.
    // Recheck absence; rename publishes complete data with exactly one hard link.
    try{await fs.lstat(connection);throw new Error('Discovery appeared during startup.')}catch(error){if(error.code!=='ENOENT')throw error}
    await fs.rename(temporary,connection);published=true;await syncDirectory();await fs.unlink(starting);
  }finally{await fs.rm(temporary,{force:true})}
})().catch(()=>{console.error('WATCH channel initialization failed; discovery was not replaced.');stop()})};
mcp.onclose=()=>{stop()};
process.on('SIGTERM',stop);process.on('SIGINT',stop);process.stdin.on('end',stop);
await mcp.connect(new StdioServerTransport());
