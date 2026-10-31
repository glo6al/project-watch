#!/usr/bin/env python3
"""Private temporary discovery and fake local HTTP; never runs a model."""
import http.server
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import claude_channel as channel
import officer_messaging as messaging
import peer_bridge
import server
from legacy_claude_fixture import seed_legacy_claude


@unittest.skipIf(os.name == 'nt', 'POSIX-private channel discovery')
class ClaudeChannelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = os.path.realpath(self.temp.name)
        self.root = os.path.join(self.base, 'claude-channels'); os.mkdir(self.root, 0o700)
        self.sid, self.instance, self.rid = (str(uuid.uuid4()) for _ in range(3))
        self.folder = os.path.join(self.root, self.sid); os.mkdir(self.folder, 0o700)
        self.path = os.path.join(self.folder, 'connection.json')
        self.calls, self.records, self.bad, self.status = [], {}, None, 200
        self.sent_content = ''
        self.sent_size = 0
        self.body_delay = 0
        owner = self
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self): self.handle_request(True)
            def do_GET(self): self.handle_request(False)
            def handle_request(self, post):
                owner.calls.append((self.command, self.path, self.headers.get('Authorization')))
                if post:
                    raw_body = self.rfile.read(int(self.headers['Content-Length']))
                    data = json.loads(raw_body)
                    owner.sent_content, owner.sent_size = data['content'], len(raw_body)
                    rid = data['request_id']
                    owner.records.setdefault(rid, dict(session_id=data['session_id'], instance_id=data['instance_id'], request_id=rid,
                                                     state='transport_written', reply='', error=''))
                else: rid = self.path.rsplit('/', 1)[1]
                record = owner.records.get(rid, {})
                raw = owner.bad if isinstance(owner.bad, bytes) else json.dumps(owner.bad if owner.bad is not None else record).encode()
                self.send_response(owner.status); self.send_header('Content-Length', str(len(raw))); self.end_headers()
                if owner.body_delay: time.sleep(owner.body_delay)
                try: self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError): pass
        self.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True); self.thread.start()
        self.descriptor = dict(version=1, session_id=self.sid, instance_id=self.instance, port=self.httpd.server_port, token='a' * 64)
        self.write_manifest()
        self.store = messaging.MessagingStore(os.path.join(self.base, 'state'), os.path.join(self.base, 'sessions'), channel_root=self.root)
        self.store.codex_available = False
        self.store.reason = 'Codex unavailable'
        self.agent = 'c:' + self.sid
        self.agents = {self.agent: {'runtime': 'Claude', 'name': 'Rowan'}}

    def tearDown(self):
        self.store.close(); self.httpd.shutdown(); self.httpd.server_close(); self.thread.join(); self.temp.cleanup()

    def write_manifest(self):
        with open(self.path, 'w') as f: json.dump(self.descriptor, f)
        os.chmod(self.path, 0o600)

    def submit(self): return seed_legacy_claude(self.store, self.agent, 'How is the work going?', self.rid, self.agents)
    def row(self): return self.store.snapshot()['requests'][0]

    def test_discovery_and_capabilities_without_codex(self):
        self.assertEqual(channel.discover(self.root, self.sid), self.descriptor)
        self.assertTrue(self.store.snapshot()['available'])
        self.assertEqual(self.store.snapshot()['capabilities'], {'codex': False, 'claude': True})
        self.assertEqual(self.submit()['state'], 'sending')
        raw = json.dumps(self.store._rows())
        self.assertNotIn('a' * 64, raw)
        self.assertIn('reply tool', raw)

    def test_v2_project_protocol_identity_and_peer_preflight(self):
        self.descriptor.update(version=2, peer_protocol=1, project=self.base)
        self.write_manifest()
        found = channel.discover(self.root, self.sid)
        self.assertEqual(found, self.descriptor)
        pin = channel.identity(found)
        self.assertEqual(pin['version'], 2)
        self.assertEqual(pin['peer_protocol'], 1)
        self.assertEqual(pin['project'], self.base)
        self.descriptor['project'] = self.base + '/..'
        self.write_manifest()
        with self.assertRaises(channel.ChannelError): channel.discover(self.root, self.sid)
        self.descriptor.update(project=self.base, peer_protocol=2)
        self.write_manifest()
        with self.assertRaises(channel.ChannelError): channel.discover(self.root, self.sid)
        self.descriptor.update(peer_protocol=1, version=1)
        self.write_manifest()
        with self.assertRaises(channel.ChannelError): channel.exchange(self.descriptor, self.rid, 'peer body', origin='peer')
        self.assertEqual(self.calls, [], 'v1 peer refusal precedes HTTP side effects')
        legacy_pin = channel.identity(channel.discover(self.root, self.sid))
        legacy_pin.pop('version')
        self.assertEqual(channel.pinned(self.root, legacy_pin)['version'], 1)
        self.descriptor.update(version=2, peer_protocol=1, project=self.base)
        self.write_manifest()
        with self.assertRaises(channel.ChannelError): channel.pinned(self.root, legacy_pin)

    def test_legacy_owner_default_and_v2_peer_origin_validation(self):
        result = channel.exchange(self.descriptor, self.rid, 'legacy owner')
        self.assertEqual(result['state'], 'transport_written')
        self.descriptor.update(version=2, peer_protocol=1, project=self.base)
        self.records[self.rid]['origin'] = 'peer'
        self.records[self.rid]['peer_input_seen'] = False
        self.assertEqual(channel.exchange(self.descriptor, self.rid, origin='peer')['origin'], 'peer')
        self.records[self.rid]['origin'] = 'owner'
        with self.assertRaises(channel.ChannelError): channel.exchange(self.descriptor, self.rid, origin='peer')
        self.records[self.rid]['origin'] = 'peer'
        self.records[self.rid]['peer_input_seen'] = 'yes'
        with self.assertRaises(channel.ChannelError): channel.exchange(self.descriptor, self.rid, origin='peer')

    def test_peer_post_gate_precedes_connection_despite_valid_live_v2_descriptor(self):
        self.descriptor.update(version=2, peer_protocol=1, project=self.base, native_identity_verified=True)
        self.write_manifest()
        with patch.object(channel.http.client, 'HTTPConnection', wraps=channel.http.client.HTTPConnection) as connection:
            with self.assertRaisesRegex(channel.ChannelError, 'native conversation identity'):
                channel.exchange(self.descriptor, self.rid, 'peer payload', origin='peer')
            connection.assert_not_called()
        self.assertEqual(self.calls, [])
        self.records[self.rid] = dict(session_id=self.sid, instance_id=self.instance, request_id=self.rid,
            state='answered', reply='Previously delivered reply', error='', origin='peer')
        self.assertEqual(channel.exchange(self.descriptor, self.rid, origin='peer')['reply'], 'Previously delivered reply')
        owner_id = str(uuid.uuid4())
        self.records[owner_id] = dict(session_id=self.sid, instance_id=self.instance, request_id=owner_id,
            state='transport_written', reply='', error='', origin='owner')
        self.assertEqual(channel.exchange(self.descriptor, owner_id, 'owner payload')['origin'], 'owner')
        self.assertEqual([call[0] for call in self.calls], ['GET', 'POST'])

    def test_shared_and_legacy_claude_selection_is_unavailable_with_gate_reason(self):
        self.descriptor.update(version=2, peer_protocol=1, project=self.base)
        self.write_manifest()
        facts = server.shared_directory_snapshot(self.store)
        self.assertEqual(facts['__claude_transport__'], {'capability': 'unavailable', 'reason': channel.PEER_IDENTITY_REASON})
        sender = peer_bridge.enroll(self.store.db, 'Legacy sender', 'codex', str(uuid.uuid4()), self.base, {})
        recipient = peer_bridge.enroll(self.store.db, 'Claude', 'claude', self.sid, self.base,
            {'instance_id': self.instance}, binding='launch')
        sender = peer_bridge.principal(self.store.db, sender['endpoint_id'])
        recipient = peer_bridge.principal(self.store.db, recipient['endpoint_id'])
        listing = self.store.peers.list_peers(sender)
        self.assertEqual((listing[0]['reachable'], listing[0]['reason']), (False, channel.PEER_IDENTITY_REASON))
        with self.assertRaisesRegex(peer_bridge.PeerError, 'native conversation identity'):
            self.store.peers._preflight(recipient)
        with self.assertRaises(peer_bridge.PeerError) as legacy_rejected:
            self.store.peers.send(sender, recipient['id'], 'legacy peer payload', str(uuid.uuid4()))
        self.assertEqual(legacy_rejected.exception.status, 409)
        credential = peer_bridge.initialize_service(self.store.db, {'providers': ['claude']})
        auth = peer_bridge.service_credential(self.store.db, credential['credential_id'])
        with self.assertRaises(peer_bridge.PeerError) as rejected:
            self.store.shared.send(auth, self.agent, 'a'*64, 'peer payload', str(uuid.uuid4()))
        self.assertEqual(rejected.exception.status, 409)
        self.assertEqual(str(rejected.exception), channel.PEER_IDENTITY_REASON)
        self.assertEqual(self.calls, [])

    def test_reserved_peer_send_fails_without_network_and_prior_peer_reply_still_polls(self):
        self.descriptor.update(version=2, peer_protocol=1, project=self.base)
        self.write_manifest()
        self.submit()
        row = self.store._rows()[0]
        row.update(origin='peer', channel=channel.identity(self.descriptor))
        state, reason = self.store._send_claude(row)
        self.assertEqual(state, 'failed')
        self.assertIn('Nothing was sent', reason)
        self.assertEqual(self.calls, [])
        row.update(state='queued', launched=True)
        self.records[self.rid] = dict(session_id=self.sid, instance_id=self.instance, request_id=self.rid,
            state='answered', reply='Prior peer reply', error='', origin='peer')
        self.store._poll_claude(row)
        self.assertEqual((row['state'], row['reply']), ('answered', 'Prior peer reply'))
        self.assertEqual([call[0] for call in self.calls], ['GET'])

    def test_reserved_shared_claude_row_fails_truthfully_before_dispatch(self):
        self.descriptor.update(version=2, peer_protocol=1, project=self.base)
        self.write_manifest()
        self.submit()
        row = self.store._rows()[0]
        identity = {'instance_id': self.instance}
        row.update(origin='peer', bridge_version=2, channel=channel.identity(self.descriptor),
            identity_revision='a'*64, target_identity=identity, target_project=self.base)
        self.store._save(row)
        credential = peer_bridge.initialize_service(self.store.db, {'providers':['claude']})
        with self.store.db:
            self.store.db.execute('INSERT INTO shared_bridge_requests VALUES (?,?,?,?,?,?,?,NULL,?)',
                (str(uuid.uuid4()), self.agent, '{}', 'hash', self.rid, credential['credential_id'], row['created'], 'fixture'))
        target = dict(agent_id=self.agent, native_id=self.sid, identity=identity, project=self.base,
            provider='claude', capability='reachable', identity_revision='a'*64)
        with patch.object(self.store.shared, '_resolve', return_value=target):
            self.store.tick()
        persisted = self.store._rows()[0]
        self.assertEqual(persisted['state'], 'failed')
        self.assertIn('native conversation identity', persisted['error'])
        self.assertIn('Nothing was sent', persisted['error'])
        self.assertFalse(persisted['launched'])
        self.assertEqual(self.calls, [])

    def test_http_auth_queue_and_correlated_reply(self):
        self.submit(); self.store.tick()
        self.assertEqual(self.row()['state'], 'queued')
        self.assertTrue(all(c[2] == 'Bearer ' + self.descriptor['token'] for c in self.calls))
        self.records[self.rid].update(state='answered', reply='One worker is testing.')
        self.store.tick()
        self.assertEqual(self.row()['reply'], 'One worker is testing.')
        self.assertEqual(self.row()['state'], 'answered')

    def test_distinct_pending_messages_keep_separate_channel_replies(self):
        self.submit(); self.store.tick()
        other = str(uuid.uuid4())
        seed_legacy_claude(self.store, self.agent, 'Please focus on the tests next.', other, self.agents)
        self.store.tick()
        self.assertEqual(sum(c[0] == 'POST' for c in self.calls), 2)
        self.records[other].update(state='answered', reply='Tests next.')
        self.store.tick()
        rows = {r['request_id']: r for r in self.store.snapshot()['requests']}
        self.assertEqual(rows[other]['reply'], 'Tests next.')
        self.assertEqual(rows[self.rid]['state'], 'queued')
        self.records[self.rid].update(state='answered', reply='Progress report.')
        self.store.tick()
        rows = {r['request_id']: r for r in self.store.snapshot()['requests']}
        self.assertEqual(rows[self.rid]['reply'], 'Progress report.')
        self.assertEqual(rows[other]['reply'], 'Tests next.')
        self.assertEqual(sum(c[0] == 'POST' for c in self.calls), 2)

    def test_full_length_unicode_message_uses_utf8(self):
        question = '😀' * 2000
        seed_legacy_claude(self.store, self.agent, question, self.rid, self.agents)
        self.store.tick()
        self.assertEqual(self.row()['state'], 'queued')
        self.assertIn(question, self.sent_content)
        self.assertLess(self.sent_size, 16384)

    def test_retry_idempotent_and_never_reposts(self):
        self.submit(); self.store.tick()
        self.store.submit(self.agent, 'How is the work going?', self.rid, {})
        self.store.tick(); self.store.tick()
        self.assertEqual(sum(c[0] == 'POST' for c in self.calls), 1)
        with self.assertRaises(messaging.MessagingError): self.store.submit(self.agent, 'changed', self.rid, self.agents)

    def test_instance_replacement_before_send_fails_without_http(self):
        self.submit(); self.descriptor['instance_id'] = str(uuid.uuid4()); self.write_manifest()
        self.store.tick()
        self.assertEqual(self.row()['state'], 'failed')
        self.assertEqual(self.calls, [])

    def test_instance_replacement_after_send_never_redirects(self):
        self.submit(); self.store.tick(); before = len(self.calls)
        self.descriptor['instance_id'] = str(uuid.uuid4()); self.write_manifest()
        self.store.tick(); self.store.tick()
        self.assertEqual(self.row()['state'], 'unknown')
        self.assertEqual(len(self.calls), before)

    def test_rotated_token_same_instance_rejected(self):
        self.submit(); self.descriptor['token'] = 'b' * 64; self.write_manifest(); self.store.tick()
        self.assertEqual(self.row()['state'], 'failed')
        self.assertFalse(self.calls)

    def test_missing_connection_actionable_and_codex_independent(self):
        os.unlink(self.path)
        with self.assertRaises(channel.ChannelError) as error: self.submit()
        self.assertIn('scripts/watch_claude.py', str(error.exception))
        self.store.codex_available = True
        self.assertTrue(self.store.snapshot()['available'])
        self.assertFalse(self.store.snapshot()['capabilities']['claude'])

    def test_manifests_reject_permissions_symlinks_and_bad_fields(self):
        os.chmod(self.path, 0o644)
        with self.assertRaises(channel.ChannelError): channel.discover(self.root, self.sid)
        os.chmod(self.path, 0o600)
        other = self.path + '.real'; os.rename(self.path, other); os.symlink(other, self.path)
        with self.assertRaises(channel.ChannelError): channel.discover(self.root, self.sid)
        os.unlink(self.path); os.rename(other, self.path)
        for key, value in [('version', True), ('port', True), ('port', 0), ('token', 'bad'), ('session_id', str(uuid.uuid4())), ('instance_id', 'bad')]:
            original = self.descriptor[key]; self.descriptor[key] = value; self.write_manifest()
            with self.assertRaises(channel.ChannelError): channel.discover(self.root, self.sid)
            self.descriptor[key] = original
        self.write_manifest()
        with open(self.path, 'w') as f: f.write(' ' * 8193)
        with self.assertRaises(channel.ChannelError): channel.discover(self.root, self.sid)

    def test_wrong_record_identity_state_and_oversized_response(self):
        self.submit()
        for invalid in [dict(session_id=self.sid, instance_id=self.instance, request_id=str(uuid.uuid4()), state='answered', reply='bad'),
                        dict(session_id=self.sid, instance_id=str(uuid.uuid4()), request_id=self.rid, state='answered', reply='bad'),
                        dict(session_id=self.sid, instance_id=self.instance, request_id=self.rid, state='mystery'),
                        b'x' * (channel.MAX_RESPONSE + 1)]:
            self.bad = invalid
            with self.assertRaises(channel.ChannelError): channel.exchange(self.descriptor, self.rid, 'hello')

    def test_post_http_error_unknown_no_retry_and_late_reply(self):
        self.submit(); self.status = 503; self.store.tick()
        self.assertEqual(self.row()['state'], 'unknown')
        self.status = 200; self.records[self.rid].update(state='answered', reply='Late answer')
        self.store.tick()
        self.assertEqual(self.row()['state'], 'answered')
        self.assertEqual(sum(c[0] == 'POST' for c in self.calls), 1)

    def test_response_body_has_total_deadline(self):
        self.body_delay = 0.5
        started = time.monotonic()
        with self.assertRaises(channel.ChannelError):
            channel.exchange(self.descriptor, self.rid, 'hello', timeout=0.05)
        self.assertLess(time.monotonic() - started, 0.4)

    def test_timeout_unknown_and_restart_does_not_send(self):
        self.submit()
        with patch.object(channel.http.client.HTTPConnection, 'request', side_effect=TimeoutError): self.store.tick()
        self.assertEqual(self.row()['state'], 'unknown')
        self.store.close()
        self.store = messaging.MessagingStore(os.path.join(self.base, 'state'), os.path.join(self.base, 'sessions'), channel_root=self.root)
        self.store.tick()
        self.assertFalse(any(c[0] == 'POST' for c in self.calls))

    def test_expiry_late_answer_and_parent_only(self):
        self.submit(); self.store.tick()
        row = self.store._rows()[0]; row['created'] -= messaging.TTL + 1; self.store._save(row)
        self.store.tick(); self.assertEqual(self.row()['state'], 'expired')
        self.records[self.rid].update(state='answered', reply='Done'); self.store.tick()
        self.assertEqual(self.row()['state'], 'answered')
        for extra in ({'sub': True}, {'observed': True}, {'runtime': 'Claude Code'}):
            agents = {self.agent: dict(self.agents[self.agent], **extra)}
            with self.assertRaises(messaging.MessagingError): messaging.validate_target(self.agent, agents)


if __name__ == '__main__': unittest.main()
