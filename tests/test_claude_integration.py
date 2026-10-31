#!/usr/bin/env python3
"""Actual Python/Node/MCP integration with a simulated Claude client. No model calls."""
import json
import http.client
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from officer_messaging import MessagingStore
import claude_channel

PEER = r'''
import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StdioClientTransport} from '@modelcontextprotocol/sdk/client/stdio.js';
import {z} from 'zod';
const client = new Client({name:'WATCH integration simulated Claude',version:'1.0.0'});
client.setNotificationHandler(z.object({method:z.literal('notifications/claude/channel'),params:z.object({content:z.string(),meta:z.record(z.string(),z.string())})}), async event => {
  const p=event.params;
  if(!p.content.includes('Implement the test feature')||p.meta.session_id!==process.env.WATCH_CLAUDE_SESSION_ID) process.exit(5);
  await new Promise(r=>setTimeout(r,200));
  const result=await client.callTool({name:'reply',arguments:{request_id:p.meta.request_id,text:'SIMULATED: received the work instruction in the matching session.'}});
  if(result.isError) process.exit(6);
});
await client.connect(new StdioClientTransport({command:process.execPath,args:['channel.mjs'],env:{...process.env}}));
process.on('SIGTERM',async()=>{await client.close();process.exit(0)});
'''


INFLUENCE_PEER = r'''
import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StdioClientTransport} from '@modelcontextprotocol/sdk/client/stdio.js';
import {z} from 'zod';
const client=new Client({name:'simulated Claude influence test',version:'1'});
let ownerID;
client.setNotificationHandler(z.object({method:z.literal('notifications/claude/channel'),params:z.object({content:z.string(),meta:z.record(z.string(),z.string())})}),async event=>{
 const p=event.params;
 if(p.meta.origin==='owner'){ownerID=p.meta.request_id;return}
 if(p.meta.origin!=='peer'||!ownerID)process.exit(7);
 for(const [id,text] of [[ownerID,'all clear'],[p.meta.request_id,'Peer received']]){
  const result=await client.callTool({name:'reply',arguments:{request_id:id,text}});
  if(result.isError)process.exit(8);
 }
});
await client.connect(new StdioClientTransport({command:process.execPath,args:['channel.mjs'],env:{...process.env}}));
process.on('SIGTERM',async()=>{await client.close();process.exit(0)});
'''


@unittest.skipIf(os.name == 'nt' or not shutil.which('node') or not (ROOT / 'integrations/claude/node_modules/@modelcontextprotocol/sdk').exists(), 'Needs POSIX and optional channel npm dependencies')
class IntegrationTests(unittest.TestCase):
    def test_historical_peer_influence_remains_flagged_with_get_polling(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            channel_root = base / 'claude-channels'; channel_root.mkdir(mode=0o700)
            session, instance = str(uuid.uuid4()), str(uuid.uuid4())
            channel_state = channel_root / session; channel_state.mkdir(mode=0o700)
            env = dict(os.environ, WATCH_CLAUDE_SESSION_ID=session, WATCH_CLAUDE_INSTANCE_ID=instance,
                       WATCH_CLAUDE_STATE_DIR=str(channel_state), WATCH_CLAUDE_PROJECT=str(base))
            peer = subprocess.Popen([shutil.which('node'), '--input-type=module', '-e', INFLUENCE_PEER],
                                    cwd=ROOT / 'integrations/claude', env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
            store = None
            try:
                deadline = time.monotonic() + 10
                while True:
                    try:
                        descriptor = claude_channel.discover(str(channel_root), session)
                        break
                    except claude_channel.ChannelError:
                        pass
                    if peer.poll() is not None: self.fail('Simulated peer failed: ' + peer.stderr.read()[:1000])
                    if time.monotonic() > deadline: self.fail('Channel discovery timed out')
                    time.sleep(.02)
                self.assertEqual(descriptor['version'], 2)
                store = MessagingStore(str(base / 'state'), str(base / 'codex'), channel_root=str(channel_root))
                owner_id, peer_id = str(uuid.uuid4()), str(uuid.uuid4())
                aid = 'c:' + session
                store.submit(aid, 'How is the work progressing?', owner_id, {aid: {'name': 'Claude', 'runtime': 'Claude'}})
                store.tick()
                self.assertEqual(store.snapshot()['requests'][0]['state'], 'queued')
                with self.assertRaisesRegex(claude_channel.ChannelError, 'native conversation identity'):
                    claude_channel.exchange(descriptor, peer_id, 'New peer payload', origin='peer')
                # Seed a historical pre-gate delivery directly into the disposable
                # fake channel. Production WATCH may only GET this existing reply.
                connection = http.client.HTTPConnection('127.0.0.1', descriptor['port'], timeout=3)
                connection.request('POST', '/requests', json.dumps(dict(session_id=session, instance_id=instance,
                    request_id=peer_id, content='Historical fixture peer input', origin='peer')),
                    {'Authorization':'Bearer '+descriptor['token'],'Content-Type':'application/json'})
                response=connection.getresponse(); response.read(); self.assertIn(response.status,(200,202)); connection.close()
                deadline = time.monotonic() + 10
                while store.snapshot()['requests'][0]['state'] != 'answered':
                    if peer.poll() is not None: self.fail('Simulated peer exited')
                    if time.monotonic() > deadline: self.fail('Owner reply timed out')
                    time.sleep(.02); store.tick()
                result = store.snapshot()['requests'][0]
                self.assertEqual(result['reply'], 'all clear')
                self.assertIn('may have received peer input before answering', result['delivery_warning'])
                record = claude_channel.exchange(descriptor, owner_id)
                self.assertTrue(record['peer_input_seen'])
                self.assertEqual(record['origin'], 'owner')
                self.assertEqual(claude_channel.exchange(descriptor, peer_id, origin='peer')['reply'], 'Peer received')
                store.close(); store = None
                store = MessagingStore(str(base / 'state'), str(base / 'codex'), channel_root=str(channel_root))
                self.assertEqual(store.snapshot()['requests'][0]['delivery_warning'], result['delivery_warning'])
            finally:
                if store: store.close()
                peer.terminate()
                try: peer.communicate(timeout=8)
                except subprocess.TimeoutExpired: peer.kill(); peer.communicate()

    def test_real_http_mcp_round_trip_and_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            channel_root=base/'claude-channels';channel_root.mkdir(mode=0o700)
            session=str(uuid.uuid4()); instance=str(uuid.uuid4())
            channel_state=channel_root/session;channel_state.mkdir(mode=0o700)
            env=dict(os.environ, WATCH_CLAUDE_SESSION_ID=session, WATCH_CLAUDE_INSTANCE_ID=instance, WATCH_CLAUDE_STATE_DIR=str(channel_state))
            peer=subprocess.Popen([shutil.which('node'),'--input-type=module','-e',PEER],cwd=ROOT/'integrations/claude',env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            store=None
            try:
                deadline=time.monotonic()+10
                while True:
                    try:
                        json.loads((channel_state/'connection.json').read_text())
                        break
                    except (OSError, ValueError):
                        pass
                    if peer.poll() is not None: self.fail('Simulated peer failed: '+peer.stderr.read()[:1000])
                    if time.monotonic()>deadline:self.fail('Channel discovery timed out')
                    time.sleep(.02)
                store=MessagingStore(str(base/'state'),str(base/'codex'),channel_root=str(channel_root))
                rid=str(uuid.uuid4()); aid='c:'+session; agents={aid:{'name':'Claude test peer','runtime':'Claude'}}
                initial=store.submit(aid,'Implement the test feature',rid,agents)
                self.assertEqual(initial['state'],'sending')
                self.assertFalse(store.snapshot()['capabilities']['codex'])
                self.assertTrue(store.snapshot()['capabilities']['claude'])
                store.tick()
                deadline=time.monotonic()+10
                while store.snapshot()['requests'][0]['state']!='answered':
                    if time.monotonic()>deadline:self.fail('Reply timed out: '+str(store.snapshot()['requests']))
                    time.sleep(.05);store.tick()
                result=store.snapshot()['requests'][0]
                self.assertEqual(result['reply'],'SIMULATED: received the work instruction in the matching session.')
                self.assertEqual(store.submit(aid,'Implement the test feature',rid,{}),result)
                store.close();store=None
                store=MessagingStore(str(base/'state'),str(base/'codex'),channel_root=str(channel_root))
                self.assertEqual(store.snapshot()['requests'],[result])
            finally:
                if store:store.close()
                peer.terminate()
                try:peer.communicate(timeout=8)
                except subprocess.TimeoutExpired:peer.kill();peer.communicate()
            self.assertFalse((channel_state/'connection.json').exists())


if __name__=='__main__':unittest.main()
