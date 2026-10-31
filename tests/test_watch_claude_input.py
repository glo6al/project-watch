"""Real loopback fixtures only; never starts native Claude or installs hooks."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(Path(__file__).parent))
import claude_native_input as n
from test_claude_native_input import payload, decision
spec = importlib.util.spec_from_file_location('watch_claude_input', ROOT / 'scripts' / 'watch_claude_input.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class Fixture:
    def __init__(self, *, mode='answer', modify=None):
        self.credential = n.HookCredential(str(uuid.uuid4()), b'k' * 32)
        self.mode, self.modify = mode, modify
        self.seen = []
        self.started = threading.Event()
        self.release = threading.Event()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def handle(self):
                try:
                    super().handle()
                except (ConnectionResetError, BrokenPipeError):
                    pass

            def log_message(self, *args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers['Content-Length']))
                fixture.seen.append((self.path, raw))
                try:
                    if self.path == n.CHALLENGE_PATH:
                        data = fixture.protocol.issue_challenge(n.parse_json(raw), self.connection)
                        if fixture.mode == 'wrong_proof': data['proof'] = '0' * 64
                        data = n.json_bytes(data); sig = None
                    else:
                        inv = fixture.protocol.accept_invocation(raw, self.headers['X-Watch-Native-Signature'], self.connection)
                        fixture.started.set()
                        if fixture.mode == 'wait': fixture.release.wait(2)
                        req = n.parse_json(raw)
                        d = None if fixture.mode == 'fallthrough' else decision()
                        if fixture.mode == 'form': d = {'kind': 'mcp_form_accept', 'content': {'name': '', 'enabled': False, 'count': 0}}
                        data, sig = fixture.protocol.response(inv, req['client_nonce'], d)
                        if fixture.mode == 'bad_signature': sig = '0' * 64
                        if fixture.modify:
                            data, sig = fixture.modify(data, sig, fixture, req, inv)
                    self.send_response(302 if fixture.mode == 'redirect' else 200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(data)))
                    if sig: self.send_header('X-Watch-Native-Signature', sig)
                    if fixture.mode == 'duplicate_header' and sig: self.send_header('X-Watch-Native-Signature', sig)
                    self.end_headers()
                    self.wfile.write(data)
                    self.wfile.flush()
                except (OSError, n.InputError):
                    self.close_connection = True
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.protocol = n.HookProtocol(self.credential, self.port)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.02), daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(1)

    def invoke(self, p=None, **kwargs):
        out = io.BytesIO()
        result = helper.run_hook(io.BytesIO(n.json_bytes(p or payload())), out, enabled=True,
                                 credential_path='/fictional/injected-credential', port=self.port,
                                 credential_loader=lambda _: self.credential, **kwargs)
        return result, out.getvalue()

    def resign(self, body, req):
        raw = n.json_bytes(body)
        return raw, n.mac(self.credential, 'watch-native-input-response-v1', self.port,
                          self.protocol.run_id, req['client_nonce'], 200, n.digest(raw))


class HelperTests(unittest.TestCase):
    def test_disabled_no_read_credentials_network_or_stdout(self):
        class Untouchable:
            def read(self, *args): self.fail('should never read')
        out = io.BytesIO()
        self.assertEqual(helper.run_hook(Untouchable(), out), 'disabled')
        self.assertEqual(out.getvalue(), b'')

    def test_command_line_cannot_activate(self):
        proc = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'watch_claude_input.py'), '--enable'],
                              input=b'native-private-sentinel', capture_output=True, timeout=5)
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, b'', b''))

    def test_real_loopback_original_callback_and_socket_pin(self):
        made = []
        class Tracked(helper.http.client.HTTPConnection):
            def connect(self):
                super().connect()
                made.append(self)
        with Fixture() as f:
            result, output = f.invoke(connection_factory=Tracked)
            self.assertEqual(result, 'returned_to_hook_unconfirmed')
            self.assertEqual(len(f.seen), 2)
            self.assertEqual(output, n.HookInvocation.create(payload(), str(uuid.uuid4()), time.monotonic() + 60).native_output(decision()))
            self.assertFalse(made[0].auto_open)

    def test_real_unproved_listener_receives_no_question(self):
        with Fixture(mode='wrong_proof') as f:
            result, output = f.invoke()
            self.assertEqual(output, b'')
            self.assertEqual(len(f.seen), 1)
            self.assertNotIn(b'Choose', f.seen[0][1])
            self.assertNotIn(b'kkkk', f.seen[0][1])

    def test_redirect_never_followed_and_has_empty_stdout(self):
        with Fixture(mode='redirect') as f:
            self.assertEqual(f.invoke(), ('relinquished_to_native', b''))
            self.assertEqual(len(f.seen), 1)

    def test_verified_fallthrough_prints_nothing(self):
        with Fixture(mode='fallthrough') as f:
            self.assertEqual(f.invoke(), ('relinquished_to_native', b''))

    def test_real_form_has_separate_native_result(self):
        with Fixture(mode='form') as f:
            result, raw = f.invoke(payload('form'))
            self.assertEqual(result, 'returned_to_hook_unconfirmed')
            self.assertEqual(n.parse_json(raw)['hookSpecificOutput'], {'hookEventName': 'Elicitation', 'action': 'accept', 'content': {'name': '', 'enabled': False, 'count': 0}})

    def test_bad_signature_and_duplicate_header_have_empty_stdout(self):
        for mode in ('bad_signature', 'duplicate_header'):
            with self.subTest(mode=mode), Fixture(mode=mode) as f:
                self.assertEqual(f.invoke(), ('relinquished_to_native', b''))

    def test_signed_binding_swaps_and_wrong_method_rejected(self):
        for field in ('invocation_id', 'binding_digest', 'payload_digest', 'run_id', 'decision'):
            def modify(raw, sig, fixture, req, inv):
                body = n.parse_json(raw)
                if field == 'decision': body[field] = {'kind': 'mcp_form_cancel'}
                else: body[field] = str(uuid.uuid4()) if field.endswith('_id') else '0' * 64
                return fixture.resign(body, req)
            with self.subTest(field=field), Fixture(modify=modify) as f:
                self.assertEqual(f.invoke(), ('relinquished_to_native', b''))

    def test_changed_original_question_rejected_even_valid_response_signature(self):
        def modify(raw, sig, fixture, req, inv):
            changed = inv.payload
            changed['tool_input']['questions'][0]['question'] = 'Different native invocation?'
            foreign = n.HookInvocation.create(changed, inv.invocation_id, inv.deadline)
            return fixture.protocol.response(foreign, req['client_nonce'], decision())
        with Fixture(modify=modify) as f:
            self.assertEqual(f.invoke(), ('relinquished_to_native', b''))

    def test_expired_and_parent_cancelled_before_any_output(self):
        for reason in ('deadline', 'cancel', 'parent'):
            with self.subTest(reason=reason), Fixture(mode='wait') as f:
                cancelled = threading.Event()
                done = []
                kwargs = {'timeout': .1} if reason == 'deadline' else {'cancelled': cancelled.is_set} if reason == 'cancel' else {'parent_alive': lambda: not cancelled.is_set()}
                t = threading.Thread(target=lambda: done.append(f.invoke(**kwargs)))
                t.start()
                self.assertTrue(f.started.wait(1))
                cancelled.set()
                t.join(1)
                self.assertFalse(t.is_alive())
                self.assertEqual(done, [('relinquished_to_native', b'')])
                self.assertEqual(len(f.seen), 2)  # one attempt only, never re-challenge/resend

    def test_invalid_or_unsupported_input_never_contacts_listener(self):
        for p in (dict(payload(), hook_event_name='PermissionRequest'), dict(payload(), tool_name='Bash')):
            with Fixture() as f:
                self.assertEqual(f.invoke(p), ('relinquished_to_native', b''))
                self.assertEqual(f.seen, [])

    def test_reject_malformed_truncated_and_redirect_http_response(self):
        for mode in ('wrong_type', 'bad_json', 'partial', 'oversize'):
            def modify(raw, sig, fixture, req, inv):
                if mode == 'wrong_type': raw = b'[]'
                elif mode == 'bad_json': raw = b'{'
                elif mode == 'partial': raw = raw[:-5]
                else: raw = b'x' * (n.MAX_RESPONSE + 1)
                return raw, sig
            with self.subTest(mode=mode), Fixture(modify=modify) as f:
                self.assertEqual(f.invoke(), ('relinquished_to_native', b''))

    def test_http_header_rules(self):
        from email.message import Message
        for headers in ([('Content-Length', '2'), ('Content-Type', 'application/json'), ('Transfer-Encoding', 'chunked')],
                        [('Content-Length', '2'), ('Content-Length', '2'), ('Content-Type', 'application/json')],
                        [('Content-Length', '999999'), ('Content-Type', 'application/json')],
                        [('Content-Length', '2'), ('Content-Type', 'text/plain')]):
            class Response:
                def __init__(self):
                    self.headers = Message()
                    for k, v in headers: self.headers[k] = v
                def read(self, count): return b'{}'
            with self.assertRaises(n.InputError): helper._response(Response(), 128)

    def test_proof_close_listener_swap_sends_zero_invocation_bytes(self):
        credential = n.HookCredential(str(uuid.uuid4()), b'k' * 32)
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', 0)); listener.listen(1)
        port = listener.getsockname()[1]
        protocol = n.HookProtocol(credential, port)
        rogue_received, errors, ready = [], [], threading.Event()
        def serve():
            rogue = None
            conn = None
            try:
                conn, _ = listener.accept()
                stream = conn.makefile('rb')
                request_line = stream.readline()
                headers = {}
                while True:
                    line = stream.readline()
                    if line == b'\r\n': break
                    key, value = line.decode().split(':', 1); headers[key.lower()] = value.strip()
                request = n.parse_json(stream.read(int(headers['content-length'])))
                proof = protocol.issue_challenge(request, conn)
                body = n.json_bytes(proof)
                # Replace listening socket before publishing the real server proof.
                listener.close()
                rogue = socket.socket(); rogue.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                rogue.bind(('127.0.0.1', port)); rogue.listen(1); rogue.settimeout(.25)
                ready.set()
                conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: ' + str(len(body)).encode() + b'\r\nConnection: close\r\n\r\n' + body)
                stream.close(); conn.close(); conn = None
                try:
                    incoming, _ = rogue.accept()
                    incoming.settimeout(.2)
                    with incoming: rogue_received.append(incoming.recv(65536))
                except socket.timeout:
                    pass
                self.assertIn(b'hook/challenge', request_line)
            except Exception as exc:
                errors.append(type(exc).__name__)
            finally:
                if conn: conn.close()
                if rogue: rogue.close()
                listener.close()
        t = threading.Thread(target=serve, daemon=True); t.start()
        stdout = io.BytesIO()
        result = helper.run_hook(io.BytesIO(n.json_bytes(payload())), stdout, enabled=True,
                                 credential_path='/fictional/injected-credential', port=port,
                                 credential_loader=lambda _: credential)
        t.join(2)
        self.assertFalse(t.is_alive())
        self.assertTrue(ready.is_set())
        self.assertEqual(errors, [])
        self.assertEqual(result, 'relinquished_to_native')
        self.assertEqual(stdout.getvalue(), b'')
        self.assertEqual(rogue_received, [])


@unittest.skipIf(os.name == 'nt', 'POSIX mode/dirfd credentials unsupported on Windows; transport tests still run')
class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name).resolve(); self.folder.chmod(0o700)
        self.path = self.folder / 'fixture-credential.json'
        self.data = {'version': 1, 'purpose': n.PURPOSE, 'credential_id': str(uuid.uuid4()), 'secret': 'ab' * 32}
        self.path.write_bytes(n.json_bytes(self.data)); self.path.chmod(0o600)

    def tearDown(self):
        self.temp.cleanup()

    def test_injected_credential_only_and_no_secret_repr(self):
        got = n.load_credential(self.path)
        self.assertEqual(got.secret, bytes.fromhex(self.data['secret']))
        self.assertNotIn(self.data['secret'], repr(got))

    def test_credential_scope_mode_symlink_link_and_directory_rejected(self):
        original = self.path.read_bytes()
        for change in ('purpose', 'mode', 'symlink', 'hardlink', 'dir_mode'):
            with self.subTest(change=change):
                if change == 'purpose': self.path.write_bytes(n.json_bytes(dict(self.data, purpose='watch-peer')))
                if change == 'mode': self.path.chmod(0o644)
                if change == 'symlink':
                    target = self.folder / 'target'; self.path.rename(target); self.path.symlink_to(target)
                if change == 'hardlink': os.link(self.path, self.folder / 'other')
                if change == 'dir_mode': self.folder.chmod(0o755)
                with self.assertRaises(n.InputError): n.load_credential(self.path)
                if self.path.is_symlink(): self.path.unlink(); (self.folder / 'target').rename(self.path)
                if (self.folder / 'other').exists(): (self.folder / 'other').unlink()
                self.folder.chmod(0o700); self.path.write_bytes(original); self.path.chmod(0o600)

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'FIFO not supported')
    def test_special_file_refused_without_blocking_before_watchdog(self):
        self.path.unlink()
        os.mkfifo(self.path, 0o600)
        # Child deadline makes a missing O_NONBLOCK regression fail without hanging.
        code = 'import sys; sys.path.insert(0, sys.argv[1]); import claude_native_input as n; n.load_credential(sys.argv[2])'
        proc = subprocess.run([sys.executable, '-c', code, str(ROOT / 'src'), str(self.path)], capture_output=True, timeout=2)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(b'InputError', proc.stderr)

    def test_parent_symlink_and_nonexistent_credential_rejected(self):
        link = self.folder / 'link'; link.symlink_to(self.folder, target_is_directory=True)
        with self.assertRaises(n.InputError): n.load_credential(link / self.path.name)
        with self.assertRaises(n.InputError): n.load_credential(self.folder / 'missing')


if __name__ == '__main__':
    unittest.main()
