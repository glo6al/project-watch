"""Real WATCH HTTP plus two official-mod runtimes with mocked native execution.

This proves protocol integration, not live Claude installation or model behavior.
All identities, owner credentials and messages are disposable test fixtures.
"""
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
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import officer_messaging
import server

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ['11111111-1111-4111-8111-111111111111', '22222222-2222-4222-8222-222222222222']


@unittest.skipIf(os.name == 'nt' or not shutil.which('node'), 'Requires private POSIX outbox and Node')
class GlobalModIntegration(unittest.TestCase):
    def test_two_loaded_chats_one_authorization_isolated_request_and_reply(self):
        previous = server.MESSAGING, server.KEY, server.PORT, dict(server.AGENTS)
        with tempfile.TemporaryDirectory() as temp:
            store = officer_messaging.MessagingStore(str(Path(temp)/'state'), str(Path(temp)/'sessions'))
            store.codex_available = False
            server.MESSAGING, server.KEY = store, 'fictional-owner-credential'
            server.AGENTS.clear()
            for native, name in zip(NATIVE, ['Atlas', 'Birch']):
                server.AGENTS['c:'+native] = dict(runtime='Claude', name=name, cwd=os.path.realpath(temp))
            httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.H)
            server.PORT = httpd.server_port
            thread = threading.Thread(target=httpd.serve_forever, daemon=True); thread.start()
            try:
                conn = http.client.HTTPConnection('127.0.0.1', server.PORT, timeout=5)
                conn.request('POST', '/claude-mod-setup', '{}', {'X-Live-Room-Key':server.KEY, 'Content-Type':'application/json'})
                reply = conn.getresponse(); body=reply.read(); conn.close()
                self.assertEqual(reply.status, 200, body)
                setup=json.loads(body)
                process = subprocess.run(['node',str(ROOT/'integrations/claude-mod/tests/global.integration.mjs')],
                    input=json.dumps(dict(port=server.PORT, code=setup['pair_code'], project=os.path.realpath(temp), owner_key=server.KEY, native=NATIVE)),
                    text=True, capture_output=True, timeout=30)
                self.assertEqual(process.returncode, 0, process.stdout+'\n'+process.stderr)
                result=json.loads(process.stdout)
                self.assertEqual(result, dict(authorized_once=True, native_submissions=2, replies=['Fictional Atlas reply.', 'Fictional Birch reply.']))
                rows=store.snapshot()['requests']
                self.assertEqual(len(rows),2)
                self.assertEqual({r['agent'] for r in rows},{'c:'+n for n in NATIVE})
                self.assertTrue(all(r['state']=='answered' for r in rows))
                self.assertEqual(len({r['request_id'] for r in rows}),2)
                self.assertEqual(store.db.execute('SELECT COUNT(*) FROM claude_mod_grants WHERE active=1').fetchone()[0],0,'the runner revokes automatic connections at completion')
            finally:
                httpd.shutdown();httpd.server_close();thread.join();store.close()
                server.MESSAGING, server.KEY, server.PORT=previous[:3]
                server.AGENTS.clear();server.AGENTS.update(previous[3])


if __name__ == '__main__':
    unittest.main()
