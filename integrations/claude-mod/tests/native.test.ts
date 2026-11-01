import {test, expect, mock} from 'claude-code/testing';

const NATIVE='11111111-1111-4111-8111-111111111111';
const GENERATION='22222222-2222-4222-8222-222222222222';
const REQUEST='33333333-3333-4333-8333-333333333333';
const ATTEMPT='44444444-4444-4444-8444-444444444444';
const ENROLLMENT='55555555-5555-4555-8555-555555555555';
const ENVELOPE='WATCH_REQUEST '+REQUEST+'\nThe owner asks through the Officer of the Watch:\nSay fictional progress.\nReply with WATCH_REPLY '+REQUEST+' on its own line. Preserve owner scope.';
const ANSWER='WATCH_REPLY '+REQUEST+'\nFictional progress.';

test('one official mod submits attributed full owner envelope and observes its own main reply',async($,on)=>{
  const clock=mock.clock(on,{now:100000});
  const values=new Map();const submissions=[];const events=[];const heartbeats=[];
  let claim=true;
  on('session.id',()=>({value:NATIVE}));
  on('session.cwd',()=>({value:'/fictional/project'}));
  on('store.get',($,e)=>({value:values.get(e.key)}));
  on('store.set',($,e)=>{values.set(e.key,JSON.parse(JSON.stringify(e.value)));return {value:undefined};});
  on('command.register',()=>({value:undefined}));
  on('session.start',($,e)=>({cwd:e.cwd}));
  on('http.fetch',($,e)=>{
    const body=JSON.parse(e.init.body);let data;
    const session={version:1,native_id:NATIVE,generation:GENERATION,heartbeat_interval_ms:2000,lease_seconds:12};
    if(e.url.endsWith('/enroll'))data={...session,token:'fictional-watch-token-only',enrollment_id:ENROLLMENT};
    else if(e.url.endsWith('/poll')){
      heartbeats.push(body);
      data={...session,requests:[],claim:claim&&body.accept?{request_id:REQUEST,attempt_id:ATTEMPT,native_id:NATIVE,generation:GENERATION,envelope:ENVELOPE,expires_at:1000}:null};
      if(data.claim)claim=false;
    }else if(e.url.endsWith('/event')){events.push(body);data={request_id:REQUEST,state:body.kind,turn_id:body.turn_id||''};}
    else throw Error('unexpected offline path');
    return {value:{ok:true,status:200,headers:{},text:JSON.stringify(data)}};
  });
  // The official test core is mocked: this verifies actual one-plugin API/observer routing,
  // not a live REPL's preprocessing, queue timing or model behavior.
  on('prompt.submit',async(core,e)=>{
    submissions.push({text:e.text,origin:e.origin});
    const stored=[...values.values()][0];
    expect(stored.requests[REQUEST].attempted).toBe(true);
    await $.turn.start({text:e.text,turnId:'offline-main-turn'});
    await $.turn.complete({turnId:'offline-main-turn',agentId:'offline-child',answer:ANSWER,isAborted:false,reason:'answer',durationMs:1});
    await $.turn.complete({turnId:'unrelated-main-turn',answer:ANSWER,isAborted:false,reason:'answer',durationMs:1});
    expect(events.some(event=>event.kind==='answered')).toBe(false);
    await $.turn.complete({turnId:'offline-main-turn',answer:ANSWER,isAborted:false,reason:'answer',durationMs:1});
    return {text:e.text};
  });
  on('turn.start',($,e)=>({turnId:e.turnId}));
  on('turn.complete',($,e)=>({text:e.answer}));
  await $.session.start({cwd:'/fictional/project',surface:'desktop',isInteractive:true});
  const connected=await $.command.run({command:'watch-connect',args:'8793:fictional_pair_code_1234',origin:{kind:'composer'}});
  expect(connected.text.includes('WATCH connected')).toBe(true);
  await clock.advance(2000);
  await clock.advance(2000);
  expect(submissions.length).toBe(1);
  expect(submissions[0].text).toBe(ENVELOPE);
  expect(submissions[0].origin.kind).toBe('plugin');
  expect(submissions[0].origin.name).toBe('watch-owner');
  // This exact host omits the optional asUser field for false (never true).
  expect(submissions[0].origin.asUser === true).toBe(false);
  expect(events.filter(event=>event.kind==='answered').length).toBe(1);
  expect(events.find(event=>event.kind==='answered').answer).toBe(ANSWER);
  expect(events.find(event=>event.kind==='answered').turn_id).toBe('offline-main-turn');
  expect(events.filter(event=>event.kind==='submitted').length).toBe(0);
});

test('nonowner enrollment refuses before HTTP or native identity access',async($,on)=>{
  on('http.fetch',()=>{throw Error('must not contact service');});
  on('session.id',()=>{throw Error('must not read identity');});
  const result=await $.command.run({command:'watch-connect',args:'8793:fictional_pair_code_1234',origin:{kind:'plugin',name:'other-mod'}});
  expect(result.text).toBe('WATCH connection commands require direct owner input.');
});
