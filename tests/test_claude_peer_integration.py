"""Production MCP/HTTP/SQLite/plugin peer integration; native execution is mocked."""
import http.client
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import chat_directory
import officer_messaging
import peer_bridge
import server

ROOT = Path(__file__).resolve().parents[1]
SDK = ROOT/'integrations/claude/node_modules/@modelcontextprotocol/sdk/dist/esm'
NATIVE = ['11111111-1111-4111-8111-111111111111', '22222222-2222-4222-8222-222222222222']


@unittest.skipIf(os.name == 'nt' or not shutil.which('node') or not SDK.is_dir(), 'Requires POSIX, Node and optional MCP dependencies')
class ClaudePeerIntegration(unittest.TestCase):
    def test_actual_mcp_to_production_plugin_reply_and_owner_isolation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve(); root.chmod(0o700)
            project=root/'fictional-project'; project.mkdir()
            codex=root/'codex'; (codex/'sessions').mkdir(parents=True)
            claude=root/'claude'; claude.mkdir(); (claude/'fixture').mkdir()
            for native,name in zip(NATIVE,['Atlas','Birch']):
                (claude/'fixture'/(native+'.jsonl')).write_text(json.dumps(dict(type='custom-title',customTitle=name,sessionId=native,cwd=str(project)))+'\n')
            store=officer_messaging.MessagingStore(str(root/'state'),str(codex/'sessions'))
            store.codex_available=False
            agents={'c:'+native:dict(runtime='Claude',name=name,cwd=str(project)) for native,name in zip(NATIVE,['Atlas','Birch'])}
            httpd=server.Server(('127.0.0.1',0),server.H)
            with patch.multiple(server,MESSAGING=store,KEY='fictional-owner-key',PORT=httpd.server_port,AGENTS=agents):
                store.shared.directory=chat_directory.ChatDirectory(str(codex),str(claude),policy={'providers':['claude'],'history_days':'all'},live_snapshot=lambda:server.shared_directory_snapshot(store),cache_seconds=0)
                with store.lock,store.db:
                    credential=peer_bridge.initialize_service(store.db,{'providers':['claude'],'history_days':'all'})
                credential['port']=httpd.server_port
                credentials=root/'bridge-credentials';credentials.mkdir(mode=0o700)
                credential_file=credentials/(credential['credential_id']+'.json')
                credential_file.write_text(json.dumps(credential));credential_file.chmod(0o600)
                thread=threading.Thread(target=httpd.serve_forever,daemon=True);thread.start()
                try:
                    conn=http.client.HTTPConnection('127.0.0.1',httpd.server_port,timeout=5)
                    conn.request('POST','/claude-mod-setup',json.dumps({'peer_receive':True}),{'X-Live-Room-Key':server.KEY,'Content-Type':'application/json'})
                    response=conn.getresponse();body=response.read();conn.close()
                    self.assertEqual(response.status,200,body);setup=json.loads(body)
                    self.assertIs(setup['peer_receive'],True)
                    process=subprocess.run(['node',str(ROOT/'integrations/claude-mod/tests/peer.integration.mjs')],input=json.dumps(dict(port=httpd.server_port,code=setup['pair_code'],project=str(project),owner_key=server.KEY,native=NATIVE,credential=str(credential_file))),text=True,capture_output=True,timeout=40)
                    self.assertEqual(process.returncode,0,process.stdout+'\n'+process.stderr+'\nDirectory: '+json.dumps(store.shared.directory.list_agents()))
                    result=json.loads(process.stdout)
                    self.assertEqual(result,dict(peer_reply='Cedar is blue.',native_submissions=[1,1],duplicate_recovered=True,owner_reply='Fictional owner reply.'))
                    rows=store._rows();peers=[r for r in rows if r.get('origin')=='peer']
                    self.assertEqual(len(peers),1);self.assertEqual(peers[0]['state'],'answered')
                    self.assertEqual(peers[0]['agent'],'c:'+NATIVE[0]);self.assertEqual(peers[0]['transport'],'claude_mod')
                    self.assertEqual(store.db.execute('SELECT COUNT(*) FROM shared_bridge_requests WHERE launched IS NOT NULL').fetchone()[0],1)
                    owner=store.snapshot()['requests'];self.assertEqual(len(owner),1);self.assertEqual(owner[0]['agent'],'c:'+NATIVE[1])
                    self.assertEqual(store.db.execute('SELECT COUNT(*) FROM claude_mod_grants WHERE active=1').fetchone()[0],0)
                finally:
                    httpd.shutdown();httpd.server_close();thread.join();store.close()


if __name__ == '__main__':unittest.main()
