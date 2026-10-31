#!/usr/bin/env node
/* The page's path helpers, run outside a browser. The block between the two markers in src/web/index.html is
   evaluated with a stub preview key, once as a macOS or demo page (paths split on / only) and once as a page served
   by a Windows server (either slash). Run: node tests/test_page.js */
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm');
const html = fs.readFileSync(path.join(__dirname, '..', 'src', 'web', 'index.html'), 'utf8');
const a = html.indexOf('/* path helpers'), b = html.indexOf('/* end path helpers */');
if (a < 0 || b < 0 || b < a) throw new Error('the path helper markers are missing from index.html');
const block = html.slice(html.indexOf('\n', a) + 1, b);
/* Run in a fresh, empty context: the block sees only PK, not Node's globals (process, require), and nothing leaks out but the
   four helpers. The code is this repository's own page, never input from outside. */
const load = win => vm.runInNewContext(block + ';WINHOST=' + win + ';({short,tail2,projName,fsUrl})', { PK: 'k' });
const check = (name, cond) => { if (!cond) { console.log('FAIL ' + name); process.exit(1); } console.log('ok', name); };

const mac = load(false);
check('macOS: a backslash in a name is part of the name',
  mac.tail2('/Users/me/a\\b') === 'me/a\\b' && mac.short('/Users/me/x/y/a\\b\\c') === '…/x/y/a\\b\\c' && mac.projName('/Users/me/a\\b') === 'a\\b');
check('macOS: a name that looks like a drive is still a name', mac.projName('/Users/me/C:\\notes/') === 'C:\\notes' && mac.tail2('/Users/me/C:\\notes') === 'me/C:\\notes');
check('macOS: Projects folder wins, else the last name', mac.projName('/Users/me/Projects/site/x') === 'site' && mac.projName('/Users/me/work/site/') === 'site' && mac.projName('') === '');
check('macOS: short keeps the last three names', mac.short('/a/b/c/d/e.css') === '…/c/d/e.css' && mac.short('/a/b/c') === '/a/b/c');
check('macOS: preview address', mac.fsUrl('/Users/me/site/index.html') === '/fs/k/Users/me/site/index.html');
check('macOS: preview address is encoded', mac.fsUrl('/Users/me/my site/a"b.html') === '/fs/k/Users/me/my%20site/a%22b.html' && mac.fsUrl('/Users/me/C:\\x.html') === '/fs/k/Users/me/C%3A%5Cx.html');

const win = load(true);
check('Windows: either slash splits', win.tail2('C:\\Users\\me\\site\\a.css') === 'site/a.css' && win.tail2('C:/Users/me/site/a.css') === 'site/a.css');
check('Windows: short keeps the last three names', win.short('C:\\Users\\me\\x\\y\\z.css') === '…/x/y/z.css');
check('Windows: project name', win.projName('C:\\Users\\me\\Projects\\site') === 'site' && win.projName('C:\\Users\\me\\work\\site\\') === 'site' && win.projName('C:/Users/me/site') === 'site');
check('Windows: a UNC path splits too', win.tail2('\\\\srv\\share\\p\\f.css') === 'p/f.css');
check('Windows: preview address is a drive path written with /', win.fsUrl('C:\\Users\\me\\site\\index.html') === '/fs/k/C%3A/Users/me/site/index.html');
console.log('all ok');

/* The Officer's answer markup, run the same way: only the tags the code chooses, no attribute from the text, every
   list closed in order, and no input that can stall the parser. */
{
  const a2 = html.indexOf('/* answer markup'), b2 = html.indexOf('/* end answer markup */');
  if (a2 < 0 || b2 < 0 || b2 < a2) throw new Error('the answer markup markers are missing from index.html');
  const esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const md = vm.runInNewContext(html.slice(html.indexOf('*/', a2) + 2, b2) + ';ofParas', { esc });
  const tags = h => (h.match(/<\/?([a-z0-9]+)/g) || []).map(x => x.replace(/[<\/]/g, ''));
  const balanced = h => { const st = []; for (const m of h.matchAll(/<(\/?)([a-z0-9]+)[^>]*>/g)) { if (m[2] === 'hr') continue; if (m[1]) { if (st.pop() !== m[2]) return false; } else st.push(m[2]); } return st.length === 0; };
  const attrs = h => (h.match(/<[a-z0-9]+\s[^>]*>/g) || []).filter(x => !/^<(p|span) class="[a-z0-9]+">$/.test(x));
  const timed = (s) => { const t0 = Date.now(); const h = md(s); return [h, Date.now() - t0]; };
  check('answer: a README lays out as tables, lists, quotes, code and headings',
    (() => { const h = md('# T\n| a | b |\n|---|---|\n| **1** | `2` |\n\n- [ ] x\n  - y\n1. z\n> q\n---\n```\n<b>\n```'); return /<table>.*<\/table>/.test(h) && /<p class="h1">T<\/p>/.test(h) && /<ul><li><span class="box">☐<\/span> x<ul><li>y<\/li><\/ul><\/li><\/ul><ol><li>z<\/li><\/ol>/.test(h) && /<blockquote><p>q<\/p><\/blockquote><hr>/.test(h) && /<pre><code>&lt;b&gt;<\/code><\/pre>/.test(h) && balanced(h); })());
  check('answer: nothing from the text becomes an attribute, a script, a link or an image',
    (() => { const h = md('<script>alert(1)</script> <a href=x>y</a> <img src=x onerror=alert(1)> [docs](https://e.example/x) ![p](https://e.example/p.png) `<i>` **<b>**'); return attrs(h).length === 0 && !/<(script|a|img)\b/.test(h) && /docs \(https:\/\/e\.example\/x\)/.test(h) && /image not shown: https:\/\/e\.example\/p\.png/.test(h) && /<code>&lt;i&gt;<\/code>/.test(h); })());
  check('answer: crossed emphasis stays as typed; nested emphasis nests',
    md('**a ~~b** c~~') === '<p><b>a ~~b</b> c~~</p>' && md('x **a ~~b~~ c**') === '<p>x <b>a <s>b</s> c</b></p>' && md('**a line alone**') === '<p class="h2">a line alone</p>' && md('2*3*4') === '<p>2*3*4</p>');
  check('answer: a change of list kind at either depth closes the list it leaves',
    (() => { const h = md('- root\n  - bullet\n  1. numbered\n- back'); return h === '<ul><li>root<ul><li>bullet</li></ul><ol><li>numbered</li></ol></li><li>back</li></ul>' && balanced(h); })());
  check('answer: odd line terminators inside a list item cannot stall the parser',
    (() => { for (const s of ['- a b', '1. a b', '- a\rb', '- a\r\n  - b\r\n1. c', '- - x']) { const [h, ms] = timed(s); if (ms > 200 || !balanced(h)) return false; } return true; })());
  check('answer: an unclosed fence and a lone separator are harmless',
    balanced(md('```\nabc')) && md('|---|---|') === '<p>|---|---|</p>' && balanced(md('| a |\n|---|\n| 1 | 2 | 3 |\n| 4 |')));
  check('answer: a 4000-character answer renders in well under a second',
    (() => { const [h, ms] = timed(('**a** *b* ~~c~~ `d` ' + '*'.repeat(40) + ' ~'.repeat(40) + '\n- x\n').repeat(40).slice(0, 4000)); return balanced(h) && ms < 500; })());
}
console.log('answer markup ok');


/* Execute real message helpers; all network responses and DOM nodes are local test doubles. */
const messageStart=html.indexOf('/* agent message helpers:'),messageEnd=html.indexOf('/* end agent message helpers */');
const messageCode=html.slice(html.indexOf('*/',messageStart)+2,messageEnd),assert=require('assert/strict');
function messaging(code=messageCode,storage=new Map()){
  const cards=[],gets=[];
  class Card{
    set innerHTML(v){this.paints=(this.paints||0)+1;this.html=v;this.button=v.includes('class="message-check"')?{}:null}
    get innerHTML(){return this.html}
    querySelector(s){return s==='.message-check'?this.button:null}
  }
  const ctx={DEMO:false,KEY:'test-key',claiming:false,connected:true,OF:{loaded:true},AbortSignal:{timeout:ms=>({timeout:ms})},
    store:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)},
    esc:s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
    ofAdd:(cls,h)=>{const el=new Card();el.className=cls;el.innerHTML=h;cards.push(el);return el},
    fetch:async(p,o)=>{gets.push({p,o});return ctx.response},
    response:{ok:true,json:async()=>({requests:[],available:true})}};
  ctx.ofParas=s=>'<p>'+ctx.esc(s)+'</p>';
  vm.createContext(ctx);const api=vm.runInContext(code+';({OFM,ofMessagePoll,ofMessageMerge,ofMessageLoad,ofControlMerge})',ctx);
  return {...api,ctx,cards,gets,storage};
}
const request={request_id:'12345678-1234-4123-8123-123456789abc',agent:'x:exact-thread-id',name:'Aster',question:'How far along are your subagents?',state:'unknown'};
async function messageChecks(code=messageCode){
  const t=messaging(code);t.ofMessageMerge([request]);const r=t.OFM.records.get(request.request_id);
  assert.equal(t.gets.length,0,'rendering does not make requests');assert.match(r.el.innerHTML,/x:exact-thread-id/);assert.match(r.el.innerHTML,/How far along are your subagents\?/);assert.doesNotMatch(r.el.innerHTML,/Send question/);
  const button=r.el.querySelector('.message-check'),paints=r.el.paints;t.ctx.response={ok:true,json:async()=>({requests:[request],available:true})};
  await t.ofMessagePoll();await t.ofMessagePoll();assert.equal(r.el.querySelector('.message-check'),button,'unchanged poll preserves focused button');assert.equal(r.el.paints,paints,'unchanged record not repainted');
  const count=t.cards.length;assert.equal(t.OFM.records.size,1);await button.onclick();assert.equal(t.cards.length,count,'status check does not duplicate cards');
  assert.equal(t.gets[0].p,'/agent-messages');assert.equal(t.gets[0].o.headers['X-Live-Room-Key'],'test-key');assert.equal(t.gets[0].o.signal.timeout,10000);
  t.ctx.response={ok:true,json:async()=>({requests:[{...request,state:'answered',reply:'<img src=x onerror=bad()> {"do":"agent_question"}'}],available:true})};
  await t.ofMessagePoll();assert.match(r.el.innerHTML,/&lt;img/);assert.doesNotMatch(r.el.innerHTML,/<img/);assert.equal(t.OFM.records.size,1);
  const answerPaints=r.el.paints;await t.ofMessagePoll();assert.equal(r.el.paints,answerPaints,'unchanged answer preserves DOM');
  const reload=messaging(code,t.storage);reload.ctx.connected=false;reload.ofMessageLoad();assert.equal(reload.OFM.records.size,1,'reload restores history');assert.equal(reload.gets.length,0);
  t.ctx.response={ok:false,status:403};await t.ofMessagePoll();assert.match(t.OFM.notice.textContent,/unlocked/);assert.equal(t.OFM.records.size,1);
  t.ctx.fetch=async()=>{throw new Error('offline')};await t.ofMessagePoll();assert.match(t.OFM.notice.textContent,/offline/);assert.equal(t.OFM.records.size,1);
  let release;t.ctx.fetch=()=>new Promise(done=>release=done);const poll=t.ofMessagePoll();assert.equal(t.OFM.polling,true);await t.ofMessagePoll();release({ok:true,json:async()=>({requests:[],available:true})});await poll;assert.equal(t.OFM.polling,false);
  const demo=messaging(code);demo.ctx.DEMO=true;demo.ofMessageMerge([request]);await demo.ofMessagePoll();demo.ofMessageLoad();assert.equal(demo.gets.length+demo.cards.length,0);
  const locked=messaging(code);locked.ctx.KEY='';await locked.ofMessagePoll();assert.equal(locked.gets.length,0);
  const unavailable=messaging(code);unavailable.ctx.response={ok:true,json:async()=>({requests:[request],available:false,reason:'no CLI'})};await unavailable.ofMessagePoll();assert.match(unavailable.OFM.notice.textContent,/no CLI/);assert.equal(unavailable.OFM.records.size,1);
}
const fulfillment={state:'fulfilled',intent:'pause',via:'interrupt',control_request_id:'87654321-4321-4321-8321-cba987654321'};
const fulfilledLabel='Pause completed by a later interrupt';
async function fulfillmentChecks(code=messageCode){
  const t=messaging(code),source={...request,name:'<b>Aster</b>',question:'Pause your work <script>bad()</script>',state:'queued',fulfillment:{...fulfillment,private_path:'/private/example',expected_turn_id:'private-turn',completed_at:'private-time'}};
  t.ofMessageMerge([source]);const r=t.OFM.records.get(request.request_id),card=r.el;
  assert.ok(card.innerHTML.includes(fulfilledLabel),'validated evidence shows historical completion');
  assert.ok(card.innerHTML.indexOf(fulfilledLabel)<card.innerHTML.indexOf('Message to'),'historical completion leads the card');
  assert.match(card.innerHTML,/Original message was queued; delivery is not confirmed\. It may still run later\./);
  assert.doesNotMatch(card.innerHTML,/still queued|currently paused|removed|cancelled|private-turn|private-time|\/private\/example/);
  assert.match(card.innerHTML,/&lt;b&gt;Aster/);assert.match(card.innerHTML,/Pause your work &lt;script&gt;/);assert.doesNotMatch(card.innerHTML,/<script>|<b>Aster/);
  assert.deepEqual(Object.keys(r.fulfillment).sort(),Object.keys(fulfillment).sort(),'only public fulfillment fields retained');
  const saved=JSON.parse(t.storage.get('liveRoomMessages'))[0];assert.deepEqual(saved.fulfillment,fulfillment);assert.doesNotMatch(JSON.stringify(saved),/private_path|expected_turn_id|completed_at/);
  const paints=card.paints;t.ctx.response={ok:true,json:async()=>({requests:[source],available:true})};await t.ofMessagePoll();await t.ofMessagePoll();assert.equal(card.paints,paints,'unchanged fulfillment polling preserves DOM');assert.equal(t.cards.filter(c=>c.className==='of message').length,1);assert.equal(t.gets.length,2,'one GET per poll');
  const reload=messaging(code,t.storage);reload.ctx.connected=false;reload.ofMessageLoad();assert.match(reload.OFM.records.get(request.request_id).el.innerHTML,/Pause completed by a later interrupt/);assert.equal(reload.gets.length,0);
  const labels={sending:'Original message was sending; delivery is not confirmed.',awaiting_reply:'Original message was delivered; awaiting a reply.',unknown:'Original message delivery is unknown; do not resend.',expired:'Original message expired; delivery may have occurred. No automatic resend.',failed:'Original message failed; no automatic resend.',answered:'Original message received a reply.',future_state:'Original message delivery is unknown; do not resend.'};
  for(const [state,label] of Object.entries(labels)){
    t.ofMessageMerge([{...source,state,reply:'Late matching reply <img src=x>'}]);assert.ok(card.innerHTML.includes(label),state+' keeps transport separate');assert.ok(card.innerHTML.includes(fulfilledLabel));assert.equal(r.state,state,'fulfillment never changes transport');
    if(state==='answered'){assert.match(card.innerHTML,/Reply from/);assert.match(card.innerHTML,/Late matching reply &lt;img/);assert.doesNotMatch(card.innerHTML,/<img/);assert.match(card.innerHTML,/Pause your work &lt;script&gt;/)}
  }
  for(const invalid of [undefined,null,{},'fulfilled',[],{...fulfillment,state:'unknown'},{...fulfillment,intent:'resume'},{...fulfillment,via:'steer'},{...fulfillment,control_request_id:'bad'},{...fulfillment,control_request_id:'<img src=x>'},{...fulfillment,control_request_id:42}]){
    t.ofMessageMerge([source]);t.ofMessageMerge([{...request,fulfillment:invalid}]);assert.equal(r.fulfillment,null,'invalid or missing snapshot annotation clears cached fulfillment');assert.doesNotMatch(card.innerHTML,/Pause completed/,'invalid annotation never claims success');
    const cached=new Map([['liveRoomMessages',JSON.stringify([{...request,fulfillment:invalid,private_path:'/private/example'}])]]),badReload=messaging(code,cached);badReload.ctx.connected=false;badReload.ofMessageLoad();assert.doesNotMatch(badReload.OFM.records.get(request.request_id).el.innerHTML,/Pause completed/);assert.doesNotMatch(cached.get('liveRoomMessages'),/private_path/);
  }
  // A server snapshot is authoritative even after valid local history has loaded.
  reload.ctx.connected=true;reload.ctx.response={ok:true,json:async()=>({requests:[request],available:true})};await reload.ofMessagePoll();assert.doesNotMatch(reload.OFM.records.get(request.request_id).el.innerHTML,/Pause completed/);assert.equal(JSON.parse(reload.storage.get('liveRoomMessages'))[0].fulfillment,null);
}
const control={request_id:'abcdef12-1234-4123-8123-123456789abc',action:'interrupt',agent:'x:exact-thread-id',name:'Aster',instruction:'Pause work',state:'interrupted',error:'',created:1800000000};
async function controlChecks(code=messageCode){
  // The POST response was entirely lost: neither the page nor its cache knows the control ID.
  const t=messaging(code),reload=messaging(code,t.storage);reload.ctx.connected=false;reload.ofMessageLoad();assert.equal(reload.OFM.controls.size,0);
  reload.ctx.connected=true;reload.ctx.response={ok:true,json:async()=>({requests:[],controls:[control],available:true})};await reload.ofMessagePoll();
  const r=reload.OFM.controls.get(control.request_id);assert.ok(r,'GET recovers a control absent from the original response/cache');assert.match(r.el.innerHTML,/targeted turn was interrupted/);assert.doesNotMatch(r.el.innerHTML,/remains paused<|<button/);
  assert.equal(reload.gets.length,1);assert.ok(reload.gets.every(g=>g.p==='/agent-messages'&&!g.o.method),'recovery never POSTs');
  const paints=r.el.paints;await reload.ofMessagePoll();assert.equal(r.el.paints,paints);assert.equal(reload.OFM.controls.size,1);assert.equal(reload.cards.filter(c=>c.className==='of control').length,1);
  reload.ofControlMerge([{...control,state:'sending'}]);assert.equal(r.state,'interrupted','late POST sending cannot regress outcome');
  reload.ctx.response={ok:true,json:async()=>({requests:[],controls:[{...control,state:'sending'}],available:true})};await reload.ofMessagePoll();assert.equal(r.state,'interrupted','stale snapshot cannot regress outcome');
  const again=messaging(code,reload.storage);again.ctx.connected=false;again.ofMessageLoad();assert.equal(again.OFM.controls.get(control.request_id).state,'interrupted');
  reload.ofControlMerge([],{snapshot:true});assert.match(r.el.innerHTML,/Recorded outcome: the targeted turn was interrupted/,'retention does not erase historical evidence');assert.match(r.el.innerHTML,/does not guarantee/);
  const blank=messaging(code);blank.ofControlMerge([{...control,instruction:''}]);assert.doesNotMatch(blank.OFM.controls.get(control.request_id).el.innerHTML,/class="draft"/,'interrupt has no blank draft');
  const steer=messaging(code);steer.ofControlMerge([{...control,action:'steer',state:'acknowledged',instruction:'Focus on tests'}]);assert.match(steer.OFM.controls.get(control.request_id).el.innerHTML,/<div class="draft">Focus on tests<\/div>/);
  const labels={sending:/Control pending/,unknown:/outcome is unknown/,no_op:/no interruption occurred/,failed:/control failed/,interrupted:/turn was interrupted/,acknowledged:/steer acknowledged; execution is not confirmed/};
  for(const [state,label] of Object.entries(labels)){
    const c=messaging(code);c.ofControlMerge([{...control,state,action:state==='acknowledged'?'steer':'interrupt'}]);const row=c.OFM.controls.get(control.request_id);assert.match(row.el.innerHTML,label);assert.doesNotMatch(row.el.innerHTML,/<button/);
    if(state==='sending'||state==='unknown'){
      const loaded=messaging(code,c.storage);loaded.ctx.connected=false;loaded.ofMessageLoad();const cached=loaded.OFM.controls.get(control.request_id);
      if(state==='sending'){
        assert.match(cached.el.innerHTML,/uncertain after reload/);loaded.ofControlMerge([{...control,state:'sending'}],{snapshot:true});assert.match(cached.el.innerHTML,/Control pending/,'authoritative sending refreshes local uncertainty');
      }else{loaded.ofControlMerge([{...control,state:'sending'}]);assert.equal(cached.state,'unknown','authoritative unknown is not a restored pending record')}
      loaded.ofControlMerge([],{snapshot:true});assert.match(cached.el.innerHTML,/no longer available from the server/);assert.doesNotMatch(cached.el.innerHTML,/Recorded outcome/);
    }
  }
  const malicious={...control,name:'<img src=x>',instruction:'<script>bad()</script>',error:'<b>bad</b>',private_path:'/private/example',expected_turn_id:'secret-turn',transcript:'secret-transcript'};
  const c=messaging(code);c.ofControlMerge([malicious]);const safe=c.OFM.controls.get(control.request_id);assert.match(safe.el.innerHTML,/&lt;img/);assert.match(safe.el.innerHTML,/&lt;script/);assert.match(safe.el.innerHTML,/&lt;b/);assert.doesNotMatch(safe.el.innerHTML,/<img|<script|secret-turn|secret-transcript/);
  assert.doesNotMatch(c.storage.get('liveRoomControls'),/private_path|expected_turn_id|transcript/);assert.equal(Object.hasOwn(safe,'private_path'),false);
  for(const invalid of [{...control,request_id:'bad'},{...control,action:'resume'},{...control,state:'paused'},{...control,action:'steer',state:'interrupted'},{...control,created:'yesterday'},{...control,instruction:42}]){
    const cached=new Map([['liveRoomControls',JSON.stringify([invalid])]]),bad=messaging(code,cached);bad.ctx.connected=false;bad.ofMessageLoad();assert.equal(bad.OFM.controls.size,0,'malformed cache cannot claim control success');
  }
  c.ofMessageMerge([{...request,delivery_warning:'Older <img> messages may still run; they are not cancelled.'}]);assert.match(c.OFM.records.get(request.request_id).el.innerHTML,/Older &lt;img&gt;/);assert.doesNotMatch(c.OFM.records.get(request.request_id).el.innerHTML,/<img/);assert.match(c.storage.get('liveRoomMessages'),/delivery_warning/);
  const cachedPrivate=new Map([['liveRoomControls',JSON.stringify([malicious])]]),privateReload=messaging(code,cachedPrivate);privateReload.ctx.connected=false;privateReload.ofMessageLoad();assert.equal(privateReload.OFM.controls.size,1);assert.doesNotMatch(cachedPrivate.get('liveRoomControls'),/private_path|expected_turn_id|transcript/);
  const warningReload=messaging(code,c.storage);warningReload.ctx.connected=false;warningReload.ofMessageLoad();assert.match(warningReload.OFM.records.get(request.request_id).el.innerHTML,/Older &lt;img&gt;/);
  const showCode=html.slice(html.indexOf('function ofShow(r){'),html.indexOf('function ofDemoAnswer(q)'));
  const shown=messaging(code);shown.ctx.ofAdd=()=>({querySelectorAll:()=>[]});shown.ctx.ofCite=()=>'';shown.ctx.ofAct=()=>[false,''];shown.ctx.OF={};vm.runInContext(showCode+';ofShow({text:"recorded",controls:['+JSON.stringify(control)+']})',shown.ctx);assert.equal(shown.OFM.controls.size,1,'Officer response merges control even without polling');
}
(async()=>{
  await controlChecks();console.log('ok controls: lost response recovery, outcomes, reload uncertainty, stale races, retention and privacy');
  await fulfillmentChecks();console.log('ok fulfillment: historical status, separate delivery, late reply, allowlist, reload and authoritative clearing');
  await messageChecks();console.log('ok messaging: server results, status checks, correlation, durable history, escaping, offline/auth, demo and timeout');
  for(const [name,from,to] of [
    ['poll duplicates','let r=OFM.records.get(v.request_id);','let r=null;'],
    ['unsafe reply',"ofParas(String(r.reply||''))","String(r.reply||'')"],
    ['lost reload','ofMessageMerge(rows.slice(-100)','void(rows.slice(-100'],
    ['unbounded poll','AbortSignal.timeout(10000)','undefined'],
    ['poll repaints','if(r.painted!==markup)','if(true)']
  ]){assert.ok(messageCode.includes(from));let failed=false;try{await messageChecks(messageCode.replace(from,to))}catch(e){failed=true}assert.ok(failed,name+' must fail tests');console.log('ok mutation detected:',name)}
  for(const [name,from,to] of [
    ['lost control recovery','ofControlMerge(v.controls,{snapshot:true})','void(v.controls)'],
    ['stale control regression',"if(r.state&&r.state!=='sending'&&v.state==='sending') continue;",''],
    ['invalid control state','if(!states.includes(v.state)) return null;','']
  ]){assert.ok(messageCode.includes(from));let failed=false;try{await controlChecks(messageCode.replace(from,to))}catch(e){failed=true}assert.ok(failed,name+' must fail tests');console.log('ok mutation detected:',name)}
  for(const [name,from,to] of [
    ['unproven fulfillment success',"v.state!=='fulfilled'",'false'],
    ['invalid evidence UUID',"!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(v.control_request_id)",'false'],
    ['stale cached fulfillment','fulfillment:ofMessageFulfillment(v.fulfillment)','fulfillment:ofMessageFulfillment(v.fulfillment)||r.fulfillment']
  ]){assert.ok(messageCode.includes(from));let failed=false;try{await fulfillmentChecks(messageCode.replace(from,to))}catch(e){failed=true}assert.ok(failed,name+' must fail tests');console.log('ok mutation detected:',name)}
})().catch(err=>{console.error(err);process.exitCode=1});

/* The Officer submission owns one idempotency ID per user question, not per proposed message. */
async function officerSubmitChecks(mutate=false){
  let sendCode=html.slice(html.indexOf('async function ofSend(){'),html.indexOf('function ofShow(r)'));
  if(mutate) sendCode=sendCode.replace('...(request_id?{request_id}:{})','...{}');
  const bodies=[],shown=[],button={disabled:false},status={textContent:''};let ids=0;
  const ctx={OF:{turns:[],busy:false},ofIn:{value:'Ask Aster for progress',focus(){}},ofSt:status,DEMO:false,
    crypto:{randomUUID:()=>`uuid-${++ids}`},ofDay(){},ofAdd:(c,t)=>shown.push(t),ofStatus:t=>status.textContent=t,
    $:()=>button,ofSave(){},ofShow(){},esc:String,
    post:async(route,body)=>{bodies.push({route,body});return {ok:true,json:async()=>({text:'Question queued',messages:[]})}}};
  vm.createContext(ctx);const send=vm.runInContext(sendCode+';ofSend',ctx);
  await send();assert.equal(bodies.length,1);assert.equal(bodies[0].route,'/ask');assert.equal(bodies[0].body.request_id,'uuid-1');assert.equal(ids,1);
  ctx.ofIn.value='Ask Aster another question';await send();assert.equal(bodies[1].body.request_id,'uuid-2');assert.equal(ids,2);
  ctx.crypto=undefined;ctx.ofIn.value='What happened?';await send();assert.equal(Object.hasOwn(bodies[2].body,'request_id'),false,'ordinary questions work without secure UUID support');
  ctx.post=async()=>{throw new Error('offline')};ctx.ofIn.value='Ask Aster for status';await send();assert.match(shown.at(-1),/check message\/control status/);
  assert.ok(!messageCode.includes('/agent-message\''),'UI never sends messages directly');
  console.log('ok Officer submission: one UUID per question, /ask only, no-crypto log queries, uncertainty warning');
}
(async()=>{await officerSubmitChecks();let failed=false;try{await officerSubmitChecks(true)}catch(err){failed=true}assert.ok(failed,'missing request ID mutation must fail');console.log('ok mutation detected: missing Officer request ID')})().catch(err=>{console.error(err);process.exitCode=1});
