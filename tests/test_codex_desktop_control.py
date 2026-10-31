#!/usr/bin/env python3
"""Bounded fake local IPC only. Never connects to a real Codex desktop."""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import codex_desktop_control as control


def ident():
    return str(uuid.uuid4())


class FakeDesktop:
    def __init__(self, path, behavior='success'):
        self.path = path
        self.behavior = behavior
        self.client, self.owner, self.turn = ident(), ident(), ident()
        self.messages = []
        self.errors = []
        self.stop = threading.Event()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        os.chmod(path, 0o600)
        self.listener.listen(1)
        self.listener.settimeout(0.2)
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    @staticmethod
    def read(sock, size):
        result = b''
        while len(result) < size:
            chunk = sock.recv(size - len(result))
            if not chunk:
                raise EOFError()
            result += chunk
        return result

    @staticmethod
    def send(sock, message):
        data = json.dumps(message).encode()
        sock.sendall(struct.pack('<I', len(data)) + data)

    def run(self):
        try:
            while not self.stop.is_set():
                try:
                    sock, _ = self.listener.accept()
                    break
                except socket.timeout:
                    pass
            else:
                return
            with sock:
                sock.settimeout(1)
                while not self.stop.is_set():
                    try:
                        size = struct.unpack('<I', self.read(sock, 4))[0]
                        message = json.loads(self.read(sock, size))
                    except (EOFError, socket.timeout):
                        return
                    self.messages.append(message)
                    method = message.get('method')
                    if method == 'initialize':
                        if self.behavior == 'pre_timeout':
                            self.stop.wait(1); return
                        if self.behavior == 'oversize':
                            sock.sendall(struct.pack('<I', control._MAX_FRAME + 1)); return
                        if self.behavior == 'flood':
                            for _ in range(control._MAX_FRAMES + 1):
                                self.send(sock, {'type': 'broadcast', 'params': {'private': 'ignored'}})
                            return
                        result, handler = {'clientId': self.client}, self.client
                    elif method == 'thread-owner-discovery':
                        result, handler = {'supportsUntrustedAppInput': True}, self.owner
                        if self.behavior == 'no_owner':
                            self.send(sock, {'type': 'response', 'requestId': message['requestId'],
                                             'resultType': 'error', 'error': 'private path and message'})
                            return
                    elif method in ('thread-follower-steer-turn', 'thread-follower-interrupt-turn'):
                        if self.behavior == 'post_timeout':
                            self.stop.wait(1); return
                        if self.behavior == 'disconnect':
                            return
                        if self.behavior == 'version_reject':
                            self.send(sock, {'type': 'response', 'requestId': message['requestId'],
                                             'resultType': 'error', 'error': 'request-version-mismatch'})
                            return
                        handler = self.owner if self.behavior != 'wrong_owner' else ident()
                        if method == 'thread-follower-steer-turn':
                            result = {'result': {'turnId': self.turn}}
                        else:
                            turn = message['params']['expectedTurnId']
                            result = {'ok': True, 'interruptedTurnId': turn}
                            if self.behavior == 'null': result['interruptedTurnId'] = None
                            if self.behavior == 'missing': del result['interruptedTurnId']
                            if self.behavior == 'wrong_turn': result['interruptedTurnId'] = ident()
                            if self.behavior == 'partial': result['goalPauseError'] = 'private error'
                    else:
                        raise AssertionError('unexpected outbound method')
                    response = {'type': 'response', 'requestId': message['requestId'],
                                'resultType': 'success', 'method': method,
                                'handledByClientId': handler, 'result': result}
                    if method.startswith('thread-follower-'):
                        if self.behavior == 'wrong_request': response['requestId'] = ident()
                        if self.behavior == 'wrong_method': response['method'] = 'turn/start'
                    self.send(sock, response)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            if not self.stop.is_set(): self.errors.append(type(error).__name__)

    def close(self):
        self.stop.set()
        self.listener.close()
        self.thread.join(2)


@unittest.skipIf(os.name == 'nt', 'Unix desktop transport')
class DesktopControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='watchctl-', dir='/tmp')
        self.base = Path(self.temp.name).resolve()
        os.chmod(self.base, 0o700)
        self.endpoint = self.base / 'ipc.sock'
        self.target, self.turn = ident(), ident()
        self.fake = None
        self.stack = []
        for mock in (patch.object(control, '_verify_build'), patch.object(control, '_endpoint', return_value=self.endpoint),
                     patch.object(control, '_TIMEOUT', 0.25)):
            mock.start(); self.stack.append(mock)

    def tearDown(self):
        if self.fake:
            self.fake.close()
            self.assertFalse(self.fake.errors)
        for mock in reversed(self.stack): mock.stop()
        self.temp.cleanup()

    def run_action(self, action='interrupt', behavior='success'):
        self.fake = FakeDesktop(self.endpoint, behavior)
        return control.execute(action, self.target, instruction='Adjust the current answer.' if action == 'steer' else '',
                               expected_turn_id=self.turn, cwd=str(self.base))

    def test_interrupt_exact_target_and_turn(self):
        self.assertEqual(self.run_action()['state'], 'interrupted')
        outgoing = self.fake.messages[-1]
        self.assertEqual(outgoing['method'], 'thread-follower-interrupt-turn')
        self.assertEqual(outgoing['version'], 4)
        self.assertEqual(outgoing['targetClientId'], self.fake.owner)
        self.assertEqual(outgoing['params'], {'conversationId': self.target, 'expectedTurnId': self.turn, 'mode': 'user-stop'})
        self.assertEqual(len(self.fake.messages), 3)

    def test_steer_proven_shape_no_permission_or_tool_output(self):
        self.assertEqual(self.run_action('steer')['state'], 'acknowledged')
        outgoing = self.fake.messages[-1]
        self.assertEqual(outgoing['method'], 'thread-follower-steer-turn')
        self.assertEqual(outgoing['version'], 1)
        self.assertEqual(outgoing['targetClientId'], self.fake.owner)
        self.assertNotIn('expectedTurnId', outgoing['params'])
        self.assertNotIn('toolOutput', outgoing['params'])
        self.assertNotIn('serviceTier', outgoing['params'])
        self.assertEqual(outgoing['params']['input'][0]['text'], 'Adjust the current answer.')
        restore = outgoing['params']['restoreMessage']
        self.assertEqual(restore['context']['prompt'], restore['text'])
        self.assertEqual(outgoing['params']['clientUserMessageId'], restore['id'])

    def test_wrong_owner_unknown(self):
        self.assertEqual(self.run_action('steer', 'wrong_owner')['state'], 'unknown')
        self.assertEqual(len(self.fake.messages), 3)

    def test_wrong_turn_unknown(self):
        self.assertEqual(self.run_action(behavior='wrong_turn')['state'], 'unknown')

    def test_missing_turn_unknown(self):
        self.assertEqual(self.run_action(behavior='missing')['state'], 'unknown')

    def test_explicit_null_no_op(self):
        self.assertEqual(self.run_action(behavior='null')['state'], 'no_op')

    def test_partial_effect_unknown(self):
        result = self.run_action(behavior='partial')
        self.assertEqual(result['state'], 'unknown')
        self.assertNotIn('private', result['error'])

    def test_wrong_response_id_unknown(self):
        self.assertEqual(self.run_action(behavior='wrong_request')['state'], 'unknown')

    def test_wrong_response_method_unknown(self):
        self.assertEqual(self.run_action(behavior='wrong_method')['state'], 'unknown')

    def test_version_rejection_unknown_no_retry(self):
        self.assertEqual(self.run_action(behavior='version_reject')['state'], 'unknown')
        self.assertEqual(len(self.fake.messages), 3)

    def test_pre_send_timeout_failed(self):
        self.assertEqual(self.run_action(behavior='pre_timeout')['state'], 'failed')
        self.assertEqual(len(self.fake.messages), 1)

    def test_post_send_timeout_unknown(self):
        self.assertEqual(self.run_action(behavior='post_timeout')['state'], 'unknown')
        self.assertEqual(len(self.fake.messages), 3)

    def test_disconnect_unknown(self):
        self.assertEqual(self.run_action(behavior='disconnect')['state'], 'unknown')

    def test_no_owner_failed_safe_message(self):
        result = self.run_action(behavior='no_owner')
        self.assertEqual(result['state'], 'failed')
        self.assertNotIn('private', result['error'])
        self.assertNotIn(self.target, str(result))
        self.assertEqual(len(self.fake.messages), 2)

    def test_oversize_frame_failed(self):
        self.assertEqual(self.run_action(behavior='oversize')['state'], 'failed')

    def test_broadcast_flood_bounded(self):
        self.assertEqual(self.run_action(behavior='flood')['state'], 'failed')

    def test_private_endpoint_required(self):
        self.fake = FakeDesktop(self.endpoint)
        os.chmod(self.endpoint, 0o666)
        self.assertFalse(control.preflight()['available'])
        self.assertEqual(control.execute('interrupt', self.target, expected_turn_id=self.turn)['state'], 'failed')
        self.assertEqual(self.fake.messages, [])

    def test_symlink_endpoint_rejected(self):
        self.fake = FakeDesktop(self.endpoint)
        link = self.base / 'alias.sock'; link.symlink_to(self.endpoint)
        with patch.object(control, '_endpoint', return_value=link):
            self.assertFalse(control.preflight()['available'])

    def test_validation_before_connect(self):
        for args in [('queue', self.target), ('steer', self.target, ''), ('interrupt', self.target), ('steer', 'bad-id', 'x')]:
            self.assertEqual(control.execute(*args)['state'], 'failed')
        with patch.object(control, '_verify_build', side_effect=RuntimeError('private details')):
            result = control.execute('interrupt', self.target, expected_turn_id=self.turn)
            self.assertEqual(result['state'], 'failed')
            self.assertNotIn('private', result['error'])


@unittest.skipIf(os.name == 'nt', 'POSIX file safeguards')
class BuildGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = Path(self.temp.name)
        (self.app / 'Contents/Resources').mkdir(parents=True)
        self.info = self.app / 'Contents/Info.plist'
        self.archive = self.app / 'Contents/Resources/app.asar'
        self.info.write_bytes(plistlib.dumps({'CFBundleShortVersionString': control._VERSION, 'CFBundleVersion': control._BUILD}))
        self.archive.write_bytes(b'test build')
        self.patches = [patch.object(control, '_APP', self.app), patch.object(control.sys, 'platform', 'darwin'),
                        patch.object(control, '_ASAR_SHA256', hashlib.sha256(b'test build').hexdigest())]
        for mock in self.patches: mock.start()
        control._build_cache = None

    def tearDown(self):
        for mock in reversed(self.patches): mock.stop()
        control._build_cache = None
        self.temp.cleanup()

    def test_hash_gate_and_cache_invalidation(self):
        control._verify_build()
        control._verify_build()
        self.archive.write_bytes(b'other build')
        with self.assertRaises(Exception): control._verify_build()

    def test_version_and_build_gate(self):
        for key in ('CFBundleShortVersionString', 'CFBundleVersion'):
            data = {'CFBundleShortVersionString': control._VERSION, 'CFBundleVersion': control._BUILD}
            data[key] = 'unsupported'
            self.info.write_bytes(plistlib.dumps(data))
            with self.assertRaises(Exception): control._verify_build()

    def test_platform_gate(self):
        with patch.object(control.sys, 'platform', 'linux'):
            with self.assertRaises(Exception): control._verify_build()

    def test_archive_size_bound(self):
        with patch.object(control, '_MAX_ASAR', 2):
            with self.assertRaises(Exception): control._verify_build()


if __name__ == '__main__':
    unittest.main()
