"""Shared service invariants using disposable state and fake native dispatch."""
import copy
import http.client
import json
import os
import shutil
import subprocess
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import officer_messaging as m
import peer_bridge as p
import server
import watch_peer


def uid():
    return str(uuid.uuid4())


class FixtureDirectory:
    def __init__(self):
        self.targets = {}

    def resolve(self, ident, revision):
        target = self.targets[ident]
        if target['identity_revision'] != revision or target['capability'] != 'reachable':
            raise p.PeerError(409, 'Stale or unavailable target')
        return copy.deepcopy(target)

    def list_agents(self, **kwargs):
        return {'agents': list(self.targets.values()), 'next_cursor': None,
                'directory': {'complete': True, 'reasons': [], 'refreshed_at': time.time()}}


@unittest.skipIf(os.name == 'nt', 'POSIX private state')
class SharedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.temp.name)
        self.sessions = os.path.join(self.root, 'sessions')
        os.mkdir(self.sessions)
        self.state = os.path.join(self.root, 'state')
        self.store = m.MessagingStore(self.state, self.sessions)
        self.store.available = True
        self.directory = FixtureDirectory()
        self.store.shared.directory = self.directory
        self.bridge = self.store.shared
        self.credential = p.initialize_service(self.store.db, {'providers': ['codex', 'claude'], 'history_days': 'all'})
        self.auth = p.service_credential(self.store.db, self.credential['credential_id'])
        self.a = self.target('A')
        self.b = self.target('B', project=os.path.join(self.root, 'other-project'))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def target(self, name, project=None):
        native = uid()
        path = os.path.join(self.sessions, 'rollout-' + native + '.jsonl')
        project = project or self.root
        os.makedirs(project, exist_ok=True)
        self.append(path, 'session_meta', {'id': native, 'cwd': project, 'source': 'vscode'})
        info = os.stat(path)
        target = dict(agent_id='x:' + native, native_id=native, provider='codex', name=name, project=project,
                      project_id=p.digest(project.encode()), project_label=os.path.basename(project),
                      identity_revision=p.digest((native+name).encode()), capability='reachable', reason='',
                      identity={'path': path, 'inode': info.st_ino, 'device': info.st_dev})
        self.directory.targets[target['agent_id']] = target
        return target

    def append(self, path, typ, payload):
        with open(path, 'ab') as f:
            f.write(json.dumps({'type': typ, 'payload': payload}).encode() + b'\n')

    def send(self, target=None, rid=None, text='Question?', sender=None, auth=None):
        target = target or self.b
        return self.bridge.send(auth or self.auth, target['agent_id'], target['identity_revision'], text, rid or uid(), sender)

    def row(self, rid):
        tid = self.store.db.execute('SELECT transport_id FROM shared_bridge_requests WHERE client_id=?', (rid,)).fetchone()[0]
        return next(row for row in self.store._rows() if row['request_id'] == tid)

    def tick(self):
        proc = Mock(); proc.wait.return_value = 0
        with patch.object(m.subprocess, 'Popen', return_value=proc) as launch:
            self.store.tick()
            return launch

    def answer(self, row, text='Answer.'):
        self.append(row['path'], 'event_msg', {'type': 'task_started', 'turn_id': uid()})
        turn = json.loads(Path(row['path']).read_text().splitlines()[-1])['payload']['turn_id']
        self.append(row['path'], 'response_item', {'type': 'message', 'role': 'user', 'content': [{'text': row['envelope']}]})
        self.append(row['path'], 'event_msg', {'type': 'task_complete', 'turn_id': turn,
                                           'last_agent_message': 'WATCH_REPLY ' + row['request_id'] + '\n' + text})

    def test_shared_clients_cross_project_reply_and_private_owner_view(self):
        result = self.send(sender='An unverified name')
        self.assertFalse(result['duplicate'])
        self.assertEqual(result['target']['project_label'], 'other-project')
        self.assertEqual(self.tick().call_count, 1)
        row = self.row(result['request_id'])
        self.assertIn('UNVERIFIED', row['envelope'])
        self.assertIn('Do not call send_message', row['envelope'])
        self.answer(row); self.tick()
        with p.transaction(self.store.db):
            other = p.issue_service_credential(self.store.db)
        reply = self.bridge.rpc(other['credential_id'], {'method': 'get_reply', 'params': {'request_id': result['request_id']}})
        self.assertEqual(reply['reply'], 'Answer.')
        self.assertEqual(self.store.snapshot()['requests'], [])
        self.assertEqual(self.bridge.list_requests()['requests'][0]['reply'], '')

    def test_retry_survives_rotation_rename_and_expiry(self):
        result = self.send(); rid = result['request_id']
        original = copy.deepcopy(self.b)
        with p.transaction(self.store.db):
            self.store.db.execute('UPDATE shared_bridge_credentials SET active=0')
            other = p.issue_service_credential(self.store.db)
        self.b.update(name='Renamed', identity_revision='a'*64)
        auth = p.service_credential(self.store.db, other['credential_id'])
        duplicate = self.send(original, rid=rid, auth=auth)
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(duplicate['target'], result['target'])
        self.assertEqual(duplicate['created_at'], result['created_at'])
        with self.assertRaises(p.PeerError):
            self.send(self.b, rid=rid, auth=auth)
        row = self.row(rid); row['created'] -= p.RETENTION+1; self.store._save(row)
        self.store.db.execute('UPDATE shared_bridge_requests SET created=?', (row['created'],)); self.store.db.commit()
        self.tick()
        duplicate = self.send(original, rid=rid, auth=auth)
        self.assertEqual((duplicate['state'], duplicate['text_preview']), ('expired', ''))
        self.assertEqual(self.store.db.execute('SELECT preview FROM shared_bridge_requests').fetchone()[0], '')
        self.assertEqual(self.store._rows(), [])

    def test_request_id_examples_and_conflicts(self):
        for bad in ('00000000-0000-4000-8000-000000000000', '11111111-1111-4111-8111-111111111111',
                    '123e4567-e89b-12d3-a456-426614174000', '123e4567-e89b-42d3-a456-426614174000', str(uuid.uuid1())):
            with self.assertRaises(p.PeerError):
                self.send(rid=bad)
        result = self.send(sender='Alice'); rid = result['request_id']
        for args in ({'text': 'changed', 'sender': 'Alice'}, {'sender': 'Bob'}, {'target': self.a, 'sender': 'Alice'}):
            with self.assertRaises(p.PeerError):
                self.send(rid=rid, **args)
        with self.assertRaises(p.PeerError):
            self.bridge.rpc(self.auth['id'], {'method': 'send_message', 'params': dict(agent_id=self.a['agent_id'],
                identity_revision=self.a['identity_revision'], text='Hello', request_id=uid(), human_requested=True)})

    def test_concurrent_same_id_creates_one_transport(self):
        rid = uid(); results = []; errors = []
        def run():
            try:
                results.append(self.send(rid=rid))
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=run) for _ in range(8)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(sum(not r['duplicate'] for r in results), 1)
        self.assertEqual(self.tick().call_count, 1)

    def test_restart_unlaunched_fails_and_launched_never_resends(self):
        first = self.send(self.a); second = self.send(self.b)
        self.bridge.authorize(self.row(second['request_id']))
        self.store.close(); self.store = m.MessagingStore(self.state, self.sessions)
        self.store.available = True; self.bridge = self.store.shared; self.bridge.directory = self.directory
        self.assertEqual(self.bridge.result(first['request_id'])['state'], 'failed')
        self.assertEqual(self.bridge.result(second['request_id'])['state'], 'unknown')
        self.assertEqual(self.tick().call_count, 0)

    def test_corrupt_shared_record_does_not_break_owner(self):
        result = self.send(); row = self.row(result['request_id'])
        self.store.db.execute('UPDATE requests SET data=? WHERE id=?', ('{bad', row['request_id'])); self.store.db.commit()
        owner = self.store.submit(self.a['agent_id'], 'Owner question', uid(), {self.a['agent_id']: {'runtime': 'Codex'}})
        self.assertEqual(self.tick().call_count, 1)
        self.assertEqual(len(self.store.snapshot()['requests']), 1)
        self.assertEqual(self.bridge.result(result['request_id'])['state'], 'unknown')
        self.assertFalse(self.store.worker_failed)
        self.store.close(); self.store = m.MessagingStore(self.state, self.sessions)
        self.assertEqual(len(self.store.snapshot()['requests']), 1)

    def test_corrupt_transport_preserves_snapshot_and_other_history_records(self):
        damaged = self.send(); healthy = self.send(self.a)
        row = self.row(damaged['request_id'])
        for raw in ('{broken', json.dumps(dict(row, error=['invalid']))):
            with self.subTest(raw=raw[:20]):
                self.store.db.execute('UPDATE requests SET data=? WHERE id=?', (raw, row['request_id']))
                self.store.db.commit()
                result = self.bridge.result(damaged['request_id'])
                self.assertEqual(result['state'], 'unknown')
                self.assertEqual(result['target'], damaged['target'])
                page = self.bridge.list_requests()
                self.assertEqual({r['request_id'] for r in page['requests']}, {damaged['request_id'], healthy['request_id']})
                self.assertEqual(next(r for r in page['requests'] if r['request_id'] == healthy['request_id'])['target'], healthy['target'])
                node = shutil.which('node')
                adapter = Path(__file__).resolve().parents[1] / 'integrations/claude/peer.mjs'
                sdk = adapter.parent / 'node_modules/@modelcontextprotocol/sdk'
                if node and sdk.is_dir():
                    # Exercise the actual adapter validator; a malformed target
                    # used to reject the entire page and even a single get_reply.
                    script = "import {safeResult} from " + json.dumps(adapter.as_uri()) + ";" + \
                        "const d=JSON.parse(await new Promise(r=>{let s='';process.stdin.on('data',b=>s+=b);process.stdin.on('end',()=>r(s))}));" + \
                        "safeResult('get_reply',d.result);safeResult('list_requests',d.page);"
                    completed = subprocess.run([node, '--input-type=module', '-e', script],
                        input=json.dumps({'result': result, 'page': page}), text=True, capture_output=True, timeout=10)
                    self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_owner_warning_after_shared_delivery(self):
        result = self.send(self.a); self.tick(); self.answer(self.row(result['request_id'])); self.tick()
        owner = self.store.submit(self.a['agent_id'], 'Owner question', uid(), {self.a['agent_id']: {'runtime': 'Codex'}})
        self.tick()
        row = next(r for r in self.store._rows() if r['request_id'] == owner['request_id'])
        self.answer(row); self.tick()
        self.assertIn(p.PEER_WARNING, self.store.snapshot()['requests'][0]['delivery_warning'])

    def test_unverified_stale_or_replaced_target_refused(self):
        original = copy.deepcopy(self.b)
        self.b['capability'] = 'unverified'
        with self.assertRaises(p.PeerError): self.send()
        self.b['capability'] = 'reachable'; self.b['identity_revision'] = 'b'*64
        with self.assertRaises(p.PeerError): self.send(original)
        self.b['identity_revision'] = original['identity_revision']
        os.rename(self.b['identity']['path'], self.b['identity']['path']+'.old')
        self.append(self.b['identity']['path'], 'session_meta', {'id': self.b['native_id'], 'cwd': self.b['project']})
        with self.assertRaises(Exception): self.send()
        self.assertEqual(self.store._rows(), [])

    def test_identity_change_or_revocation_between_reservation_and_launch(self):
        result = self.send(); self.b['identity_revision'] = 'd'*64
        self.assertEqual(self.tick().call_count, 0)
        self.assertEqual(self.bridge.result(result['request_id'])['state'], 'failed')
        result = self.send(self.a)
        self.store.db.execute('UPDATE shared_bridge_credentials SET active=0'); self.store.db.commit()
        self.assertEqual(self.tick().call_count, 0)
        self.assertEqual(self.bridge.result(result['request_id'])['state'], 'failed')

    def test_global_chain_cap_no_declared_sender_bypass_and_self_target(self):
        # No caller identity is asserted. Explicitly targeting A is allowed and bounded.
        for target in (self.a, self.b, self.target('C'), self.target('D')):
            self.send(target, sender='any declaration '+uid())
        with self.assertRaises(p.PeerError) as error:
            self.send(self.target('E'), sender='Owner')
        self.assertEqual(error.exception.status, 429)
        with self.assertRaises(p.PeerError): self.send(self.a, sender='different')

    def test_completed_malformed_turn_releases_target_capacity(self):
        result = self.send(); self.tick(); row = self.row(result['request_id'])
        self.answer(row, ''); self.tick()
        self.assertTrue(self.row(result['request_id']).get('settled'))
        self.send()

    def test_late_reply_is_polled_without_resend(self):
        result = self.send(); self.tick(); row = self.row(result['request_id'])
        row['created'] -= p.TTL+10; self.store._save(row)
        self.store.db.execute('UPDATE shared_bridge_requests SET created=?', (row['created'],)); self.store.db.commit()
        self.assertEqual(self.tick().call_count, 0)
        self.assertEqual(self.bridge.result(result['request_id'])['state'], 'expired')
        with self.assertRaises(p.PeerError): self.send()
        self.answer(row); self.tick()
        self.assertEqual(self.bridge.result(result['request_id'])['state'], 'answered')
        self.send()

    def test_legacy_recovery_metadata_keeps_history_separate(self):
        with p.transaction(self.store.db):
            self.store.db.execute('DELETE FROM shared_bridge_service')
        first = p.enroll(self.store.db, 'Legacy A', 'codex', self.a['native_id'], self.a['project'], self.a['identity'])
        second = p.enroll(self.store.db, 'Legacy B', 'codex', self.b['native_id'], self.b['project'], self.b['identity'])
        # Legacy endpoints require same project; fixture target A receives from a separate same-project log.
        target = self.target('Legacy target')
        third = p.enroll(self.store.db, 'Legacy C', 'codex', target['native_id'], target['project'], target['identity'])
        sender = p.principal(self.store.db, first['endpoint_id'])
        legacy_id = uid(); self.store.peers.send(sender, third['endpoint_id'], 'Legacy payload', legacy_id)
        p.initialize_service(self.store.db, {'providers': ['codex']})
        self.assertEqual(self.tick().call_count, 0)
        self.assertEqual(self.store.peers.result(sender['id'], legacy_id)['state'], 'failed')
        self.assertEqual(self.bridge.list_requests()['requests'], [])
        with self.assertRaises(p.PeerError): self.bridge.result(legacy_id)

    def test_preflight_outside_store_lock_and_transaction(self):
        original = self.directory.resolve
        def resolve(*args):
            self.assertFalse(self.store.db.in_transaction)
            self.assertFalse(self.store.lock._is_owned())
            return original(*args)
        with patch.object(self.directory, 'resolve', side_effect=resolve):
            self.send(); self.tick()

    def test_per_target_hourly_and_global_daily_limits(self):
        def settle(result):
            row = self.row(result['request_id']); row.update(state='failed', settled=True)
            self.store._save(row)
        for _ in range(10): settle(self.send())
        with self.assertRaises(p.PeerError): self.send(sender='rotating claimed identity')
        for n in range(9):
            target = self.target('Target ' + str(n))
            for _ in range(10): settle(self.send(target))
        with self.assertRaises(p.PeerError) as error: self.send(self.a)
        self.assertEqual(error.exception.status, 429)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM shared_bridge_requests').fetchone()[0], 100)
        first = self.bridge.list_requests(); self.assertEqual(len(first['requests']), 50)
        second = self.bridge.list_requests(first['next_cursor']); self.assertEqual(len(second['requests']), 50)
        self.assertIsNone(second['next_cursor'])
        self.assertFalse(set(r['request_id'] for r in first['requests']) & set(r['request_id'] for r in second['requests']))

    def test_corrupt_origin_or_version_never_becomes_owner_history(self):
        result = self.send(); row = self.row(result['request_id'])
        for key in ('origin', 'bridge_version'):
            broken = dict(row); broken.pop(key); self.store._save(broken)
            self.assertEqual(self.store.snapshot()['requests'], [])
            self.assertEqual(self.bridge.result(result['request_id'])['state'], 'unknown')
            self.assertEqual(self.tick().call_count, 0)

    def test_launched_legacy_poll_survives_shared_activation(self):
        with p.transaction(self.store.db): self.store.db.execute('DELETE FROM shared_bridge_service')
        target = self.target('Legacy target')
        sender = p.enroll(self.store.db, 'Legacy sender', 'codex', self.a['native_id'], self.a['project'], self.a['identity'])
        recipient = p.enroll(self.store.db, 'Legacy recipient', 'codex', target['native_id'], target['project'], target['identity'])
        rid = uid()
        self.store.peers.send(p.principal(self.store.db, sender['endpoint_id']), recipient['endpoint_id'], 'Legacy payload', rid)
        self.assertEqual(self.tick().call_count, 1)
        row = next(r for r in self.store._rows() if r.get('origin') == 'peer')
        p.initialize_service(self.store.db, {'providers': ['codex']})
        self.store.close(); self.store = m.MessagingStore(self.state, self.sessions); self.store.available = True
        self.bridge = self.store.shared; self.bridge.directory = self.directory
        self.answer(row)
        self.assertEqual(self.tick().call_count, 0)
        self.assertEqual(self.store.peers.result(sender['endpoint_id'], rid)['state'], 'answered')
        self.assertEqual(self.bridge.list_requests()['requests'], [])

    def test_closed_desktop_is_unavailable_even_with_active_room_metadata(self):
        with patch.object(server, 'AGENTS', {self.a['agent_id']: {'runtime': 'Codex'}}), \
                patch.object(server.sys, 'platform', 'darwin'), \
                patch.object(server.subprocess, 'run', return_value=Mock(returncode=1)):
            snapshot = server.shared_directory_snapshot(self.store)
        self.assertEqual(snapshot[self.a['agent_id']]['capability'], 'unavailable')
        self.assertEqual(snapshot['__codex_transport__']['capability'], 'unavailable')

    def test_reply_wire_limit_preserves_complete_answers_only(self):
        result = self.send(); row = self.row(result['request_id'])
        row.update(state='answered', reply='😀' * 16000, settled=True); self.store._save(row)
        reply = self.bridge.result(result['request_id'])
        self.assertEqual((reply['state'], reply['reply']), ('unknown', ''))
        self.assertLess(len(json.dumps(reply).encode()), 65536)

    def test_http_socket_bound_proof_domains_and_legacy_body_refusal(self):
        previous = server.MESSAGING; server.MESSAGING = self.store
        httpd = server.Server(('127.0.0.1', 0), server.H)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True); thread.start()
        port = httpd.server_address[1]
        try:
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
            nonce = 'f'*64
            conn.request('POST', '/bridge/v2/challenge', json.dumps({'credential_id': self.auth['id'], 'nonce': nonce}))
            response = conn.getresponse(); proof = json.loads(response.read()); self.assertEqual(response.status, 200)
            self.assertEqual(proof['proof'], p.mac(self.auth['secret'], 'watch-bridge-server-v2', port, self.auth['id'], nonce, proof['run_id']))
            raw = json.dumps({'method': 'list_requests', 'params': {}}).encode(); stamp = str(int(time.time())); nonce = 'e'*64
            headers = {'X-Watch-Bridge': self.auth['id'], 'X-Watch-Time': stamp, 'X-Watch-Nonce': nonce, 'X-Watch-Run': proof['run_id']}
            headers['X-Watch-Signature'] = p.mac(self.auth['secret'], 'watch-bridge-request-v2', port, 'POST', '/bridge/v2/rpc', stamp, nonce, proof['run_id'], p.digest(raw))
            conn.request('POST', '/bridge/v2/rpc', raw, headers); response = conn.getresponse(); body = response.read()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader('X-Watch-Signature'), p.mac(self.auth['secret'], 'watch-bridge-response-v2', port, proof['run_id'], nonce, response.status, p.digest(body)))
            conn.request('POST', '/bridge/v2/rpc', raw, headers); response = conn.getresponse(); response.read()
            self.assertEqual(response.status, 403); conn.close()
            # Deliberately omit advertised body: refusal must happen before reading it.
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
            conn.putrequest('POST', '/peer/rpc'); conn.putheader('Content-Length', '1000'); conn.endheaders()
            response = conn.getresponse(); response.read(); self.assertEqual(response.status, 410); conn.close()
        finally:
            httpd.shutdown(); httpd.server_close(); thread.join(); server.MESSAGING = previous

    def test_enabled_service_starts_directory_before_first_rpc(self):
        refreshed = threading.Event()
        directory = Mock(work_pending=False)
        directory.refresh.side_effect = refreshed.set
        self.bridge.directory = None
        self.bridge.directory_factory = Mock(return_value=directory)
        with patch.object(self.store, 'probe', return_value=(True, '')), \
                patch.object(self.store, '_run', side_effect=lambda: self.store.stop.wait()):
            self.store.start()
            self.bridge.directory_factory.assert_called_once()
            self.assertTrue(refreshed.wait(1))
            self.assertTrue(self.bridge.directory_thread.is_alive())

    def test_init_after_start_keeps_lazy_directory_fallback(self):
        self.store.close()
        self.store = m.MessagingStore(os.path.join(self.root, 'fresh-state'), self.sessions)
        self.bridge = self.store.shared
        directory = Mock(work_pending=False)
        self.bridge.directory_factory = Mock(return_value=directory)
        with patch.object(self.store, 'probe', return_value=(True, '')), \
                patch.object(self.store, '_run', side_effect=lambda: self.store.stop.wait()):
            self.store.start()
            self.bridge.directory_factory.assert_not_called()
            with self.store.lock:
                credential = p.initialize_service(self.store.db, {'providers':['codex']})
            self.bridge.rpc(credential['credential_id'], {'method':'list_requests', 'params':{}})
            self.bridge.directory_factory.assert_called_once()

    def test_offline_policy_change_preserves_dedupe_and_restart_recovery(self):
        unlaunched = self.send(self.a)
        launched = self.send(self.b)
        self.bridge.authorize(self.row(launched['request_id']))
        original_key = self.store.db.execute('SELECT ledger_key FROM shared_bridge_service').fetchone()[0]
        self.store.close()
        policy = p.update_service_policy(self.state, {'project_deny':[self.root]})
        self.store = m.MessagingStore(self.state, self.sessions)
        self.bridge = self.store.shared
        self.assertEqual(self.bridge.policy(), policy)
        self.assertEqual(self.store.db.execute('SELECT ledger_key FROM shared_bridge_service').fetchone()[0], original_key)
        self.assertEqual(p.service_credential(self.store.db, self.auth['id']), self.auth)
        self.assertEqual(self.bridge.result(unlaunched['request_id'])['state'], 'failed')
        self.assertEqual(self.bridge.result(launched['request_id'])['state'], 'unknown')
        duplicate = self.send(self.a, rid=unlaunched['request_id'])
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(duplicate['target'], unlaunched['target'])
        directory = Mock(work_pending=False)
        self.bridge.directory_factory = Mock(return_value=directory)
        with patch.object(self.store, 'probe', return_value=(True, '')), \
                patch.object(self.store, '_run', side_effect=lambda: self.store.stop.wait()):
            self.store.start()
            self.bridge.directory_factory.assert_called_once_with(policy)

    def test_invalid_challenges_do_not_consume_valid_client_budget(self):
        for _ in range(130):
            for body in ({}, {'credential_id':uid(), 'nonce':'b'*64}):
                with self.assertRaises(p.PeerError) as error:
                    self.bridge.challenge(body)
                self.assertEqual(error.exception.status, 403)
        self.assertEqual(self.bridge.challenge_times, [])
        proof = self.bridge.challenge({'credential_id':self.auth['id'], 'nonce':'b'*64})
        self.assertIn('proof', proof)

    def test_challenge_limits_are_per_credential_and_globally_bounded(self):
        with p.transaction(self.store.db): other = p.issue_service_credential(self.store.db)
        for _ in range(120): self.bridge.challenge({'credential_id':self.auth['id'], 'nonce':'b'*64})
        with self.assertRaises(p.PeerError) as error:
            self.bridge.challenge({'credential_id':self.auth['id'], 'nonce':'b'*64})
        self.assertEqual(error.exception.status, 429)
        self.assertIn('proof', self.bridge.challenge({'credential_id':other['credential_id'], 'nonce':'b'*64}))
        self.bridge.challenge_times.clear()
        with patch.object(p, 'MAX_CHALLENGE_ENTRIES', 3):
            for _ in range(3): self.bridge.challenge({'credential_id':other['credential_id'], 'nonce':'b'*64})
            with self.assertRaises(p.PeerError): self.bridge.challenge({'credential_id':self.auth['id'], 'nonce':'b'*64})
            self.assertEqual(len(self.bridge.challenge_times), 3)

    def test_auth_wrong_port_replay_and_owner_key_rejected(self):
        raw = b'{"method":"list_requests","params":{}}'
        headers = {'X-Watch-Bridge': self.auth['id'], 'X-Watch-Time': str(int(time.time())), 'X-Watch-Nonce': 'b'*64,
                   'X-Watch-Run': self.bridge.run_id}
        def sign(port):
            headers['X-Watch-Signature'] = p.mac(self.auth['secret'], 'watch-bridge-request-v2', port, 'POST', '/bridge/v2/rpc',
                    headers['X-Watch-Time'], headers['X-Watch-Nonce'], self.bridge.run_id, p.digest(raw))
        sign(1)
        with self.assertRaises(p.PeerError): self.bridge.authenticate(headers, raw, self.auth['id'], 2)
        sign(2); self.bridge.authenticate(headers, raw, self.auth['id'], 2)
        with self.assertRaises(p.PeerError): self.bridge.authenticate(headers, raw, self.auth['id'], 2)
        with self.assertRaises(p.PeerError): self.bridge.authenticate({'X-Live-Room-Key': 'owner'}, raw, self.auth['id'], 2)


if __name__ == '__main__':
    unittest.main()
