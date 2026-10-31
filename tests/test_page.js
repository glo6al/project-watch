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
  ctx.ofParas=s=>'<p>'+ctx.esc(s)+'</p>';ctx.clock=String;ctx.nearEnd=()=>true;ctx.ofLog={};ctx.CU={nodes:new Map()};ctx.cuPaint=()=>{};const add=ctx.ofAdd;ctx.ofAdd=(c,h,m)=>{const el=add(c,h);if(m?.id)ctx.CU.nodes.set(m.id,{el});return el};ctx.cuCard=(r,k)=>ctx.ofAdd('of '+k,'');
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
    t.ofMessageMerge([source]);t.ofMessageMerge([{...source,fulfillment:invalid}]);assert.equal(r.fulfillment,null,'invalid or missing snapshot annotation clears cached fulfillment');assert.doesNotMatch(card.innerHTML,/Pause completed/,'invalid annotation never claims success');
    const cached=new Map([['liveRoomMessages',JSON.stringify([{...request,fulfillment:invalid,private_path:'/private/example'}])]]),badReload=messaging(code,cached);badReload.ctx.connected=false;badReload.ofMessageLoad();assert.doesNotMatch(badReload.OFM.records.get(request.request_id).el.innerHTML,/Pause completed/);assert.doesNotMatch(cached.get('liveRoomMessages'),/private_path/);
  }
  // A server snapshot is authoritative even after valid local history has loaded.
  reload.ctx.connected=true;reload.ctx.response={ok:true,json:async()=>({requests:[{...source,fulfillment:undefined}],available:true})};await reload.ofMessagePoll();assert.doesNotMatch(reload.OFM.records.get(request.request_id).el.innerHTML,/Pause completed/);assert.equal(JSON.parse(reload.storage.get('liveRoomMessages'))[0].fulfillment,null);
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
  const showCode=html.slice(html.indexOf('function ofShow('),html.indexOf('function ofDemoAnswer('));
  const shown=messaging(code);Object.assign(shown.ctx,{OFUI:{epoch:1,tab:'officer',recipient:'watch'},focus:null,ofEl:{classList:{contains:()=>false}},ofMayAct:()=>true,ofAttributed:(c,h)=>h});shown.ctx.ofAdd=()=>({querySelectorAll:()=>[]});shown.ctx.ofCite=()=>'';shown.ctx.ofAct=()=>[false,''];shown.ctx.OF={};vm.runInContext(showCode+';ofShow({text:"recorded",controls:['+JSON.stringify(control)+']})',shown.ctx);assert.equal(shown.OFM.controls.size,1,'Officer response merges control even without polling');
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
  let sendCode=html.slice(html.indexOf('async function ofSend(){'),html.indexOf('/* response actions preserve'));
  if(mutate) sendCode=sendCode.replace('...(request_id?{request_id}:{})','...{}');
  const bodies=[],shown=[],button={disabled:false},status={textContent:''};let ids=0;
  const ctx={OF:{turns:[],busy:false},ofIn:{value:'Ask Aster for progress',focus(){}},ofSt:status,DEMO:false,
    crypto:{randomUUID:()=>`uuid-${++ids}`},cuTurnMeta:()=>({id:'turn:1',ts:1}),cuDraftSave(){},ofContextPaint(){},ofHistory:t=>t.map(({role,content,context})=>({role,content,context})),ofDay(){},ofAdd:(c,t)=>shown.push(t),ofStatus:t=>status.textContent=t,
    $:()=>button,ofSave(){},ofShow(){},esc:String,ofCurrent:()=>({context:{scope:'room'},history:{scope:'room'},epoch:0}),ofAttributed:(c,h)=>h,ofOwnsFocus:()=>false,
    post:async(route,body)=>{bodies.push({route,body});return {ok:true,json:async()=>({text:'Question queued',messages:[]})}}};
  vm.createContext(ctx);const send=vm.runInContext(sendCode+';ofSend',ctx);
  await send();assert.equal(bodies.length,1);assert.equal(bodies[0].route,'/ask');assert.equal(bodies[0].body.request_id,'uuid-1');assert.equal(ids,1);
  ctx.ofIn.value='Ask Aster another question';await send();assert.equal(bodies[1].body.request_id,'uuid-2');assert.equal(ids,2);
  ctx.crypto=undefined;ctx.ofIn.value='What happened?';await send();assert.equal(Object.hasOwn(bodies[2].body,'request_id'),false,'ordinary questions work without secure UUID support');
  ctx.post=async()=>{throw new Error('offline')};ctx.ofIn.value='Ask Aster for status';await send();assert.match(shown.at(-1),/check message\/control status/);
  assert.ok(!sendCode.includes('/agent-message\''),'WATCH never bypasses its explicit /ask route');
  console.log('ok Officer submission: one UUID per question, /ask only, no-crypto log queries, uncertainty warning');
}
(async()=>{await officerSubmitChecks();let failed=false;try{await officerSubmitChecks(true)}catch(err){failed=true}assert.ok(failed,'missing request ID mutation must fail');console.log('ok mutation detected: missing Officer request ID')})().catch(err=>{console.error(err);process.exitCode=1});

/* Actual context/history helpers: metadata is optional, bounded data, not a changed prompt. */
const contextStart=html.indexOf('/* officer context helpers:'),contextEnd=html.indexOf('/* end officer context helpers */');
const contextCode=html.slice(html.indexOf('*/',contextStart)+2,contextEnd);
function contextChecks(code=contextCode){
  const h=vm.runInNewContext(code+';({ofContextValid,ofHistory,ofSnapshot,ofMayAct})');
  const aid='x:12345678-abcd',agents={[aid]:{name:'Birch'}};
  const r=h.ofSnapshot('station',aid,agents,7);agents[aid].name='Renamed';
  assert.equal(r.history.name,'Birch');assert.equal(r.context.name,undefined);assert.ok(Object.isFrozen(r.context));assert.ok(Object.isFrozen(r.history));
  assert.equal(h.ofSnapshot('room',aid,agents,8).context.agent,undefined);
  for(const invalid of [null,[],{scope:'room',agent:aid},{scope:'station',agent:'Birch'},{scope:'station',agent:aid+'\n'},{scope:'station',agent:aid,name:'x'.repeat(121)},{scope:'station',agent:aid,name:''}])assert.equal(h.ofContextValid(invalid),null);
  const rows=h.ofHistory([{role:'user',content:'legacy'},{role:'assistant',content:'old',context:{scope:'station',agent:aid,name:'Historical'}},{role:'user',content:'bad metadata',context:{scope:'station',agent:'bad'}}]);
  assert.equal(rows[0].context,undefined);assert.equal(rows[1].context.name,'Historical');assert.equal(rows[2].context,undefined);
  assert.equal(h.ofHistory(Array.from({length:60},()=>({role:'user',content:'x'.repeat(4100)}))).length,40);assert.equal(h.ofHistory([{role:'user',content:'x'.repeat(4100)}])[0].content.length,4000);
  assert.equal(h.ofMayAct(r,7,true),true);assert.equal(h.ofMayAct(r,8,true),false);assert.equal(h.ofMayAct(r,7,false),false);
}
contextChecks();console.log('ok officer context: frozen snapshots, legacy and malformed history, bounds, navigation gate');
for(const [label,from,to] of [['late navigation gate','request.epoch===epoch','true'],['history bound','.slice(-40)','.slice(-60)'],['name bound','>120','>200']]){let failed=false;try{contextChecks(contextCode.replace(from,to))}catch(e){failed=true}assert.ok(failed,label+' mutation caught');console.log('ok mutation detected: '+label)}

/* An in-flight question never borrows the selected station on return or steals focus. */
async function officerLateChecks(){
  const code=html.slice(html.indexOf('async function ofSend(){'),html.indexOf('/* response actions preserve'));
  let release,epoch=1,selected='Birch',focuses=0,posted;const shown=[],responses=[];
  const ctx={OF:{turns:[],busy:false},ofIn:{value:'Why did it stop?',focus(){focuses++}},ofSt:{textContent:''},DEMO:false,
    ofCurrent:()=>({context:{scope:'station',agent:'x:12345678'},history:{scope:'station',agent:'x:12345678',name:selected},epoch}),
    cuTurnMeta:()=>({id:'turn:1',ts:1}),cuDraftSave(){},ofContextPaint(){},ofHistory:t=>t.map(({role,content,context})=>({role,content,context})),ofDay(){},ofAdd:(c,t)=>shown.push(t),ofAttributed:(c,h)=>c.name+' '+h,esc:String,ofStatus(t){ctx.ofSt.textContent=t},$:()=>({disabled:false}),ofSave(){},ofShow:(r,q)=>responses.push(q),ofOwnsFocus:q=>q.epoch===epoch,
    post:async(p,b)=>{posted=b;await new Promise(r=>release=r);return {ok:true,json:async()=>({text:'Stopped'})}}};
  vm.createContext(ctx);const send=vm.runInContext(code+';ofSend',ctx);const pending=send();selected='Aster';epoch++;release();await pending;
  assert.equal(posted.context.agent,'x:12345678');assert.equal(responses[0].history.name,'Birch');assert.equal(ctx.OF.turns[1].context.name,'Birch');assert.equal(focuses,0);
  ctx.ofIn.value='Explain';ctx.post=async()=>{selected='Other';epoch++;throw Error('offline')};await send();assert.match(shown.at(-1),/^Aster /);assert.equal(focuses,0);
  console.log('ok delayed response: immutable attribution on success/error, no focus theft');
}
officerLateChecks().catch(e=>{console.error(e);process.exitCode=1});

/* Explicit suggestion clicks run the real action boundary once, never on arrival. */
{
  const code=html.slice(html.indexOf('function ofSuggestion('),html.indexOf('function ofShow('));let calls=0;const buttons=[],lines=[];
  const ctx={A:{},focus:'x:12345678',ofAdd:(c,h)=>{lines.push(h);const b={};buttons.push(b);return {querySelector:()=>b}},esc:String,ofAct:()=>{calls++;return [true,'applied']},ofToggle(){throw Error('unexpected fold')},$:()=>({focus(){}})};
  const suggest=vm.runInNewContext(code+';ofSuggestion',ctx);suggest({do:'watch_draft',words:'test'});assert.equal(calls,0);buttons[0].onclick();buttons[0].onclick();assert.equal(calls,1);assert.equal(buttons[0].disabled,true);assert.match(lines.at(-1),/^Done/);console.log('ok deferred suggestion: explicit single use, truthful outcome');
}

/* Conversation navigation uses agent + local entry identity and expands old speech. */
function citationChecks(mutate=false){
  let jumpCode=html.slice(html.indexOf('function ofJump('),html.indexOf('/* agent message helpers:'));
  let revealCode=html.slice(html.indexOf('function cuRevealEvent('),html.indexOf('function cuCard('));
  if(mutate===true){jumpCode=jumpCode.replace('x.dataset.sourceAgent===aid&&','');revealCode=revealCode.replace('x.dataset.sourceAgent===id&&','')}
  if(mutate==='collapsed'){jumpCode=jumpCode.replace('if(detail)detail.open=true','if(false)detail.open=true');revealCode=revealCode.replace('if(detail)detail.open=true','if(false)detail.open=true')}
  for(const t of ['you','say']){
    let scrolled=0,notices=[];const entry={t,seq:7,n:17,key:'message'},detail={open:false},el={dataset:{sourceN:'17',sourceAgent:'id'},querySelector:()=>detail,getClientRects:()=>[{}],scrollIntoView(){scrolled++},classList:{add(){},remove(){}}},wrong={...el,dataset:{sourceN:'17',sourceAgent:'other'},getClientRects:()=>[],scrollIntoView(){throw Error('wrong station')}};
    const env={A:{id:{name:'Birch',story:[entry],open:new Set()}},focus:'id',setFocus(){},ofTab(){},capAdd:(s,k)=>s.add(k),STATION:{compact:true},render(){},performance:{now:()=>0},STEPPING:false,ofLog:{dataset:{a:'id'},querySelectorAll:()=>[wrong,el]},storyEl:{},setTimeout(){},cuNotice:text=>notices.push(text),cuSources(){},cuPaint(){},esc:String};
    vm.runInNewContext(jumpCode+';ofJump("id",7)',env);assert.equal(scrolled,1);assert.equal(detail.open,true);assert.equal(env.deskView,undefined);
    detail.open=false;vm.runInNewContext(revealCode+';cuRevealEvent("id",17)',env);assert.equal(scrolled,2);assert.equal(detail.open,true);
    vm.runInNewContext(jumpCode+';ofJump("id",404);ofJump("missing",7)',env);assert.equal(notices.length,2);assert.match(notices[0],/no longer/);assert.match(notices[1],/no longer/);
  }
  assert.doesNotMatch(html,/data-v="said"|deskView='said'/);
}
citationChecks();let collisionCaught=false;try{citationChecks(true)}catch(e){collisionCaught=true}assert.ok(collisionCaught);let collapseCaught=false;try{citationChecks('collapsed')}catch(e){collapseCaught=true}assert.ok(collapseCaught);console.log('ok conversation navigation: two stations with same local n, collapsed speech, absent citations; collision and collapsed-target mutations detected');
{
  const nodes=new Map(),ctx={CU:{serial:0,nodes},ofAdd:(c,h,m)=>{const el={text:h};nodes.set(m.id,{el});return el},cuPaint(){}};
  const noticeCode=html.slice(html.indexOf('function cuNotice('),html.indexOf('function cuRevealEvent('));vm.runInNewContext(noticeCode+';cuNotice("Unavailable citation")',ctx);assert.equal([...nodes.values()][0].global,true,'navigation failure remains visible regardless of selected context');
}

/* Visibility is an all-rectangle safety check, including compact-host clipping. */
function visibilityChecks(mutate=false){
  let code=html.slice(html.indexOf('function onScreen('),html.indexOf('const covered='));if(mutate)code=code.replace('r.top<h.top','false');
  const r={width:210,height:100,top:90,bottom:190,left:0,right:210},host={getBoundingClientRect:()=>({top:100,bottom:500,left:0,right:500})};
  const el={getBoundingClientRect:()=>r,closest:()=>host,contains:()=>true};
  const visible=vm.runInNewContext(code+';onScreen',{innerHeight:600,innerWidth:500,$:()=>({offsetHeight:40}),document:{elementFromPoint:()=>el}});
  assert.equal(visible(el),false);r.top=110;r.bottom=210;assert.equal(visible(el),true);r.height=70;assert.equal(visible(el),false);
}
visibilityChecks();let clippingFailed=false;try{visibilityChecks(true)}catch(e){clippingFailed=true}assert.ok(clippingFailed);console.log('ok compact approval clipping guard and mutation detection');

/* A still-visible dock is not ownership: another focused control defers navigation actions. */
function responseFocusChecks(mutate=false){
  let code=html.slice(html.indexOf('function ofShow('),html.indexOf('function ofDemoAnswer('));if(mutate)code=code.replace('&&(!request||ofOwnsFocus(request))','');
  let acts=0,suggestions=0;const ctx={OFUI:{epoch:1,tab:'officer',recipient:'watch'},focus:'station',ofEl:{classList:{contains:()=>false}},ofOwnsFocus:()=>false,ofMayAct:(q,e,a)=>q.epoch===e&&a,ofAttributed:(c,h)=>h,ofParas:String,ofAdd:()=>({querySelectorAll:()=>[]}),ofSuggestion:()=>suggestions++,ofAct:()=>{acts++;return [false,'']},esc:String};
  const show=vm.runInNewContext(code+';ofShow',ctx);show({text:'Draft',actions:[{do:'watch_draft'}]},{epoch:1});assert.equal(acts,0);assert.equal(suggestions,1);
  ctx.ofOwnsFocus=()=>true;show({text:'Draft',actions:[{do:'watch_draft'}]},{epoch:1});assert.equal(acts,1);
}
responseFocusChecks();let focusFailed=false;try{responseFocusChecks(true)}catch(e){focusFailed=true}assert.ok(focusFailed);console.log('ok response action focus ownership and mutation detection');
{
  const match=html.match(/\$\('#compactDesk'\)\.addEventListener\('pointerdown',\(\)=>\{([^}]+)\}\)/);assert.ok(match);const ctx={moved:true,drop(){}};vm.runInNewContext(match[1],ctx);assert.equal(ctx.moved,false);console.log('ok compact pointerdown clears stale room-drag click suppression');
}

/* Compact approval placement moves the original action banner next to the code and restores order. */
{
  const parent={children:[]};
  function node(name){const n={name,parentNode:parent,remove(){const i=parent.children.indexOf(this);if(i>=0)parent.children.splice(i,1)},before(el){el.remove();parent.children.splice(parent.children.indexOf(this),0,el)},after(el){el.remove();parent.children.splice(parent.children.indexOf(this)+1,0,el)}};Object.defineProperty(n,'nextElementSibling',{get(){return parent.children.slice(parent.children.indexOf(n)+1).find(x=>x.name!=='anchor')}});return n}
  const banner=node('banner'),header=node('header'),mon=node('mon');parent.children=[banner,header,mon];
  const code=html.slice(html.indexOf('function stationBefore('),html.indexOf('function stationLayout('));const ctx={STATION:{anchors:new Map()},document:{createComment:()=>node('anchor')}};
  const api=vm.runInNewContext(code+';({stationBefore,stationRestore})',ctx);api.stationBefore(banner,mon);assert.equal(parent.children.at(-2),banner);api.stationBefore(banner,mon);assert.equal(ctx.STATION.anchors.size,1);api.stationRestore(banner);assert.deepEqual(parent.children,[banner,header,mon]);assert.equal(ctx.STATION.anchors.size,0);console.log('ok compact approval original-banner placement and exact restoration');
}

/* Leave scrolling room around the approval banner and code, not only an exact fit. */
{
  const code=html.slice(html.indexOf('function compactMonitorHeight('),html.indexOf('function stationLayout('));const height=vm.runInNewContext(code+';compactMonitorHeight');assert.equal(height(390,97),169);assert.equal(height(200,100),120);assert.ok(64+169+12<=293-40);console.log('ok compact approval geometry reserves scroll breathing space and minimum monitor height');
}

/* Unified conversation identities, context isolation and legacy metadata. */
const unifiedCode=html.slice(html.indexOf('function cuEventKey('),html.indexOf('/* end unified conversation helpers */'));
function unifiedChecks(code=unifiedCode){
  const api=vm.runInNewContext(code+';({cuSourceRows,cuEnvelopeMatch,cuDraftKey,cuVisible,cuMeta})');
  const a={story:[{t:'you',seq:1,n:1,text:'same'},{t:'say',seq:2,n:2,text:'same'},{t:'you',seq:3,n:3,text:'same'}],msgs:[{seq:1,text:'same'},{seq:0,text:'older'}]};
  const rows=api.cuSourceRows(a);assert.equal(rows.length,4);assert.equal(rows.filter(r=>r.text==='same').length,3,'same text with different identities remains visible');assert.equal(rows.filter(r=>r.seq===1).length,1,'same event across two stores appears once');
  const id='12345678-1234-4123-8123-123456789abc',record={request_id:id,agent:'x:agent-a',question:'Hello',state:'answered',reply:'Done'},records=new Map([[id,record]]);
  const reply={t:'say',text:'WATCH_REPLY '+id+'\nDone'};assert.equal(api.cuEnvelopeMatch(reply,'x:agent-a',records),true);assert.equal(api.cuEnvelopeMatch(reply,'x:agent-b',records),false);assert.equal(api.cuEnvelopeMatch({...reply,text:reply.text+' extra'},'x:agent-a',records),false);assert.equal(api.cuEnvelopeMatch({...reply,text:'WATCH_REPLY '+id},'x:agent-a',records),false);assert.equal(api.cuEnvelopeMatch(reply,'x:agent-a',new Map()),false);
  const requestText='WATCH_REQUEST '+id+'\nThe owner asks through the Officer of the Watch:\nHello\nReply in your final answer with WATCH_REPLY '+id+' on its own line, followed by your answer. This is a question, not permission to change files or send messages. For a progress question, inspect your own current state.';
  assert.equal(api.cuEnvelopeMatch({t:'you',text:requestText},record.agent,records),true);assert.equal(api.cuEnvelopeMatch({t:'you',text:requestText.slice(0,-1)},record.agent,records),false);assert.equal(api.cuEnvelopeMatch({t:'you',text:requestText.replace('Hello','Different')},record.agent,records),false);
  assert.equal(api.cuVisible({scope:'room'},'A'),true);assert.equal(api.cuVisible({scope:'station',agent:'A'},'A'),true);assert.equal(api.cuVisible({scope:'station',agent:'A'},'B'),false);assert.equal(api.cuVisible(null,'A'),false);assert.equal(api.cuVisible(null,null),true);
  assert.equal(api.cuMeta(null),null);assert.equal(api.cuMeta({id:'turn:old'}),null);assert.equal(api.cuMeta({id:'turn:x',ts:NaN}),null);assert.equal(api.cuMeta({id:'turn:x',ts:4}).ts,4);
  const keys=[api.cuDraftKey('A','agent','station'),api.cuDraftKey('A','watch','station'),api.cuDraftKey('A','watch','room'),api.cuDraftKey('B','agent','station')];assert.equal(new Set(keys).size,4);
}
unifiedChecks();
for(const [label,from,to] of [['cross-agent envelope','r.agent!==agent','false'],['event dedup','if(key&&seen.has(key))continue','if(false)continue'],['legacy timestamp invention',"return value&&typeof value.id", "return !value?{id:'turn:invented',ts:1}:value&&typeof value.id"]]){
  let failed=false;try{unifiedChecks(unifiedCode.replaceAll(from,to))}catch(e){failed=true}assert.ok(failed,label+' must be caught');
}
console.log('ok unified identities, exact envelope correlation, legacy dates, context and draft partitions; three mutations detected');

/* The real draft save/restore helpers preserve independent destinations. */
{
  const code=html.slice(html.indexOf('function cuDraftSave('),html.indexOf('function cuTurnMeta('));
  const key=vm.runInNewContext(unifiedCode+';cuDraftKey');const ctx={focus:'A',OFUI:{recipient:'agent',scope:'station'},CU:{drafts:new Map()},ofIn:{value:'Agent A draft'},cuDraftKey:key};
  const api=vm.runInNewContext(code+';({cuDraftSave,cuDraftRestore})',ctx);api.cuDraftSave();ctx.OFUI.recipient='watch';api.cuDraftRestore();assert.equal(ctx.ofIn.value,'');ctx.ofIn.value='WATCH A draft';api.cuDraftSave();ctx.OFUI.scope='room';api.cuDraftRestore();assert.equal(ctx.ofIn.value,'');ctx.ofIn.value='Room draft';api.cuDraftSave();ctx.focus='B';api.cuDraftRestore();assert.equal(ctx.ofIn.value,'');ctx.focus='A';api.cuDraftRestore();assert.equal(ctx.ofIn.value,'Room draft');ctx.OFUI.recipient='agent';api.cuDraftRestore();assert.equal(ctx.ofIn.value,'Agent A draft');
  console.log('ok drafts: agent, WATCH station, WATCH room and station switches');
}

/* Exercise the actual direct send boundary with delayed responses and failures. */
const directCode=html.slice(html.indexOf('async function cuSend('),html.indexOf('async function ofSend('));
function directHarness(code=directCode){
  const posts=[],lines=[],painted=[];let ids=0;
  const ctx={focus:'x:A',A:{'x:A':{name:'Ada',runtime:'Codex'},'x:B':{name:'Bram',runtime:'Codex'}},OFUI:{recipient:'agent'},CU:{busy:new Set()},OFM:{records:new Map()},ofIn:{value:'Please inspect'},DEMO:false,KEY:'key',claiming:false,locked:false,connected:true,
    crypto:{randomUUID:()=>`12345678-1234-4123-8123-${String(++ids).padStart(12,'0')}`},ofSend:()=>{ctx.watchCalls++;},watchCalls:0,esc:String,ofAdd:(c,h,m)=>lines.push({h,m}),ofMessagePaint:r=>painted.push({...r}),ofMessageSave(){},cuDraftSave(){},ofContextPaint(){},ofMessageMerge:rows=>{for(const v of rows)Object.assign(ctx.OFM.records.get(v.request_id),v)},post:async(p,b)=>{posts.push({p,b});return {ok:true,json:async()=>({...b,state:'queued'})}}};
  ctx.cuDirectReason=vm.runInNewContext(unifiedCode+';cuDirectReason');ctx.OF=Object.freeze({turns:Object.freeze([])});vm.createContext(ctx);const send=vm.runInContext(code+';cuSend',ctx);return {ctx,send,posts,lines,painted};
}
async function directChecks(code=directCode){
  const h=directHarness(code);let release;h.ctx.post=async(p,b)=>{h.posts.push({p,b});await new Promise(r=>release=r);return {ok:true,json:async()=>({...b,state:'queued'})}};
  const pending=h.send();assert.equal(h.posts[0].p,'/agent-message');assert.equal(h.posts[0].b.agent,'x:A');assert.equal(h.posts[0].b.question,'Please inspect');assert.ok(h.posts[0].b.request_id);assert.equal(h.ctx.watchCalls,0);assert.equal(h.ctx.OF.turns.length,0);
  h.ctx.ofIn.value='second';await h.send();assert.equal(h.posts.length,1,'same target busy gate');h.ctx.focus='x:B';h.ctx.OFUI.recipient='watch';h.ctx.ofIn.value='Bram WATCH draft';h.ctx.A['x:A'].name='Renamed';release();await pending;
  const r=[...h.ctx.OFM.records.values()][0];assert.equal(r.agent,'x:A');assert.equal(r.name,'Ada');assert.equal(r.question,'Please inspect');assert.equal(h.ctx.ofIn.value,'Bram WATCH draft');assert.equal(h.ctx.CU.busy.size,0);
  await h.send();assert.equal(h.ctx.watchCalls,1,'WATCH routes only to ofSend');
  for(const field of ['locked','claiming']){const t=directHarness(code);t.ctx[field]=true;await t.send();assert.equal(t.posts.length,0);assert.match(t.lines[0].h,/Nothing was sent/)}
  for(const [field,value] of [['KEY',''],['connected',false],['crypto',undefined]]){const t=directHarness(code);t.ctx[field]=value;await t.send();assert.equal(t.posts.length,0);assert.equal(t.ctx.OFM.records.size,0)}
  const tooLong=directHarness(code);tooLong.ctx.ofIn.value='x'.repeat(2001);await tooLong.send();assert.equal(tooLong.posts.length,0);
  const unsupported=directHarness(code);unsupported.ctx.A['x:A'].observed=true;await unsupported.send();assert.equal(unsupported.posts.length,0);
  const lost=directHarness(code);lost.ctx.post=async()=>{throw Error('offline')};await lost.send();assert.equal([...lost.ctx.OFM.records.values()][0].state,'unknown');assert.match(lost.painted.at(-1).error,/do not resend/);
  const mismatch=directHarness(code);mismatch.ctx.post=async(p,b)=>({ok:true,json:async()=>({...b,agent:'x:B'})});await mismatch.send();assert.equal([...mismatch.ctx.OFM.records.values()][0].state,'unknown');
  for(const status of [403,500]){const lateError=directHarness(code);lateError.ctx.post=async(p,b)=>{Object.assign(lateError.ctx.OFM.records.get(b.request_id),{state:'answered',reply:'Already received'});return {ok:false,status,text:async()=> 'Late error'}};await lateError.send();assert.equal([...lateError.ctx.OFM.records.values()][0].state,'answered');}
  const late=directHarness(code);late.ctx.post=async(p,b)=>{Object.assign(late.ctx.OFM.records.get(b.request_id),{state:'answered',reply:'Already received'});return {ok:true,json:async()=>({...b,state:'sending'})}};await late.send();assert.equal([...late.ctx.OFM.records.values()][0].state,'answered');
}
(async()=>{await directChecks();for(const [label,from,to] of [['wrong route',"post('/agent-message'","post('/ask'"],['mutable target','{agent:id,question:q,request_id}','{agent:focus,question:q,request_id:undefined}'],['lost uncertainty',"r.state='unknown';r.error='Delivery", "r.state='failed';r.error='Delivery"],['late HTTP regression',"if(r.state==='sending'){r.state=res.status","if(true){r.state=res.status"]]){let failed=false;try{await directChecks(directCode.replace(from,to))}catch(e){failed=true}assert.ok(failed,label+' mutation caught')}console.log('ok direct routing: exact identity, independent busy gates, delayed response, drafts, errors, uncertainty and no replay; four mutations detected')})().catch(e=>{console.error(e);process.exitCode=1});

/* Card updates cannot relabel historical recipients; pending reloads are uncertain. */
{
  const t=messaging();t.ofMessageMerge([{...request,name:'Original',state:'sending',created:22}]);const r=t.OFM.records.get(request.request_id);t.ofMessageMerge([{...request,name:'Renamed',state:'answered',reply:'Matched'}]);assert.equal(r.name,'Original');assert.equal(r.created,22);assert.equal(r.reply,'Matched');t.ofMessageMerge([{...request,agent:'x:other',state:'answered',reply:'Wrong'}]);assert.equal(r.reply,'Matched');t.ofMessageMerge([{...request,question:'Wrong',state:'answered',reply:'Wrong'}]);assert.equal(r.reply,'Matched');
  for(const state of ['sending','queued','awaiting_reply']){const cache=new Map([['liveRoomMessages',JSON.stringify([{...request,state,created:22}])]]),reload=messaging(messageCode,cache);reload.ctx.connected=false;reload.ofMessageLoad();assert.equal(reload.OFM.records.get(request.request_id).state,'unknown');assert.equal(reload.OFM.records.get(request.request_id).created,22);assert.equal(reload.gets.length,0)}
  console.log('ok immutable message attribution, exact request identity and uncertain reload recovery');
}

/* Real source reconciliation preserves expanded nodes and keeps queue evidence separate. */
function sourceChecks(mutate=false){
  let code=html.slice(html.indexOf('function cuSources('),html.indexOf('function cuNotice('));
  if(mutate)code=code.replace("if(!r){const el=", "if(true){const el=");
  class Node{constructor(){this.dataset={};this.paints=0}set innerHTML(v){this.html=v;this.paints++;this.detail=v.includes('<details')?{open:v.includes('<details open')}:null}get innerHTML(){return this.html}querySelector(){return this.detail}remove(){this.removed=true}}
  const helpers=vm.runInNewContext(unifiedCode+';({cuSourceRows,cuEventKey,cuEnvelopeMatch})');const pending={state:'queued'},ctx={...helpers,CU:{nodes:new Map()},OFM:{records:new Map([['pending',pending]])},document:{createElement:()=>new Node()},plain:String,one:String,esc:String,clock:String,STEADY:false,need:a=>a.need};
  const sources=vm.runInNewContext(code+';cuSources',ctx),a={id:'A',name:'Ada',story:[{t:'say',n:1,seq:1,ts:1,text:'First'}],msgs:[],q:[{seq:2,ts:2,text:'Queued task'}],need:{sure:false,kind:'Decide',text:'Choose one'}};
  sources(a);const row=ctx.CU.nodes.get('source:A:seq:1'),card=row.el;card.detail.open=false;const paints=card.paints;sources(a);assert.equal(ctx.CU.nodes.get('source:A:seq:1').el,card);assert.equal(card.paints,paints);assert.equal(card.detail.open,false,'expanded state survives refresh');assert.match(ctx.CU.nodes.get('queue:A:seq:2').el.innerHTML,/Queue observation does not confirm delivery/);assert.match(ctx.CU.nodes.get('source:A:request').el.innerHTML,/Possible request · inferred/);
  a.name='Renamed';a.need.sure=true;a.q=[];sources(a);assert.equal(row.context.name,'Ada');assert.match(ctx.CU.nodes.get('source:A:request').el.innerHTML,/Confirmed request/);assert.equal(ctx.CU.nodes.has('queue:A:seq:2'),false);assert.equal(pending.state,'queued','queue removal never changes independent delivery');
  a.story.push({t:'say',n:3,seq:3,ts:3,text:'Later'});sources(a);assert.equal(card.detail.open,false);assert.equal(ctx.CU.nodes.get('source:A:seq:3').el.detail.open,true);assert.equal(row.el,card);
}
sourceChecks();let reconciliationCaught=false;try{sourceChecks(true)}catch(e){reconciliationCaught=true}assert.ok(reconciliationCaught);console.log('ok stable source DOM/details, immutable name, confirmed/inferred requests and independent native queue; recreation mutation detected');

/* Unsupported stations explain their limitation before a send is attempted. */
{
  const reason=vm.runInNewContext(unifiedCode+';cuDirectReason');assert.equal(reason('x:demo',{runtime:'Codex'}),'');assert.equal(reason('c:demo',{runtime:'Claude'}),'');assert.match(reason('e:cloud',{runtime:'Codex',observed:true}),/ChatGPT/);assert.match(reason('c:child',{runtime:'Claude',sub:true}),/parent/);assert.match(reason('x:gone',null),/no longer/);assert.match(reason('k:remote',{runtime:'Codex'}),/local/);
  console.log('ok direct availability: local parents supported, cloud/subagent/unavailable targets explained');
}
