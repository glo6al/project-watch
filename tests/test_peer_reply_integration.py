"""Real MCP, hook, HTTP, SQLite and plugin integration; native turns are mocked."""
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import chat_directory
import officer_messaging
import peer_bridge
import server

SDK = ROOT / 'integrations/claude/node_modules/@modelcontextprotocol/sdk/dist/esm'
NATIVE = ['11111111-1111-4111-8111-111111111111',
          '22222222-2222-4222-8222-222222222222',
          '33333333-3333-4333-8333-333333333333']
CODEX_SOURCE = '44444444-4444-4444-8444-444444444444'


@unittest.skipIf(os.name == 'nt' or not shutil.which('node') or not SDK.is_dir(),
                 'Requires POSIX, Node and optional MCP dependencies')
class PeerReplyIntegration(unittest.TestCase):
    def test_two_source_providers_return_after_the_first_turn(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            root.chmod(0o700)
            project = root / 'fictional-project'
            project.mkdir()
            codex = root / 'codex'
            sessions = codex / 'sessions'
            sessions.mkdir(parents=True)
            transcript = sessions / ('rollout-' + CODEX_SOURCE + '.jsonl')
            transcript.write_text(json.dumps({'type': 'session_meta', 'payload': {
                'id': CODEX_SOURCE, 'cwd': str(project), 'source': 'vscode',
                'agent_path': '/root'}}) + '\n')
            (codex / 'session_index.jsonl').write_text(json.dumps({
                'id': CODEX_SOURCE, 'thread_name': 'Fictional source'}) + '\n')
            claude = root / 'claude'
            (claude / 'fixture').mkdir(parents=True)
            for native, name in zip(NATIVE, ['Target', 'Source', 'Sibling']):
                (claude / 'fixture' / (native + '.jsonl')).write_text(json.dumps({
                    'type': 'custom-title', 'customTitle': name,
                    'sessionId': native, 'cwd': str(project)}) + '\n')
            source_path = claude / 'fixture' / (NATIVE[1] + '.jsonl')
            with source_path.open('a') as stream:
                stream.write(json.dumps({'type': 'assistant', 'sessionId': NATIVE[1],
                    'cwd': str(project), 'message': {'content': 'x' * 600000}}) + '\n')
            self.assertGreater(source_path.stat().st_size, 524288)
            notices = root / 'codex-notices.jsonl'
            cli = root / 'mock-native-queue'
            cli.write_text('#!' + sys.executable + '\n' +
                           'import json,sys\n'
                           'args=sys.argv[1:]\n'
                           'assert args[:1]==["queue"]\n'
                           'thread=args[args.index("--thread")+1]\n'
                           'message=args[args.index("--message")+1]\n'
                           'assert thread==' + repr(CODEX_SOURCE) + '\n'
                           'assert message.startswith("WATCH_PEER_REPLY ")\n'
                           'with open(' + repr(str(notices)) + ',"a") as f:\n'
                           ' f.write(json.dumps({"thread":thread,"message":message})+"\\n")\n')
            cli.chmod(0o700)
            store = officer_messaging.MessagingStore(str(root / 'state'), str(sessions), cli=str(cli))
            store.available = store.codex_available = True
            httpd = server.Server(('127.0.0.1', 0), server.H)
            stop = threading.Event()
            errors = []

            def tick():
                try:
                    while not stop.wait(.02):
                        store.tick()
                except BaseException as error:
                    errors.append(type(error).__name__)

            def live_snapshot():
                # Only native admission is faked. Claude capability comes from
                # the production registration manager and its live leases.
                live = {'x:' + CODEX_SOURCE: {'capability': 'reachable', 'reason': ''}}
                with store.lock, store.db:
                    store.mods.maintain()
                    for row in store.db.execute('SELECT id FROM claude_mod_enrollments WHERE active=1'):
                        entry = store.mods.get(row[0])
                        live[entry['agent']] = {
                            'capability': 'reachable' if store.mods.peer_ready(entry) else 'unavailable',
                            'reason': '', 'supported_transport': 'claude_mod',
                            'mod_enrollment': entry['id'], 'project': entry['project']}
                return live

            def owner_post(route, data):
                conn = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=5)
                try:
                    conn.request('POST', route, json.dumps(data), {
                        'X-Live-Room-Key': server.KEY, 'Content-Type': 'application/json'})
                    response = conn.getresponse()
                    body = response.read()
                    self.assertEqual(response.status, 200, body)
                    return json.loads(body)
                finally:
                    conn.close()

            worker = threading.Thread(target=tick, daemon=True)
            http_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            with patch.multiple(server, MESSAGING=store, KEY='fictional-owner-key',
                                PORT=httpd.server_port, AGENTS={}):
                try:
                    store.shared.directory = chat_directory.ChatDirectory(
                        str(codex), str(claude), policy={'providers': ['codex', 'claude'],
                        'history_days': 'all'}, live_snapshot=live_snapshot, cache_seconds=0)
                    for _ in range(40):
                        if store.shared.directory.refresh(force=True)['complete']:
                            break
                    self.assertTrue(store.shared.directory.status()['complete'])
                    with store.lock, store.db:
                        credential = peer_bridge.initialize_service(store.db, {
                            'providers': ['codex', 'claude'], 'history_days': 'all'})
                    credential['port'] = httpd.server_port
                    private = root / 'private-credentials'
                    private.mkdir(mode=0o700)

                    def private_file(name, data):
                        path = private / name
                        path.write_text(json.dumps(data))
                        path.chmod(0o600)
                        return str(path)

                    shared_file = private_file('shared.json', credential)
                    http_thread.start()
                    setup = owner_post('/claude-mod-setup', {'peer_receive': True})
                    hook = owner_post('/peer-reply-setup', {'enabled': True,
                        'providers': ['codex', 'claude'], 'source_scope': 'native_parent',
                        'consent_version': 1})
                    hook_file = private_file('reply-hook.json', hook)
                    worker.start()
                    prepare = store.reply_delivery.prepare
                    cold_source = []

                    def prepare_with_cold_selected_cache(agent, *args, **kwargs):
                        if agent == 'c:' + NATIVE[1] and not cold_source:
                            directory = store.shared.directory
                            with directory._file_lock(str(source_path)):
                                broad = directory._files[str(source_path)]
                                self.assertTrue(broad['done'])
                                self.assertTrue(broad['native_id_verified'])
                                # A caller already in the room can register without
                                # warming this separate selected-path cache.
                                directory._claude_selected.pop(str(source_path), None)
                                cold_source.append(True)
                        return prepare(agent, *args, **kwargs)

                    with patch.object(store.reply_delivery, 'prepare', side_effect=prepare_with_cold_selected_cache):
                        process = subprocess.run(['node', str(ROOT / 'integrations/claude-mod/tests/reply.integration.mjs')],
                            input=json.dumps({'port': httpd.server_port, 'code': setup['pair_code'],
                                'project': str(project), 'native': NATIVE, 'codex_source': CODEX_SOURCE,
                                'credential': shared_file, 'hook_credential': hook_file,
                                'hook_script': str(ROOT / 'scripts/watch_reply_hook.py'),
                                'python': sys.executable, 'codex_notice_file': str(notices)}),
                            text=True, capture_output=True, timeout=60)
                    self.assertEqual(process.returncode, 0, process.stdout + '\n' + process.stderr)
                    self.assertEqual(cold_source, [True])
                    result = json.loads(process.stdout)
                    self.assertEqual(len(result['cases']), 2)
                    for case, provider in zip(result['cases'], ['codex', 'claude']):
                        self.assertEqual(case['source_provider'], provider)
                        self.assertEqual(case['reply'], 'Fictional ' + provider + ' answer.')
                        self.assertEqual(case['question_submissions'], 1)
                        self.assertEqual(case['notice_submissions'], 1)
                        self.assertEqual(case['get_reply_calls'], 1)
                        self.assertIs(case['source_turn_finished_before_answer'], True)
                        self.assertEqual(case['sibling_notice_submissions'], 0)
                    self.assertFalse(errors, errors)
                    self.assertEqual(server.AGENTS, {})
                    with store.lock:
                        deliveries = store.db.execute('SELECT request_id,notice_id,state,data FROM peer_reply_deliveries').fetchall()
                        self.assertEqual({row[0] for row in deliveries},
                                         {case['public_request_id'] for case in result['cases']})
                        self.assertEqual(len({row[1] for row in deliveries}), 2)
                        sources = set()
                        attempts = set()
                        for public_id, notice_id, state, raw in deliveries:
                            delivery = json.loads(raw)
                            self.assertNotEqual(public_id, notice_id)
                            self.assertEqual(state, 'queued')
                            self.assertTrue(delivery['attempt_id'])
                            self.assertTrue(delivery['attempted_at'])
                            attempts.add(delivery['attempt_id'])
                            sources.add(delivery['source']['agent_id'])
                        self.assertEqual(len(attempts), 2)
                        self.assertEqual(sources, {'x:' + CODEX_SOURCE, 'c:' + NATIVE[1]})
                        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM shared_bridge_requests').fetchone()[0], 2)
                    self.assertEqual(store.snapshot()['requests'], [])
                finally:
                    stop.set()
                    if worker.ident is not None:
                        worker.join(10)
                    if http_thread.ident is not None:
                        httpd.shutdown()
                    httpd.server_close()
                    if http_thread.ident is not None:
                        http_thread.join(10)
                    store.close()
            self.assertFalse(worker.is_alive())
            self.assertFalse(http_thread.is_alive())


if __name__ == '__main__':
    unittest.main()
