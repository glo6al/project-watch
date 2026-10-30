#!/usr/bin/env python3
"""Actual Python/Node/MCP integration with a simulated Claude client. No model calls."""
import json
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


@unittest.skipIf(os.name == 'nt' or not shutil.which('node') or not (ROOT / 'integrations/claude/node_modules/@modelcontextprotocol/sdk').exists(), 'Needs POSIX and optional channel npm dependencies')
class IntegrationTests(unittest.TestCase):
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
