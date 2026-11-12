#!/usr/bin/env python3
"""Real SDK/HTTP/SQLite/queue integration; simulated clients, never live agents."""
import json
from http.client import HTTPConnection
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
CONNECTOR = ROOT / 'integrations/claude'
SDK = CONNECTOR / 'node_modules/@modelcontextprotocol/sdk/dist/esm'
sys.path.insert(0, str(ROOT / 'src'))
import officer_messaging
import peer_bridge
import chat_directory
import claude_channel
from legacy_claude_fixture import seed_legacy_claude
import server

RPC = r'''
const {Client}=await import(process.env.TEST_SDK+'/client/index.js');
const {StdioClientTransport}=await import(process.env.TEST_SDK+'/client/stdio.js');
const c=new Client({name:'simulated shared client',version:'1.0.0'});
try {
 await c.connect(new StdioClientTransport({command:process.execPath,args:[process.env.TEST_ADAPTER,'--credential',process.env.TEST_CREDENTIAL]}));
 const tools=await c.listTools();
 if(tools.tools.map(t=>t.name).sort().join(',')!=='get_reply,list_agents,list_requests,send_message')throw Error('Unexpected tool surface');
 const result=await c.callTool({name:process.env.TEST_METHOD,arguments:JSON.parse(process.env.TEST_PARAMS)});
 process.stdout.write(JSON.stringify(result)+'\n');
} finally {await c.close()}
'''

CHANNEL = r'''
const {Client}=await import(process.env.TEST_SDK+'/client/index.js');
const {StdioClientTransport}=await import(process.env.TEST_SDK+'/client/stdio.js');
const {z}=await import(process.env.TEST_ZOD);
const fs=await import('node:fs/promises');
const c=new Client({name:'simulated Claude channel client',version:'1.0.0'});
c.setNotificationHandler(z.object({method:z.literal('notifications/claude/channel'),params:z.object({content:z.string(),meta:z.record(z.string(),z.string())})}),async event=>{
 const p=event.params;
 await fs.appendFile(process.env.TEST_EVENTS,JSON.stringify(p)+'\n');
 const reply='SIMULATED Claude: '+p.meta.origin+' reply';
 const result=await c.callTool({name:'reply',arguments:{request_id:p.meta.request_id,text:reply}});
 if(result.isError)throw Error('Channel reply rejected');
});
await c.connect(new StdioClientTransport({command:process.execPath,args:[process.env.TEST_CHANNEL],env:{...process.env}}));
process.on('SIGTERM',async()=>{await c.close();process.exit(0)});
'''


@unittest.skipIf(os.name == 'nt' or not shutil.which('node') or not SDK.is_dir(),
                 'Needs POSIX, Node.js, and optional channel npm dependencies')
class PeerIntegrationTests(unittest.TestCase):
    def test_shared_outbound_codex_and_blocked_claude_with_owner_channel(self):
        for label,text in [('unicode','🚀'*16000),('escaped-controls','\x01'*15999+'🚀')]:
            with self.subTest(message=label):
                self.exercise_shared_outbound(text)

    def exercise_shared_outbound(self,large_text):
        """Shared access is independent of native caller, target provider, and project."""
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            project = base / 'project-one'; project.mkdir(mode=0o700)
            other_project = base / 'project-two'; other_project.mkdir(mode=0o700)
            state = base / 'state'; state.mkdir(mode=0o700)
            codex_root = base / 'codex'; codex_root.mkdir(mode=0o700)
            sessions = codex_root / 'sessions'; sessions.mkdir(mode=0o700)
            claude_root = base / 'claude-projects'; claude_root.mkdir(mode=0o700)
            claude_project = claude_root / 'fixture'; claude_project.mkdir(mode=0o700)
            channels = base / 'channels'; channels.mkdir(mode=0o700)
            credentials = state / 'bridge-credentials'; credentials.mkdir(mode=0o700)
            codex_session, claude_session, instance = (str(uuid.uuid4()) for _ in range(3))
            transcript = sessions / ('rollout-' + codex_session + '.jsonl')
            transcript.write_text(json.dumps({'type':'session_meta','payload':{
                'id':codex_session,'cwd':str(project),'source':'vscode','agent_path':'/root'}})+'\n')
            (codex_root / 'session_index.jsonl').write_text(json.dumps({'id':codex_session,'thread_name':'Codex fixture'})+'\n')
            (claude_project / (claude_session+'.jsonl')).write_text(json.dumps({'type':'custom-title','customTitle':'Claude fixture','cwd':str(other_project),'sessionId':claude_session})+'\n')
            channel_state = channels / claude_session; channel_state.mkdir(mode=0o700)
            events, launches = base / 'events.jsonl', base / 'launches.jsonl'
            fake_cli = base / 'codex-fixture'
            fake_cli.write_text('#!' + sys.executable + '\n' + '''import json,sys
args=sys.argv[1:]
assert args[:1]==['queue']
message=args[args.index('--message')+1]
assert message.startswith('WATCH_PEER_REQUEST ')
assert args[args.index('--thread')+1]==SESSION
with open(LAUNCHES,'a') as f:f.write(json.dumps(args)+'\\n')
'''.replace('SESSION', repr(codex_session)).replace('LAUNCHES', repr(str(launches))))
            fake_cli.chmod(0o700)
            env = dict(os.environ, TEST_SDK=SDK.as_uri(),
                       TEST_ZOD=(CONNECTOR/'node_modules/zod/index.js').as_uri(),
                       TEST_CHANNEL=os.environ.get('WATCH_PEER_INTEGRATION_CHANNEL', str(CONNECTOR/'channel.mjs')),
                       TEST_EVENTS=str(events), WATCH_CLAUDE_SESSION_ID=claude_session,
                       WATCH_CLAUDE_INSTANCE_ID=instance, WATCH_CLAUDE_STATE_DIR=str(channel_state),
                       WATCH_CLAUDE_PROJECT=str(other_project))
            channel = subprocess.Popen([shutil.which('node'),'--input-type=module','-e',CHANNEL],
                                       cwd=other_project,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            store = http = thread = None
            try:
                self.wait_for(lambda:(channel_state/'connection.json').exists(), channel)
                descriptor = json.loads((channel_state/'connection.json').read_text())
                self.assertEqual((descriptor['version'],descriptor['peer_protocol'],descriptor['project']), (2,1,str(other_project)))
                store = officer_messaging.MessagingStore(str(state),str(sessions),cli=str(fake_cli),channel_root=str(channels))
                store.available = store.codex_available = True
                def configure_directory(current):
                    live = lambda: {'x:'+codex_session:{'capability':'reachable','reason':'Verified fixture transport'},
                        '__claude_transport__':{'capability':'unavailable','reason':claude_channel.PEER_IDENTITY_REASON}}
                    current.shared.directory = chat_directory.ChatDirectory(str(codex_root),str(claude_root),
                        policy={'history_days':'all'},live_snapshot=live,cache_seconds=0)
                configure_directory(store)
                with store.lock:
                    client_one = peer_bridge.initialize_service(store.db, {'history_days':'all'})
                    client_two = peer_bridge.issue_service_credential(store.db)
                    store.db.commit()
                http = server.Server(('127.0.0.1',0),server.H)
                thread = threading.Thread(target=http.serve_forever,daemon=True); thread.start()
                for credential in (client_one,client_two):
                    credential['port'] = http.server_address[1]
                    path = credentials/(credential['credential_id']+'.json')
                    path.write_text(json.dumps(credential)); path.chmod(0o600)
                def rpc(endpoint, method, params=None, expect_error=False):
                    rpc_env = dict(env,TEST_ADAPTER=str(CONNECTOR/'peer.mjs'),
                        TEST_CREDENTIAL=str(credentials/(endpoint['credential_id']+'.json')),
                        TEST_METHOD=method,TEST_PARAMS=json.dumps(params or {},ensure_ascii=False))
                    result = subprocess.run([shutil.which('node'),'--input-type=module','-e',RPC],
                        cwd=project,env=rpc_env,capture_output=True,text=True,timeout=15)
                    self.assertEqual(result.returncode,0,result.stderr[:1500])
                    tool = json.loads(result.stdout)
                    if expect_error:
                        self.assertTrue(tool.get('isError'), tool)
                        return tool
                    self.assertFalse(tool.get('isError'),tool)
                    wrapped = json.loads(tool['content'][0]['text'])
                    self.assertEqual(wrapped['origin'],'peer')
                    return wrapped['untrusted_peer_data']
                with patch.multiple(server,MESSAGING=store,PORT=http.server_address[1],KEY='fictional-owner-test-key'):
                    listing = rpc(client_one,'list_agents')
                    self.assertTrue(listing['directory']['complete'],listing)
                    peers = listing['agents']
                    self.assertEqual({p['agent_id'] for p in peers},{'x:'+codex_session,'c:'+claude_session})
                    self.assertEqual({p['provider'] for p in peers},{'codex','claude'})
                    self.assertEqual(len({p['project_id'] for p in peers}),2)
                    self.assertEqual({p['provider']:p['capability'] for p in peers}, {'codex':'reachable','claude':'unavailable'})
                    codex = next(p for p in peers if p['provider']=='codex')
                    claude = next(p for p in peers if p['provider']=='claude')
                    # Exercise the actual argv transport near the worst JSON-escape
                    # expansion, including a non-BMP scalar counted as one character.
                    self.assertEqual(len(large_text),16000)
                    first = {'agent_id':codex['agent_id'],'identity_revision':codex['identity_revision'],
                        'text':large_text,'request_id':str(uuid.uuid4()),'declared_sender':'Unverified fixture label'}
                    oversized = dict(first,text=large_text+'x',request_id=str(uuid.uuid4()))
                    rejected_size = rpc(client_one,'send_message',oversized,expect_error=True)
                    self.assertIn('16001',rejected_size['content'][0]['text'])
                    self.assertIn('16000',rejected_size['content'][0]['text'])
                    with store.lock:
                        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM shared_bridge_requests').fetchone()[0],0)
                    self.assertFalse(launches.exists())
                    accepted = rpc(client_one,'send_message',first)
                    self.assertEqual(accepted['state'],'sending')
                    self.assertFalse(accepted['duplicate'])
                    # A different configured client recovers the same ID without redispatch.
                    duplicate = rpc(client_two,'send_message',first)
                    self.assertEqual(duplicate['state'],'sending')
                    self.assertTrue(duplicate['duplicate'])
                    self.assertEqual(duplicate['created_at'],accepted['created_at'])
                    store.tick()
                    queued = rpc(client_two,'get_reply',{'request_id':first['request_id']})
                    self.assertEqual((queued['state'],queued['reply']),('queued',''))
                    self.assertEqual(len(launches.read_text().splitlines()),1)
                    native_args=json.loads(launches.read_text())
                    native_envelope=native_args[native_args.index('--message')+1]
                    self.assertEqual(json.loads(native_envelope.splitlines()[1].removeprefix('Message: ')),large_text)
                    with store.lock:
                        transport_id = store.db.execute('SELECT transport_id FROM shared_bridge_requests WHERE client_id=?',
                            (first['request_id'],)).fetchone()[0]
                        late = next(row for row in store._rows() if row['request_id'] == transport_id)
                        self.assertEqual(late['question'],large_text)
                        self.assertEqual(late['envelope'],native_envelope)
                        late['created'] = time.time()-peer_bridge.TTL-60
                        store._save(late)
                        store.db.execute('UPDATE shared_bridge_requests SET created=? WHERE transport_id=?',(late['created'],transport_id))
                        store.db.commit()
                    store.tick()
                    expired = rpc(client_two,'get_reply',{'request_id':first['request_id']})
                    self.assertEqual((expired['state'],expired['reply']),('expired',''))
                    # Queue acceptance happens first; an independent later Codex turn writes its answer.
                    self.append_codex_turn(transcript,late,launches=launches)
                    store.tick()
                    answer = rpc(client_two,'get_reply',{'request_id':first['request_id']})
                    self.assertEqual((answer['state'],answer['reply']),('answered','SIMULATED Codex reply'))
                    retry = rpc(client_two,'send_message',first)
                    self.assertTrue(retry.pop('duplicate'))
                    self.assertEqual(retry,{k:v for k,v in answer.items() if k!='duplicate'})
                    sent = rpc(client_two,'list_requests')['requests']
                    self.assertEqual([r['request_id'] for r in sent],[first['request_id']])
                    self.assertNotIn('reply',sent[0])
                    second = {'agent_id':claude['agent_id'],'identity_revision':claude['identity_revision'],'text':'Report fixture progress','request_id':str(uuid.uuid4())}
                    rejected = rpc(client_one,'send_message',second,expect_error=True)
                    self.assertIn('native conversation identity', rejected['content'][0]['text'])
                    store.tick(); store.tick()
                    self.assertEqual(len(launches.read_text().splitlines()),1)
                    self.assertFalse(events.exists(), 'Blocked shared delivery emits no Claude channel notification')
                    self.assertEqual(store.snapshot()['requests'],[])
                    owner_http = HTTPConnection('127.0.0.1',http.server_address[1],timeout=3)
                    try:
                        owner_http.request('GET','/agent-messages',headers={'X-Live-Room-Key':'fictional-owner-test-key'})
                        owner_response = owner_http.getresponse()
                        self.assertEqual(owner_response.status,200)
                        public = owner_response.read().decode()
                        self.assertEqual(json.loads(public)['requests'],[])
                        self.assertNotIn(first['request_id'],public)
                        self.assertNotIn(second['request_id'],public)
                        self.assertNotIn('SIMULATED Codex reply',public)
                    finally:
                        owner_http.close()
                    with store.lock:
                        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM shared_bridge_requests').fetchone()[0],1)
                        rows = store._rows()
                    self.assertEqual({r['state'] for r in rows},{'answered'})
                    self.assertEqual({r['origin'] for r in rows},{'peer'})
                    # Restart the actual SQLite store; get_reply remains readable without redispatch.
                    store.close(); store = None
                    store = officer_messaging.MessagingStore(str(state),str(sessions),cli=str(fake_cli),channel_root=str(channels))
                    store.available = store.codex_available = True
                    configure_directory(store)
                    server.MESSAGING = store
                    self.assertEqual(rpc(client_two,'get_reply',{'request_id':first['request_id']}),answer)
                    store.tick()
                    self.assertEqual(store.snapshot()['requests'],[])
                    self.assertEqual(len(launches.read_text().splitlines()),1)
                    self.assertFalse(events.exists())
                    refreshed = rpc(client_two,'list_agents')['agents']
                    codex = next(p for p in refreshed if p['provider']=='codex')
                    # A completed turn with a bad marker is settled, not an uncertain live delivery.
                    # Repeated model formatting slips must not occupy the target until retention.
                    for bad_marker in ('no marker here', 'WATCH_REPLY '+str(uuid.uuid4())):
                        malformed = {'agent_id':codex['agent_id'],'identity_revision':codex['identity_revision'],'text':'Formatting fixture '+str(uuid.uuid4()),
                                     'request_id':str(uuid.uuid4())}
                        self.assertEqual(rpc(client_two,'send_message',malformed)['state'],'sending')
                        store.tick()
                        with store.lock:
                            transport_id = store.db.execute('SELECT transport_id FROM shared_bridge_requests WHERE client_id=?',
                                (malformed['request_id'],)).fetchone()[0]
                            row = next(row for row in store._rows() if row['request_id'] == transport_id)
                        self.append_codex_turn(transcript,row,answer=bad_marker,launches=launches)
                        store.tick()
                        rejected = rpc(client_two,'get_reply',{'request_id':malformed['request_id']})
                        self.assertEqual((rejected['state'],rejected['reply']),('unknown',''))
                        peers = rpc(client_two,'list_agents')['agents']
                        self.assertEqual(next(p for p in peers if p['agent_id'] == codex['agent_id'])['capability'],'reachable')
                    recovered = {'agent_id':codex['agent_id'],'identity_revision':codex['identity_revision'],'text':'After formatting slips','request_id':str(uuid.uuid4())}
                    self.assertEqual(rpc(client_two,'send_message',recovered)['state'],'sending')
                    store.tick()
                    with store.lock:
                        transport_id = store.db.execute('SELECT transport_id FROM shared_bridge_requests WHERE client_id=?',
                            (recovered['request_id'],)).fetchone()[0]
                        row = next(row for row in store._rows() if row['request_id'] == transport_id)
                    self.append_codex_turn(transcript,row,launches=launches)
                    store.tick()
                    recovered_reply = rpc(client_two,'get_reply',{'request_id':recovered['request_id']})
                    self.assertEqual((recovered_reply['state'],recovered_reply['reply']),('answered','SIMULATED Codex reply'))
                    self.assertTrue(rpc(client_two,'send_message',recovered)['duplicate'])
                    store.tick()
                    self.assertEqual(len(launches.read_text().splitlines()),4)
                    self.assertFalse(events.exists())
                    self.assertEqual(store.snapshot()['requests'],[])
                    # Legacy owner rows retain their existing transport and reply behavior.
                    owner_id = str(uuid.uuid4())
                    seed_legacy_claude(store, 'c:'+claude_session, 'Owner fixture question', owner_id,
                        {'c:'+claude_session:{'runtime':'Claude','name':'Claude fixture'}})
                    store.tick()
                    self.wait_for(lambda: events.exists(),channel)
                    self.wait_for(lambda: self.channel_answered(channel_state),channel)
                    store.tick()
                    owner_reply = next(row for row in store.snapshot()['requests'] if row['request_id']==owner_id)
                    self.assertEqual((owner_reply['state'],owner_reply['reply']),('answered','SIMULATED Claude: owner reply'))
                    self.assertEqual(json.loads(events.read_text())['meta']['origin'],'owner')

            finally:
                if http:
                    http.shutdown(); http.server_close()
                if thread: thread.join(3)
                if store: store.close()
                channel.terminate()
                try: channel.communicate(timeout=8)
                except subprocess.TimeoutExpired:
                    channel.kill(); channel.communicate(timeout=3)

    def append_codex_turn(self, transcript, row, *, launches, answer=None):
        queued = json.loads(launches.read_text().splitlines()[-1])
        self.assertEqual(queued[queued.index('--message')+1],row['envelope'])
        turn = str(uuid.uuid4())
        records = [
            {'type':'event_msg','payload':{'type':'task_started','turn_id':turn}},
            {'type':'response_item','payload':{'type':'message','role':'user','turn_id':turn,
                'content':[{'type':'input_text','text':row['envelope']}]}},
            {'type':'event_msg','payload':{'type':'task_complete','turn_id':turn,
                'last_agent_message':answer if answer is not None else
                    'WATCH_REPLY '+row['request_id']+'\nSIMULATED Codex reply'}},
        ]
        with transcript.open('a') as output:
            for record in records:
                output.write(json.dumps(record)+'\n')

    def wait_for(self, predicate, process):
        deadline = time.monotonic()+8
        while not predicate():
            if process.poll() is not None:
                self.fail('Simulated channel exited: '+process.stderr.read()[:1500])
            if time.monotonic()>deadline:
                self.fail('Timed out waiting for actual simulated channel round trip')
            time.sleep(0.03)

    @staticmethod
    def channel_answered(directory):
        for path in directory.glob('*.json'):
            if path.name != 'connection.json' and json.loads(path.read_text()).get('state') == 'answered':
                return True
        return False


if __name__ == '__main__':
    unittest.main()
