// Production mod and production WATCH HTTP; native execution is explicitly mocked.
import assert from 'node:assert/strict';
import {register} from '../hooks/register.js';
let input='';for await(const part of process.stdin)input+=part;
const config=JSON.parse(input),url='http://127.0.0.1:'+config.port,storage=new Map();
const clone=value=>JSON.parse(JSON.stringify(value));
const pause=()=>new Promise(resolve=>setTimeout(resolve,10));
async function owner(path,body){
 const response=await fetch(url+path,{method:'POST',headers:{'Content-Type':'application/json','X-Live-Room-Key':config.owner_key},body:JSON.stringify(body)});
 const value=await response.json();assert(response.ok,JSON.stringify(value));return value;
}
async function runtime(native,index){
 const hooks=new Map(),timers=[],submissions=[];
 let api;
 const $={session:{id:async()=>native,cwd:async()=>config.project,surfaces:async()=>['desktop']},
  store:{keys:async()=>[...storage.keys()],get:async key=>storage.has(key)?clone(storage.get(key)):undefined,set:async(key,value)=>{storage.set(key,clone(value))}},
  command:{register:async()=>{}},clock:{now:async()=>Date.now(),every:(delay,fn)=>{const timer={fn,cancelled:false,cancel(){this.cancelled=true}};timers.push(timer);return timer}},
  http:{fetch:async(address,init)=>{const response=await fetch(address,init);return {ok:response.ok,status:response.status,headers:{},text:await response.text()}}},
  prompt:{submit:async value=>{
   assert.equal(value.asUser,false);submissions.push(value.text);
   const match=/^WATCH_REQUEST ([0-9a-f-]+)\n/.exec(value.text);assert(match);
   assert(value.text.includes(index===0?'Ask fictional Atlas.':'Ask fictional Birch.'),'request reached its intended runtime');
   const turn='fictional-turn-'+index;
   const text='The watch-owner plugin sent a message:\n'+value.text+"\n\nThis is how Claude Code surfaces a prompt a plugin submits between turns — it starts this turn in the user's place. Address the message above.";
   await api.call('turn.start',{text,turnId:turn});
   await api.call('turn.complete',{turnId:turn,answer:'WATCH_REPLY '+match[1]+'\n'+(index===0?'Fictional Atlas reply.':'Fictional Birch reply.'),isAborted:false,reason:'answer'});
   return {text:value.text};
  }}};
 register((name,handler)=>{hooks.set(name,handler);return {catch(){}}});
 api={submissions,call:async(name,value)=>hooks.get(name)?.($,value,async()=>({})),tick:async()=>{for(const t of timers.filter(t=>!t.cancelled))await t.fn()},status:async()=>JSON.parse((await api.call('command.run',{command:'watch-status',args:'',origin:{kind:'composer'}})).text)};
 await api.call('session.start',{cwd:config.project,surface:null,isInteractive:false});return api;
}
const runtimes=await Promise.all(config.native.map(runtime));
const setup=await runtimes[0].call('command.run',{command:'watch-setup',args:config.port+':'+config.code,origin:{kind:'composer'}});
assert(/authoriz|setup|connected/i.test(setup.text),setup.text);
async function until(check){
 for(let n=0;n<800;n++){
  await Promise.all(runtimes.map(r=>r.tick()));
  if(await check())return;
  await pause();
 }
 throw Error('Automatic global registration/delivery did not settle');
}
await until(async()=>(await Promise.all(runtimes.map(r=>r.status()))).every(r=>r.connected));
for(let index=0;index<2;index++)await owner('/agent-message',{agent:'c:'+config.native[index],question:index===0?'Ask fictional Atlas.':'Ask fictional Birch.',request_id:crypto.randomUUID()});
let replies;
await until(async()=>{
 const response=await fetch(url+'/agent-messages',{headers:{'X-Live-Room-Key':config.owner_key}});assert(response.ok);
 const value=await response.json();if(value.requests.length!==2||value.requests.some(r=>r.state!=='answered'))return false;
 replies=value.requests.map(r=>r.reply).sort();return true;
});
assert.deepEqual(replies,['Fictional Atlas reply.','Fictional Birch reply.']);
assert(runtimes.every(r=>r.submissions.length===1));
const revoked=await owner('/claude-mod-revoke',{});assert.equal(revoked.revoked,true);
console.log(JSON.stringify({authorized_once:true,native_submissions:runtimes.reduce((n,r)=>n+r.submissions.length,0),replies}));
