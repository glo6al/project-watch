"""Read adapter contract and real framing over a fake socket; no native calls."""
import copy
import json
from pathlib import Path
import socket
import struct
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import codex_model_settings as model
import codex_desktop_control as transport

TARGET = '11111111-1111-4111-8111-111111111111'
CLIENT = '22222222-2222-4222-8222-222222222222'
OWNER = '33333333-3333-4333-8333-333333333333'
OTHER = '44444444-4444-4444-8444-444444444444'
UNSET = object()
SETTINGS = {'model': 'fictional-model', 'reasoningEffort': None, 'mode': 'default', 'modelProvider': 'fictional-provider'}


class Wire:
    def __init__(self, settings=UNSET, behavior=None):
        self.settings = copy.deepcopy(SETTINGS if settings is UNSET else settings)
        self.behavior = behavior
        self.messages = []
        self.data = b''
        self.closed = False

    def settimeout(self, seconds):
        pass

    def connect(self, path):
        pass

    def close(self):
        self.closed = True

    def sendall(self, raw):
        n = struct.unpack('<I', raw[:4])[0]
        assert n == len(raw) - 4
        message = json.loads(raw[4:])
        self.messages.append(message)
        method = message['method']
        if self.behavior == 'timeout':
            return
        if self.behavior == 'oversize':
            self.data = struct.pack('<I', transport._MAX_FRAME + 1)
            return
        if self.behavior == 'flood':
            body = json.dumps({'type': 'broadcast'}).encode()
            self.data = (struct.pack('<I', len(body)) + body) * (transport._MAX_FRAMES + 1)
            return
        if method == 'initialize':
            result, owner = {'clientId': CLIENT}, CLIENT
        elif method == 'thread-owner-discovery':
            result, owner = {'supportsUntrustedAppInput': True}, OWNER
            if self.behavior == 'self_owner':
                owner = CLIENT
            if self.behavior == 'missing_capability':
                result = {}
        elif method == 'thread-follower-read-model-settings':
            result, owner = {'settings': self.settings}, OWNER
        else:
            raise AssertionError('Not a read method: ' + method)
        response = {'type': 'response', 'requestId': message['requestId'], 'method': method,
                    'resultType': 'success', 'handledByClientId': owner, 'result': result}
        if method == 'thread-follower-read-model-settings':
            if self.behavior == 'wrong_owner': response['handledByClientId'] = OTHER
            if self.behavior == 'wrong_request': response['requestId'] = OTHER
            if self.behavior == 'wrong_method': response['method'] = 'turn/start'
            if self.behavior == 'native_error': response.update(resultType='error', error='private details')
        body = json.dumps(response).encode()
        self.data += struct.pack('<I', len(body)) + body

    def recv(self, n):
        if not self.data:
            raise socket.timeout()
        data, self.data = self.data[:n], self.data[n:]
        return data


class ModelSettingsTests(unittest.TestCase):
    def setUp(self):
        self.family = object()  # A fake-only family, including on hosts without Unix sockets.
        family = patch.object(transport.socket, 'AF_UNIX', self.family, create=True)
        family.start()
        self.addCleanup(family.stop)

    def run_read(self, settings=UNSET, behavior=None, target=TARGET, host='local', build_error=False):
        self.wire = Wire(settings, behavior)
        with patch.object(transport, '_verify_build', side_effect=RuntimeError('private') if build_error else None) as build, \
             patch.object(transport, '_verify_endpoint', return_value=['fixture']), \
             patch.object(transport.socket, 'socket', return_value=self.wire) as socket_factory:
            result = model.read_settings(target, host_id=host)
            if socket_factory.called:
                socket_factory.assert_called_once_with(self.family, socket.SOCK_STREAM)
            self.build_calls = build.call_count
        return result

    def test_read_exact_owner_target_and_no_write_capability(self):
        r = self.run_read()
        self.assertEqual(r['state'], 'observed')
        self.assertEqual(r['settings']['effort'], None)
        self.assertEqual(r['settings']['model'], 'fictional-model')
        self.assertEqual(r['settings']['source'], 'native_desktop_settings')
        self.assertIsInstance(r['settings']['observedAt'], float)
        self.assertEqual(r['capability'], {'read': True, 'selectModel': False, 'selectEffort': False,
                                         'reason': 'catalog_unavailable', 'scope': 'next_turn'})
        self.assertIsNone(r['catalog'])
        self.assertEqual(len(self.wire.messages), 3)
        for m in self.wire.messages[1:]: self.assertEqual(m['params']['conversationId'], TARGET)
        self.assertEqual(self.wire.messages[-1]['targetClientId'], OWNER)
        self.assertEqual(self.wire.messages[-1]['version'], 1)
        self.assertTrue(self.wire.closed)

    def test_null_and_plan_modes_preserved(self):
        for mode in (None, 'plan', 'default'):
            with self.subTest(mode=mode):
                s = dict(SETTINGS, mode=mode, reasoningEffort='high', modelProvider=None)
                r = self.run_read(s)
                self.assertEqual(r['state'], 'observed')
                self.assertEqual(r['settings']['mode'], mode)
                self.assertEqual(r['settings']['effort'], 'high')
                self.assertIsNone(r['settings']['modelProvider'])

    def test_invalid_target_no_connection_or_build_read(self):
        for target in (None, True, [], 'not-a-uuid', TARGET.upper().replace('1', 'A', 1)):
            r = self.run_read(target=target)
            self.assertEqual(r['state'], 'unavailable')
            self.assertEqual(self.build_calls, 0)
            self.assertEqual(self.wire.messages, [])

    def test_unsupported_host_and_build_fail_closed(self):
        for kwargs, reason in [({'host': 'remote'}, 'unsupported_host'), ({'build_error': True}, 'incompatible_desktop')]:
            r = self.run_read(**kwargs)
            self.assertEqual(r['capability']['reason'], reason)
            self.assertEqual(self.wire.messages, [])
            self.assertIsNone(r['settings'])

    def test_native_identity_framing_and_timeout_failures(self):
        for behavior in ('wrong_owner', 'wrong_request', 'wrong_method', 'self_owner', 'missing_capability',
                         'native_error', 'timeout', 'oversize', 'flood'):
            with self.subTest(behavior=behavior):
                r = self.run_read(behavior=behavior)
                self.assertEqual(r['state'], 'unavailable')
                self.assertFalse(r['capability']['read'])
                self.assertIsNone(r['settings'])
                self.assertTrue(self.wire.closed)
                self.assertNotIn('private', json.dumps(r))

    def test_missing_null_malformed_and_oversize_fields_refused(self):
        values = [None, [], 'settings', False]
        for key in SETTINGS:
            s = dict(SETTINGS); del s[key]; values.append(s)
        for key, bad in [('model', None), ('model', ''), ('model', 'a' * 257), ('model', True),
                         ('model', ' hidden'), ('model', 'bad\ntext'), ('reasoningEffort', []),
                         ('reasoningEffort', 4), ('reasoningEffort', 'x' * 65), ('reasoningEffort', ''),
                         ('modelProvider', {}), ('modelProvider', 'x' * 129), ('mode', 'unknown'), ('mode', False)]:
            values.append(dict(SETTINGS, **{key: bad}))
        for value in values:
            with self.subTest(value=value):
                r = self.run_read(value)
                self.assertEqual(r['capability']['reason'], 'invalid_native_settings')
                self.assertIsNone(r['settings'])

    def test_import_has_no_desktop_effects(self):
        import importlib
        with patch.object(transport, '_verify_build') as build, \
             patch.object(transport.socket, 'socket') as connect:
            importlib.reload(model)
            build.assert_not_called()
            connect.assert_not_called()

    def test_total_byte_budget_is_inherited(self):
        with patch.object(transport, '_MAX_TOTAL', 10):
            r = self.run_read()
        self.assertEqual(r['state'], 'unavailable')
        self.assertIsNone(r['settings'])
        self.assertTrue(self.wire.closed)

    def test_native_extra_data_not_returned(self):
        r = self.run_read(dict(SETTINGS, resumeState={'private': 'discard'}, threadSource='discard'))
        self.assertNotIn('discard', json.dumps(r))

    def test_read_transport_blocks_writes_wrong_target_and_versions(self):
        with patch.object(transport, '_verify_endpoint', return_value=[]), \
             patch.object(transport.socket, 'socket', return_value=Wire()) as socket_factory:
            c = model._ReadConnection(Path('/fixture'), TARGET)
            socket_factory.assert_called_once_with(self.family, socket.SOCK_STREAM)
            for method, params, version in [
                ('thread-follower-update-thread-settings', {'conversationId': TARGET}, 2),
                ('thread-follower-read-model-settings', {'conversationId': OTHER}, 1),
                ('thread-follower-read-model-settings', {'conversationId': TARGET}, 2),
                ('thread-follower-read-model-settings', {'conversationId': TARGET}, True),
                ('thread-owner-discovery', {'hostId': 'remote', 'conversationId': TARGET}, 1),
                ('turn/start', {}, 1),
            ]:
                with self.subTest(method=method, params=params), self.assertRaises(transport._Invalid):
                    c.request(method, params, CLIENT, version, OWNER)
            with self.assertRaises(transport._Invalid): c._send({'type': 'broadcast', 'method': 'model/change'})
            self.assertEqual(socket_factory.return_value.messages, [])
            c.close()


if __name__ == '__main__':
    unittest.main()
