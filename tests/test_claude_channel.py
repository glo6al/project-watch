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

    def submit(self): return self.store.submit(self.agent, 'How is the work going?', self.rid, self.agents)
    def row(self): return self.store.snapshot()['requests'][0]

    def test_discovery_and_capabilities_without_codex(self):
        self.assertEqual(channel.discover(self.root, self.sid), self.descriptor)
        self.assertTrue(self.store.snapshot()['available'])
        self.assertEqual(self.store.snapshot()['capabilities'], {'codex': False, 'claude': True})
        self.assertEqual(self.submit()['state'], 'sending')
        raw = json.dumps(self.store._rows())
        self.assertNotIn('a' * 64, raw)
        self.assertIn('reply tool', raw)

    def test_http_auth_queue_and_correlated_reply(self):
        self.submit(); self.store.tick()
        self.assertEqual(self.row()['state'], 'queued')
        self.assertTrue(all(c[2] == 'Bearer ' + self.descriptor['token'] for c in self.calls))
        self.records[self.rid].update(state='answered', reply='One worker is testing.')
        self.store.tick()
        self.assertEqual(self.row()['reply'], 'One worker is testing.')
        self.assertEqual(self.row()['state'], 'answered')

    def test_full_length_unicode_message_uses_utf8(self):
        question = '😀' * 2000
        self.store.submit(self.agent, question, self.rid, self.agents)
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
        with self.assertRaises(messaging.MessagingError) as error: self.submit()
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
