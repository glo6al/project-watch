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
const answerStart=html.indexOf('/* answer markup'),answerEnd=html.indexOf('/* end answer markup */');
const answerCode=html.slice(html.indexOf('*/',answerStart)+2,answerEnd);
const escapeCode=html.split('\n').filter(line=>line.startsWith('const str=')||line.startsWith('const esc=')).join('\n');
const safeEsc=vm.runInNewContext(escapeCode+';esc');
const responseMarkup=vm.runInNewContext(answerCode+';ofParas',{esc:safeEsc,URL});



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
    let scrolled=0,notices=[],reveals=[];const entry={t,seq:7,n:17,key:'message'},detail={open:false},el={dataset:{sourceN:'17',sourceAgent:'id'},querySelector:()=>detail,getClientRects:()=>[{}],scrollIntoView(){scrolled++},classList:{add(){},remove(){}}},wrong={...el,dataset:{sourceN:'17',sourceAgent:'other'},getClientRects:()=>[],scrollIntoView(){throw Error('wrong station')}};
    const env={A:{id:{name:'Birch',story:[entry],open:new Set()}},focus:'id',setFocus(){},deskReveal:view=>reveals.push(view),ofTab(){},capAdd:(s,k)=>s.add(k),STATION:{compact:true},render(){},performance:{now:()=>0},STEPPING:false,ofLog:{dataset:{a:'id'},querySelectorAll:()=>[wrong,el]},storyEl:{},setTimeout(){},cuNotice:text=>notices.push(text),cuSources(){},cuPaint(){},esc:String};
    vm.runInNewContext(jumpCode+';ofJump("id",7)',env);assert.equal(scrolled,1);assert.equal(detail.open,true);assert.equal(env.deskView,undefined);
    detail.open=false;vm.runInNewContext(revealCode+';cuRevealEvent("id",17)',env);assert.equal(scrolled,2);assert.equal(detail.open,true);assert.deepEqual(reveals,['conversation','conversation'],'speech reveals the conversation, never work');
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
  let inert=false,occluded=false;const el={getBoundingClientRect:()=>r,closest:s=>s==='[inert]'?(inert?host:null):s==='#compactDesk'?host:null,contains:hit=>hit===el};
  const visible=vm.runInNewContext(code+';onScreen',{innerHeight:600,innerWidth:500,$:()=>({offsetHeight:40}),document:{elementFromPoint:()=>occluded?{}:el}});
  assert.equal(visible(el),false);r.top=110;r.bottom=210;assert.equal(visible(el),true);inert=true;assert.equal(visible(el),false,'closing work or drawer background rejects approval immediately');inert=false;occluded=true;assert.equal(visible(el),false,'occluded request code cannot approve');occluded=false;r.height=70;assert.equal(visible(el),false);
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

/* Placement preserves real nodes and listener identity across hosts and repeated room visits. */
function placementChecks(mutate=false){
  function node(name,comment=false){
    const n={name,comment,children:[],parentNode:null,remove(){if(!this.parentNode)return;const p=this.parentNode;p.children.splice(p.children.indexOf(this),1);this.parentNode=null},
      appendChild(el){el.remove();this.children.push(el);el.parentNode=this;return el},
      before(el){const p=this.parentNode;assert.ok(p,'insertion target must have parent');el.remove();p.children.splice(p.children.indexOf(this),0,el);el.parentNode=p},
      after(el){const p=this.parentNode;assert.ok(p,'insertion target must have parent');el.remove();p.children.splice(p.children.indexOf(this)+1,0,el);el.parentNode=p},
      contains(el){return this===el||this.children.some(x=>x.contains(el))}};
    Object.defineProperty(n,'nextElementSibling',{get(){return this.parentNode?.children.slice(this.parentNode.children.indexOf(this)+1).find(x=>!x.comment)||null}});return n;
  }
  const room=node('room'),shell=node('shell'),work=node('work'),conversation=node('conversation'),left=node('left'),right=node('right');
  for(const el of [work,conversation,left,right])shell.appendChild(el);
  const a=node('A'),b=node('B'),deskL=node('deskL'),deskR=node('deskR'),bottom=node('bottom'),bay=node('bay'),badge=node('badge'),officer=node('officer');
  for(const el of [a,b,deskL,deskR,bottom])room.appendChild(el);bottom.appendChild(bay);bottom.appendChild(badge);deskR.appendChild(officer);
  const banner=node('banner'),header=node('header'),monitor=node('monitor');for(const el of [banner,header,monitor])a.appendChild(el);
  let clicks=0;banner.onclick=()=>clicks++;const handler=banner.onclick;
  let code=html.slice(html.indexOf('function stationMove('),html.indexOf('function compactMonitorHeight('));
  if(mutate)code=code.replace('a.after(el);','');
  const ctx={STATION:{anchors:new Map()},document:{createComment:()=>node('anchor',true)}};
  const api=vm.runInNewContext(code+';({stationMove,stationBefore,stationRestore})',ctx);
  for(let visit=0;visit<2;visit++){
    api.stationBefore(banner,monitor);api.stationBefore(banner,monitor);api.stationMove(a,work);api.stationMove(a,work);
    for(const [el,parent] of [[deskL,work],[deskR,conversation],[bay,left],[badge,right]]){api.stationMove(el,parent);api.stationMove(el,parent)}
    assert.equal(ctx.STATION.anchors.size,6,'repeated placement creates only one anchor per moved node');
    assert.equal(officer.parentNode,deskR);assert.equal(banner.onclick,handler);banner.onclick();
    api.stationRestore(banner);api.stationRestore(a);api.stationMove(b,work);
    assert.deepEqual(a.children,[banner,header,monitor],'switching stations restores original banner order');assert.equal(a.parentNode,room);assert.equal(b.parentNode,work);
    api.stationRestore(b);for(const el of [deskL,deskR,bay,badge])api.stationRestore(el);
    assert.deepEqual(room.children,[a,b,deskL,deskR,bottom],'room order survives cross-parent moves');assert.deepEqual(bottom.children,[bay,badge]);assert.equal(officer.parentNode,deskR);
    assert.equal(ctx.STATION.anchors.size,0);assert.equal(work.children.length+conversation.children.length+left.children.length+right.children.length,0);
  }
  assert.equal(clicks,2,'preserved listener still runs exactly once per interaction');
}
placementChecks();let placementCaught=false;try{placementChecks(true)}catch(e){placementCaught=true}assert.ok(placementCaught,'missing original-parent restoration must fail');console.log('ok placement: cross-parent restoration, repeated station switches, original order/listeners, no leaked anchors; mutation detected');

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

/* Composer pending evidence distinguishes message queues, observations and explicit steering. */
function pendingChecks(code=unifiedCode){
  const rows=vm.runInNewContext(code+';cuPendingRows'),id=request.request_id,agent='x:A',a={q:[]};
  const records=new Map([[id,{request_id:id,agent,question:'Same text',state:'queued'}]]),controls=new Map();
  let items=rows(agent,a,records,controls);assert.equal(items.length,1);assert.equal(items[0].label,'Queued · WATCH');assert.match(items[0].note,/does not steer/);
  assert.equal(rows('x:B',a,records,controls).length,0);assert.equal(rows(null,a,records,controls).length,0);assert.equal(rows(agent,null,records,controls).length,0);
  const record=records.get(id);
  for(const state of ['sending','unknown']){record.state=state;items=rows(agent,a,records,controls);assert.equal(items.length,1);assert.match(items[0].label,state==='unknown'?/Delivery unknown/:/Sending/)}
  for(const state of ['awaiting_reply','answered','expired','failed']){record.state=state;assert.equal(rows(agent,a,records,controls).length,0,state+' leaves pending strip');assert.equal(records.get(id),record,'history remains untouched')}
  record.state='queued';
  a.q=[{seq:1,text:'Same text'},{seq:2,text:'Same text'},{seq:2,text:'Same text'}];assert.equal(rows(agent,a,records,controls).length,3,'different identities with identical text survive; same event deduplicates');
  a.q=[{text:'Same text'},{text:'Same text'}];assert.equal(rows(agent,a,records,controls).length,3,'unidentified entries are not text-deduplicated');
  const envelope='WATCH_REQUEST '+id+'\nThe owner asks through the Officer of the Watch:\nSame text\nReply in your final answer with WATCH_REPLY '+id+' on its own line, followed by your answer. This is a question, not permission to change files or send messages. For a progress question, inspect your own current state.';
  a.q=[{seq:4,text:envelope}];assert.equal(rows(agent,a,records,controls).length,1,'exact WATCH request envelope is one item');record.state='answered';assert.equal(rows(agent,a,records,controls).length,0,'terminal correlated request cannot remain as a duplicate observation');a.q[0].text+='extra';assert.equal(rows(agent,a,records,controls).length,1,'partial envelope match never drops unproven entry');a.q=[];
  const control={request_id:id,agent,action:'steer',state:'sending',instruction:'Focus on tests'};controls.set(id,control);assert.equal(rows(agent,a,records,controls)[0].label,'Steer pending');
  control.restoredPending=true;assert.equal(rows(agent,a,records,controls)[0].label,'Steer outcome unknown');assert.equal(rows(agent,a,records,controls)[0].unresolved,true);control.restoredPending=false;
  control.unavailable=true;assert.equal(rows(agent,a,records,controls).length,0,'unavailable controls leave strip');assert.equal(controls.get(id),control,'unavailable control history remains');control.unavailable=false;
  control.state='unknown';assert.equal(rows(agent,a,records,controls)[0].label,'Steer outcome unknown');
  for(const state of ['acknowledged','failed']){control.state=state;assert.equal(rows(agent,a,records,controls).length,0);assert.equal(controls.get(id),control)}
  control.state='sending';control.action='interrupt';assert.equal(rows(agent,a,records,controls).length,0,'interrupt is not misrepresented as steer');
}
pendingChecks();
for(const [label,from,to] of [['cross-station pending','r.agent===agent&&Object.hasOwn','true&&Object.hasOwn'],['false pending acknowledgement',"['sending','unknown'].includes(r.state)","['sending','unknown','acknowledged'].includes(r.state)"],['lost queue identity','if(key&&seen.has(key))continue','if(false)continue']]){let caught=false;try{pendingChecks(unifiedCode.replaceAll(from,to))}catch(e){caught=true}assert.ok(caught,label+' mutation detected')}
function pendingPaintChecks(code=html.slice(html.indexOf('function cuPendingPaint('),html.indexOf('function cuSources('))){
  const doc={activeElement:null};let moves=0;
  class PendingNode{
    constructor(){this.children=[];this.dataset={};this.parts={};this.hidden=false;this.open=false;this.textContent=''}
    set innerHTML(markup){this.template=markup;for(const tag of ['summary','b','span','pre','small']){const part=this.parts[tag]=new PendingNode();this.children.push(part);part.parent=this}}
    querySelector(tag){return this.parts[tag]}
    contains(el){return !!el&&(this===el||this.children.some(child=>child.contains(el)))}
    focus(){doc.activeElement=this}
    get firstElementChild(){return this.children[0]||null}
    get nextElementSibling(){return this.parent?.children[this.parent.children.indexOf(this)+1]||null}
    insertBefore(el,at){if(el.parent)moves++;el.remove();const index=at?this.children.indexOf(at):this.children.length;assert.ok(index>=0);this.children.splice(index,0,el);el.parent=this}
    remove(){if(this.parent){if(this.contains(doc.activeElement))doc.activeElement=doc.body;this.parent.children.splice(this.parent.children.indexOf(this),1);this.parent=null}}
  }
  doc.body=new PendingNode();doc.activeElement=doc.body;doc.createElement=()=>new PendingNode();
  const els=Object.fromEntries(['#pendingQueue','#pendingQueueList','#pendingQueueTitle'].map(id=>[id,new PendingNode()])),r={request_id:'first',agent:'A',state:'queued',question:'<img src=x onerror=bad()>'.repeat(30)},ctx={focus:'A',A:{A:{name:'Ada',q:[]},B:{name:'Bram',q:[]}},OFM:{records:new Map([['first',r]]),controls:new Map()},$:s=>els[s],txt:(el,text)=>{el.textContent=text},document:doc,ofIn:new PendingNode()};
  const paint=vm.runInNewContext(unifiedCode+'\n'+code+';cuPendingPaint',ctx);
  paint();const list=els['#pendingQueueList'],row=list.children[0];assert.equal(row.querySelector('pre').textContent,r.question);assert.doesNotMatch(row.template,/<img/,'untrusted text never becomes markup');row.open=true;paint();assert.equal(list.children[0],row);assert.equal(row.open,true,'ordinary polling preserves expansion');assert.equal(els['#pendingQueueTitle'].textContent,'Pending · 1 · Ada');
  r.state='unknown';paint();assert.equal(list.children[0],row);assert.match(row.querySelector('b').textContent,/unknown/);assert.equal(els['#pendingQueueTitle'].textContent,'Unresolved · 1 · Ada','unknown-only title never counts as pending');
  const second={request_id:'second',agent:'A',state:'queued',question:'Second request'};ctx.OFM.records.set('second',second);paint();const secondRow=list.children[1],secondSummary=secondRow.querySelector('summary');secondRow.open=true;secondSummary.focus();assert.equal(els['#pendingQueueTitle'].textContent,'Pending · 1 · Unresolved · 1 · Ada');
  const movesBefore=moves;r.state='answered';paint();assert.equal(list.children[0],secondRow);assert.equal(doc.activeElement,secondSummary,'clearing earlier sibling preserves focused summary');assert.equal(moves,movesBefore,'clearing earlier row does not move survivor');assert.equal(secondRow.open,true);
  second.state='answered';paint();assert.equal(doc.activeElement,ctx.ofIn,'removed focused last row returns focus to composer');assert.equal(els['#pendingQueue'].hidden,true);
  r.state='queued';second.state='queued';paint();list.children[0].querySelector('summary').focus();r.state='answered';paint();assert.equal(doc.activeElement,ctx.ofIn,'removed focused row returns focus even with another row remaining');
  const elsewhere=new PendingNode();elsewhere.focus();second.state='answered';paint();assert.equal(doc.activeElement,elsewhere,'terminal updates never steal focus outside removed row');
  second.state='queued';paint();ctx.focus='B';paint();assert.equal(list.children.length,0);assert.equal(els['#pendingQueue'].hidden,true,'switching stations clears prior agent queue');assert.equal(doc.activeElement,elsewhere,'navigation does not steal focus');ctx.focus='A';paint();assert.equal(list.children.length,1);second.state='answered';paint();assert.equal(list.children.length,0);assert.equal(ctx.OFM.records.size,2,'clearing pending does not remove historical record');
}
pendingPaintChecks();
{
  const paintCode=html.slice(html.indexOf('function cuPendingPaint('),html.indexOf('function cuSources('));
  const removal='for(const [id,el] of existing)if(!active.has(id)){if(el.contains(focused))restoreFocus=true;el.remove()}';
  for(const [label,code] of [['stale rows removed late',paintCode.replace(removal,'').replace('if(restoreFocus)ofIn.focus',removal+';if(restoreFocus)ofIn.focus')],['focus not restored',paintCode.replace('if(restoreFocus)ofIn.focus({preventScroll:true});','')],['unknown counted pending',paintCode.replace('rows.filter(r=>r.unresolved).length','0')]]){let caught=false;try{pendingPaintChecks(code)}catch(e){caught=true}assert.ok(caught,label+' mutation detected')}
}
console.log('ok pending strip: identity/source isolation, observed queue, truthful message/steer states, terminal clearing, safe text, stable expansion, focused sibling removal, composer restoration and navigation; six mutations detected');

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
  const helpers=vm.runInNewContext(unifiedCode+';({cuSourceRows,cuEventKey,cuEnvelopeMatch})');const pending={state:'queued'},ctx={...helpers,CU:{nodes:new Map()},OFM:{records:new Map([['pending',pending]])},document:{createElement:()=>new Node()},plain:String,one:String,esc:safeEsc,ofParas:responseMarkup,clock:String,STEADY:false,need:a=>a.need};
  const sources=vm.runInNewContext(code+';cuSources',ctx),a={id:'A',name:'Ada',story:[{t:'say',n:1,seq:1,ts:1,text:'First'}],msgs:[],q:[{seq:2,ts:2,text:'Queued task'}],need:{sure:false,kind:'Decide',text:'Choose one'}};
  sources(a);const row=ctx.CU.nodes.get('source:A:seq:1'),card=row.el;card.detail.open=false;const paints=card.paints;sources(a);assert.equal(ctx.CU.nodes.get('source:A:seq:1').el,card);assert.equal(card.paints,paints);assert.equal(card.detail.open,false,'expanded state survives refresh');assert.match(ctx.CU.nodes.get('queue:A:seq:2').el.innerHTML,/Queue observation does not confirm delivery/);assert.equal(ctx.CU.nodes.has('source:A:request'),false,'response card owns current requests without duplicate pseudo-messages');
  a.name='Renamed';a.need.sure=true;a.q=[];sources(a);assert.equal(row.context.name,'Ada');assert.equal(ctx.CU.nodes.has('source:A:request'),false);assert.equal(ctx.CU.nodes.has('queue:A:seq:2'),false);assert.equal(pending.state,'queued','queue removal never changes independent delivery');
  a.story.push({t:'say',n:3,seq:3,ts:3,text:'Later'});sources(a);assert.equal(card.detail.open,false);assert.equal(ctx.CU.nodes.get('source:A:seq:3').el.detail.open,true);assert.equal(row.el,card);
}
sourceChecks();let reconciliationCaught=false;try{sourceChecks(true)}catch(e){reconciliationCaught=true}assert.ok(reconciliationCaught);console.log('ok stable source DOM/details, immutable name, no duplicate current request and independent native queue; recreation mutation detected');

/* Unsupported stations explain their limitation before a send is attempted. */
{
  const reason=vm.runInNewContext(unifiedCode+';cuDirectReason');assert.equal(reason('x:demo',{runtime:'Codex'}),'');assert.equal(reason('c:demo',{runtime:'Claude'}),'');assert.match(reason('e:cloud',{runtime:'Codex',observed:true}),/ChatGPT/);assert.match(reason('c:child',{runtime:'Claude',sub:true}),/parent/);assert.match(reason('x:gone',null),/no longer/);assert.match(reason('k:remote',{runtime:'Codex'}),/local/);
  console.log('ok direct availability: local parents supported, cloud/subagent/unavailable targets explained');
}

/* Use the real navigation boundary while a direct request resolves on a different desk. */
async function navigationDraftChecks(mutate=false){
  const h=directHarness(),{ctx}=h;ctx.OFUI.scope='station';ctx.OFUI.epoch=1;ctx.CU.drafts=new Map();
  Object.assign(ctx,{cuDraftKey:vm.runInNewContext(unifiedCode+';cuDraftKey'),PROJ:null,order:['x:A','x:B'],pos:{},STATION:{compact:false},peekT:null,peek:null,clearTimeout(){},screenPose:()=>null,glideN:0,fly(){},apply(){},render(){},ofPlace(){},forget(){throw Error('unexpected forget')}});
  for(const a of Object.values(ctx.A)){a.$={split:{style:{setProperty(){}}}};a.el={}}
  const drafts=html.slice(html.indexOf('function cuDraftSave('),html.indexOf('function cuTurnMeta('));
  let navigation=html.slice(html.indexOf('function setFocus('),html.indexOf("$('#back').onclick"));
  if(mutate)navigation=navigation.replace('cuDraftSave();OFUI.epoch++','OFUI.epoch++');
  const api=vm.runInContext(drafts+'\n'+navigation+';({cuDraftSave,cuDraftRestore,setFocus})',ctx);
  ctx.ofIn.value='A agent draft\nsecond line';api.setFocus('x:B');assert.equal(ctx.ofIn.value,'');ctx.ofIn.value='B agent draft';api.setFocus('x:A');assert.equal(ctx.ofIn.value,'A agent draft\nsecond line');
  api.cuDraftSave();ctx.OFUI.recipient='watch';api.cuDraftRestore();ctx.ofIn.value='A WATCH station draft';api.cuDraftSave();ctx.OFUI.scope='room';api.cuDraftRestore();ctx.ofIn.value='A WATCH room draft';api.cuDraftSave();
  api.setFocus(null);assert.equal(ctx.ofIn.value,'');ctx.ofIn.value='Room WATCH draft';api.setFocus('x:A');assert.equal(ctx.ofIn.value,'A agent draft\nsecond line');
  let release;ctx.post=async(p,b)=>{h.posts.push({p,b});await new Promise(done=>release=done);return {ok:true,json:async()=>({...b,state:'queued'})}};
  const pending=h.send();assert.equal(h.posts.length,1);const submitted=h.posts[0].b;api.setFocus('x:B');assert.equal(ctx.ofIn.value,'B agent draft');ctx.ofIn.value='B new multiline\ndraft';ctx.A['x:A'].name='Changed after submission';release();await pending;
  assert.equal(ctx.ofIn.value,'B new multiline\ndraft');assert.equal(h.posts.length,1,'navigation and completing the old send never submits or replays');
  const record=ctx.OFM.records.get(submitted.request_id);assert.equal(record.agent,'x:A');assert.equal(record.name,'Ada');assert.equal(record.question,'A agent draft\nsecond line');
  api.setFocus('x:A');assert.equal(ctx.ofIn.value,'','submitted draft stays cleared');api.cuDraftSave();ctx.OFUI.recipient='watch';api.cuDraftRestore();assert.equal(ctx.ofIn.value,'A WATCH station draft');ctx.OFUI.scope='room';api.cuDraftRestore();assert.equal(ctx.ofIn.value,'A WATCH room draft');
  api.setFocus(null);assert.equal(ctx.ofIn.value,'Room WATCH draft');assert.equal(h.posts.length,1);
  console.log('ok actual navigation: per-address multiline drafts, room roundtrip and late request identity without resend');
}
(async()=>{await navigationDraftChecks();let caught=false;try{await navigationDraftChecks(true)}catch(e){caught=true}assert.ok(caught,'navigation losing outgoing draft must fail');console.log('ok mutation detected: navigation loses outgoing draft')})().catch(e=>{console.error(e);process.exitCode=1});

/* Boundary helpers are exercised together with their actual input handlers. */
const deskStart=html.indexOf('/* desk interaction helpers:'),deskEnd=html.indexOf('/* end desk interaction helpers */');
assert.ok(deskStart>=0&&deskEnd>deskStart,'desk helper extraction markers exist');
const deskHelpers=html.slice(html.indexOf('*/',deskStart)+2,deskEnd);
function deskBoundsChecks(code=deskHelpers){
  const {deskWidthBounds:bounds}=vm.runInNewContext(code+';({deskWidthBounds})');
  for(const width of [0,-4,80,390,759,760,1000,1600,NaN,Infinity])for(const preference of [-1,0,.46,.8,1,2,NaN,Infinity]){
    const b=bounds(width,preference);assert.ok([b.min,b.max,b.value].every(Number.isFinite));assert.ok(0<=b.min&&b.min<=b.value&&b.value<=b.max);assert.ok(b.max<=(Number.isFinite(width)?Math.max(0,width):0),'work never exceeds available width');
    if(Number.isFinite(width)&&width>=680){assert.ok(width-b.max>=360,'wide split reserves readable conversation');assert.ok(b.min>=320,'wide work remains usable')}
  }
  const wide=bounds(1400,.6);bounds(390,.6);assert.equal(bounds(1400,.6).value,wide.value,'transient narrowing does not mutate stored preference');
  assert.equal(bounds(1000,0).value,bounds(1000,0).min);assert.equal(bounds(1000,1).value,bounds(1000,1).max);
}
deskBoundsChecks();let boundsCaught=false;try{deskBoundsChecks(deskHelpers.replace('Math.min(max,w*p)','w*p'))}catch(e){boundsCaught=true}assert.ok(boundsCaught,'removing effective upper clamp must fail');
function composerKeyChecks(code=deskHelpers){
  let handler,sends=0;const line=html.split('\n').find(x=>x.startsWith("ofIn.addEventListener('keydown'"));assert.ok(line);
  const ctx={ofIn:{addEventListener:(name,fn)=>{assert.equal(name,'keydown');handler=fn}},cuSend:()=>sends++};vm.runInNewContext(code+'\n'+line,ctx);
  for(const extra of [{shiftKey:true},{ctrlKey:true},{altKey:true},{metaKey:true},{isComposing:true},{keyCode:229},{key:'a'}]){let prevented=false;handler({key:'Enter',preventDefault(){prevented=true},...extra});assert.equal(sends,0,'modified/composing key must not send');assert.equal(prevented,false,'native typing behavior remains available')}
  let prevented=0;handler({key:'Enter',preventDefault(){prevented++}});assert.equal(sends,1);assert.equal(prevented,1);
}
composerKeyChecks();for(const guard of ['&&!e.shiftKey','&&!e.isComposing','&&e.keyCode!==229']){let caught=false;try{composerKeyChecks(deskHelpers.replace(guard,''))}catch(e){caught=true}assert.ok(caught,'removed composer guard '+guard+' must fail')}
console.log('ok desk widths: finite clamps/transients/readable split; composer: actual Enter handler, modifiers and IME; mutations detected');

/* A small cross-parent DOM for the rail; moving a focused subtree models browser blur. */
function railDOM(){
  const nodes=new Map(),doc={activeElement:null},events={moves:0,focus:[]};
  function element(tag='div'){
    const el={tag,hidden:false,textContent:'',children:[],dataset:{},parentElement:null,
      setAttribute(k,v){this[k]=v},
      append(...items){for(const item of items)this.insertBefore(item,null)},
      insertBefore(item,before){
        if(before&&before.parentElement!==this)throw Error('stale insertion cursor');
        if(item===before)return;
        for(let p=this;p;p=p.parentElement)if(p===item)throw Error('cyclic DOM move');
        if(item.parentElement){if(item.contains(doc.activeElement))doc.activeElement=null;item.remove()}
        const i=before?this.children.indexOf(before):this.children.length;this.children.splice(i,0,item);item.parentElement=this;events.moves++;
      },
      remove(){if(this.parentElement){const p=this.parentElement;p.children.splice(p.children.indexOf(this),1);this.parentElement=null}},
      contains(other){for(let n=other;n;n=n.parentElement)if(n===this)return true;return false},
      querySelector(selector){const stack=[...this.children];while(stack.length){const n=stack.shift();if(selector==='[aria-current=true]'?n['aria-current']==='true':selector[0]==='.'?n.className===selector.slice(1):n.tag===selector)return n;stack.unshift(...n.children)}return null},
      focus(options){doc.activeElement=this;events.focus.push({el:this,options})}
    };
    Object.defineProperties(el,{firstElementChild:{get:()=>el.children[0]||null},nextElementSibling:{get:()=>el.parentElement?.children[el.parentElement.children.indexOf(el)+1]||null},isConnected:{get:()=>{let n=el;while(n.parentElement)n=n.parentElement;return !!n.root}},innerHTML:{set(value){if(tag!=='button'||!value.startsWith('<i aria-hidden="true">'))throw Error('Untrusted text must not become HTML');const icon=element('i'),body=element('span'),name=element('strong'),meta=element('span'),provider=element('span'),status=element('span'),relation=element('small');provider.className='agent-provider';status.className='agent-status';relation.className='agent-relation';if(value.includes('<span class="agent-provider"></span><span class="agent-meta">')){meta.append(status);body.append(name,provider,meta,relation)}else{meta.append(provider,status);body.append(name,meta,relation)};el.append(icon,body)}}});
    return el;
  }
  const node=id=>{if(!nodes.has(id)){const n=element();n.root=true;nodes.set(id,n)}return nodes.get(id)};
  doc.createElement=element;return {node,doc,events};
}
function deskForestChecks(code=deskHelpers){
  const forest=vm.runInNewContext(code+';deskAgentForest'),a={child:{parent:'parent',runtime:' Codex '},parent:{runtime:'Claude'},launch:{parent:'parent',soft:true},related:{parent:'parent',soft:true,via:'related, not launched by it: files'},maybe:{maybe:{parent:'parent'}},missing:{parent:'gone'},gone:{name:'Hidden parent'},self:{parent:'self'},x:{parent:'y'},y:{parent:'x'},unknown:{model:'claude-5',runtime:' '},cloud:{runtime:'Codex cloud'},future:{runtime:'Future tool'}};
  const ids=['child','launch','related','maybe','parent','missing','self','x','y','unknown','cloud','future'];
  const rows=forest(ids,a),by=new Map(rows.map(n=>[n.id,n]));
  assert.deepEqual(Array.from(rows,n=>n.id),['related','maybe','parent','child','launch','missing','self','x','y','unknown','cloud','future']);
  assert.equal(by.get('child').parent,'parent');assert.equal(by.get('child').relation,'recorded');assert.equal(by.get('launch').caption,'Parent inferred');
  assert.equal(by.get('related').parent,null);assert.match(by.get('related').caption,/not a parent/);assert.equal(by.get('maybe').parent,null);assert.match(by.get('maybe').caption,/unconfirmed/);
  assert.equal(by.get('missing').parent,null);assert.equal(by.get('missing').caption,'Parent not shown · Hidden parent','dismissed parent remains outside visible forest');
  for(const id of ['self','x','y']){assert.equal(by.get(id).parent,null);assert.match(by.get(id).caption,/uncertain/)}
  assert.equal(by.get('child').provider,'Codex');assert.equal(by.get('unknown').provider,'Unknown');assert.equal(by.get('cloud').provider,'Codex cloud');assert.equal(by.get('future').provider,'Future tool');
  assert.equal(forest(['child','child','absent'],a).length,1,'exactly once, only available agents');
  a.child.parent='related';assert.equal(forest(ids,a).find(n=>n.id==='child').parent,'related','recorded child of unrelated root still nested');delete a.child.parent;assert.equal(forest(ids,a).find(n=>n.id==='child').parent,null);
  const deep={},order=[];for(let i=0;i<12000;i++){const id=String(i);order.unshift(id);deep[id]={parent:i?String(i-1):''}}
  const chain=forest(order,deep);assert.equal(chain.length,12000);assert.equal(chain.at(-1).depth,11999,'iterative traversal handles deep input');
}
deskForestChecks();for(const mutation of [c=>c.replace("/^related/.test(a.via||'')","false"),c=>c.replace("if(a.soft)n.caption='Parent inferred'","if(false)n.caption='Parent inferred'")]){let caught=false;try{deskForestChecks(mutation(deskHelpers))}catch(e){caught=true}assert.ok(caught,'relationship truth mutation must fail')}
console.log('ok agent forest: relationship truth, cycles, missing/dismissed parents, input ordering, unique rows, provider source, 12000-level iterative chain; mutations detected');

/* Agent rail signals preserve their existing alternate views without duplicate lists. */
function deskSignalsChecks(mutate=false){
  const nodes=new Map(),node=id=>{if(!nodes.has(id))nodes.set(id,{hidden:false,textContent:'',firstElementChild:null,children:[],setAttribute(k,v){this[k]=v}});return nodes.get(id)};
  const requests=[];
  const ctx={focus:'A',A:{A:{name:'Ada',runtime:'Codex'}},DESK:{open:false,agents:new Map(),agentRows:new Map()},bayPg:0,PINS:{},STEADY:false,order:[],document:{activeElement:null},$:node,txt:(el,text)=>el.textContent=text,one:String,stateOf:a=>({label:'Waiting',need:a.request}),responsePaint:(a,n)=>requests.push(n),deskSetViewUI(){},deskFocus(){},render(){}};
  let code=html.slice(html.indexOf('function deskPaint('),html.indexOf('function deskTheme('));
  if(mutate)code=code.replace("$('#bay').hidden=bayPg===0;","$('#bay').hidden=false;");
  const signals=[1,2].map(page=>({dataset:{bay:String(page)},setAttribute(k,v){this[k]=v}}));node('#deskSignals').children=signals;
  code+='\n'+html.split('\n').find(x=>x.startsWith("$('#deskSignals').onclick="));
  vm.createContext(ctx);const paint=vm.runInContext(deskHelpers+'\n'+code+';deskPaint',ctx);ctx.desk=paint;
  paint();assert.equal(node('#bay').hidden,true);assert.equal(node('#deskAgentList').hidden,false);assert.equal(node('#deskRespond').hidden,true);
  for(const signal of signals){node('#deskSignals').onclick({target:{closest:()=>signal}});assert.equal(ctx.bayPg,Number(signal.dataset.bay));assert.equal(node('#bay').hidden,false);assert.equal(signal['aria-pressed'],'true');node('#deskSignals').onclick({target:{closest:()=>signal}});assert.equal(ctx.bayPg,0);assert.equal(node('#bay').hidden,true);assert.equal(signal['aria-pressed'],'false')}
  ctx.A.A.request={kind:'Approve',perm:{pid:'p1'}};paint();assert.equal(requests.at(-1),ctx.A.A.request);assert.equal(node('#deskRespond').hidden,false);assert.equal(ctx.DESK.open,false);
}
deskSignalsChecks();let duplicateCaught=false;try{deskSignalsChecks(true)}catch(e){duplicateCaught=true}assert.ok(duplicateCaught);console.log('ok rail signals and request-to-card handoff; duplicate-list mutation detected');

/* Live command alerts remain visible outside closed Work, without replacing a pending request. */
function deskAlertChecks(mutation=''){
  const headStart=html.indexOf('<header id="deskHead"'),headEnd=html.indexOf('</header>',headStart),alertAt=html.indexOf('id="deskAlert"');
  assert.ok(headStart>=0&&alertAt>headStart&&alertAt<headEnd,'alert belongs to the persistent desk header');
  assert.ok(headEnd<html.indexOf('id="deskWork"',headEnd),'alert is outside the closable Work area');
  const {node,doc,events}=railDOM(),list=node('#deskAgentList'),agents=new Map();
  const ctx={focus:'A',A:{A:{name:'Ada',runtime:'Codex'},B:{name:'Bram',runtime:'Claude'}},DESK:{open:false,agents,agentRows:new Map()},bayPg:0,PINS:{},STEADY:false,order:['A','B'],document:doc,Date:{now:()=>1000*1000},$:node,one:t=>String(t||'').replace(/\s+/g,' '),stateOf:a=>({label:'between actions',need:a.request,busy:!!a.busy}),responsePaint:(a,n)=>ctx.cardRequest=n,deskSetViewUI(){},deskFocus:el=>el.focus({preventScroll:true}),post(){throw Error('Painting alerts must not submit')},cuSend(){throw Error('Painting alerts must not send')}};
  let code=html.slice(html.indexOf('function deskPaint('),html.indexOf('function deskTheme('));
  const mutations={
    hide:c=>c.replace("$('#deskAlert').hidden=!alert;","$('#deskAlert').hidden=true;"),
    expiry:c=>c.replace('now-a.alert.ts<180','now-a.alert.ts<=180'),
    railExpiry:c=>c.replace('now-agent.alert.ts<180','now-agent.alert.ts<=180'),
    rail:c=>c.replace("alarm?'alarm':",''),
    priority:c=>c.replace("state.need?'need':alarm?'alarm':state.busy?'busy':'idle'","alarm?'alarm':state.need?'need':state.busy?'busy':'idle'"),
    steady:c=>c.replace("STEADY?'(text hidden in steady mode)':one(alert.text).slice(0,300)",'one(alert.text).slice(0,300)')
  };
  if(mutation){const altered=mutations[mutation](code);assert.notEqual(altered,code,'mutation must change extracted behavior');code=altered}
  const realTxt=html.split('\n').find(line=>line.startsWith('const txt='));const paint=vm.runInNewContext(realTxt+'\n'+deskHelpers+'\n'+code+';deskPaint',ctx);
  paint();assert.equal(node('#deskAlert').hidden,true);assert.equal(node('#deskAlert').textContent,'');assert.equal(agents.get('A').dataset.state,'idle');
  const command='git push origin main <img src=x onerror=bad()>',alert={ts:990,label:'a push',text:command};ctx.A.A.alert=alert;paint();
  assert.equal(ctx.DESK.open,false);assert.equal(node('#deskAlert').hidden,false);assert.equal(node('#deskAlert').textContent,'Looks like a push · '+command);assert.equal(agents.get('A').dataset.state,'alarm');assert.equal(agents.get('A').querySelector('.agent-status').textContent,'Looks like a push','rail alarm remains compact');assert.equal(agents.get('A').title,undefined,'rail has no raw-command tooltip');
  assert.equal(ctx.DESK.agents.get('A'),list.children[0].children[0],'keyed agent identity survives the alert repaint');
  ctx.A.A.request={kind:'Approve',text:'Inspect a different command',sure:true,perm:{pid:'p1',sent:false}};paint();
  assert.equal(node('#deskAlert').hidden,false);assert.equal(ctx.cardRequest,ctx.A.A.request,'independent response card receives the concurrent request');assert.equal(node('#deskRespond').hidden,false);assert.equal(agents.get('A').dataset.state,'need');assert.equal(agents.get('A').querySelector('.agent-status').textContent,'Approve','pending request keeps rail priority');
  ctx.A.A.request.sure=false;paint();assert.equal(agents.get('A').querySelector('.agent-status').textContent,'Approve · possible');assert.equal(node('#deskAlert').hidden,false);
  delete ctx.A.A.request;ctx.focus='B';paint();assert.equal(node('#deskAlert').hidden,true);assert.equal(node('#deskAlert').textContent,'','station switch clears previous alert');assert.equal(agents.get('A').dataset.state,'alarm','other agent still carries its live rail alarm');
  ctx.A.B.alert={ts:995,label:'a delete',text:'remove generated files'};paint();assert.equal(node('#deskAlert').textContent,'Looks like a delete · remove generated files');delete ctx.A.B.alert;paint();assert.equal(node('#deskAlert').hidden,true);assert.equal(node('#deskAlert').textContent,'');
  ctx.focus='A';ctx.A.A.alert.ts=821;paint();assert.equal(node('#deskAlert').hidden,false,'179 seconds is still live');assert.equal(agents.get('A').dataset.state,'alarm');
  ctx.A.A.alert.ts=820;paint();assert.equal(node('#deskAlert').hidden,true,'180 seconds expires');assert.equal(node('#deskAlert').textContent,'');assert.equal(agents.get('A').dataset.state,'idle','rail uses the same expiry');assert.equal(agents.get('A').querySelector('.agent-status').textContent,'between actions');
  ctx.A.A.alert.ts=819;paint();assert.equal(node('#deskAlert').hidden,true);
  ctx.A.A.alert.ts=990;ctx.STEADY=true;paint();assert.equal(node('#deskAlert').textContent,'Looks like a push · (text hidden in steady mode)');assert.equal(agents.get('A').querySelector('.agent-status').textContent,'Looks like a push');assert.equal(agents.get('A').title,undefined,'steady rail has no raw-command tooltip');assert.equal(node('#deskAlert').textContent.includes(command),false);
  ctx.STEADY=false;ctx.A.A.alert.text='x'.repeat(350);paint();assert.equal(node('#deskAlert').textContent,'Looks like a push · '+'x'.repeat(300));
  delete ctx.A.A.alert;paint();assert.equal(node('#deskAlert').hidden,true);assert.equal(node('#deskAlert').textContent,'');assert.equal(agents.get('A').dataset.state,'idle');assert.equal(ctx.DESK.open,false);
  const oldA=agents.get('A'),oldB=agents.get('B'),rowA=ctx.DESK.agentRows.get('A'),rowB=ctx.DESK.agentRows.get('B');
  doc.activeElement=oldB;const moves=events.moves;paint();assert.equal(events.moves,moves,'ordinary paint does not move nodes');assert.equal(events.focus.length,0,'ordinary paint does not steal focus');
  ctx.A.B.parent='A';paint();assert.equal(agents.get('B'),oldB);assert.equal(ctx.DESK.agentRows.get('B'),rowB);assert.equal(rowB.li.parentElement,rowA.group);assert.equal(doc.activeElement,oldB);assert.equal(events.focus.at(-1).options.preventScroll,true);
  assert.equal(oldB.querySelector('.agent-provider').textContent,'Claude');assert.equal(oldB.querySelector('.agent-provider').parentElement,oldB.children[1],'provider lives beside the name, outside status line');assert.equal(rowB.li.dataset.depth,'1');
  assert.equal(rowA.group['aria-label'],'Children of Ada');
  delete ctx.A.B.parent;ctx.A.C={name:'Cleo',runtime:'Cursor'};ctx.order.push('C');paint();
  const oldC=agents.get('C'),rowC=ctx.DESK.agentRows.get('C');ctx.A.B.parent='A';paint();
  assert.equal(rowB.li.parentElement,rowA.group);assert.equal(rowC.li.parentElement,list,'later root survives earlier sibling moving beneath parent');assert.equal(list.children[1],rowC.li);
  delete ctx.A.B.parent;paint();assert.equal(rowB.li.parentElement,list);assert.equal(list.children[1],rowB.li);assert.equal(agents.get('C'),oldC,'child-to-root preserves other rows');
  delete ctx.A.B.parent;ctx.A.A.parent='B';paint();assert.equal(rowA.li.parentElement,rowB.group,'reversing ancestry never makes a transient DOM cycle');assert.equal(doc.activeElement,oldB);
  ctx.A.A.soft=true;paint();assert.equal(oldA.querySelector('.agent-relation').textContent,'Parent inferred');
  ctx.A.A.via='related, not launched by it: files';paint();assert.equal(rowA.li.parentElement,list);assert.match(oldA.querySelector('.agent-relation').textContent,/not a parent/);
  doc.activeElement=oldB;ctx.order=['A'];paint();assert.equal(agents.has('B'),false);assert.equal(doc.activeElement,oldA,'removed focused row falls back to selected agent');assert.equal(agents.get('A'),oldA);
}
deskAlertChecks();for(const mutation of ['hide','expiry','railExpiry','rail','priority','steady']){let caught=false;try{deskAlertChecks(mutation)}catch(e){caught=true}assert.ok(caught,'live-alert mutation '+mutation+' must fail')}
console.log('ok live desk alerts: closed Work, expiry boundaries, station changes, concurrent requests, keyed rail priority, safe text and steady masking; six mutations detected');

/* Dispatch actual capture/bubble handlers in browser order, including existing Officer/Watch handlers. */
function deskKeyboardChecks(mutate=false){
  const callbacks=[],actions=[],nodes=new Map(),node=id=>{if(!nodes.has(id))nodes.set(id,{hidden:true,focus:()=>actions.push('focus '+id)});return nodes.get(id)};
  const target={insideOfficer:true,closest:()=>null,matches:()=>false};
  const ctx={focus:'A',DESK:{drawer:'agents',open:true},OFUI:{docked:true,epoch:1},ofEl:{contains:t=>t.insideOfficer,classList:{contains:()=>true}},$:node,document:{activeElement:target},
    addEventListener:(event,fn,capture)=>{if(event==='keydown')callbacks.push({fn,capture:!!capture})},
    deskCloseDrawer:()=>{ctx.DESK.drawer=null;actions.push('drawer')},deskCloseWork:()=>{ctx.DESK.open=false;actions.push('work')},render(){},setFocus:id=>{ctx.focus=id;actions.push('room')},watchOpen:on=>{node('#watchP').hidden=!on;actions.push('watch')},ofToggle:()=>actions.push('officer'),deskVisible:()=>true};
  const watch=html.split('\n').find(x=>x.startsWith("addEventListener('keydown',e=>{if(e.key==='Escape'&&!$('#watchP').hidden)"));
  let desk=html.slice(html.indexOf('function deskKeydown('),html.indexOf('/* end desk keyboard handling */'));assert.ok(desk.includes("addEventListener('keydown',deskKeydown,true)"));
  if(mutate)desk=desk.replace('e.stopImmediatePropagation();','');
  const bubbleStart=html.indexOf("addEventListener('keydown',e=>{\n  if((e.key==='Enter'");assert.ok(bubbleStart>=0);const bubble=html.slice(bubbleStart,html.indexOf('let wasPortrait=',bubbleStart));
  const officer=html.split('\n').find(x=>x.startsWith("addEventListener('keydown',e=>{if(e.key!=='Escape')return;"));assert.ok(watch&&officer);
  vm.runInNewContext(deskHelpers+'\n'+watch+'\n'+desk+'\n'+bubble+'\n'+officer,ctx);
  const dispatch=(key,extra={})=>{let immediate=false,stopped=false;const e={key,target,preventDefault(){},stopImmediatePropagation(){immediate=true;stopped=true},stopPropagation(){stopped=true},...extra};for(const capture of [true,false]){if(!capture&&stopped)break;for(const c of callbacks.filter(c=>c.capture===capture)){c.fn(e);if(immediate)break}if(immediate)break}};
  dispatch('Escape');assert.deepEqual(actions,['drawer']);assert.equal(ctx.DESK.open,true);assert.equal(ctx.focus,'A');assert.equal(ctx.OFUI.epoch,1,'drawer Escape never also runs Officer handling');
  actions.length=0;dispatch('Escape');assert.deepEqual(actions,['work']);assert.equal(ctx.focus,'A');assert.equal(ctx.OFUI.epoch,1,'work Escape never also runs Officer handling');
  actions.length=0;dispatch('Escape');assert.deepEqual(actions,['focus #recipient [aria-pressed="true"]']);assert.equal(ctx.focus,'A','existing Officer focus-return remains intact');
  actions.length=0;target.insideOfficer=false;dispatch('Escape');assert.deepEqual(actions,['room']);assert.equal(ctx.focus,null);
  ctx.focus='A';ctx.DESK.open=true;node('#watchP').hidden=false;actions.length=0;dispatch('Escape');assert.deepEqual(actions,['watch','focus #watchB']);assert.equal(ctx.DESK.open,true,'topmost Watch panel consumes Escape alone');
  actions.length=0;dispatch('Escape');assert.deepEqual(actions,['work']);
  const first={focus:()=>actions.push('first'),disabled:false,tabIndex:0},last={focus:()=>actions.push('last'),disabled:false,tabIndex:0},pane=node('#deskAgents');pane.contains=el=>el===first||el===last;pane.querySelectorAll=()=>[first,last];ctx.DESK.drawer='agents';
  actions.length=0;ctx.document.activeElement=last;dispatch('Tab');assert.deepEqual(actions,['first']);actions.length=0;ctx.document.activeElement=first;dispatch('Tab',{shiftKey:true});assert.deepEqual(actions,['last']);
}
deskKeyboardChecks();let escapeCaught=false;try{deskKeyboardChecks(true)}catch(e){escapeCaught=true}assert.ok(escapeCaught,'missing capture stop must fail');console.log('ok layered keyboard: actual Watch/desk/Officer/room handlers, one-layer Escape, drawer focus trap; propagation mutation detected');

/* Rapid close/reopen and reduced motion keep hidden state and focus consistent. */
function workTransitionChecks(mutate=false){
  const nodes=new Map(),node=id=>{if(!nodes.has(id))nodes.set(id,{hidden:true,inert:false,offsetWidth:100,classList:{values:new Set(),add(x){this.values.add(x)},remove(x){this.values.delete(x)}},setAttribute(k,v){this[k]=v},contains:el=>el?.owner===id});return nodes.get(id)};
  let serial=0,reduced=false;const timers=new Map(),focused=[];
  const ctx={focus:'A',A:{A:{permScreen:'p1',$:{yes:{disabled:false},no:{disabled:false}}}},DESK:{open:false,timer:0,compact:false,expanded:false},document:{activeElement:{owner:'#deskWork'}},$:node,clearTimeout:id=>timers.delete(id),setTimeout:fn=>{timers.set(++serial,fn);return serial},deskCloseDrawer(){},deskSetViewUI(){},deskSize(){},deskEndDrag(){},deskInert(){},deskReduced:()=>reduced,deskFocus:el=>focused.push(el),watchOpen:on=>node('#watchP').hidden=!on};
  let code=html.slice(html.indexOf('function deskReveal('),html.indexOf('function deskCloseDrawer('));
  if(mutate)code=code.replace("$('#deskWork').inert=true;","$('#deskWork').inert=false;");
  const api=vm.runInNewContext(code+';({deskReveal,deskCloseWork})',ctx);api.deskReveal('doing');assert.equal(ctx.DESK.open,true);assert.equal(node('#deskWork').hidden,false);assert.equal(node('#deskWork').inert,false);
  api.deskCloseWork();assert.equal(ctx.DESK.open,false);assert.equal(node('#deskWork').inert,true,'closing pane becomes noninteractive before animation ends');assert.equal(ctx.A.A.permScreen,null,'closing invalidates approval eligibility immediately');assert.equal(ctx.A.A.$.yes.disabled,true,'closing disables Allow immediately');assert.equal(ctx.A.A.$.no.disabled,true,'closing disables Deny immediately');assert.equal(node('#deskWork').hidden,false,'animated close remains painted until settled');assert.equal(focused.length,1);
  const stale=[...timers.values()][0];api.deskReveal('seeing');assert.equal(timers.size,0,'reopening cancels pending hide');stale();assert.equal(node('#deskWork').hidden,false,'stale completion cannot hide reopened work');assert.equal(node('#deskWork').inert,false);
  api.deskCloseWork(false,true);assert.equal(node('#deskWork').hidden,true);assert.equal(timers.size,0);
  reduced=true;api.deskReveal('doing');api.deskCloseWork();assert.equal(node('#deskWork').hidden,true,'reduced motion completes immediately');assert.equal(timers.size,0);
}
workTransitionChecks();let closingCaught=false;try{workTransitionChecks(true)}catch(e){closingCaught=true}assert.ok(closingCaught,'missing immediate inertness must fail');console.log('ok work lifecycle: close/reopen race, stale timer, immediate ineligibility/focus return, reduced motion; inert mutation detected');

/* Keyboard resizing uses effective clamped width, including endpoints and transient zero-width hosts. */
{
  let keydown,width=1000;const ctx={focus:null,DESK:{preference:.99},$:id=>id==='#deskWorkspace'?{clientWidth:width}:{addEventListener:(name,fn)=>keydown=fn},deskSize(){}};
  const line=html.split('\n').find(x=>x.startsWith("$('#deskSizer').addEventListener('keydown'"));assert.ok(line);vm.runInNewContext(deskHelpers+'\n'+line,ctx);
  const key=k=>keydown({key:k,preventDefault(){},stopPropagation(){}});
  key('ArrowRight');assert.equal(ctx.DESK.preference,.624,'first arrow starts from actual 640px cap rather than oversized preferred width');key('Home');assert.equal(ctx.DESK.preference,.32);key('End');assert.equal(ctx.DESK.preference,.64);
  width=0;key('ArrowLeft');assert.ok(Number.isFinite(ctx.DESK.preference));width=1000;key('End');assert.equal(ctx.DESK.preference,.64);
  console.log('ok actual resize keyboard: effective width, both endpoints and zero-width recovery');
}

/* Work citations reveal Activity while retaining the precise historical evidence pin. */
{
  const evidence={seq:42,cmd:'inspect evidence'},entry={t:'did',n:8,e:evidence},reveals=[];let scrolled=0;
  const row={getClientRects:()=>[{}],querySelector:()=>null,scrollIntoView:()=>scrolled++,classList:{add(){},remove(){}}};
  const ctx={focus:'A',A:{A:{name:'Ada',story:[entry],open:new Set()}},setFocus:id=>{ctx.focus=id},deskReveal:v=>reveals.push(v),capAdd:(set,key)=>set.add(key),STATION:{compact:true},render(){},performance:{now:()=>0},STEPPING:false,storyEl:{dataset:{a:'A'},querySelector:selector=>selector==='[data-s="8"]'?row:null},setTimeout(){},cuNotice(){throw Error('known work evidence should be revealed')},esc:String};
  const code=html.slice(html.indexOf('function ofJump('),html.indexOf('/* agent message helpers:'));vm.runInNewContext(code+';ofJump("A",42)',ctx);
  assert.deepEqual(reveals,['doing']);assert.equal(ctx.A.A.pin,evidence);assert.equal(ctx.deskView,'doing');assert.equal(scrolled,1);
  console.log('ok work citation: Activity revealed, exact historical pin and entry preserved');
}

/* Agent responses opt into rich Markdown; WATCH defaults and typed user text remain distinct. */
function richResponseChecks(parser=answerCode,source=html.slice(html.indexOf('function cuSources('),html.indexOf('function cuNotice('))){
  const md=vm.runInNewContext(parser+';ofParas',{esc:safeEsc,URL});
  assert.equal(md('**Ready**',true),'<p><b>Ready</b></p>');assert.equal(md('**Ready**'),'<p class="h2">Ready</p>','WATCH shorthand unchanged');
  assert.equal(md('### Checks',true),'<h6 class="h3">Checks</h6>');assert.equal(md('### Checks'),'<p class="h3">Checks</p>');
  for(const [syntax,tag]of [['#','h4'],['##','h5'],['###','h6']])assert.equal(md(syntax+' Summary',true),'<'+tag+' class="h'+syntax.length+'">Summary</'+tag+'>');
  assert.match(md('[x](https://a.com/`b`)',true),/href="https:\/\/a.com\/%60b%60"/);assert.doesNotMatch(md('[x](https://a.com/`b`)',true),/%00/);
  for(const address of ['https://u@evil.example','https://u:pw@evil.example'])assert.doesNotMatch(md('[label]('+address+')',true),/<a /);
  assert.match(md('[Different label](https://example.com/a?x=1&y=2)',true),/title="https:\/\/example.com\/a\?x=1&amp;y=2"/);
  assert.match(md('[guide](https://example.com)',true),/<a href="https:\/\/example.com\/" title="https:\/\/example.com\/" target="_blank" rel="noopener noreferrer">guide<\/a>/);
  assert.equal(md('[guide](https://example.com)'),'<p>guide (https://example.com)</p>','WATCH links remain inert text');
  for(const u of ['javascript:alert','data:text/html,test','file:///private/test','/relative/path'])assert.doesNotMatch(md('[link]('+u+')',true),/<a /);
  assert.match(md('[x](https://example.com/"onmouseover=bad)',true),/href="https:\/\/example.com\/%22onmouseover=bad"/);
  assert.match(md('[x](https://example.com/**path**)',true),/href="https:\/\/example.com\/\*\*path\*\*"/,'emphasis cannot modify a generated href');
  assert.match(md('[see `code`](https://example.com)',true),/>see <code>code<\/code><\/a>/);assert.doesNotMatch(md('[see `code`](https://example.com)',true),/[\u0000\u0001]/);
  assert.equal(md('`[literal](https://example.com)`',true),'<p><code>[literal](https://example.com)</code></p>');
  assert.doesNotMatch(md('```\n[link](https://example.com) <img src=x>\n```',true),/<a |<img/);
  assert.doesNotMatch(md('<script>bad()</script> ![image](https://example.com/image.png)',true),/<script>|<img/);
  assert.match(md('| Case | Result |\n|---|---|\n| One | **pass** |',true),/<table><thead>.*<b>pass<\/b>/);
  const plain=vm.runInNewContext(html.split('\n').find(l=>l.startsWith('const plain='))+';plain',{str:String});
  class Row{constructor(){this.dataset={};this.writes=0}set innerHTML(v){this.html=v;this.writes++;this.details=v.includes('<details')?{open:v.includes('<details open')}:null}querySelector(){return this.details}remove(){}}
  const helpers=vm.runInNewContext(unifiedCode+';({cuSourceRows,cuEventKey,cuEnvelopeMatch})');
  const ctx={...helpers,CU:{nodes:new Map()},OFM:{records:new Map()},document:{createElement:()=>new Row()},plain,one:String,esc:safeEsc,ofParas:md,clock:String,STEADY:false};
  const paint=vm.runInNewContext(source+';cuSources',ctx),text='### Ready\n\n**Done** in `season.ts`. <img src=x>';
  const a={id:'A',name:'Ada',story:[{t:'you',seq:1,ts:1,text},{t:'say',seq:2,ts:2,text}],msgs:[],q:[]};paint(a);
  const user=ctx.CU.nodes.get('source:A:seq:1').el,agent=ctx.CU.nodes.get('source:A:seq:2').el;
  assert.match(user.html,/### Ready/);assert.match(user.html,/\*\*Done\*\* in `season.ts`/);assert.doesNotMatch(user.html,/<h[4-6]|<code>|<img/);
  assert.match(agent.html,/<div class="rich-source"><h6 class="h3">Ready<\/h6>/);assert.match(agent.html,/<b>Done<\/b> in <code>season.ts<\/code>/);assert.doesNotMatch(agent.html,/<img/);
  assert.match(agent.html,/<b>Ada<\/b><small>Recorded log · 2<\/small>/);assert.doesNotMatch(agent.html,/<span class="closed-update">###/,'collapsed summary uses plain preview');
  agent.details.open=false;const writes=agent.writes;paint(a);assert.equal(agent.writes,writes);assert.equal(agent.details.open,false);
  ctx.STEADY=true;paint(a);assert.doesNotMatch(agent.html,/season.ts|<h[4-6]/);assert.match(agent.html,/narration hidden/);
}
richResponseChecks();for(const [label,parser,source]of [
 ['unsafe protocol',answerCode.replace("if(!['http:','https:'].includes(u.protocol)||u.username||u.password)throw Error();",''),undefined],
 ['bold shorthand',answerCode.replace('if(!agent&&(m=x.match','if((m=x.match'),undefined],
 ['lost rich mode',answerCode,html.slice(html.indexOf('function cuSources('),html.indexOf('function cuNotice(')).replace('ofParas(content,true)','ofParas(content)')]
]){let failed=false;try{richResponseChecks(parser,source)}catch(e){failed=true}assert.ok(failed,label+' mutation detected')}
console.log('ok response Markdown: raw say/plain user, preserved WATCH defaults, safe HTTP/S links, semantic headings, code/HTML/image safety, stable disclosure and masking; mutations detected');

/* A supported-size malformed response must not trigger quadratic link scans. */
function malformedResponseChecks(code=answerCode){
  for(const input of ['['.repeat(60000),'[x]('.repeat(15000),')'+'[x]('.repeat(14999)]){
    const out=vm.runInNewContext(code+';ofParas(input,true)',{esc:safeEsc,URL,input},{timeout:150});
    assert.equal(out,'<p>'+input+'</p>','malformed response stays literal within the VM budget');
  }
  const md=vm.runInNewContext(code+';ofParas',{esc:safeEsc,URL});
  assert.match(md('[Guide](https://example.com)',true),/<a href="https:\/\/example.com\/"/);
  assert.equal(md('![Diagram](https://example.com/a.png)',true),'<p>Diagram (image not shown: https://example.com/a.png)</p>');
  assert.equal(md('[a [b](https://example.com)'),'<p>a [b (https://example.com)</p>','WATCH default matcher is unchanged');
  assert.match(md('[<b>label](https://example.com)',true),/>&lt;b&gt;label<\/a>/);
}
malformedResponseChecks();for(const old of ["x.replace(/!?\\[([^\\]\\n]*)\\]\\(([^)\\n]*)\\)/g,renderLink)","x.replace(/!?\\[([^\\[\\]\\n]*)\\]\\(([^)\\n]*)\\)/g,renderLink)"]){
  assert.ok(answerCode.includes('ofResponseLinks(x,renderLink)'));
  let timedOut=false;try{malformedResponseChecks(answerCode.replace('ofResponseLinks(x,renderLink)',old))}catch(e){timedOut=e.code==='ERR_SCRIPT_EXECUTION_TIMEOUT'}
  assert.ok(timedOut,'old quadratic label/URL matcher must exceed bounded VM test');
}
console.log('ok response link scan: 60000-char bracket and URL-tail cases, safe links/images, WATCH defaults; both quadratic mutations time out');

/* Live repainting must not reparse unchanged agent responses. Exercise the real parser. */
function sourceFormattingChecks(source=html.slice(html.indexOf('function cuSources('),html.indexOf('function cuNotice('))){
  const helpers=vm.runInNewContext(unifiedCode+';({cuSourceRows,cuEventKey,cuEnvelopeMatch})');let calls=0;
  class Row{constructor(){this.dataset={}}set innerHTML(v){this.html=v;this.details=v.includes('<details')?{open:v.includes('<details open')}:null}querySelector(){return this.details}remove(){}}
  const ctx={...helpers,CU:{nodes:new Map()},OFM:{records:new Map()},document:{createElement:()=>new Row()},plain:String,one:String,esc:safeEsc,clock:String,STEADY:false,ofParas:(...args)=>{calls++;return responseMarkup(...args)}};
  const paint=vm.runInNewContext(source+';cuSources',ctx),a={id:'A',name:'Ada',story:[{t:'you',seq:1,ts:1,text:'Review the results'},...Array.from({length:3},(_,i)=>({t:'say',seq:i+2,ts:i+2,text:'### Results '+i+'\n\n**Checked** `season.ts`\n\n'+('The pricing checks pass. '.repeat(80))}))],msgs:[],q:[]};
  paint(a);assert.equal(calls,3,'initial paint formats every agent response once');
  const older=ctx.CU.nodes.get('source:A:seq:2').el,latest=ctx.CU.nodes.get('source:A:seq:4').el;assert.equal(older.details.open,false);assert.equal(latest.details.open,true);older.details.open=true;
  calls=0;paint(a);paint(a);assert.equal(calls,0,'unchanged live repaints never invoke Markdown parser');assert.equal(older.details.open,true,'disclosure choice survives cached painting');
  a.story[2].text+='\n\nOne revised result.';paint(a);assert.equal(calls,1,'editing one response reparses only that response');assert.equal(older.details.open,true);
  calls=0;ctx.STEADY=true;paint(a);assert.equal(calls,3,'mask change invalidates every narration');calls=0;paint(a);assert.equal(calls,0,'unchanged masked responses also reuse formatting');
}
sourceFormattingChecks();{
  let source=html.slice(html.indexOf('function cuSources('),html.indexOf('function cuNotice('));
  const format=source.split('\n').find(line=>line.trim().startsWith("const html='<b>'+esc(by)"));assert.ok(format);
  source=source.replace(format+'\n','').replace('    if(r.content!==content||r.by!==by){',format+'\n    if(r.content!==content||r.by!==by){');
  let failed=false;try{sourceFormattingChecks(source)}catch(e){failed=true}assert.ok(failed,'hoisting formatting outside invalidation must fail');
}
console.log('ok source formatting cache: initial parse, zero unchanged parses, one edited response, disclosure and mask transitions; hoisted-parser mutation detected');

/* Actual header resize, drawer and theme handlers share native desktop/mobile controls. */
const headerBehavior=html.slice(html.indexOf('function deskVisible('),html.indexOf('function deskSetViewUI('));
const drawerBehavior=html.slice(html.indexOf('function deskCloseDrawer('),html.indexOf('function deskEndDrag('));
function headerChecks(code=headerBehavior){
  const nodes=new Map(),controls=[],focused=[],listeners={};let ctx;
  const visible=el=>{if(!el)return false;if(el.dataset?.theme)return el.host==='deskChainTools'?ctx.innerWidth>1000:ctx.innerWidth<=1000;if(el.dataset?.drawer)return el.host==='deskChainTools'?ctx.innerWidth>1000&&ctx.innerWidth<=1380:ctx.innerWidth<=1000;return true};
  const node=id=>{if(!nodes.has(id))nodes.set(id,{id,dataset:{},style:{setProperty(){}},classList:{toggle(){}},clientWidth:1100,isConnected:true,getClientRects(){return visible(this)?[{}]:[]},setAttribute(k,v){this[k]=v},contains:el=>el?.owner===id,querySelector:()=>node(id+' close'),getBoundingClientRect:()=>({left:10,right:40}),focus(options){ctx.document.activeElement=this;focused.push({el:this,options});listeners.focusin?.({target:this})},closest(){return null}});return nodes.get(id)};
  for(const host of ['deskToolbar','deskChainTools'])for(const [kind,value]of [['drawer','facts'],['drawer','agents'],['theme','original'],['theme','neutral']]){if(host==='deskChainTools'&&value==='agents')continue;const b=node('#'+host+' [data-'+kind+'='+value+']');b.host=host;b.dataset[kind]=value;b.closest=selector=>selector==='[inert]'?(node(host==='deskChainTools'?'#deskT':'#deskToolbar').inert?node('#'+host):null):selector.includes('#'+host)?node('#'+host):null;controls.push(b)}
  ctx={innerWidth:1000,focus:'A',A:{A:{}},DESK:{preference:.46,compact:false,open:false,expanded:false,drawer:null},document:{activeElement:null,body:{},addEventListener:(type,fn)=>listeners[type]=fn,documentElement:{dataset:{}},querySelectorAll:selector=>controls.filter(b=>b.dataset[selector.includes('data-theme')?'theme':'drawer'])},$:node,deskEndDrag(){},responseInvalidate(){},watchOpen(){},render(){},store:{setItem(){}},getComputedStyle:el=>({visibility:el.visibility||'visible'}),performance:{now:()=>0},requestAnimationFrame:f=>f()};
  const tracking=html.slice(html.indexOf("document.addEventListener('focusin',e=>"),html.indexOf('/* end header focus tracking */'));
  const theme=html.slice(html.indexOf('function deskTheme('),html.indexOf("let initialDeskTheme="));
  const click=html.split('\n').find(line=>line.startsWith("$('#deskToolbar').onclick="));
  vm.createContext(ctx);const api=vm.runInContext(deskHelpers+'\n'+tracking+'\n'+code+'\n'+drawerBehavior+'\n'+theme+'\n'+click+';({deskSize,deskCloseDrawer,deskToggleDrawer,deskTheme})',ctx);
  const mobileTheme=node('#deskToolbar [data-theme=neutral]'),desktopTheme=node('#deskChainTools [data-theme=neutral]'),mobileFacts=node('#deskToolbar [data-drawer=facts]'),desktopFacts=node('#deskChainTools [data-drawer=facts]');
  ctx.document.activeElement=mobileTheme;ctx.innerWidth=1001;api.deskSize();assert.equal(ctx.document.activeElement,desktopTheme);assert.equal(focused.at(-1).options.preventScroll,true);
  ctx.innerWidth=1000;api.deskSize();assert.equal(ctx.document.activeElement,mobileTheme);
  mobileTheme.focus({preventScroll:true});ctx.innerWidth=1001;ctx.document.activeElement=ctx.document.body;api.deskSize();assert.equal(ctx.document.activeElement,desktopTheme,'native CSS blur before resize still restores equivalent control');
  ctx.innerWidth=1000;ctx.document.activeElement=ctx.document.body;api.deskSize();assert.equal(ctx.document.activeElement,mobileTheme,'reverse native blur sequence restores mobile control');
  listeners.pointerdown({target:{closest:()=>null}});ctx.innerWidth=1001;ctx.document.activeElement=ctx.document.body;api.deskSize();assert.equal(ctx.document.activeElement,ctx.document.body,'intentional click elsewhere must not resurrect header focus');
  ctx.innerWidth=1200;api.deskToggleDrawer('facts',desktopFacts);assert.equal(node('#deskT').inert,true);assert.equal(node('#deskToolbar').inert,true);assert.equal(desktopFacts['aria-expanded'],'true');assert.equal(mobileFacts['aria-expanded'],'true');
  api.deskCloseDrawer();assert.equal(ctx.document.activeElement,desktopFacts);assert.equal(desktopFacts['aria-expanded'],'false');assert.equal(mobileFacts['aria-expanded'],'false');assert.equal(node('#deskT').inert,false);
  api.deskToggleDrawer('facts',desktopFacts);ctx.innerWidth=1000;api.deskSize();api.deskCloseDrawer();assert.equal(ctx.document.activeElement,mobileFacts,'drawer return follows newly visible counterpart');
  api.deskToggleDrawer('facts',mobileFacts);ctx.innerWidth=1381;api.deskSize();assert.equal(ctx.DESK.drawer,null);assert.equal(ctx.document.activeElement,node('#deskWorkToggle'),'Details becomes a permanent rail above 1380');
  ctx.document.activeElement={owner:'#deskFacts'};ctx.innerWidth=1380;api.deskSize();assert.equal(ctx.document.activeElement,desktopFacts,'hiding facts rail moves focus to visible Details trigger');
  node('#deskChainTools').onclick({target:{closest:()=>desktopTheme}});assert.equal(ctx.document.documentElement.dataset.theme,'neutral');for(const b of controls.filter(b=>b.dataset.theme))assert.equal(b['aria-pressed'],String(b.dataset.theme==='neutral'));
}
headerChecks();let headerFailed=false;try{headerChecks(headerBehavior.replace('el=deskHeaderPeer(el);',''))}catch(e){headerFailed=true}assert.ok(headerFailed,'removing responsive focus counterpart must fail');
function headerWheelChecks(mutate=false){let listener;const steps=[],ctx={focus:'A',gT:0,gx:0,gz:0,gFired:false,gZone:null,gPinch:null,performance:{now:()=>500},addEventListener:(event,fn)=>listener=fn,canScrollX:()=>false,step:n=>steps.push(n),setView(){throw Error('unexpected view')},desk(){throw Error('unexpected bay')}};
  const start=html.lastIndexOf("addEventListener('wheel',e=>{");let code=html.slice(start,html.indexOf('/* ---------- stepped up:',start));
  if(mutate)code=code.replace('#officer,#compactDesk,#deskChainTools','#officer,#compactDesk');vm.runInNewContext(code,ctx);
  const event=tools=>({target:{closest:selector=>selector.includes('#deskChainTools')&&tools?{}:selector==='#deskT'?{}:null},deltaX:120,deltaY:0,preventDefault(){}});
  listener(event(true));assert.equal(steps.length,0,'gestures on theme/Details do not navigate agents');listener(event(false));assert.deepEqual(steps,[1],'chain navigation elsewhere remains active');
}
headerWheelChecks();let wheelFailed=false;try{headerWheelChecks(true)}catch(e){wheelFailed=true}assert.ok(wheelFailed,'missing control gesture exclusion must fail');
console.log('ok native header controls: 1000/1001 focus, 1380/1381 Details transitions, shared themes/aria/inert, gesture isolation; mutations detected');

/* Exercise the real visibility predicate independently of layout stubs. */
const visibleSource=html.split('\n').find(line=>line.startsWith('function deskVisible('));
function headerVisibilityChecks(code=visibleSource){
  const check=vm.runInNewContext(code+';deskVisible',{getComputedStyle:el=>({visibility:el.visibility})});
  const node={isConnected:true,visibility:'visible',inert:false,rects:[{}],closest(){return this.inert?{}:null},getClientRects(){return this.rects}};
  assert.equal(check(node),true);assert.equal(check(null),false);
  for(const [field,value]of [['isConnected',false],['visibility','hidden'],['inert',true],['rects',[]]]){const previous=node[field];node[field]=value;assert.equal(check(node),false,field+' excludes hidden/inert controls');node[field]=previous}
}
headerVisibilityChecks();for(const guard of ['el.isConnected&&',"!el.closest('[inert]')&&",'!!el.getClientRects().length&&',"&&getComputedStyle(el).visibility!=='hidden'"]){let failed=false;try{headerVisibilityChecks(visibleSource.replace(guard,''))}catch(e){failed=true}assert.ok(failed,'visibility guard mutation detected: '+guard)}

/* Small rule reader verifies width-conditioned declarations, without pretending to be a browser cascade. */
const deskCSS=html.slice(html.indexOf('<style>')+7,html.indexOf('</style>'));
function cssRules(text,conditions=[],out=[]){
  text=text.replace(/\/\*[\s\S]*?\*\//g,'');let at=0;
  while(at<text.length){const open=text.indexOf('{',at);if(open<0)break;let close=open+1,depth=1;while(close<text.length&&depth){if(text[close]==='{')depth++;else if(text[close]==='}')depth--;close++}
    const selector=text.slice(at,open).trim(),body=text.slice(open+1,close-1);at=close;
    if(selector.startsWith('@media'))cssRules(body,[...conditions,selector.slice(6).trim()],out);
    else if(!selector.startsWith('@'))out.push({selectors:selector.split(',').map(x=>x.trim()),conditions,body});
  }return out;
}
function layoutStyleChecks(css=deskCSS){
  const rules=cssRules(css),value=(selector,property,width)=>{let result;for(const r of rules){if(!r.selectors.includes(selector)||!r.conditions.every(c=>!/(forced-colors|pointer|hover|prefers-)/.test(c)&&[...c.matchAll(/\((min|max)-width:\s*(\d+)px\)/g)].every(m=>m[1]==='min'?width>=+m[2]:width<=+m[2])))continue;for(const declaration of r.body.split(';')){const i=declaration.indexOf(':');if(declaration.slice(0,i).trim()===property)result=declaration.slice(i+1).trim()}}return result};
  for(const w of [390,1000,1001,1380,1381]){
    assert.equal(value('#deskToolbar','display',w),w<=1000?'flex':'none','mobile toolbar at '+w);
    assert.equal(value('#deskChainTools','display',w),w<=1000?'none':'flex','desktop controls at '+w);
    assert.equal(value('#deskChainTools [data-drawer=facts]','display',w),w>1000&&w<=1380?'block':'none','desktop Details at '+w);
  }
  assert.equal(value('#deskAgentList li[data-relation=inferred]:after','border-top-style',1600),'dashed');
  assert.equal(value('#crumb','padding',1600),'4px','focus outline fits inside scroll clipping');
  const checklist='#compactDesk #ofLog .source .rich-source li:has(>.box)';
  assert.notEqual(value(checklist,'display',1600),'flex','checklist inline content and nested lists use normal flow');
  assert.equal(value(checklist,'position',1600),'relative');
  assert.equal(value('#compactDesk #ofLog .source .rich-source .h1','font-size',1600),'16px','agent heading gets scoped response typography');
  for(const hashes of ['#','##','###'])assert.doesNotMatch(responseMarkup(hashes+' Heading',true),/<h3\b/,'agent headings cannot match .hud h3 chrome');
}
layoutStyleChecks();for(const [before,after]of [
  ['#deskToolbar{display:none}','#deskToolbar{}'],
  ['#deskChainTools [data-drawer=facts]{display:block}','#deskChainTools [data-drawer=facts]{}'],
  ['#deskChainTools{display:none}','#deskChainTools{}'],
  ['#deskAgentList li[data-relation=inferred]:after{border-top-style:dashed}','#deskAgentList li[data-relation=inferred]:after{}'],
  ['overflow:auto;padding:4px;scrollbar-width:thin','overflow:auto;scrollbar-width:thin']
]){assert.ok(deskCSS.includes(before));let failed=false;try{layoutStyleChecks(deskCSS.replace(before,after))}catch(e){failed=true}assert.ok(failed,'critical CSS deletion mutation detected: '+before)}
console.log('ok real visibility and critical responsive/header/connector CSS; guard and stylesheet deletion mutations detected');


/* Run the real response-card lifecycle and shared dispatch against synthetic requests only. */
const responseSource=html.slice(html.indexOf('/* Desk response card:'),html.indexOf('/* end desk response card */'));
const dispatchSource=html.slice(html.indexOf('function permissionDispatch('),html.indexOf('/* end permission dispatch */'));
const needSource=html.slice(html.indexOf('function need('),html.indexOf('/* Context in use.'));
function responseFixture(source=responseSource,dispatch=dispatchSource){
  const nodes=new Map(),listeners={},posts=[],pending=[],reveals=[];let now=1000,focusCalls=0;
  const rect=(top=100,bottom=600)=>({top,bottom,left:0,right:600,width:600,height:bottom-top});
  function node(id){if(!nodes.has(id)){const el={id,hidden:false,disabled:false,textContent:'',scrollTop:0,clientHeight:500,value:'my unchanged draft',children:[],rect:rect(),setAttribute(k,v){this[k]=v},getBoundingClientRect(){return this.rect},addEventListener(k,f){this[k]=f},focus(){focusCalls++}};Object.defineProperty(el,'innerHTML',{set(){throw Error('Request text must not become HTML')}});nodes.set(id,el)}return nodes.get(id)}
  function button(kind){return {kind,disabled:true,classList:{contains:c=>c==='response-'+kind},closest(){return this},cloneNode(){return button(kind)},replaceWith(b){const holder=this.parentNode,i=holder.children.indexOf(this);holder.children[i]=b;b.parentNode=holder;this.parentNode=null}}}
  const holder=node('#responseDecisions');holder.children=['allow','deny'].map(button);for(const b of holder.children)b.parentNode=holder;
  node('#responseCard').rect=rect(200,500);node('#responseEvidence').rect=rect(240,400);node('#responseActions').rect=rect(410,445);
  const a={id:'c:A',name:'Ada',runtime:'Claude',perms:{},calls:{},asks:{}},b={id:'x:B',name:'Bram',runtime:'Codex',perms:{},calls:{},asks:{}};
  const log=node('#ofLog');Object.assign(log,{scrollTop:600,scrollHeight:1000,clientHeight:400});
  const ctx={ofLog:log,nearEnd:el=>el.scrollHeight-el.scrollTop-el.clientHeight<40,ResizeObserver:class{constructor(f){listeners.observedResize=f}observe(){}},focus:a.id,A:{[a.id]:a,[b.id]:b},APPROVE:true,STEADY:false,DEMO:false,document:{hidden:false},Date:{now:()=>now},$:node,txt:(el,t)=>{if(el.textContent!==t)el.textContent=t},saidKind:()=>null,covered:()=>ctx.coveredNow,onScreen:()=>ctx.visible&&!ctx.inert,visible:true,inert:false,coveredNow:false,render(){},deskReveal:v=>reveals.push(v),addEventListener:(k,f)=>listeners[k]=f,post:(route,body)=>{posts.push({route,body});return new Promise((resolve,reject)=>pending.push({resolve,reject}))}};
  vm.createContext(ctx);const api=vm.runInContext(html.split('\n').find(x=>x.startsWith('function dismissNotice('))+'\n'+needSource+'\n'+dispatch+'\n'+source+';({responsePaint,responseGate,responseCanDecide,responseInvalidate,responseReveal,RESPONSE,permissionDispatch,need})',ctx);
  const paint=()=>api.responsePaint(ctx.A[ctx.focus],ctx.A[ctx.focus]?api.need(ctx.A[ctx.focus]):null);ctx.render=paint;
  const perm=(id='p1',text='rm build\n'+('Full evidence <b>not markup</b>\n'.repeat(60)))=>{const p={pid:id,tool:'Bash',text};a.perms={[id]:p};return p};
  const allow=()=>holder.children[0],click=b=>holder.onclick({target:b||allow()});
  const ready=()=>{paint();now+=450;api.responseGate();assert.equal(allow().disabled,false)};
  return {ctx,api,node,posts,pending,listeners,a,b,perm,paint,ready,allow,click,tick:ms=>now+=ms,focusCalls:()=>focusCalls,reveals};
}
function responseGuardChecks(source=responseSource){
  const cases=[
    ['station',t=>t.ctx.focus=t.b.id],['room',t=>t.ctx.focus=null],['ended',t=>t.a.perms={}],
    ['replacement object',t=>t.a.perms.p1={...t.a.perms.p1}],['changed text',t=>{t.a.perms.p1.text+=' changed';t.node('#responseEvidence').textContent='Bash\n\n'+t.a.perms.p1.text}],['changed tool',t=>t.a.perms.p1.tool='Edit'],
    ['new PID',t=>t.perm('p2')],['sent',t=>t.a.perms.p1.sent=true],['uncertain',t=>t.a.perms.p1.uncertain=true],
    ['collapsed',t=>t.api.RESPONSE.current.entry.collapsed=true],['Answer here off',t=>t.ctx.APPROVE=false],['steady',t=>t.ctx.STEADY=true],
    ['covered',t=>t.ctx.coveredNow=true],['offscreen',t=>t.ctx.visible=false],['inert conversation',t=>t.ctx.inert=true],['hidden tab',t=>t.ctx.document.hidden=true],
    ['masked evidence',t=>t.node('#responseEvidence').textContent='masked'],['clipped card',t=>t.node('#responseCard').rect.bottom=700]
  ];
  for(const [name,change] of cases){const t=responseFixture(source);t.perm();t.ready();change(t);assert.equal(t.api.responseCanDecide(t.allow()),false,name);t.click();assert.equal(t.posts.length,0,name+' cannot dispatch')}
  const t=responseFixture(source);t.perm();t.paint();t.tick(449);assert.equal(t.api.responseCanDecide(t.allow()),false,'450ms dwell');t.tick(1);assert.equal(t.api.responseCanDecide(t.allow()),true);const foreign={parentNode:t.node('#responseDecisions')};assert.equal(t.api.responseCanDecide(foreign),false,'only freshly bound controls act');
}
responseGuardChecks();
for(const [from,to] of [['p===e.perm&&',''],['p.tool===e.tool&&p.text===e.text&&',''],['&&!p.sent',''],['&&!p.uncertain',''],['&&!c.entry.collapsed',''],['&&APPROVE',''],['&&!STEADY',''],['&&!covered()',''],['&&responseVisible()',''],['&&Date.now()-e.at>=450','']]){const altered=responseSource.replace(from,to);assert.notEqual(altered,responseSource);let caught=false;try{responseGuardChecks(altered)}catch(e){caught=true}assert.ok(caught,'response guard mutation '+from+' must fail')}
function responseLifecycleChecks(){
  const t=responseFixture(),p=t.perm();t.paint();assert.equal(t.node('#responseEvidence').textContent,'Bash\n\n'+p.text,'full evidence is not truncated');assert.equal(t.node('#responseTitle').textContent,'Approval requested');assert.equal(t.focusCalls(),0,'new request does not steal focus');
  const initial=t.allow();t.paint();assert.equal(t.allow(),initial,'unchanged painting retains buttons');t.tick(450);t.api.responseGate();assert.equal(t.allow().disabled,false);
  t.node('#responseToggle').onclick();assert.equal(t.node('#responseBody').hidden,true);assert.equal(t.allow().disabled,true);assert.equal(p.sent,undefined,'collapse does not answer');t.paint();assert.equal(t.node('#responseBody').hidden,true,'collapse survives painting');
  t.node('#responseToggle').onclick();assert.notEqual(t.allow(),initial);assert.equal(t.allow().disabled,true,'reopen restarts dwell');t.tick(450);t.api.responseGate();assert.equal(t.allow().disabled,false);
  const beforeHidden=t.allow();t.ctx.visible=false;t.api.responseGate();t.ctx.visible=true;t.api.responseGate();assert.notEqual(t.allow(),beforeHidden);assert.equal(t.allow().disabled,true,'visibility return restarts dwell');
  t.tick(450);t.api.responseGate();const beforeReplace=t.allow();p.text+=' amended';t.paint();assert.notEqual(t.allow(),beforeReplace);assert.equal(t.allow().disabled,true,'text replacement restarts dwell');
  t.node('#responseToggle').onclick();t.perm('p2');t.paint();assert.equal(t.node('#responseBody').hidden,false,'new request autoexpands');assert.equal(t.focusCalls(),0);
  t.ctx.STEADY=true;t.paint();assert.match(t.node('#responseEvidence').textContent,/hidden/);assert.equal(t.node('#responseDecisions').hidden,true);assert.equal(t.node('#responseOpenApp').hidden,false);
  t.ctx.STEADY=false;t.ctx.APPROVE=false;t.paint();assert.equal(t.node('#responseDecisions').hidden,true);assert.match(t.node('#responseNote').textContent,/Answer here is off/);assert.equal(t.ctx.APPROVE,false);
  t.ctx.APPROVE=true;t.a.perms={};t.a.asks={q1:{text:'Choose a release channel?',maybe:false}};t.paint();assert.equal(t.node('#responseTitle').textContent,'Question');assert.equal(t.node('#responseDecisions').hidden,true);assert.equal(t.node('#responseOpenApp').hidden,false);t.click();assert.equal(t.posts.length,0,'question has no fake Allow action');
  t.a.asks.q1.maybe=true;t.paint();assert.equal(t.node('#responseTitle').textContent,'Possible request');assert.match(t.node('#responseNote').textContent,/Possible request/);
  t.node('#responseToggle').onclick();t.api.responseReveal();assert.deepEqual(t.reveals,['conversation']);assert.equal(t.node('#responseBody').hidden,false);assert.equal(t.ctx.APPROVE,true);assert.equal(t.posts.length,0);
  t.a.asks={};t.paint();assert.equal(t.node('#responseCard').hidden,true);assert.equal(t.api.RESPONSE.exposure,null);assert.equal(t.node('#ofIn').value,'my unchanged draft');
  const monitor=html.slice(html.indexOf("const yn=t.closest('.yes,.no')"),html.indexOf("if(t.closest('.go'))"));assert.match(monitor,/!p\|\|p.sent\|\|focus!==id\|\|a.permScreen!==p.pid\|\|!APPROVE\|\|covered\(\)\|\|!onScreen\(a\.\$\.code\)\|\|Date.now\(\)-\(\+yn.dataset.at\|\|0\)<450/);assert.match(monitor,/permissionDispatch\(a,p,/,'old monitor validates before shared dispatch');
}
responseLifecycleChecks();
async function responseDispatchChecks(dispatch=dispatchSource){
  const t=responseFixture(responseSource,dispatch),p=t.perm();t.ready();t.click();t.click();assert.equal(t.posts.length,1,'duplicate click cannot dispatch twice');assert.deepEqual(JSON.parse(JSON.stringify(t.posts[0])),{route:'/decide',body:{pid:'p1',allow:true}});assert.equal(p.sent,true);
  t.ctx.focus=t.b.id;t.b.asks={q2:{text:'Which plan?',maybe:false}};t.paint();t.pending.shift().reject(Error('accepted but response lost'));await new Promise(setImmediate);assert.equal(p.sent,true,'uncertain decision remains non-retriable');assert.equal(p.uncertain,true);assert.equal(p.notice,'Outcome unknown. Check the app before trying again.');assert.equal(t.b.permMsg,undefined,'late response cannot update another agent');
  t.ctx.focus=t.a.id;t.paint();t.api.RESPONSE.current.entry.note='Opened app.';t.paint();assert.match(t.node('#responseNote').textContent,/Outcome unknown/,'app-open notice cannot mask uncertain decision');t.tick(1000);t.click();assert.equal(t.posts.length,1,'uncertain result never retries');
  const p2=t.perm('p2');t.ready();t.click();assert.equal(t.posts.length,2,'new independent request remains answerable');assert.equal(t.posts[1].body.pid,'p2');t.pending.shift().resolve({ok:false,status:409});await new Promise(setImmediate);assert.equal(p2.sent,true);assert.match(p2.notice,/Already answered/);
  const p3=t.perm('p3');t.ready();t.click();const oldPending=t.pending.shift(),p4=t.perm('p4');t.paint();oldPending.reject(Error('lost'));await new Promise(setImmediate);assert.equal(p4.sent,undefined);assert.equal(t.a.permMsg,p2.notice,'old response cannot overwrite current request message');t.ready();t.click();t.pending.shift().resolve({ok:false,status:404});await new Promise(setImmediate);assert.match(p4.notice,/expired/);
  t.a.perms={};t.a.asks={q:{text:'Pick a color',maybe:false}};t.paint();const open=t.node('#responseOpenApp').onclick();assert.equal(t.posts.at(-1).route,'/open');assert.equal(t.posts.at(-1).body.agent,t.a.id);t.pending.shift().resolve({ok:false,status:404,text:async()=> 'This session was not started in Claude Desktop. <img src=x onerror=bad()>'});await open;assert.equal(t.node('#responseNote').textContent,'This session was not started in Claude Desktop. <img src=x onerror=bad()>');assert.equal(t.node('#ofIn').value,'my unchanged draft');
  t.ctx.DEMO=true;const count=t.posts.length;await t.node('#responseOpenApp').onclick();assert.equal(t.posts.length,count);assert.match(t.node('#responseNote').textContent,/no app was opened/);
  t.ctx.DEMO=false;t.a.observed=true;t.paint();assert.equal(t.node('#responseOpenApp').disabled,true);await t.node('#responseOpenApp').onclick();assert.equal(t.posts.length,count,'observed ChatGPT targets do not attempt unsupported open');
  t.a.asks={};await t.node('#responseOpenApp').onclick();assert.equal(t.posts.length,count,'ended request cannot open from stale card');
  assert.ok(t.posts.every(x=>['/decide','/open'].includes(x.route)),'no question masquerades as queued message or steering');
}
(async()=>{await responseDispatchChecks();const changed=dispatchSource.replaceAll('p.uncertain=true;', 'p.sent=false;p.uncertain=true;');let caught=false;try{await responseDispatchChecks(changed)}catch(e){caught=true}assert.ok(caught,'uncertain outcome retry mutation must fail');console.log('ok response card: real lifecycle/dispatch, full safe evidence, native question routing, precise identity/visibility/dwell, collapse, independent requests, late and unknown outcomes; eleven mutations detected')})().catch(e=>{console.error(e);process.exitCode=1});

function responseReviewChecks(source=responseSource,needCode=needSource){
  const t=responseFixture(source),p=t.perm();t.a.wait={why:'attention',text:'Retained underlying notice',ts:1};t.ready();t.node('#responseDismiss').onclick();assert.ok(t.a.wait,'held permission cannot invoke the shared notice mutation');t.a.wait=null;assert.equal(t.a.perms.p1,p,'held permissions cannot be dismissed');assert.equal(t.node('#responseDismiss').hidden,true);
  t.a.perms={};t.a.asks={q:{text:'Native question',maybe:false}};t.paint();t.node('#responseDismiss').onclick();assert.ok(t.a.asks.q,'confirmed question cannot be dismissed');assert.equal(t.node('#responseDismiss').hidden,true);
  t.a.asks.q.maybe=true;t.paint();assert.equal(t.node('#responseDismiss').hidden,false);t.node('#responseDismiss').onclick();assert.equal(t.a.asks.q,undefined);assert.equal(t.node('#responseCard').hidden,true,'dismiss removes inferred request without answering');
  t.a.wait={why:'attention',text:'Needs your attention',ts:1};t.paint();assert.equal(t.node('#responseDismiss').hidden,false);t.node('#responseDismiss').onclick();assert.equal(t.a.wait,null);assert.equal(t.node('#responseCard').hidden,true);
  t.a.wait={why:'attention',text:'Earlier notice',ts:2};t.paint();t.a.wait={why:'attention',text:'New notice',ts:3};t.node('#responseDismiss').onclick();assert.ok(t.a.wait,'stale dismiss cannot clear newer request');
  t.a.wait=null;t.perm();t.ready();t.ctx.visible=false;t.listeners.scroll({target:{contains:()=>true}});t.ctx.visible=true;assert.equal(t.api.responseCanDecide(t.allow()),false,'out and back ancestor scroll invalidates without paint');t.api.responseGate();t.tick(449);assert.equal(t.api.responseCanDecide(t.allow()),false);t.tick(1);assert.equal(t.api.responseCanDecide(t.allow()),true);
  t.listeners.resize();assert.equal(t.api.responseCanDecide(t.allow()),false);t.api.responseGate();t.tick(450);t.api.responseGate();t.listeners.observedResize();assert.equal(t.api.responseCanDecide(t.allow()),false,'ancestor resize also resets exposure');
  t.ctx.visible=false;t.paint();assert.equal(t.node('#responseOpenApp').hidden,false);assert.match(t.node('#responseNote').textContent,/Scroll the full request/);assert.equal(t.allow().disabled,true,'fallback never weakens geometry guard');
  const need=vm.runInNewContext(needCode+';need',{saidKind:()=>null}),a={perms:{p1:{pid:'p1',tool:'Bash',text:'First',sent:true,uncertain:true},p2:{pid:'p2',tool:'Bash',text:'Second'}},calls:{},asks:{}};
  assert.equal(need(a).perm,a.perms.p2,'coexisting unsent permission takes priority');assert.equal(need(a).more,0);assert.equal(need(a).unresolved,1);delete a.perms.p2;assert.equal(need(a).perm,a.perms.p1,'frozen first request remains after later request ends');
  t.ctx.visible=true;t.a.perms=a.perms;t.paint();assert.equal(t.node('#responseDecisions').children[0].disabled,true);t.a.perms.p2={pid:'p2',tool:'Bash',text:'Second'};t.ready();assert.equal(t.api.RESPONSE.exposure.pid,'p2');assert.equal(t.a.perms.p1.uncertain,true);
  const log=t.node('#ofLog');log.clientHeight=400;log.scrollTop=600;let hidden=false;Object.defineProperty(t.node('#responseCard'),'hidden',{get:()=>hidden,set:v=>{hidden=v;log.clientHeight=v?400:200},configurable:true});
  t.paint();assert.ok(log.scrollHeight-log.scrollTop-log.clientHeight<40,'new card preserves existing follow-to-bottom');log.clientHeight=400;log.scrollTop=100;t.paint();assert.equal(log.scrollTop,100,'reading earlier conversation never jumps to bottom');
  assert.equal(t.posts.length,0,'notice, scroll and rendering never send');
}
responseReviewChecks();
for(const [name,source,needCode] of [
 ['dismiss restriction',responseSource.replace("v.n.perm||v.n.sure&&v.n.kind!=='Needs you'||",''),needSource],
 ['scroll invalidation',responseSource.replace("e.target.contains($('#responseCard'))","false"),needSource],
 ['pending priority',responseSource,needSource.replace("perms.find(p=>!p.sent&&!p.uncertain)||perms[0]","perms[0]")],
 ['follow bottom',responseSource.replace('if(follow)ofLog.scrollTop=ofLog.scrollHeight',''),needSource]
]){let caught=false;try{responseReviewChecks(source,needCode)}catch(e){caught=true}assert.ok(caught,name+' review mutation must fail')}
console.log('ok review repairs: safe notice dismissal, continuous exposure, short-screen fallback, coexisting permissions, server reason and follow-bottom; four additional mutations detected');

/* The short-height override must not match an absent or collapsed request. Browser QA measures final pixels. */
function responseShortScopeChecks(css=html){
  const at=css.indexOf('@media(max-height:700px)'),block=css.slice(at,css.indexOf('\n',at));
  const rules=[...block.slice(block.indexOf('{')+1,-1).matchAll(/([^{}]+)\{([^{}]*)\}/g)];
  const scoped=rules.filter(([_,selector])=>selector.includes('#compactDesk'));
  assert.ok(scoped.length>=7);
  for(const [,selectors] of scoped)for(const selector of selectors.split(','))assert.ok(selector.includes(':has(#responseCard:not([hidden]) #responseBody:not([hidden]))'),'short-height layout applies only with a visible expanded card: '+selector);
  const log=scoped.find(([,selector])=>selector.endsWith('#ofLog'));assert.ok(log);assert.match(log[2],/max-height:160px/,'cap remains available for expanded short-screen requests');
}
responseShortScopeChecks();let scopeCaught=false;try{responseShortScopeChecks(html.replaceAll(':has(#responseCard:not([hidden]) #responseBody:not([hidden]))',''))}catch(e){scopeCaught=true}assert.ok(scopeCaught,'unscoped short-height layout mutation must fail');
function responseFinalChecks(source=responseSource,needCode=needSource){
  const t=responseFixture(source);t.perm();t.ctx.visible=false;t.node('#deskConversation').clientHeight=100;t.paint();assert.match(t.node('#responseNote').textContent,/window is too short/);assert.doesNotMatch(t.node('#responseNote').textContent,/Scroll/);
  t.api.RESPONSE.current.entry.note='Server says terminal sessions have no app link.';t.paint();assert.match(t.node('#responseNote').textContent,/terminal sessions/,'geometry explanation preserves app result');assert.match(t.node('#responseNote').textContent,/too short/);assert.equal(t.node('#responseOpenApp').hidden,false);assert.equal(t.allow().disabled,true);
  t.node('#deskConversation').clientHeight=500;t.paint();assert.match(t.node('#responseNote').textContent,/Scroll the full request/);t.api.RESPONSE.current.entry.note='';t.ctx.visible=true;
  const card=t.node('#responseCard'),parent=t.node('#deskConversation');card.rect={top:650,bottom:950,left:0,right:600,width:600,height:300};t.node('#responseEvidence').rect={top:690,bottom:850,left:0,right:600,width:600,height:160};t.node('#responseActions').rect={top:860,bottom:895,left:0,right:600,width:600,height:35};t.node('#ofBody').rect.bottom=2000;
  let scroll=0;Object.defineProperty(parent,'scrollTop',{get:()=>scroll,set:v=>{const delta=v-scroll;scroll=v;for(const id of ['#responseCard','#responseEvidence','#responseActions']){t.node(id).rect.top-=delta;t.node(id).rect.bottom-=delta}}});
  t.paint();assert.equal(scroll,0,'request arrival never scrolls outer conversation');t.api.responseReveal();assert.equal(scroll,350,'explicit Respond brings the card inside visible outer bounds');assert.equal(t.allow().disabled,true,'revealing restarts dwell');t.tick(450);t.api.responseGate();assert.equal(t.allow().disabled,false);
  const need=vm.runInNewContext(needCode+';need',{saidKind:()=>null});t.a.perms={p1:{pid:'p1',tool:'Bash',text:'First',sent:true,uncertain:true},p2:{pid:'p2',tool:'Bash',text:'Second'},p3:{pid:'p3',tool:'Bash',text:'Third'},p4:{pid:'p4',tool:'Bash',text:'Fourth',sent:true}};
  const n=need(t.a);assert.equal(n.more,1,'only other actionable requests count as pending');assert.equal(n.unresolved,1);t.api.responsePaint(t.a,n);assert.match(t.node('#responseAgent').textContent,/\+1 pending/);assert.match(t.node('#responseAgent').textContent,/1 earlier outcome unknown/);assert.match(t.node('#responseNote').textContent,/earlier approval has an unknown outcome/);
}
responseFinalChecks();
for(const [name,source,needCode] of [
 ['preserve fallback status',responseSource.replace('[entry.note||unavailable,reason,earlier]','[reason,earlier]'),needSource],
 ['impossible geometry',responseSource.replace("card.getBoundingClientRect().height>$('#deskConversation').clientHeight","false"),needSource],
 ['Respond scroll',responseSource.replace('responseScrollIntoView();',''),needSource],
 ['pending count',responseSource,needSource.replace('perms.filter(p=>p!==perm&&!p.sent&&!p.uncertain).length','perms.length-1')],
 ['unknown status',responseSource.replace('earlier=n.unresolved?', 'earlier=false?'),needSource]
]){let caught=false;try{responseFinalChecks(source,needCode)}catch(e){caught=true}assert.ok(caught,name+' mutation must fail')}
{
  const line=html.split('\n').find(x=>x.includes('Allow and Deny are in the bar above'));
  const expression=line.slice(line.indexOf('${STATION.compact')+2,line.indexOf('}</span>'));
  const copy=(compact,answer)=>vm.runInNewContext(expression,{STATION:{compact},APPROVE:answer});
  assert.match(copy(true,false),/Answer here is off/);assert.doesNotMatch(copy(true,false),/to answer in the conversation/);assert.match(copy(true,true),/Choose Respond to answer/);assert.match(copy(false,true),/bar above/);
}
(async()=>{const t=responseFixture();t.perm();t.ctx.visible=false;t.paint();const opening=t.node('#responseOpenApp').onclick();assert.match(t.node('#responseNote').textContent,/Opening Claude/);const reason='This session was not started in Claude Desktop. <b>terminal</b>';t.pending.shift().resolve({ok:false,status:404,text:async()=>reason});await opening;assert.ok(t.node('#responseNote').textContent.includes(reason),'hidden held permission preserves exact safe server reason');assert.match(t.node('#responseNote').textContent,/Scroll the full request/);assert.equal(t.posts.length,1);assert.equal(t.posts[0].route,'/open');console.log('ok final review: request-scoped short layout, retained app errors, truthful geometry and approval copy, explicit reveal scrolling, actionable and unknown counts; six additional mutations detected')})().catch(e=>{console.error(e);process.exitCode=1});

function responseFallbackHeightChecks(source=responseSource){
  const t=responseFixture(source);t.perm();t.a.perms.earlier={pid:'earlier',tool:'Bash',text:'Earlier',sent:true,uncertain:true};t.ctx.visible=false;t.node('#deskConversation').clientHeight=168;
  t.node('#responseCard').getBoundingClientRect=()=>{const note=t.node('#responseNote').textContent,height=150+(/Scroll the full request|window is too short/.test(note)?20:0)+(/earlier approval/.test(note)?15:0);return {top:100,bottom:100+height,left:0,right:600,width:600,height}};
  t.paint();assert.match(t.node('#responseNote').textContent,/window is too short/,'fallback must measure the height its own text adds');assert.match(t.node('#responseNote').textContent,/earlier approval has an unknown outcome/);
  t.api.RESPONSE.current.entry.note='Could not open terminal session.';t.paint();assert.match(t.node('#responseNote').textContent,/Could not open terminal session/);assert.match(t.node('#responseNote').textContent,/window is too short/);assert.equal(t.allow().disabled,true);const settled=t.node('#responseNote').textContent;t.paint();assert.equal(t.node('#responseNote').textContent,settled,'provisional render resolves to the same instruction on each draw');
}
responseFallbackHeightChecks();
{
  const before="fallback('Scroll the full request into view to answer here.');\n    if(card.getBoundingClientRect().height>$('#deskConversation').clientHeight)",after="const tooTall=card.getBoundingClientRect().height>$('#deskConversation').clientHeight;fallback('Scroll the full request into view to answer here.');\n    if(tooTall)";
  const changed=responseSource.replace(before,after);assert.notEqual(changed,responseSource);let caught=false;try{responseFallbackHeightChecks(changed)}catch(e){caught=true}assert.ok(caught,'measuring before fallback text mutation must fail');
}
console.log('ok fallback height: measures geometry and uncertainty text after rendering, retains app result, stable instruction; premature-measurement mutation detected');

/* Native observations execute their actual private reader and inert renderer. */
const nativeReaderSource=html.split('/* native observation reader: private focused state, never LOG or message transport */')[1].split('/* end native observation reader */')[0];
function nativeFixture(source=nativeReaderSource){
  let time=1000;const jobs=new Map(),requests=[],paints=[];let seq=0;
  const create=vm.runInNewContext(source+';createNativeReader',{AbortController,Date,JSON,Number,Set,Error});
  const api=create({clock:()=>time,later:(fn,ms)=>{jobs.set(++seq,{fn,at:time+ms/1000});return seq},cancel:id=>jobs.delete(id),paint:s=>paints.push(s.agent),request:(url,opts)=>new Promise((resolve,reject)=>requests.push({url,opts,resolve,reject}))});
  return {api,requests,jobs,paints,tick(seconds){time+=seconds;for(const [id,j] of [...jobs])if(j.at<=time){jobs.delete(id);j.fn()}},answer(row,value){row.resolve({ok:true,json:async()=>value})}};
}
const nativeAgent='x:11111111-1111-4111-8111-111111111111',nativeOther='x:22222222-2222-4222-8222-222222222222';
function nativeSnapshot(agent=nativeAgent){return {version:1,agent_id:agent,provider:'codex',observation_epoch:'33333333-3333-4333-8333-333333333333',revision:1,model_settings:{state:'observed',read_active:false,expires_at:1015,stale:false,catalog:null,settings:{model:'fixture-model',effort:null,mode:null,modelProvider:'fixture-provider',source:'native_desktop_settings',observedAt:1000},capability:{read:true,selectModel:false,selectEffort:false,reason:'catalog_unavailable',scope:'next_turn'}},inputs:{state:'observed',read_active:true,expires_at:1010,stale:false,requests:[{handle:'44444444-4444-4444-8444-444444444444',revision:1,kind:'question',state:'observed',presentation:{questions:[{id:'q0',header:'Choose',question:'<img src=x onerror=alert(1)> **plain**',options:[{label:'One',description:'[link](https://example.test)'}],isOther:true}]},capability:{answer:false,reason:'native_generation_unproven'}}],capability:{observe:true,answer:false,reason:'read_only'}}}}
const nativeFlush=async()=>{for(let i=0;i<8;i++)await Promise.resolve()};
async function nativeBehaviorChecks(source=nativeReaderSource){
  const t=nativeFixture(source);t.api.sync(nativeAgent,'owner-key');assert.equal(t.requests.length,1);assert.equal(t.requests[0].url,'/native/v1/read');assert.equal(t.requests[0].opts.headers['X-Live-Room-Key'],'owner-key');assert.deepEqual(JSON.parse(t.requests[0].opts.body),{agent_id:nativeAgent,include:['model_settings','native_inputs']});
  t.answer(t.requests[0],nativeSnapshot());await nativeFlush();assert.equal(t.api.state.snapshot.agent_id,nativeAgent);t.tick(.7);assert.equal(t.requests.length,2,'first observed still active must poll');assert.equal(t.requests[1].opts.method,'GET');
  t.requests[1].reject(Error('network'));await nativeFlush();assert.equal(t.api.state.snapshot.inputs.stale,true,'failed poll revokes freshness');assert.equal(t.api.state.snapshot.inputs.read_active,false);t.tick(20);assert.equal(t.requests.length,2,'failure cannot restart native read');
  const e=nativeFixture(source);e.api.sync(nativeAgent,'key');e.answer(e.requests[0],nativeSnapshot());await nativeFlush();e.tick(11);assert.equal(e.api.state.snapshot.inputs.stale,true,'local expiry independent of GET');assert.equal(e.api.state.snapshot.inputs.read_active,false);
  const race=nativeFixture(source);race.api.sync(nativeAgent,'key');const first=race.requests[0];race.api.sync(nativeOther,'key');assert.equal(first.opts.signal.aborted,true);race.answer(first,nativeSnapshot());await nativeFlush();assert.equal(race.api.state.snapshot,null,'late old-agent response ignored');race.answer(race.requests[1],nativeSnapshot(nativeOther));await nativeFlush();assert.equal(race.api.state.snapshot.agent_id,nativeOther);race.api.sync(null,null);assert.equal(race.api.state.snapshot,null,'locking purges private snapshot');
  const aba=nativeFixture(source);aba.api.sync(nativeAgent,'key');const old=aba.requests[0];aba.api.sync(nativeOther,'key');aba.api.sync(nativeAgent,'key');aba.answer(old,nativeSnapshot());await nativeFlush();assert.equal(aba.api.state.snapshot,null,'returning to same agent must not revive old observation epoch');
  for(const modify of [s=>s.agent_id=nativeOther,s=>s.inputs.capability.answer=true,s=>s.model_settings.capability.selectModel=true,s=>s.inputs.expires_at=Infinity,s=>s.inputs.expires_at=null,s=>s.inputs.requests[0].presentation.questions[0].isSecret=true]){
    const f=nativeFixture(source);f.api.sync(nativeAgent,'key');const value=nativeSnapshot();modify(value);f.answer(f.requests[0],value);await nativeFlush();assert.equal(f.api.state.snapshot,null,'invalid or secret snapshot rejected');
  }
  const rev=nativeFixture(source);rev.api.sync(nativeAgent,'key');let value=nativeSnapshot();value.revision=2;rev.answer(rev.requests[0],value);await nativeFlush();rev.tick(.7);value=nativeSnapshot();rev.answer(rev.requests[1],value);await nativeFlush();assert.equal(rev.api.state.snapshot.revision,2);assert.equal(rev.api.state.snapshot.inputs.stale,true,'revision regression invalidates freshness');
}
function nativeRenderChecks(source=html.slice(html.indexOf('function nativePaint(s){'),html.indexOf('function nativeSync(){'))){
  class Node{constructor(tag){this.tag=tag;this.children=[];this.textContent='';this.hidden=false;this.disabled=false}append(...nodes){this.children.push(...nodes)}replaceChildren(...nodes){this.children=nodes}set innerHTML(value){throw Error('native text must never use HTML')}}
  const nodes=new Map(),node=id=>{if(!nodes.has(id))nodes.set(id,new Node(id));return nodes.get(id)};
  const paint=vm.runInNewContext(source+';nativePaint',{$:node,txt:(n,v)=>n.textContent=v,document:{createElement:tag=>new Node(tag)},A:{[nativeAgent]:{runtime:'Codex'}},responseOpenReason:()=>''});
  const snap=nativeSnapshot();paint({agent:nativeAgent,snapshot:snap,busy:false,note:''});
  const all=n=>[n,...n.children.flatMap(all)],text=[...nodes.values()].flatMap(all).map(n=>n.textContent).join('\n');
  assert.ok(text.includes('<img src=x onerror=alert(1)> **plain**'));assert.ok(text.includes('[link](https://example.test)'));assert.ok(text.includes('Default (unspecified)'));assert.ok(text.includes('Mode: Unknown'));assert.equal([...nodes.values()].flatMap(all).some(n=>['input','select','textarea','a'].includes(n.tag)),false);
  const retained=node('#nativeQuestions').children[1],retainedModel=node('#nativeModel').children[0];node('#nativeQuestions').scrollTop=83;paint({agent:nativeAgent,snapshot:snap,busy:true,note:'Checking'});assert.equal(node('#nativeQuestions').children[1],retained,'unchanged poll retains selected question DOM');assert.equal(node('#nativeModel').children[0],retainedModel);assert.equal(node('#nativeQuestions').scrollTop,83);snap.inputs.requests[0].observed_at=1001;paint({agent:nativeAgent,snapshot:snap,busy:false,note:''});assert.equal(node('#nativeQuestions').children[1],retained,'observation timestamp alone does not replace text');
  snap.inputs.requests[0].presentation=null;paint({agent:nativeAgent,snapshot:snap,busy:false,note:''});assert.ok(all(node('#nativeQuestions')).map(n=>n.textContent).join('\n').includes('only in the native app'));assert.ok(!all(node('#nativeQuestions')).some(n=>n.textContent.includes('<img')));
}
(async()=>{await nativeBehaviorChecks();nativeRenderChecks();for(const [name,before,after] of [
 ['target correlation','v.agent_id!==state.agent','false'],
 ['private read capability','i.capability.answer!==false','false'],
 ['expiry revocation','p.expires_at<=now','false'],
 ['active polling','parts.some(p=>p.read_active)','false'],
 ['focus epoch','if(epoch!==state.epoch)return;','if(false)return;'],
]){const changed=nativeReaderSource.replaceAll(before,after);assert.notEqual(changed,nativeReaderSource);let caught=false;try{await nativeBehaviorChecks(changed)}catch(e){caught=true}assert.ok(caught,name+' mutation must fail')}
const renderSource=html.slice(html.indexOf('function nativePaint(s){'),html.indexOf('function nativeSync(){'));let cacheCaught=false;try{nativeRenderChecks(renderSource.replace('questions._nativeSignature!==questionSignature','true'))}catch(e){cacheCaught=true}assert.ok(cacheCaught,'question DOM cache removal must fail');
console.log('ok native observation reader: exact target/epoch, active polling, expiry, failure, readonly text, secret refusal; five guard mutations detected')})().catch(e=>{console.error(e);process.exitCode=1});

function nativeVisitChecks(wrapper=html.slice(html.indexOf('function nativeSync(){'),html.indexOf("$('#nativeRefresh').onclick"))){
  const f=nativeFixture(),context={NATIVE:f.api,NATIVE_VISIT:{agent:null,key:null,read:false},focus:nativeAgent,A:{[nativeAgent]:{},[nativeOther]:{}},KEY:'key',DEMO:false,claiming:false,locked:false,connected:true,document:{hidden:false},STEADY:false};
  const sync=vm.runInNewContext(wrapper+';nativeSync',context);
  sync();assert.equal(f.requests.length,1,'first selected desk read');const first=f.requests[0];
  context.connected=false;sync();assert.equal(first.opts.signal.aborted,true);assert.equal(f.api.state.snapshot,null);assert.equal(f.api.state.agent,null);
  context.connected=true;sync();assert.equal(f.requests.length,1,'same-visit SSE reconnect cannot auto-read');assert.equal(f.api.state.agent,nativeAgent);
  f.api.refresh();assert.equal(f.requests.length,2,'explicit Refresh remains available after reconnect');
  context.document.hidden=true;sync();context.document.hidden=false;sync();assert.equal(f.requests.length,2,'returning visible same visit cannot auto-read');
  context.locked=true;sync();context.locked=false;sync();assert.equal(f.requests.length,2,'lock visibility alone is not a new visit');
  context.focus=nativeOther;sync();assert.equal(f.requests.length,3,'explicit different desk starts one new read');
  context.KEY='rotated-key';sync();assert.equal(f.requests.length,4,'new owner credential starts a fresh authorization context');assert.equal(f.requests[3].opts.headers['X-Live-Room-Key'],'rotated-key');
  context.focus=null;sync();context.focus=nativeOther;sync();assert.equal(f.requests.length,5,'leaving and reopening desk is new visit');
}
nativeVisitChecks();
{
  const source=html.slice(html.indexOf('function nativeSync(){'),html.indexOf("$('#nativeRefresh').onclick"));
  let caught=false;try{nativeVisitChecks(source.replace('const auto=!NATIVE_VISIT.read','const auto=true'))}catch(e){caught=true}assert.ok(caught,'same-visit autoread guard mutation must fail');
}
console.log('ok native desk visits: SSE/visibility/lock purge without automatic retry; explicit refresh/focus/key transitions; autoread mutation detected');
