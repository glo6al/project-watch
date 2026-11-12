#!/usr/bin/env python3
"""Native-open prototype checks using fictional metadata and ephemeral brokers only."""
import contextlib
import http.client
import importlib.util
import io
import json
import os
import re
from pathlib import Path
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import types
import unittest
import urllib.parse
import uuid


ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / 'integrations' / 'antigravity-open'
SPEC = importlib.util.spec_from_file_location('watch_antigravity_open_prototype', INTEGRATION / 'broker.py')
prototype = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prototype)


class PrototypeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(os.path.realpath(self.tmp.name))
        self.source = self.home / '.gemini' / 'antigravity'
        self.source.mkdir(parents=True)
        self.target, self.second = str(uuid.uuid4()), str(uuid.uuid4())
        self.dbpath = self.source / 'conversation_summaries.db'
        with contextlib.closing(sqlite3.connect(self.dbpath)) as conn, conn:
            conn.execute('CREATE TABLE conversation_summaries(conversation_id TEXT PRIMARY KEY)')
            conn.executemany('INSERT INTO conversation_summaries VALUES(?)', [(self.target,), (self.second,)])
        self.owner, self.ui = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.parent = 'https://127.0.0.1:49321'
        self.clock = 100.0
        self.broker = prototype.Broker(self.source, {self.target, self.second}, self.owner, self.ui,
                                      self.parent, clock=lambda: self.clock, wall=lambda: self.clock)
        self.server = prototype.make_server(self.broker, port=0)
        self.port = self.server.server_address[1]
        self.host = '127.0.0.1:' + str(self.port)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.page, self.lease = str(uuid.uuid4()), None

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())

    def call(self, method, path, body=None, *, role='owner', extra=None, raw=None):
        headers = {'Host': self.host}
        if role == 'owner':
            headers['Authorization'] = 'Bearer ' + self.owner
        elif role == 'ui':
            headers['X-Sidecar-Token'] = self.ui
            headers['Origin'] = self.broker.ui_origin
        if body is not None:
            raw = json.dumps(body).encode()
        if raw is not None:
            headers['Content-Type'] = 'application/json'
        if extra:
            headers.update(extra)
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=2)
        try:
            conn.request(method, path, body=raw, headers=headers)
            response = conn.getresponse()
            data = response.read()
            result = json.loads(data) if response.getheader('Content-Type', '').split(';')[0] == 'application/json' else data.decode('utf-8', 'replace')
            self.assertNotIn(self.owner, data.decode('utf-8', 'replace'), 'Owner credential must never be reflected.')
            self.assertNotIn(self.ui, data.decode('utf-8', 'replace'), 'UI credential must never be reflected.')
            if response.status < 300 and isinstance(result, dict) and 'request_id' in result:
                self.assertEqual(set(result), {'request_id', 'conversation_id', 'state', 'confirmed'})
                self.assertIs(result['confirmed'], False)
            return response.status, result, dict(response.getheaders())
        finally:
            conn.close()

    def hello(self):
        status, value, _ = self.call('POST', '/ui/hello', {'page_id': self.page, 'parent_origin': self.parent}, role='ui')
        self.assertEqual(status, 200)
        self.assertEqual(value['page_id'], self.page)
        self.assertIsInstance(value['lease'], str)
        self.assertTrue(value['lease'])
        self.lease = value['lease']
        return value

    def open(self, *, request=None, target=None):
        ident = request or str(uuid.uuid4())
        status, value, headers = self.call('POST', '/owner/open', {'request_id': ident, 'conversation_id': target or self.target})
        return ident, status, value, headers

    def poll(self, **extra):
        return self.call('POST', '/ui/poll', dict(page_id=self.page, lease=self.lease, **extra), role='ui')

    def receipt(self, ident, *, target=None, state='called', **extra):
        return self.call('POST', '/ui/receipt', dict(page_id=self.page, lease=self.lease,
                         request_id=ident, conversation_id=target or self.target, state=state, **extra), role='ui')

    def status(self, ident=None):
        return self.call('GET', '/owner/status' + ('?request_id=' + urllib.parse.quote(ident) if ident else ''))

    def rejected(self, response):
        status, value, _ = response
        self.assertGreaterEqual(status, 400)
        self.assertIsInstance(value, dict)
        self.assertEqual(set(value), {'error'})
        self.assertIsInstance(value['error'], str)

    def test_startup_is_loopback_only_and_not_connected_without_native_ui(self):
        self.assertEqual(self.server.server_address[0], '127.0.0.1')
        self.assertEqual(self.broker.ui_origin, 'plugin://watch-open-prototype-' + str(self.port))
        status, data, _ = self.status()
        self.assertEqual(status, 200)
        self.assertFalse(data['connected'])
        _, status, _, _ = self.open()
        self.assertGreaterEqual(status, 400, 'No loaded native sidecar means no open operation.')

    def test_owner_ui_roles_and_exact_ui_origin_are_not_interchangeable(self):
        self.rejected(self.call('GET', '/owner/status', role=None))
        self.rejected(self.call('GET', '/owner/status', role='ui'))
        self.rejected(self.call('GET', '/owner/status', extra={'Authorization': 'Bearer invalid'}))
        for origin in (self.broker.ui_origin, 'null', 'https://example.invalid', self.parent):
            self.rejected(self.call('GET', '/owner/status', extra={'Origin': origin}))
        payload = {'page_id': self.page, 'parent_origin': self.parent}
        self.rejected(self.call('POST', '/ui/hello', payload, role='owner'))
        for origin in ('null', self.parent, 'https://example.invalid', 'plugin://watch-open-prototype-' + str(self.port + 1)):
            self.rejected(self.call('POST', '/ui/hello', payload, role='ui', extra={'Origin': origin}))
        self.rejected(self.call('POST', '/ui/hello', payload, role=None, extra={'X-Sidecar-Token': 'invalid'}))

    def test_host_content_type_and_parent_origin_are_exact(self):
        payload = {'page_id': self.page, 'parent_origin': self.parent}
        for host in ('example.invalid', '127.0.0.1', 'native.localhost:' + str(self.port), '127.0.0.1:' + str(self.port + 1)):
            self.rejected(self.call('POST', '/ui/hello', payload, role='ui', extra={'Host': host}))
        for content_type in ('text/plain', 'application/x-www-form-urlencoded'):
            self.rejected(self.call('POST', '/ui/hello', payload, role='ui', extra={'Content-Type': content_type}))
        for parent in ('https://127.0.0.1:49322', 'https://localhost:49321', 'null', 'https://example.invalid'):
            self.rejected(self.call('POST', '/ui/hello', dict(payload, parent_origin=parent), role='ui'))
        self.hello()
        self.assertEqual(self.call('GET', '/owner/status', extra={'Host': 'localhost:' + str(self.port)})[0], 200)

    def test_only_exact_open_existing_envelope_and_uuid_targets_are_accepted(self):
        self.hello()
        base = {'request_id': str(uuid.uuid4()), 'conversation_id': self.target}
        for extra in ({'prompt': 'Do not send this.'}, {'command': 'newConversation'}, {'agent': 'another provider'}, {'url': 'https://example.invalid'}):
            self.rejected(self.call('POST', '/owner/open', dict(base, **extra)))
        for field in ('request_id', 'conversation_id'):
            for value in ('', 'x:' + self.target, '../../outside', 42, None, self.owner, self.target.upper()):
                self.rejected(self.call('POST', '/owner/open', dict(base, **{field: value})))
        self.rejected(self.call('POST', '/owner/open', {'request_id': str(uuid.uuid4())}))
        self.rejected(self.call('POST', '/owner/open', {'conversation_id': self.target}))
        self.rejected(self.call('POST', '/owner/open', [], role='owner'))

    def test_native_metadata_and_owner_allowlist_are_both_required_without_transcripts(self):
        self.hello()
        outside = str(uuid.uuid4())
        with contextlib.closing(sqlite3.connect(self.dbpath)) as conn, conn:
            conn.execute('INSERT INTO conversation_summaries VALUES(?)', (outside,))
        _, status, _, _ = self.open(target=outside)
        self.assertGreaterEqual(status, 400, 'Native history alone grants no owner allowance.')
        with contextlib.closing(sqlite3.connect(self.dbpath)) as conn, conn:
            conn.execute('DELETE FROM conversation_summaries WHERE conversation_id=?', (self.second,))
        _, status, _, _ = self.open(target=self.second)
        self.assertGreaterEqual(status, 400, 'An allowance alone proves no native target.')
        self.assertFalse((self.source / 'brain').exists(), 'No transcript or request body is needed for navigation proof.')
        _, status, value, _ = self.open()
        self.assertIn(status, (200, 202))
        self.assertEqual(value['conversation_id'], self.target)
        self.assertIs(value['confirmed'], False)

    def test_native_target_is_revalidated_when_claimed(self):
        self.hello()
        ident, status, _, _ = self.open()
        self.assertIn(status, (200, 202))
        with contextlib.closing(sqlite3.connect(self.dbpath)) as conn, conn:
            conn.execute('DELETE FROM conversation_summaries WHERE conversation_id=?', (self.target,))
        status, value, _ = self.poll()
        self.assertEqual(status, 200)
        self.assertIsNone(value['command'])
        status, row, _ = self.status(ident)
        self.assertEqual(status, 200)
        self.assertIs(row['confirmed'], False)

    def test_single_slot_idempotent_request_and_conflicting_reuse(self):
        self.hello()
        ident, status, first, _ = self.open()
        self.assertIn(status, (200, 202))
        _, status, duplicate, _ = self.open(request=ident)
        self.assertIn(status, (200, 202))
        self.assertEqual(duplicate['request_id'], first['request_id'])
        _, status, _, _ = self.open(request=ident, target=self.second)
        self.assertEqual(status, 409)
        _, status, _, _ = self.open()
        self.assertEqual(status, 409, 'A pending request occupies the only slot.')

    def test_poll_consumes_command_once_and_ack_does_not_claim_desktop_selection(self):
        self.hello()
        ident, status, _, _ = self.open()
        self.assertIn(status, (200, 202))
        status, data, _ = self.poll()
        self.assertEqual(status, 200)
        command = data['command']
        self.assertEqual(set(command), {'request_id', 'conversation_id', 'expires_at_ms', 'remaining_ms'})
        self.assertEqual(command['request_id'], ident)
        self.assertEqual(command['conversation_id'], self.target)
        self.assertGreater(command['remaining_ms'], 0)
        self.assertLessEqual(command['remaining_ms'], 5000)
        self.assertIsNone(self.poll()[1]['command'], 'Claimed commands must never be redelivered.')
        status, row, _ = self.receipt(ident)
        self.assertEqual(status, 200)
        self.assertEqual(row['state'], 'called')
        self.assertIs(row['confirmed'], False, 'An SDK acknowledgement is not proof of selected native chat.')
        status, row, _ = self.status(ident)
        self.assertEqual(status, 200)
        self.assertEqual(row['state'], 'called')
        self.assertIs(row['confirmed'], False)

    def test_receipt_must_echo_both_identifiers_and_allowed_state(self):
        self.hello()
        ident, _, _, _ = self.open(); self.poll()
        for kwargs in ({'target': self.second}, {'state': 'selected'}, {'state': 'success'}, {'state': 'called', 'prompt': 'No prompts.'}):
            self.rejected(self.receipt(ident, **kwargs))
        self.rejected(self.receipt(str(uuid.uuid4())))
        status, row, _ = self.receipt(ident, state='failed')
        self.assertEqual(status, 200)
        self.assertEqual(row['state'], 'failed')
        self.assertIs(row['confirmed'], False)

    def test_expired_pending_command_is_not_delivered_or_automatically_retried(self):
        self.hello()
        ident, _, _, _ = self.open()
        self.clock += 5.1
        status, data, _ = self.poll()
        self.assertEqual(status, 200)
        self.assertIsNone(data['command'])
        _, row, _ = self.status(ident)
        self.assertEqual(row['state'], 'expired')
        self.assertIs(row['confirmed'], False)
        _, status, replay, _ = self.open(request=ident)
        self.assertIn(status, (200, 202))
        self.assertEqual(replay['state'], row['state'], 'Duplicate owner requests do not requeue expired commands.')
        self.assertIsNone(self.poll()[1]['command'])

    def test_expired_ui_lease_cannot_claim_or_ack_and_dropped_delivery_is_not_replayed(self):
        self.hello()
        ident, _, _, _ = self.open(); self.poll()
        old_page, old_lease = self.page, self.lease
        self.clock += 8.1
        self.rejected(self.poll())
        self.rejected(self.receipt(ident))
        self.page = str(uuid.uuid4()); self.hello()
        self.assertIsNone(self.poll()[1]['command'], 'A replacement native page must not replay uncertain delivery.')
        self.rejected(self.call('POST', '/ui/poll', {'page_id': old_page, 'lease': old_lease}, role='ui'))
        _, row, _ = self.status(ident)
        self.assertEqual(row['state'], 'unknown')
        self.assertIs(row['confirmed'], False)

    def test_bye_revokes_lease_and_does_not_requeue_a_claimed_command(self):
        self.hello()
        ident, _, _, _ = self.open(); self.poll()
        status, _, _ = self.call('POST', '/ui/bye', {'page_id': self.page, 'lease': self.lease}, role='ui')
        self.assertEqual(status, 200)
        self.rejected(self.poll())
        _, state, _ = self.status()
        self.assertFalse(state['connected'])
        self.page = str(uuid.uuid4()); self.hello()
        self.assertIsNone(self.poll()[1]['command'])
        _, row, _ = self.status(ident)
        self.assertEqual(row['state'], 'unknown')
        self.assertIs(row['confirmed'], False)

    def test_another_page_and_arbitrary_routes_cannot_acquire_command(self):
        self.hello(); self.open()
        self.rejected(self.call('POST', '/ui/poll', {'page_id': str(uuid.uuid4()), 'lease': self.lease}, role='ui'))
        for path in ('/ui/send', '/ui/new', '/owner/prompt', '/open', '/ui/%70oll'):
            self.rejected(self.call('POST', path, {'page_id': self.page, 'lease': self.lease}, role='ui'))
        status, data, _ = self.poll()
        self.assertEqual(status, 200)
        self.assertEqual(data['command']['conversation_id'], self.target)

    def test_credentials_and_provider_diagnostics_are_not_reflected_or_logged(self):
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            self.rejected(self.call('POST', '/ui/hello', {'page_id': self.owner, 'parent_origin': self.ui}, role='ui'))
            self.rejected(self.call('POST', '/owner/open', {'request_id': self.owner, 'conversation_id': self.ui}))
            self.rejected(self.call('GET', '/owner/status?request_id=' + urllib.parse.quote(self.ui)))
        self.assertNotIn(self.owner, captured.getvalue())
        self.assertNotIn(self.ui, captured.getvalue())

    def test_malformed_duplicate_or_oversized_json_and_extra_ui_fields_fail_closed(self):
        for raw in (b'{', b'null', b'[]', b'"unexpected"', b'{}', b'x' * 100000):
            self.rejected(self.call('POST', '/ui/hello', role='ui', raw=raw))
        duplicate = ('{"page_id":%s,"parent_origin":"https://example.invalid",'
                     '"parent_origin":%s}' % (json.dumps(self.page), json.dumps(self.parent))).encode()
        self.rejected(self.call('POST', '/ui/hello', role='ui', raw=duplicate))
        self.rejected(self.call('POST', '/ui/hello', {'page_id': self.page, 'parent_origin': self.parent, 'command': 'new'}, role='ui'))
        self.hello()
        self.rejected(self.poll(command='openConversation'))

    def test_live_wal_metadata_is_read_without_modifying_native_rows(self):
        with contextlib.closing(sqlite3.connect(self.dbpath)) as conn, conn:
            conn.execute('DELETE FROM conversation_summaries WHERE conversation_id=?', (self.target,))
        writer = sqlite3.connect(self.dbpath)
        self.addCleanup(writer.close)
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('INSERT INTO conversation_summaries VALUES(?)', (self.target,))
        writer.commit()
        before = writer.execute('SELECT conversation_id FROM conversation_summaries ORDER BY conversation_id').fetchall()
        self.assertTrue(Path(str(self.dbpath) + '-wal').exists())
        self.hello()
        _, status, _, _ = self.open()
        self.assertIn(status, (200, 202), 'A current native row in WAL must be visible.')
        status, data, _ = self.poll()
        self.assertEqual(status, 200)
        self.assertEqual(data['command']['conversation_id'], self.target)
        self.assertEqual(writer.execute('SELECT conversation_id FROM conversation_summaries ORDER BY conversation_id').fetchall(), before)

    def test_missing_or_unreadable_native_metadata_has_fixed_failure(self):
        self.hello()
        self.dbpath.unlink()
        _, status, data, _ = self.open()
        self.assertEqual(status, 404)
        self.assertEqual(set(data), {'error'})
        self.assertFalse(self.dbpath.exists(), 'Opening must never create a missing native database.')
        self.dbpath.write_text('fictional invalid SQLite metadata')
        _, status, data, _ = self.open()
        self.assertEqual(status, 404)
        self.assertNotIn('SQLite', data['error'])

    def test_metadata_symlink_cannot_redirect_target_proof(self):
        self.hello()
        original = self.source / 'fictional-original.db'
        self.dbpath.rename(original)
        try:
            self.dbpath.symlink_to(original)
        except OSError:
            self.skipTest('Creating a fixture symlink is unavailable on this platform.')
        _, status, _, _ = self.open()
        self.assertEqual(status, 404)

    def test_lifetime_request_bound_preserves_old_idempotency_instead_of_evicting_ids(self):
        self.hello()
        first = None
        for _ in range(64):
            ident, status, _, _ = self.open()
            self.assertIn(status, (200, 202))
            first = first or ident
            self.assertEqual(self.poll()[1]['command']['request_id'], ident)
            self.assertEqual(self.receipt(ident)[0], 200)
        _, status, _, _ = self.open()
        self.assertEqual(status, 429)
        _, status, duplicate, _ = self.open(request=first)
        self.assertIn(status, (200, 202))
        self.assertEqual(duplicate['state'], 'called')
        self.assertIsNone(self.poll()[1]['command'], 'Lifetime bound must not forget delivered IDs and permit replay.')

    def test_bootstrap_and_static_files_contain_no_owner_or_ui_capability(self):
        self.rejected(self.call('GET', '/', role=None))
        self.rejected(self.call('GET', '/?token=invalid', role=None))
        status, page, headers = self.call('GET', '/?token=' + urllib.parse.quote(self.ui), role=None)
        self.assertEqual(status, 200)
        self.assertIn(self.parent, page)
        self.assertIn(self.broker.ui_origin, page)
        self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertIn("frame-ancestors " + self.parent, headers['Content-Security-Policy'])
        status, source, _ = self.call('GET', '/connector.js', role=None)
        self.assertEqual(status, 200)
        self.assertIn('toggle-conversation', source)
        self.rejected(self.call('GET', '/../config.json', role=None))
        self.rejected(self.call('GET', '/owner/status?request_id=' + str(uuid.uuid4()) + '&prompt=never'))

    def test_aux_bootstrap_accepts_only_registered_conversation_context(self):
        manifest = json.loads((INTEGRATION / 'sidecar.json').read_text())
        self.assertIs(manifest['has_web_ui'], True)
        views = manifest['ui_config']['views']
        self.assertEqual(len(views), 1, 'This proof declares one temporary native view, not a global route.')
        self.assertEqual(views[0]['entrypoint'], 'SIDECAR_UI_ENTRYPOINT_AUX_PANE')
        self.assertEqual(views[0]['path'], '/')
        for target in (self.target, self.second):
            query = urllib.parse.urlencode({'token': self.ui, 'conversationId': target})
            status, page, headers = self.call('GET', '/?' + query, role=None)
            self.assertEqual(status, 200)
            self.assertIn(self.parent, page)
            self.assertIn(self.broker.ui_origin, page)
            self.assertIn('frame-ancestors ' + self.parent, headers['Content-Security-Policy'])
        self.assertFalse(self.status()[1]['connected'], 'Loading HTML alone cannot establish the native frame lease.')

    def test_aux_bootstrap_rejects_foreign_malformed_duplicate_and_unknown_context(self):
        token = urllib.parse.quote(self.ui)
        for value in (str(uuid.uuid4()), self.target.upper(), 'x:' + self.target, '', '[]', 'not-a-conversation'):
            query = urllib.parse.urlencode({'token': self.ui, 'conversationId': value})
            self.rejected(self.call('GET', '/?' + query, role=None))
        for query in (
            'token=' + token + '&conversationId=' + self.target + '&conversationId=' + self.target,
            'token=' + token + '&conversationId=' + self.target + '&conversationId=' + self.second,
            'token=' + token + '&token=' + token + '&conversationId=' + self.target,
            'token=' + token + '&conversationId=' + self.target + '&unknown=never',
            'token=' + token + '&conversation_id=' + self.target,
            'token=' + urllib.parse.quote(self.owner) + '&conversationId=' + self.target,
            'conversationId=' + self.target,
        ):
            self.rejected(self.call('GET', '/?' + query, role=None))
        self.assertFalse(self.status()[1]['connected'])

    def forwarded_host(self):
        return 'localhost:' + str(self.port + 1 if self.port < 65535 else self.port - 1)

    def test_forwarded_native_host_supports_only_authenticated_native_lifecycle(self):
        forwarded = {'Host': self.forwarded_host()}
        query = urllib.parse.urlencode({'token': self.ui, 'conversationId': self.target})
        self.assertEqual(self.call('GET', '/?' + query, role=None, extra=forwarded)[0], 200)
        self.assertEqual(self.call('GET', '/connector.js', role=None, extra=forwarded)[0], 200)
        status, hello, _ = self.call('POST', '/ui/hello', {'page_id': self.page, 'parent_origin': self.parent},
                                     role='ui', extra=forwarded)
        self.assertEqual(status, 200)
        self.lease = hello['lease']
        ident, status, _, _ = self.open()  # Owner requests retain the exact listener Host.
        self.assertEqual(status, 200)
        status, polled, _ = self.call('POST', '/ui/poll', {'page_id': self.page, 'lease': self.lease}, role='ui', extra=forwarded)
        self.assertEqual(status, 200)
        self.assertEqual(polled['command']['request_id'], ident)
        self.assertEqual(polled['command']['conversation_id'], self.target)
        status, row, _ = self.call('POST', '/ui/receipt', {'page_id': self.page, 'lease': self.lease,
            'request_id': ident, 'conversation_id': self.target, 'state': 'called'}, role='ui', extra=forwarded)
        self.assertEqual(status, 200)
        self.assertEqual(row['state'], 'called')
        self.assertIs(row['confirmed'], False)
        self.assertEqual(self.status(ident)[1]['state'], 'called')
        status, closed, _ = self.call('POST', '/ui/bye', {'page_id': self.page, 'lease': self.lease}, role='ui', extra=forwarded)
        self.assertEqual(status, 200)
        self.assertEqual(closed, {'closed': True})
        self.assertFalse(self.status()[1]['connected'])

    def test_forwarded_native_host_never_relaxes_owner_or_unknown_routes(self):
        self.hello()
        forwarded = {'Host': self.forwarded_host()}
        for method, path, body, role in (
            ('GET', '/owner/status', None, 'owner'),
            ('POST', '/owner/open', {'request_id': str(uuid.uuid4()), 'conversation_id': self.target}, 'owner'),
            ('GET', '/unknown', None, 'owner'),
            ('POST', '/ui/unknown', {'page_id': self.page, 'lease': self.lease}, 'ui'),
            ('POST', '/ui/poll?unknown=never', {'page_id': self.page, 'lease': self.lease}, 'ui'),
            ('GET', '/connector.js?unknown=never', None, None),
            ('GET', 'http://' + self.forwarded_host() + '/?token=' + urllib.parse.quote(self.ui), None, None),
        ):
            with self.subTest(method=method, path=path):
                status, data, headers = self.call(method, path, body, role=role, extra=forwarded)
                self.assertEqual(status, 403, 'Only the narrow native route class receives forwarded-Host compatibility.')
                self.rejected((status, data, headers))

    def test_forwarded_native_host_rejects_noncanonical_foreign_and_duplicate_hosts(self):
        port = self.forwarded_host().split(':')[1]
        query = '/?' + urllib.parse.urlencode({'token': self.ui, 'conversationId': self.target})
        for host in ('localhost:0', 'localhost:65536', 'localhost:01', 'localhost:00080',
                     'localhost:+' + port, 'localhost:-1', 'localhost:12.5', 'localhost: ' + port,
                     'localhost:' + port + ' ', 'localhost:\t' + port, 'localhost:' + port + '/path',
                     'localhost:' + port + '@other', 'owner@localhost:' + port, 'http://localhost:' + port,
                     'localhost.evil.invalid:' + port, 'LOCALHOST:' + port, 'localhöst:' + port,
                     'localhost:²', '127.0.0.1:' + port):
            with self.subTest(host=host):
                status, data, headers = self.call('GET', query, role=None, extra={'Host': host})
                self.assertEqual(status, 403)
                self.rejected((status, data, headers))
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=2)
        try:
            conn.putrequest('GET', query, skip_host=True, skip_accept_encoding=True)
            conn.putheader('Host', self.forwarded_host())
            conn.putheader('Host', self.host)
            conn.endheaders()
            response = conn.getresponse()
            self.assertEqual(response.status, 403)
            self.rejected((response.status, json.loads(response.read()), {}))
        finally:
            conn.close()

    def test_forwarded_native_host_does_not_bypass_tokens_origin_parent_or_bootstrap(self):
        forwarded = {'Host': self.forwarded_host()}
        payload = {'page_id': self.page, 'parent_origin': self.parent}
        self.rejected(self.call('GET', '/?token=invalid&conversationId=' + self.target, role=None, extra=forwarded))
        for role, headers in (
            (None, forwarded),
            (None, dict(forwarded, **{'X-Sidecar-Token': 'invalid'})),
            ('ui', dict(forwarded, **{'X-Sidecar-Token': 'invalid'})),
            ('ui', dict(forwarded, **{'Origin': 'null'})),
            ('ui', dict(forwarded, **{'Origin': self.parent})),
            ('owner', forwarded),
        ):
            self.rejected(self.call('POST', '/ui/hello', payload, role=role, extra=headers))
        self.rejected(self.call('POST', '/ui/hello', dict(payload, parent_origin='https://example.invalid'), role='ui', extra=forwarded))
        self.assertFalse(self.status()[1]['connected'])

    def test_absent_native_origin_with_exact_ui_capability_completes_forwarded_lifecycle(self):
        native = {'Host': self.forwarded_host(), 'X-Sidecar-Token': self.ui}
        status, hello, _ = self.call('POST', '/ui/hello', {'page_id': self.page, 'parent_origin': self.parent}, role=None, extra=native)
        self.assertEqual(status, 200)
        self.lease = hello['lease']
        ident, status, _, _ = self.open()
        self.assertEqual(status, 200)
        status, data, _ = self.call('POST', '/ui/poll', {'page_id': self.page, 'lease': self.lease}, role=None, extra=native)
        self.assertEqual(status, 200)
        self.assertEqual(data['command']['request_id'], ident)
        self.assertEqual(data['command']['conversation_id'], self.target)
        status, row, _ = self.call('POST', '/ui/receipt', {'page_id': self.page, 'lease': self.lease,
            'request_id': ident, 'conversation_id': self.target, 'state': 'called'}, role=None, extra=native)
        self.assertEqual(status, 200)
        self.assertEqual(row['state'], 'called')
        self.assertIs(row['confirmed'], False)
        status, closed, _ = self.call('POST', '/ui/bye', {'page_id': self.page, 'lease': self.lease}, role=None, extra=native)
        self.assertEqual(status, 200)
        self.assertEqual(closed, {'closed': True})
        self.assertFalse(self.status()[1]['connected'])

    def test_absent_origin_never_replaces_ui_capability_or_weakens_cross_role_checks(self):
        host = {'Host': self.forwarded_host()}
        payload = {'page_id': self.page, 'parent_origin': self.parent}
        for role, extra in (
            (None, host),
            (None, dict(host, **{'X-Sidecar-Token': 'invalid'})),
            (None, dict(host, **{'X-Sidecar-Token': self.owner})),
            ('owner', host),
        ):
            self.rejected(self.call('POST', '/ui/hello', payload, role=role, extra=extra))
        for origin in ('', 'null', 'https://example.invalid', self.parent,
                       self.broker.ui_origin + ', https://example.invalid'):
            self.rejected(self.call('POST', '/ui/hello', payload, role=None,
                extra=dict(host, **{'X-Sidecar-Token': self.ui, 'Origin': origin})))
        only_ui = {'X-Sidecar-Token': self.ui}
        self.rejected(self.call('GET', '/owner/status', role=None, extra=only_ui))
        self.rejected(self.call('POST', '/owner/open', {'request_id': str(uuid.uuid4()), 'conversation_id': self.target}, role=None, extra=only_ui))
        self.assertFalse(self.status()[1]['connected'])
        body = json.dumps(payload).encode()
        for origins in ((self.broker.ui_origin, self.broker.ui_origin), (self.broker.ui_origin, 'null')):
            conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=2)
            try:
                conn.putrequest('POST', '/ui/hello', skip_host=True, skip_accept_encoding=True)
                for key, value in [('Host', self.forwarded_host()), ('Content-Type', 'application/json'),
                                   ('Content-Length', str(len(body))), ('X-Sidecar-Token', self.ui)]:
                    conn.putheader(key, value)
                for origin in origins:
                    conn.putheader('Origin', origin)
                conn.endheaders(body)
                response = conn.getresponse()
                self.assertEqual(response.status, 403)
                self.rejected((response.status, json.loads(response.read()), {}))
            finally:
                conn.close()

    def test_ambiguous_duplicate_auth_and_http_framing_are_rejected(self):
        body = json.dumps({'page_id': self.page, 'parent_origin': self.parent}).encode()
        base = [('Host', self.host), ('Content-Type', 'application/json'),
                ('Content-Length', str(len(body))), ('Origin', self.broker.ui_origin), ('X-Sidecar-Token', self.ui)]
        for extra in [('Host', self.host), ('Origin', self.broker.ui_origin), ('X-Sidecar-Token', self.ui),
                      ('Content-Type', 'application/json'), ('Content-Length', str(len(body))), ('Transfer-Encoding', 'chunked')]:
            with self.subTest(header=extra[0]):
                conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=2)
                try:
                    conn.putrequest('POST', '/ui/hello', skip_host=True, skip_accept_encoding=True)
                    for key, value in base + [extra]:
                        conn.putheader(key, value)
                    conn.endheaders(body)
                    response = conn.getresponse()
                    data = json.loads(response.read())
                    self.rejected((response.status, data, {}))
                finally:
                    conn.close()

    def test_shutdown_marks_claim_uncertain_and_new_broker_has_no_retained_commands(self):
        self.hello()
        ident, _, _, _ = self.open(); self.poll()
        self.broker.close()
        _, row, _ = self.status(ident)
        self.assertEqual(row['state'], 'unknown')
        self.assertFalse(self.status()[1]['connected'])
        self.rejected(self.poll())
        fresh = prototype.Broker(self.source, {self.target}, self.owner, self.ui, self.parent,
                                 clock=lambda: self.clock, wall=lambda: self.clock)
        self.assertEqual(fresh.status(), {'connected': False, 'pending': False})
        with self.assertRaises(prototype.ProtocolError) as missing:
            fresh.status(ident)
        self.assertEqual(missing.exception.status, 404)

    def test_other_provider_root_and_shared_capabilities_cannot_start_broker(self):
        other = self.home / '.gemini' / 'antigravity-cli'
        other.mkdir()
        for root, owner, ui, parent in ((other, self.owner, self.ui, self.parent),
                                       (self.source, self.owner, self.owner, self.parent),
                                       (self.source, self.owner, self.ui, 'https://example.invalid')):
            with self.assertRaises(ValueError):
                prototype.Broker(root, {self.target}, owner, ui, parent)

    def test_security_and_delivery_regressions_detect_in_memory_guard_mutations(self):
        global prototype
        original = prototype
        path = INTEGRATION / 'broker.py'
        source = path.read_text()
        mutations = [
            ('target not in self.allowed or not canonical_uuid(target)', 'not canonical_uuid(target)',
             'test_native_metadata_and_owner_allowlist_are_both_required_without_transcripts'),
            ("origins not in ([], [broker.ui_origin])", 'False',
             'test_owner_ui_roles_and_exact_ui_origin_are_not_interchangeable'),
            ("row['state'] = 'claimed'", "row['state'] = 'queued'",
             'test_poll_consumes_command_once_and_ack_does_not_claim_desktop_selection'),
            ('confirmed=False', 'confirmed=True',
             'test_poll_consumes_command_once_and_ack_does_not_claim_desktop_selection'),
            ('if native_route and len(hosts) == 1', 'if len(hosts) == 1',
             'test_forwarded_native_host_never_relaxes_owner_or_unknown_routes'),
        ]
        for before, after, method in mutations:
            with self.subTest(guard=before):
                self.assertIn(before, source)
                mutated = types.ModuleType('fictional_open_guard_mutant')
                mutated.__file__ = str(path)
                exec(compile(source.replace(before, after, 1), str(path), 'exec'), mutated.__dict__)
                try:
                    prototype = mutated
                    result = unittest.TestResult()
                    PrototypeTests(method).run(result)
                finally:
                    prototype = original
                self.assertEqual(result.errors, [], 'A mutant must fail a real assertion, not a fixture error.')
                self.assertTrue(result.failures, 'Removing this boundary must fail its regression.')


@unittest.skipUnless(shutil.which('node'), 'Node is needed to execute the actual browser adapter in a VM.')
class ConnectorTests(unittest.TestCase):
    def run_node(self, program):
        result = subprocess.run([shutil.which('node'), '-e', program, str(INTEGRATION / 'connector.js')],
                                capture_output=True, text=True, timeout=10, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_native_html_loads_actual_connector_as_a_deferred_classic_script(self):
        page = (INTEGRATION / 'index.html').read_text()
        scripts = re.findall(r'<script\b([^>]*)>', page)
        self.assertEqual(len(scripts), 1)
        self.assertRegex(scripts[0], r'\bdefer(?:\s|$)')
        self.assertRegex(scripts[0], r'\bsrc="/connector\.js"')
        self.assertNotRegex(scripts[0], r'\btype\s*=')
        self.assertNotRegex((INTEGRATION / 'connector.js').read_text(), r'(?m)^\s*(?:import|export)\b')

    def test_actual_connector_exact_command_expiry_duplicate_failure_and_closed_page(self):
        self.run_node(r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8');
const create=vm.runInNewContext(source+';createConnector',{Date});
const page='11111111-2222-4333-8444-555555555555',requestId='22222222-3333-4444-8555-666666666666';
const target='33333333-4444-4555-8666-777777777777',lease='a'.repeat(64),time=100000;
const command={request_id:requestId,conversation_id:target,expires_at_ms:time+5000,remaining_ms:5000};
const clone=value=>JSON.parse(JSON.stringify(value));
function fixture(value=command,{failInvoke=false,loseReceipt=false,factory=create}={}){
  const calls=[],invoked=[],states=[];
  const request=async(route,body)=>{
    body=clone(body);calls.push({route,body});
    if(route==='/ui/hello'){assert.deepStrictEqual(body,{page_id:page});return {page_id:page,lease,lease_ms:8000}}
    if(route==='/ui/poll'){assert.deepStrictEqual(body,{page_id:page,lease});return {command:value}}
    if(route==='/ui/receipt'){
      assert.deepStrictEqual(Object.keys(body).sort(),['page_id','lease','request_id','conversation_id','state'].sort());
      assert.equal(body.request_id,requestId);assert.equal(body.conversation_id,target);
      if(loseReceipt)throw Error('fictional private error must not escape');
      return {request_id:requestId,conversation_id:target,state:body.state,confirmed:false};
    }
    if(route==='/ui/bye')return {closed:true};
    throw Error('Unapproved route: '+route);
  };
  const connector=factory({request,invoke:id=>{invoked.push(id);if(failInvoke)throw Error('fictional private error must not escape')},pageId:page,now:()=>time,onState:s=>states.push(s)});
  return {connector,calls,invoked,states};
}
(async()=>{
  const normal=fixture();await normal.connector.tick();
  assert.deepStrictEqual(normal.invoked,[target]);
  assert.deepStrictEqual(normal.calls.map(c=>c.route),['/ui/hello','/ui/poll','/ui/receipt']);
  assert.equal(normal.calls.at(-1).body.state,'called');assert.equal(normal.states.at(-1),'dispatched; selection unconfirmed');
  await normal.connector.tick();assert.equal(normal.invoked.length,1,'An identical command must not navigate twice.');
  const lost=fixture(command,{loseReceipt:true});await lost.connector.tick();await lost.connector.tick();
  assert.equal(lost.invoked.length,1,'An uncertain receipt must never cause automatic navigation replay.');
  const failed=fixture(command,{failInvoke:true});await failed.connector.tick();assert.equal(failed.calls.at(-1).body.state,'failed');
  assert.ok(!JSON.stringify([failed.calls,failed.states,lost.calls,lost.states]).includes('fictional private error'),'Native or transport diagnostics must not escape.');
  for(const value of [{...command,request_id:'not-a-request'},{...command,conversation_id:'x:'+target},
    {...command,expires_at_ms:time},{...command,remaining_ms:0},{...command,remaining_ms:5001},
    {...command,prompt:'Never send a prompt.'},[], 'open anything']){
    const malformed=fixture(value);await malformed.connector.tick();assert.equal(malformed.invoked.length,0,'Invalid/expired commands cannot reach native navigation.');
  }
  async function strictIdentifiers(factory){
    for(const value of [[page],[],{},new String(page),{toString:()=>page},42,true,null,undefined]){
      let requests=0;
      assert.throws(()=>factory({pageId:value,request:async()=>{requests++},invoke(){}}),/Invalid connector configuration/,
        'A non-string page identity must fail before connection or coercion.');
      assert.equal(requests,0);
    }
    for(const [field,valid] of [['request_id',requestId],['conversation_id',target]]){
      for(const value of [[valid],[],{},new String(valid),{toString:()=>valid},42,true,null,undefined]){
        const malformed=fixture({...command,[field]:value},{factory});await malformed.connector.tick();
        assert.equal(malformed.invoked.length,0,'Non-string '+field+' must never reach native navigation.');
        assert.equal(malformed.calls.filter(call=>call.route==='/ui/receipt').length,0);
      }
    }
  }
  await strictIdentifiers(create);
  const coercingSource=source.replace("typeof value === 'string' && UUID.test(value)",'UUID.test(value)');
  assert.notEqual(coercingSource,source,'The mutation must restore the original regex-coercion behavior.');
  const coercingFactory=vm.runInNewContext(coercingSource+';createConnector',{Date});
  const reproduced=fixture({...command,conversation_id:[target]},{factory:coercingFactory});await reproduced.connector.tick();
  assert.equal(reproduced.invoked.length,1,'In-memory original behavior independently reproduces array dispatch.');
  await assert.rejects(strictIdentifiers(coercingFactory),assert.AssertionError,
    'The actual type regressions must fail when the string guard is removed.');
  let releasePoll,pollStarted=false,invoked=0;
  const racing=create({pageId:page,now:()=>time,invoke:()=>invoked++,request:async(route)=>{
    if(route==='/ui/hello')return {page_id:page,lease,lease_ms:8000};
    if(route==='/ui/poll'){pollStarted=true;return new Promise(resolve=>releasePoll=resolve)}
    if(route==='/ui/bye')return {closed:true};
    throw Error('Unexpected route after close.');
  }});
  const pending=racing.tick();for(let i=0;i<20&&!pollStarted;i++)await Promise.resolve();assert.ok(pollStarted);
  await racing.close();releasePoll({command});await pending;assert.equal(invoked,0,'A closed/unloaded native page cannot dispatch a late response.');
  console.log('actual connector protocol checks passed');
})().catch(error=>{console.error(error);process.exitCode=1});
""")

    def test_actual_native_boot_only_uses_pinned_owned_frame_and_fixed_navigation_message(self):
        self.run_node(r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8');
const page='11111111-2222-4333-8444-555555555555',requestId='22222222-3333-4444-8555-666666666666';
const target='33333333-4444-4555-8666-777777777777',lease='a'.repeat(64),parent='https://127.0.0.1:49321';
const origin='plugin://watch-open-prototype-49322',token='fixture-native-ui-capability',now=100000;
const clone=v=>JSON.parse(JSON.stringify(v));
async function boot(change=()=>{},contextChange=()=>{}){
  const posts=[],requests=[],listeners={},states=[];let sequence=0,statusText='';
  const status={get textContent(){return statusText},set textContent(value){states.push(value);statusText=value}};
  const win={location:{protocol:'plugin:',host:'watch-open-prototype-49322',search:'?token='+token,ancestorOrigins:[parent]},
    parent:{postMessage:(message,destination)=>posts.push({message:clone(message),destination})},addEventListener:(event,callback)=>listeners[event]=callback};
  const metas={'watch-native-parent':parent,'watch-native-origin':origin};
  const doc={referrer:'',getElementById:()=>status,querySelector:selector=>({content:metas[selector.match(/"([^"]+)"/)[1]]})};
  change(win,doc,metas);
  const ctx={window:win,document:doc,Date:{now:()=>now},crypto:{randomUUID:()=>page},URL,URLSearchParams,
    setTimeout:()=>++sequence,clearTimeout(){},AbortController:class{constructor(){this.signal={}}abort(){}},
    fetch:async(path,options)=>{
      const body=JSON.parse(options.body);requests.push({path,body,options});
      assert.equal(options.method,'POST');assert.equal(options.redirect,'error');assert.equal(options.cache,'no-store');
      assert.equal(options.credentials,'omit');assert.equal(options.referrerPolicy,'no-referrer');
      assert.deepStrictEqual(clone(options.headers),{'Content-Type':'application/json','X-Sidecar-Token':token});
      let value;
      if(path==='/ui/hello'){assert.deepStrictEqual(body,{page_id:page,parent_origin:parent});value={page_id:page,lease,lease_ms:8000}}
      else if(path==='/ui/poll')value={command:{request_id:requestId,conversation_id:target,expires_at_ms:now+5000,remaining_ms:5000}};
      else if(path==='/ui/receipt'){assert.deepStrictEqual(body,{page_id:page,lease,request_id:requestId,conversation_id:target,state:'called'});value={confirmed:false}}
      else if(path==='/ui/bye')value={closed:true};
      else throw Error('Only fixed navigation broker routes are allowed.');
      return {ok:true,json:async()=>value};
    }};
  contextChange(ctx);vm.runInNewContext(source,ctx);for(let i=0;i<40;i++)await Promise.resolve();
  return {win,posts,requests,status,states,listeners};
}
(async()=>{
  const good=await boot();assert.equal(good.posts.length,1);assert.equal(good.states[0],'Starting native connection…');
  assert.deepStrictEqual(good.posts[0],{message:{type:'toggle-conversation',payload:{conversationId:target}},destination:parent});
  assert.equal('sidecar' in good.win,false,'The adapter must not install a replacement vendor SDK.');
  assert.equal(good.status.textContent,'dispatched; selection unconfirmed');
  assert.ok(!JSON.stringify(good.posts).includes(token),'No credential may cross the native navigation message.');
  good.listeners.pagehide();for(let i=0;i<20;i++)await Promise.resolve();assert.equal(good.requests.at(-1).path,'/ui/bye');
  for(const change of [w=>{w.parent=w},w=>{w.location.ancestorOrigins=['https://example.invalid']},
    w=>{w.location.protocol='http:'},w=>{w.location.host='wrong-plugin'},w=>{w.location.search=''},
    w=>{w.location.search='?token=one&token=two'},(w,d,m)=>{m['watch-native-parent']='https://example.invalid'}]){
    const bad=await boot(change);assert.equal(bad.posts.length,0,'An unowned or incompatible iframe must not contact the native host.');
    assert.equal(bad.requests.length,0,'Missing bootstrap must fail before contacting the broker.');
    assert.match(bad.status.textContent,/Unavailable/);
  }
  for(const breakCrypto of [ctx=>{ctx.crypto=undefined},ctx=>{ctx.crypto={randomUUID(){throw Error('fictional bootstrap detail '+token)}}}]){
    const failed=await boot(()=>{},breakCrypto);assert.equal(failed.posts.length,0);assert.equal(failed.requests.length,0);
    assert.equal(failed.status.textContent,'Unavailable: native connector could not start.');
    assert.ok(!failed.status.textContent.includes(token));assert.ok(!failed.status.textContent.includes('fictional bootstrap detail'));
  }
  console.log('actual native frame/navigation checks passed');
})().catch(error=>{console.error(error);process.exitCode=1});
""")


if __name__ == '__main__':
    unittest.main()
