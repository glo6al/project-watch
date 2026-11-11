#!/usr/bin/env python3
"""Officer model selection with synthetic API transports and private temporary state."""
import copy
import http.client
import inspect
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
import server


class OfficerModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.realpath(self.tmp.name)
        self.saved = {key: getattr(server, key) for key in ('HOME', 'OFFICER_KEY', 'OFFICER_MODEL', 'OFFICER_MODEL_ERROR', 'OFFICER_CATALOG', 'KEY', 'PORT', 'MESSAGING')}
        server.HOME = self.home
        server.OFFICER_KEY = 'fixture-api-key'
        server.KEY = 'fixture-owner-key'
        server.OFFICER_MODEL = 'claude-fixture-a'
        server.OFFICER_MODEL_ERROR = ''
        server.OFFICER_CATALOG = {'key': '', 'at': 0, 'models': []}
        self.calls = []
        self.srv = None
        # Windows has no Officer key-file support; portable protocol tests use
        # synthetic in-memory persistence and make no Windows privacy claim.
        self.memory_save = patch.object(server, 'save_officer_model') if os.name == 'nt' else None
        if self.memory_save: self.memory_save.start()

    def tearDown(self):
        if self.srv:
            self.srv.shutdown(); self.thread.join(2); self.srv.server_close()
        if self.memory_save: self.memory_save.stop()
        for key, value in self.saved.items(): setattr(server, key, value)
        self.tmp.cleanup()

    def item(self, model='claude-fixture-a', name='Fixture A', low=True):
        caps = None if low is None else {'effort': {'supported': low, 'low': {'supported': low}}}
        return {'id': model, 'display_name': name, 'capabilities': caps, 'line': 'future-unrecognized-line', 'private_extra': server.OFFICER_KEY}

    def cache(self, items=None):
        items = items or [self.item(), self.item('claude-fixture-b', 'Fixture B', False)]
        page = {'data': items, 'has_more': False, 'last_id': items[-1]['id']}
        with patch.object(server, 'anthropic_request', return_value=(page, '')):
            models, error = server.officer_catalog()
        self.assertFalse(error)
        return models

    def start_http(self):
        self.srv = server.Server(('127.0.0.1', 0), server.H)
        server.PORT = self.srv.server_address[1]
        self.thread = threading.Thread(target=lambda: self.srv.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()

    def request(self, route, data=None, key=True):
        connection = http.client.HTTPConnection('127.0.0.1', server.PORT, timeout=3)
        headers = {'Content-Type': 'application/json'}
        if key: headers['X-Live-Room-Key'] = server.KEY
        connection.request('POST' if data is not None else 'GET', route, json.dumps(data) if data is not None else None, headers)
        response = connection.getresponse(); raw = response.read(); connection.close()
        return response.status, json.loads(raw) if response.getheader('Content-Type', '').startswith('application/json') else raw.decode()

    def test_paginated_catalog_complete_cache_and_key_binding(self):
        pages = [({'data': [self.item()], 'has_more': True, 'last_id': 'claude-fixture-a'}, ''),
                 ({'data': [self.item('claude-fixture-b', 'Fixture B', False)], 'has_more': False}, '')]
        def fake(method, path, body=None, end=None):
            self.calls.append((method, path, body, end))
            return pages.pop(0)
        with patch.object(server, 'anthropic_request', side_effect=fake):
            models, error = server.officer_catalog()
        self.assertFalse(error)
        self.assertEqual([m['id'] for m in models], ['claude-fixture-a', 'claude-fixture-b'])
        self.assertIn('after_id=claude-fixture-a', self.calls[1][1])
        self.assertTrue(all(c[0] == 'GET' and c[1].startswith('/v1/models?') and c[2] is None for c in self.calls))
        with patch.object(server, 'anthropic_request', side_effect=AssertionError('cache should avoid network')):
            self.assertEqual(server.officer_catalog()[0], models)
        server.OFFICER_KEY = 'different-fixture-key'
        with patch.object(server, 'anthropic_request', return_value=(None, 'synthetic unavailable')) as fetch:
            self.assertIsNone(server.officer_catalog()[0])
            self.assertEqual(fetch.call_count, 1)
        self.assertEqual(server.OFFICER_MODEL, 'claude-fixture-a')

    def test_malformed_loops_bounds_and_raw_errors_are_fixed(self):
        malformed = [
            {'data': [], 'has_more': True, 'last_id': 'claude-fixture-a'},
            {'data': [self.item()], 'has_more': 'yes'},
            {'data': [self.item(), self.item()], 'has_more': False},
            {'data': [dict(self.item(), display_name=server.OFFICER_KEY)], 'has_more': False},
            {'data': [dict(self.item(), id=server.OFFICER_KEY)], 'has_more': False},
            {'data': [self.item()], 'has_more': True, 'last_id': 'wrong-cursor'},
        ]
        for page in malformed:
            with self.subTest(page=page), patch.object(server, 'anthropic_request', return_value=(page, '')):
                rows, error = server.officer_catalog(force=True)
                self.assertIsNone(rows)
                self.assertTrue(error)
                self.assertNotIn(server.OFFICER_KEY, error)
        with patch.object(server, 'OFFICER_CATALOG_PAGES', 1), patch.object(server, 'anthropic_request', return_value=({'data': [self.item()], 'has_more': True, 'last_id': 'claude-fixture-a'}, '')):
            self.assertIsNone(server.officer_catalog(force=True)[0])
        with patch.object(server, 'OFFICER_CATALOG_MAX', 1), patch.object(server, 'anthropic_request', return_value=({'data': [self.item(), self.item('claude-fixture-b')], 'has_more': False}, '')):
            self.assertIsNone(server.officer_catalog(force=True)[0])
        with patch.object(server, 'anthropic_request', return_value=(None, 'provider echoed ' + server.OFFICER_KEY)):
            self.assertNotIn(server.OFFICER_KEY, server.officer_catalog(force=True)[1])

    def test_capabilities_are_positive_and_null_or_old_metadata_omits_effort(self):
        self.cache()
        self.assertEqual(server.officer_request_options(server.officer_model_snapshot())['output_config'], {'effort': 'low'})
        self.assertEqual(server.select_officer_model('claude-fixture-b')[0], 200)
        self.assertNotIn('output_config', server.officer_request_options(server.officer_model_snapshot()))
        server.OFFICER_CATALOG = {'key': '', 'at': 0, 'models': []}
        self.cache([self.item('claude-fixture-b', 'Fixture B', None)])
        self.assertNotIn('output_config', server.officer_request_options(server.officer_model_snapshot()))

    @unittest.skipIf(os.name == 'nt', 'POSIX Officer persistence boundary')
    def test_persistence_restart_missing_model_and_failed_write_no_change(self):
        self.cache()
        status, result = server.select_officer_model('claude-fixture-b')
        self.assertEqual(status, 200)
        self.assertEqual(result['selected_model'], 'claude-fixture-b')
        path = server.officer_model_path()
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        server.OFFICER_MODEL = 'claude-fixture-a'
        server.load_officer_model()
        self.assertEqual(server.OFFICER_MODEL, 'claude-fixture-b')
        server.OFFICER_CATALOG = {'key': '', 'at': 0, 'models': []}
        with patch.object(server, 'anthropic_request', side_effect=AssertionError('restart ask must not fetch')):
            self.assertEqual(server.officer_model_snapshot(), ('claude-fixture-b', False))
        models = self.cache([self.item()])
        view = server.officer_models_view(models)
        self.assertTrue(view['available'], 'catalog remains selectable even if saved model vanished')
        self.assertFalse(view['selection_available'])
        self.assertIn('unavailable', view['error'])
        with self.assertRaises(ValueError): server.officer_model_snapshot()
        with patch.object(server.os, 'replace', side_effect=OSError('synthetic write failure')):
            status, view = server.select_officer_model('claude-fixture-a')
        self.assertEqual(status, 503)
        self.assertEqual(view['selected_model'], 'claude-fixture-b')
        with open(path) as saved:
            self.assertEqual(json.load(saved)['model'], 'claude-fixture-b')
        self.assertEqual(server.select_officer_model('claude-fixture-a')[0], 200)

    @unittest.skipIf(os.name == 'nt', 'Officer settings are intentionally unavailable on Windows')
    def test_unsafe_persistence_refuses_symlink_mode_and_invalid_content(self):
        self.cache()
        os.mkdir(os.path.join(self.home, '.live-room'), 0o700)
        target = os.path.join(self.home, 'fictional-target')
        with open(target, 'w') as out: out.write('unchanged')
        path = server.officer_model_path()
        os.symlink(target, path)
        self.assertEqual(server.select_officer_model('claude-fixture-b')[0], 503)
        with open(target) as saved:
            self.assertEqual(saved.read(), 'unchanged')
        server.load_officer_model()
        self.assertTrue(server.OFFICER_MODEL_ERROR)
        with self.assertRaises(ValueError): server.officer_model_snapshot()
        os.unlink(path)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        os.write(fd, b'{"model":"invalid model"}'); os.close(fd)
        server.load_officer_model()
        self.assertTrue(server.OFFICER_MODEL_ERROR)
        os.chmod(path, 0o644)
        self.assertEqual(server.select_officer_model('claude-fixture-b')[0], 503)

    @unittest.skipIf(os.name == 'nt', 'POSIX Officer persistence boundary')
    def test_loader_rejects_key_shaped_or_exact_private_key_model_ids(self):
        models = self.cache()
        os.mkdir(os.path.join(self.home, '.live-room'), 0o700)
        path = server.officer_model_path()
        shaped = 'sk' + '-ant-' + 'x' * 32
        for value in (shaped, server.OFFICER_KEY):
            with self.subTest(value_kind='shaped' if value == shaped else 'exact'):
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                os.write(descriptor, json.dumps({'model': value}).encode())
                os.close(descriptor)
                server.load_officer_model()
                view = server.officer_models_view(models)
                self.assertTrue(server.OFFICER_MODEL_ERROR)
                self.assertEqual(view['selected_model'], 'claude-fixture-a')
                self.assertNotIn(value, json.dumps(view))
                with self.assertRaises(ValueError): server.officer_model_snapshot()

    def test_authenticated_http_contract_invalid_posts_and_unavailable_fetch(self):
        self.start_http()
        with patch.object(server, 'anthropic_request', side_effect=AssertionError('unauthorized fetch')):
            self.assertEqual(self.request('/officer/models', key=False)[0], 403)
            self.assertEqual(self.request('/officer/model', {'model': 'claude-fixture-b'}, key=False)[0], 403)
        self.cache()
        status, result = self.request('/officer/models')
        self.assertEqual(status, 200)
        self.assertEqual(result['models'][0], {'id': 'claude-fixture-a', 'display_name': 'Fixture A'})
        self.assertNotIn('capabilities', json.dumps(result))
        self.assertNotIn(server.OFFICER_KEY, json.dumps(result))
        self.assertEqual(self.request('/officer/model', {'model': 'unknown'})[0], 400)
        self.assertEqual(self.request('/officer/model', {'model': 'claude-fixture-b', 'key': 'ignored'})[0], 400)
        self.assertEqual(self.request('/officer/models?extra=1')[0], 400)
        status, result = self.request('/officer/model', {'model': 'claude-fixture-b'})
        self.assertEqual(status, 200)
        self.assertEqual(result['selected_model'], 'claude-fixture-b')
        server.OFFICER_CATALOG['at'] = time.monotonic() - server.OFFICER_CATALOG_TTL - 1
        with patch.object(server, 'anthropic_request', return_value=(None, 'secret provider error')):
            status, result = self.request('/officer/models')
        self.assertEqual(status, 503)
        self.assertFalse(result['complete'])
        self.assertEqual(result['selected_model'], 'claude-fixture-b')
        self.assertNotIn('secret provider error', json.dumps(result))

    def test_actual_get_transport_headers_redirect_size_and_timeout(self):
        calls = []
        release = threading.Event()
        class Response:
            status = 200
            payload = json.dumps({'data': [], 'has_more': False}).encode()
            slow = False
            def __init__(self): self.sent = False
            def read(self, size):
                if self.slow: release.wait(3)
                if self.sent: return b''
                self.sent = True
                return self.payload
            def close(self): release.set()
        class Connection:
            sock = None
            def request(self, method, path, body, headers): calls.append((method, path, body, headers))
            def getresponse(self): return Response()
            def close(self): release.set()
        with patch.object(server, 'CONNECT', return_value=Connection()):
            response, error = server.anthropic_request('GET', '/v1/models?limit=100', end=time.monotonic() + 3)
        self.assertFalse(error)
        self.assertEqual(response['data'], [])
        self.assertEqual(calls[0][:3], ('GET', '/v1/models?limit=100', None))
        self.assertEqual(calls[0][3]['x-api-key'], server.OFFICER_KEY)
        self.assertEqual(calls[0][3]['anthropic-version'], '2023-06-01')
        with patch.object(Response, 'status', 302), patch.object(server, 'CONNECT', return_value=Connection()):
            response, error = server.anthropic_request('GET', '/v1/models', end=time.monotonic() + 3)
        self.assertIsNone(response)
        self.assertIn('redirect', error)
        with patch.object(Response, 'payload', b'x' * 2000001), patch.object(server, 'CONNECT', return_value=Connection()):
            response, error = server.anthropic_request('GET', '/v1/models', end=time.monotonic() + 3)
        self.assertIsNone(response)
        self.assertIn('more than', error)
        release.clear()
        with patch.object(Response, 'slow', True), patch.object(server, 'CONNECT', return_value=Connection()):
            response, error = server.anthropic_request('GET', '/v1/models', end=time.monotonic() + 1.1)
        self.assertIsNone(response)
        self.assertIn('too long', error)
        deadline = time.monotonic() + 1
        while server.OFFICER_WORKERS[0] and time.monotonic() < deadline: time.sleep(0.005)
        self.assertEqual(server.OFFICER_WORKERS[0], 0)

    def test_cache_refresh_cannot_change_a_pinned_exchange(self):
        self.cache()
        calls = []
        def call(body, **kwargs):
            calls.append(copy.deepcopy(body))
            if len(calls) == 1:
                page = {'data': [self.item('claude-fixture-b', 'Fixture B', None)], 'has_more': False}
                with patch.object(server, 'anthropic_request', return_value=(page, '')):
                    self.assertIsNotNone(server.officer_catalog(force=True)[0])
                return {'content': [{'type': 'tool_use', 'id': 'read', 'name': 'list_agents', 'input': {}}], 'stop_reason': 'tool_use'}, ''
            return {'content': [{'type': 'text', 'text': 'Pinned answer.'}], 'stop_reason': 'end_turn'}, ''
        out = server.officer_exchange([{'role': 'user', 'content': 'What happened?'}], call, context={'scope': 'room'})
        self.assertEqual(out['text'], 'Pinned answer.')
        self.assertEqual([b['model'] for b in calls], ['claude-fixture-a', 'claude-fixture-a'])
        with self.assertRaises(ValueError): server.officer_model_snapshot()

    def test_one_snapshot_pins_dispatcher_and_all_tool_rounds(self):
        self.cache()
        server.MESSAGING = None
        calls = []
        opaque_thinking = {'type': 'thinking', 'thinking': 'synthetic retained assistant block', 'signature': 'fixture-signature'}
        def call(body, **kwargs):
            calls.append(copy.deepcopy(body))
            if len(calls) == 1:
                self.assertEqual(server.select_officer_model('claude-fixture-b')[0], 200)
                return {'content': [], 'stop_reason': 'end_turn'}, ''
            if len(calls) == 2:
                return {'content': [opaque_thinking, {'type': 'tool_use', 'id': 'read', 'name': 'list_agents', 'input': {}}], 'stop_reason': 'tool_use'}, ''
            self.assertEqual(body['messages'][-2]['content'][0], opaque_thinking)
            return {'content': [{'type': 'text', 'text': 'Recorded answer.'}], 'stop_reason': 'end_turn'}, ''
        result = server.officer_ask([{'role': 'user', 'content': 'What happened?'}], call=call, request_id=str(uuid.uuid4()), context={'scope': 'room'})
        self.assertEqual(result['text'], 'Recorded answer.')
        self.assertEqual([b['model'] for b in calls], ['claude-fixture-a'] * 3)
        self.assertTrue(all(b['output_config'] == {'effort': 'low'} for b in calls))
        following = []
        server.officer_ask([{'role': 'user', 'content': 'Next question'}], call=lambda body, **kwargs: (following.append(body) or {'content': [{'type': 'text', 'text': 'Next.'}], 'stop_reason': 'end_turn'}, ''), context={'scope': 'room'})
        self.assertEqual(following[0]['model'], 'claude-fixture-b')
        self.assertNotIn('output_config', following[0])


class ModelGuardMutationTests(unittest.TestCase):
    def test_guards_have_causal_regressions(self):
        cases = [
            ('officer_catalog', "query['after_id'] = cursor", "query['wrong_cursor'] = cursor", 'test_paginated_catalog_complete_cache_and_key_binding'),
            ('officer_request_options', 'if snapshot[1] else {}', 'if True else {}', 'test_capabilities_are_positive_and_null_or_old_metadata_omits_effort'),
            ('officer_exchange', 'officer_request_options(model_snapshot)', 'officer_request_options(officer_model_snapshot())', 'test_one_snapshot_pins_dispatcher_and_all_tool_rounds'),
        ]
        for attribute, before, after, method in cases:
            original = getattr(server, attribute)
            source = inspect.getsource(original)
            self.assertIn(before, source)
            try:
                exec(compile(source.replace(before, after, 1), '<isolated Officer model mutant>', 'exec'), server.__dict__)
                result = unittest.TestResult()
                OfficerModelTests(method).run(result)
                self.assertTrue(result.failures or result.errors, attribute + ' mutation must be caught')
            finally:
                setattr(server, attribute, original)


if __name__ == '__main__': unittest.main()
