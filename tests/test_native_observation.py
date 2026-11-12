"""Fictional owner-only observations. Every native reader is injected."""
from copy import deepcopy
from dataclasses import replace
import http.client
import json
from pathlib import Path
import socket
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import native_observation as obs
import codex_native_input as native
import server

THREAD = '11111111-1111-4111-8111-111111111111'
AID = 'x:' + THREAD
OTHER = '55555555-5555-4555-8555-555555555555'


def wait_for(fn):
    end = time.monotonic() + 3
    while time.monotonic() < end:
        if fn():
            return
        time.sleep(.005)
    raise AssertionError('offline worker did not finish')


def descriptors(secret=False, revision=3, identity=7):
    binding = native.Binding(THREAD, '22222222-2222-4222-8222-222222222222',
                             '33333333-3333-4333-8333-333333333333',
                             '44444444-4444-4444-8444-444444444444')
    req = {'id': identity, 'method': native.QUESTION, 'params': {
        'threadId': THREAD, 'turnId': 'turn-fiction', 'itemId': 'item-fiction',
        'isBlocking': True, 'questions': [
            {'id': 'native-question-id', 'header': 'Choose', 'question': 'FULL PRIVATE QUESTION',
             'isSecret': secret, 'isOther': True,
             'options': [{'label': 'Blue', 'description': 'Full description'}]},
            {'id': 'free', 'header': 'Text', 'question': 'Free text?', 'options': None}]}}
    return native._descriptors({'id': THREAD, 'hostId': 'local', 'requests': [req]},
                               binding, revision, 'fictional-incarnation')


def model(stamp=None):
    return {'state': 'observed', 'settings': {'model': 'fictional-model', 'effort': None,
            'mode': None, 'modelProvider': None, 'source': 'native_desktop_settings',
            'observedAt': time.time() if stamp is None else stamp},
            'capability': {'selectModel': True, 'selectEffort': True}, 'secret': 'DO NOT LEAK'}


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.rows = {AID: {'runtime': 'Codex'}}
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        self.services = []

    def service(self, **kw):
        s = obs.NativeObservationService(lambda aid: self.rows.get(aid), **kw)
        s.start()
        self.services.append(s)
        self.addCleanup(s.close)
        return s

    def test_import_and_constructor_have_no_reader_effect(self):
        calls = []
        s = obs.NativeObservationService(lambda _: None, lambda _: calls.append(1))
        self.assertEqual(s.threads, [])
        self.assertEqual(calls, [])
        s.close()

    def test_exact_target_rejection_before_reads(self):
        calls = []
        s = self.service(model_reader=lambda aid: calls.append(aid))
        for aid in ('name', 'x:' + OTHER, 'x:' + THREAD.upper(), 'x:' + THREAD + '/bad'):
            if aid == AID:  # fictional UUID contains no letters
                continue
            with self.assertRaises(obs.ReadError):
                s.request_read(aid, ['model_settings'])
        for row in ({'runtime': 'Claude'}, {'runtime': 'Codex', 'hostId': 'remote'},
                    {'runtime': 'Codex', 'observed': True}, {'runtime': 'Codex', 'stale': True}):
            self.rows[AID] = row
            with self.assertRaises(obs.ReadError):
                s.request_read(AID, ['model_settings'])
        self.assertEqual(calls, [])

    def test_default_adapter_seams_use_exact_read_only_modules(self):
        import codex_model_settings
        calls = []
        def read_model(aid):
            calls.append(('model', aid))
            return model()
        def read_inputs(aid, callback, *, seconds):
            calls.append(('input', aid, seconds))
            callback(descriptors())
        with patch.object(codex_model_settings, 'read_settings', read_model), patch.object(native, 'subscribe_exact', read_inputs):
            s = self.service()
            s.request_read(AID, list(obs.PARTS))
            wait_for(lambda: len(calls) == 2 and not s.snapshot(AID)['model_settings']['read_active']
                     and not s.snapshot(AID)['inputs']['read_active'])
        self.assertCountEqual(calls, [('model', THREAD), ('input', THREAD, 10)])
        self.assertEqual(s.snapshot(AID)['inputs']['request_count'], 1)

    def test_model_whitelist_cache_and_capabilities(self):
        calls = []
        s = self.service(model_reader=lambda aid: (calls.append(aid), model(1100))[1],
                         clock=lambda: 100, wall=lambda: 1100)
        self.assertEqual(s.request_read(AID, ['model_settings'])[0], 202)
        def finalized():
            # Publishing the observation precedes the worker's job cleanup.
            with s.lock:
                return (AID, 'model_settings') not in s.jobs
        wait_for(finalized)
        view = s.snapshot(AID)
        p = view['model_settings']
        self.assertEqual(p['settings']['model'], 'fictional-model')
        self.assertIsNone(p['settings']['effort'])
        self.assertIsNone(p['settings']['mode'])
        self.assertFalse(p['capability']['selectModel'])
        self.assertFalse(p['capability']['selectEffort'])
        self.assertIsNone(p['catalog'])
        self.assertNotIn('DO NOT LEAK', json.dumps(view))
        self.assertNotIn('nativeThreadId', json.dumps(view))
        self.assertEqual(s.request_read(AID, ['model_settings'])[0], 200)
        self.assertEqual(calls, [THREAD])

    def test_first_frame_active_close_stale_complete_question(self):
        captured = []
        def reader(aid, callback, seconds):
            self.assertEqual((aid, seconds), (THREAD, 10))
            callback(descriptors())
            captured.append(callback)
            self.release.wait(2)
        s = self.service(input_reader=reader)
        s.request_read(AID, ['native_inputs'])
        wait_for(lambda: captured)
        p = s.snapshot(AID)['inputs']
        self.assertTrue(p['read_active'])
        self.assertFalse(p['stale'])
        self.assertGreater(p['expires_at'], time.time())
        row = p['requests'][0]
        self.assertFalse(row['capability']['answer'])
        self.assertEqual(row['presentation']['questions'][0]['question'], 'FULL PRIVATE QUESTION')
        self.assertEqual(row['presentation']['questions'][1]['options'], None)
        self.assertEqual(row['presentation']['questions'][0]['id'], 'q0')
        self.assertNotIn('native-question-id', json.dumps(p))
        self.release.set()
        wait_for(lambda: not s.snapshot(AID)['inputs']['read_active'])
        self.assertTrue(s.snapshot(AID)['inputs']['requests'][0]['stale'])
        self.assertEqual(s.snapshot(AID)['inputs']['state'], 'stale_observation')

    def test_job_epoch_rejects_late_callback(self):
        callbacks = []
        def reader(aid, cb, seconds):
            callbacks.append(cb)
            self.release.wait(2)
        s = self.service(input_reader=reader)
        s.request_read(AID, ['native_inputs'])
        wait_for(lambda: callbacks)
        with s.lock:
            s.entries[AID]['view']['observation_epoch'] = 'replaced-watch-epoch'
        callbacks[0](descriptors())
        self.assertEqual(s.snapshot(AID)['inputs']['requests'], [])

    def test_secret_and_unsupported_are_metadata_only(self):
        for ds in (descriptors(True),
                   tuple(replace(d, supported_schema=True, secret=False) for d in descriptors(True)),
                   tuple(replace(d, secret=True) for d in descriptors()),
                   tuple(replace(d, supported_schema=False) for d in descriptors())):
            rows, _ = obs._questions(ds, (AID, 'codex', THREAD))
            self.assertIsNone(rows[0][2]['presentation'])
            self.assertNotIn('FULL PRIVATE', json.dumps(rows))

    def test_oversized_presentation_is_metadata_without_truncation(self):
        d = descriptors()[0]
        for header in ('H' * 4097, '\U0001f680' * 2049):
            raw = d.raw_request()
            raw['params']['questions'][0]['header'] = header
            text = native._json(raw)
            import hashlib
            oversized = replace(d, raw_json=text, schema_hash=hashlib.sha256(text.encode()).hexdigest())
            rows, _ = obs._questions((oversized,), (AID, 'codex', THREAD))
            self.assertIsNone(rows[0][2]['presentation'])
            self.assertEqual(rows[0][2]['capability']['reason'], 'oversized_native_only')
        raw = d.raw_request()
        raw['params']['questions'] = [dict(id=str(i), header='h', question='Q' * 65536) for i in range(4)]
        text = native._json(raw)
        oversized = replace(d, raw_json=text, schema_hash=hashlib.sha256(text.encode()).hexdigest())
        rows, _ = obs._questions((oversized,), (AID, 'codex', THREAD))
        self.assertIsNone(rows[0][2]['presentation'])

    def test_lone_surrogate_is_metadata_valid_emoji_is_complete(self):
        import hashlib
        d = descriptors()[0]
        for field, value, expected in [('question', '\ud800' * 60000, False),
                                       ('header', '\udfff', False),
                                       ('question', '\U0001f680' * 1000, True)]:
            raw = d.raw_request()
            raw['params']['questions'][0][field] = value
            text = native._json(raw)
            row = replace(d, raw_json=text, schema_hash=hashlib.sha256(text.encode()).hexdigest())
            rows, _ = obs._questions((row,), (AID, 'codex', THREAD))
            self.assertEqual(rows[0][2]['presentation'] is not None, expected)
            if expected:
                self.assertEqual(rows[0][2]['presentation']['questions'][0][field], value)
        for field in ('label', 'description'):
            raw = d.raw_request()
            raw['params']['questions'][0]['options'][0][field] = '\ud800'
            text = native._json(raw)
            row = replace(d, raw_json=text, schema_hash=hashlib.sha256(text.encode()).hexdigest())
            rows, _ = obs._questions((row,), (AID, 'codex', THREAD))
            self.assertIsNone(rows[0][2]['presentation'])

    def test_descriptor_hash_target_group_guard(self):
        d = descriptors()[0]
        bad = [replace(d, schema_hash='wrong'), replace(d, binding=replace(d.binding, conversation_id=OTHER)),
               replace(d, native_id=('string', '7')), replace(d, method='wrong')]
        for item in bad:
            with self.assertRaises(ValueError):
                obs._questions((item,), (AID, 'codex', THREAD))
        with self.assertRaises(ValueError):
            obs._questions((d, replace(d, revision=4)), (AID, 'codex', THREAD))

    def test_dedupe_late_callback_invalidate_and_close(self):
        callbacks, calls = [], []
        def reader(aid, cb, seconds):
            calls.append(aid)
            callbacks.append(cb)
            self.release.wait(2)
        s = self.service(input_reader=reader)
        s.request_read(AID, ['native_inputs'])
        wait_for(lambda: callbacks)
        s.request_read(AID, ['native_inputs'])
        self.assertEqual(calls, [THREAD])
        s.invalidate_agent(AID)
        with self.assertRaises(obs.ReadError) as exc:
            s.request_read(AID, ['native_inputs'])
        self.assertEqual(exc.exception.status, 429)
        callbacks[0](descriptors())
        with self.assertRaises(obs.ReadError):
            s.snapshot(AID)
        s.close()
        callbacks[0](descriptors())
        self.assertEqual(s.entries, {})

    def test_expiry_monotonic_rejects_late_publication(self):
        now, callbacks = [100.0], []
        def reader(aid, cb, seconds):
            callbacks.append(cb)
            self.release.wait(2)
        s = self.service(input_reader=reader, clock=lambda: now[0], wall=lambda: 1000 + now[0])
        s.request_read(AID, ['native_inputs'])
        wait_for(lambda: callbacks)
        callbacks[0](descriptors())
        now[0] += 11
        p = s.snapshot(AID)['inputs']
        self.assertFalse(p['read_active'])
        self.assertTrue(p['stale'])
        old = s.snapshot(AID)
        callbacks[0](descriptors(revision=4))
        self.assertEqual(old, s.snapshot(AID))

    def test_source_epoch_revision_and_immutable_copies(self):
        callbacks = []
        def reader(aid, cb, seconds):
            callbacks.append(cb)
            self.release.wait(2)
        s = self.service(input_reader=reader)
        s.request_read(AID, ['native_inputs'])
        wait_for(lambda: callbacks)
        callbacks[0](descriptors())
        first = s.snapshot(AID)
        handle = first['inputs']['requests'][0]['handle']
        first['inputs']['requests'][0]['presentation']['questions'][0]['question'] = 'MUTATED'
        callbacks[0](descriptors(revision=4))
        self.assertEqual(handle, s.snapshot(AID)['inputs']['requests'][0]['handle'])
        self.assertNotIn('MUTATED', json.dumps(s.snapshot(AID)))
        callbacks[0](())
        callbacks[0](descriptors(revision=5))
        self.assertNotEqual(handle, s.snapshot(AID)['inputs']['requests'][0]['handle'])
        with self.assertRaises(ValueError):
            callbacks[0](descriptors(revision=2))
        changed = tuple(replace(d, observation_incarnation='other') for d in descriptors(revision=5))
        with self.assertRaises(ValueError):
            callbacks[0](changed)

    def test_claude_unavailable_no_adapter_calls(self):
        aid = 'c:' + THREAD
        self.rows[aid] = {'runtime': 'Claude'}
        calls = []
        s = self.service(model_reader=lambda _: calls.append(1), input_reader=lambda *_: calls.append(2))
        status, view = s.request_read(aid, list(obs.PARTS))
        self.assertEqual(status, 200)
        self.assertEqual(calls, [])
        self.assertFalse(view['inputs']['capability']['observe'])

    def test_unknown_include_and_unavailable_model(self):
        s = self.service(model_reader=lambda _: {'state': 'observed', 'settings': {'model': 'PRIVATE ERROR'}})
        for include in ([], ['bad'], ['model_settings'] * 2, [[]]):
            with self.assertRaises(obs.ReadError):
                s.request_read(AID, include)
        s.request_read(AID, ['model_settings'])
        wait_for(lambda: not s.snapshot(AID)['model_settings']['read_active'])
        self.assertNotIn('PRIVATE ERROR', json.dumps(s.snapshot(AID)))

    def test_removed_station_purges_snapshot_on_get_and_post(self):
        s = self.service(model_reader=lambda _: model())
        s.request_read(AID, ['model_settings'])
        wait_for(lambda: not s.snapshot(AID)['model_settings']['read_active'])
        del self.rows[AID]
        with self.assertRaises(obs.ReadError):
            s.snapshot(AID)
        self.assertNotIn(AID, s.entries)
        self.rows[AID] = {'runtime': 'Codex'}
        s.request_read(AID, ['model_settings'])
        self.rows[AID] = {'runtime': 'Codex', 'hostId': 'remote'}
        with self.assertRaises(obs.ReadError):
            s.request_read(AID, ['model_settings'])
        self.assertNotIn(AID, s.entries)

    def test_fresh_read_epoch_handle_and_rate_limit(self):
        now, callbacks = [100.0], []
        def reader(aid, cb, seconds):
            callbacks.append(cb)
            cb(descriptors())
        s = self.service(input_reader=reader, clock=lambda: now[0])
        s.request_read(AID, ['native_inputs'])
        wait_for(lambda: callbacks and not s.snapshot(AID)['inputs']['read_active'])
        first = s.snapshot(AID)
        now[0] += 6
        s.request_read(AID, ['native_inputs'])
        wait_for(lambda: len(callbacks) == 2 and not s.snapshot(AID)['inputs']['read_active'])
        second = s.snapshot(AID)
        self.assertNotEqual(first['observation_epoch'], second['observation_epoch'])
        self.assertNotEqual(first['inputs']['requests'][0]['handle'], second['inputs']['requests'][0]['handle'])
        # Invalidation cannot bypass the physical per-target in-flight dedupe.
        s.invalidate_agent(AID)
        self.assertNotIn(AID, s.entries)

    def test_capacity_bounded_queue_and_physical_input_slot(self):
        calls = []
        def reader(aid, cb, seconds):
            calls.append(aid)
            self.release.wait(2)
        s = self.service(input_reader=reader)
        s.request_read(AID, ['native_inputs'])
        wait_for(lambda: calls)
        for i in range(2, 6):
            aid = 'x:00000000-0000-4000-8000-%012d' % i
            self.rows[aid] = {'runtime': 'Codex'}
            s.request_read(aid, ['native_inputs'])
        blocked = 'x:00000000-0000-4000-8000-000000000009'
        self.rows[blocked] = {'runtime': 'Codex'}
        with self.assertRaises(obs.ReadError) as exc:
            s.request_read(blocked, ['native_inputs'])
        self.assertEqual(exc.exception.status, 429)
        self.assertEqual(len(s.queue), 4)
        self.assertEqual(calls, [THREAD])
        s.invalidate_agent(AID)
        self.assertEqual(calls, [THREAD])
        self.assertEqual(len(s.queue), 4)

    def test_cache_ttl_and_idle_retention(self):
        now = [100.0]
        s = self.service(model_reader=lambda _: model(1100), clock=lambda: now[0], wall=lambda: 1000 + now[0])
        s.request_read(AID, ['model_settings'])
        wait_for(lambda: not s.snapshot(AID)['model_settings']['read_active'])
        self.assertFalse(s.snapshot(AID)['model_settings']['stale'])
        now[0] += 16
        self.assertTrue(s.snapshot(AID)['model_settings']['stale'])
        now[0] += 61
        with self.assertRaises(obs.ReadError):
            s.snapshot(AID)

    def test_stale_question_retention_bounded_despite_cache_gets(self):
        now = [100.0]
        s = self.service(input_reader=lambda aid, cb, seconds: cb(descriptors()), clock=lambda: now[0])
        s.request_read(AID, ['native_inputs'])
        wait_for(lambda: s.snapshot(AID)['inputs']['request_count'] == 1)
        now[0] += 40
        self.assertIn('FULL PRIVATE QUESTION', json.dumps(s.snapshot(AID)))
        now[0] += 21
        self.assertNotIn('FULL PRIVATE QUESTION', json.dumps(s.snapshot(AID)))

    def test_full_private_question_never_general_stream(self):
        before = list(server.LOG)
        s = self.service(input_reader=lambda aid, cb, seconds: cb(descriptors()))
        s.request_read(AID, ['native_inputs'])
        wait_for(lambda: s.snapshot(AID)['inputs']['request_count'] == 1)
        self.assertIn('FULL PRIVATE QUESTION', json.dumps(s.snapshot(AID)))
        self.assertEqual(list(server.LOG), before)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.svc = obs.NativeObservationService(lambda aid: {'runtime': 'Codex'} if aid == AID else None,
                    model_reader=lambda aid: (self.calls.append(aid), model())[1], input_reader=lambda *a, **k: None)
        self.svc.start()
        self.http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.H)
        self.http.daemon_threads = True
        self.port = self.http.server_address[1]
        self.patches = [patch.object(server, 'KEY', 'fiction-owner-key'), patch.object(server, 'PORT', self.port),
                        patch.object(server, 'NATIVE_OBSERVATIONS', self.svc)]
        for p in self.patches:
            p.start()
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.svc.close()
        for p in reversed(self.patches):
            p.stop()

    def request(self, method='POST', path='/native/v1/read', body=None, headers=None):
        if body is None:
            body = json.dumps({'agent_id': AID, 'include': ['model_settings']})
        hs = {'X-Live-Room-Key': 'fiction-owner-key'}
        if headers:
            hs.update(headers)
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=2)
        conn.request(method, path, body=body if method == 'POST' else None, headers=hs)
        response = conn.getresponse()
        data = response.read()
        status = response.status
        self.assertEqual(response.getheader('Cache-Control'), 'no-store')
        conn.close()
        return status, json.loads(data)

    def test_real_handler_read_cache_no_general_publication(self):
        before = list(server.LOG)
        status, _ = self.request()
        self.assertEqual(status, 202)
        wait_for(lambda: self.calls)
        wait_for(lambda: not self.svc.snapshot(AID)['model_settings']['read_active'])
        status, view = self.request('GET', '/native/v1/agents/' + urllib_quote(AID))
        self.assertEqual(status, 200)
        self.assertEqual(view['model_settings']['settings']['model'], 'fictional-model')
        self.assertEqual(list(server.LOG), before)
        self.assertEqual(self.calls, [THREAD])

    def test_auth_body_host_origin_and_query_before_reads(self):
        for hs in ({'X-Live-Room-Key': ''}, {'X-Live-Room-Key': 'peer-key'},
                   {'Host': 'evil.example'}, {'Origin': 'null'}):
            self.assertEqual(self.request(headers=hs)[0], 403)
        self.assertEqual(self.request(path='/native/v1/read?key=fiction-owner-key')[0], 400)
        for body in ('{}', '{"agent_id":1,"include":[]}', '{"agent_id":"x","include":["model_settings"]}',
                     '{"agent_id":"x","agent_id":"y","include":[]}'):
            self.assertIn(self.request(body=body)[0], (400, 404))
        self.assertEqual(self.calls, [])

    def test_no_write_routes_or_native_id_overrides(self):
        for path in ('/native/v1/answer', '/native/v1/set-model', '/native/v1/decide-native'):
            self.assertEqual(self.request(path=path)[0], 404)
        body = json.dumps({'agent_id': AID, 'include': ['model_settings'], 'nativeThreadId': OTHER})
        self.assertEqual(self.request(body=body)[0], 400)
        self.assertEqual(self.calls, [])

    def test_duplicate_headers_length_and_transfer_encoding(self):
        base = ['POST /native/v1/read HTTP/1.1', 'Host: 127.0.0.1:%d' % self.port,
                'X-Live-Room-Key: fiction-owner-key', 'Content-Length: 2']
        for extra, expected in [('X-Live-Room-Key: fiction-owner-key', 403),
                                ('Host: localhost:%d' % self.port, 403), ('Origin: null', 403),
                                ('Content-Length: 2', 400), ('Transfer-Encoding: chunked', 400)]:
            with socket.create_connection(('127.0.0.1', self.port), timeout=2) as conn:
                conn.sendall(('\r\n'.join(base + [extra, '', '{}'])).encode())
                response = conn.recv(4096)
                self.assertIn((' %d ' % expected).encode(), response.split(b'\r\n')[0])
        self.assertEqual(self.calls, [])

    def test_partial_body_has_bounded_deadline(self):
        with socket.create_connection(('127.0.0.1', self.port), timeout=4) as conn:
            start = time.monotonic()
            conn.sendall(('POST /native/v1/read HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n'
                          'X-Live-Room-Key: fiction-owner-key\r\nContent-Length: 40\r\n\r\n{' % self.port).encode())
            response = conn.recv(4096)
            self.assertIn(b' 400 ', response.split(b'\r\n')[0])
            self.assertLess(time.monotonic() - start, 3.5)
        self.assertEqual(self.calls, [])

    def test_trickled_body_has_total_deadline(self):
        stop = threading.Event()
        with socket.create_connection(('127.0.0.1', self.port), timeout=4) as conn:
            conn.sendall(('POST /native/v1/read HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n'
                          'X-Live-Room-Key: fiction-owner-key\r\nContent-Length: 40\r\n\r\n{' % self.port).encode())
            def trickle():
                while not stop.wait(.2):
                    try:
                        conn.sendall(b' ')
                    except OSError:
                        return
            sender = threading.Thread(target=trickle, daemon=True)
            sender.start()
            start = time.monotonic()
            try:
                response = conn.recv(4096)
                self.assertIn(b' 400 ', response.split(b'\r\n')[0])
                self.assertLess(time.monotonic() - start, 3.5)
            finally:
                stop.set()
                sender.join(.5)
        self.assertEqual(self.calls, [])

    def test_response_write_timeout_is_bounded(self):
        real_send, observed = server.H.send, []
        def send(handler, *args, **kwargs):
            observed.append(handler.connection.gettimeout())
            return real_send(handler, *args, **kwargs)
        with patch.object(server.H, 'send', send):
            self.assertEqual(self.request('GET', '/native/v1/agents/' + urllib_quote(AID))[0], 404)
        self.assertEqual(observed, [2.0])

    def test_get_never_starts_read_and_service_unavailable(self):
        self.assertEqual(self.request('GET', '/native/v1/agents/' + urllib_quote(AID))[0], 404)
        with patch.object(server, 'NATIVE_OBSERVATIONS', None):
            self.assertEqual(self.request()[0], 503)
        self.assertEqual(self.calls, [])


def urllib_quote(value):
    from urllib.parse import quote
    return quote(value, safe='')


if __name__ == '__main__':
    unittest.main()
