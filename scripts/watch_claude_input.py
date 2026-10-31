#!/usr/bin/env python3
"""Disabled native-input helper. Import run_hook for explicitly enabled fixtures.

There is intentionally no CLI activation switch, default credential path, hook
installer or credential creation. Native cancellation/privacy/rotation proofs and
root-owned integration must precede a production activation path.
"""
import http.client
import os
from pathlib import Path
import select
import socket
import sys
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import claude_native_input as native


def _response(response, limit):
    lengths = response.headers.get_all('Content-Length', [])
    if (len(lengths) != 1 or not lengths[0].isdigit() or
            response.headers.get_all('Transfer-Encoding') or
            len(response.headers.get_all('Content-Type', [])) != 1 or
            response.headers.get('Content-Type') != 'application/json'):
        native.reject()
    length = int(lengths[0])
    if not 0 < length <= limit:
        native.reject()
    raw = response.read(length + 1)
    if len(raw) != length:
        native.reject()
    return raw


def _same_socket(connection, pinned):
    if connection.sock is not pinned or pinned.fileno() < 0:
        native.reject()
    # HTTPConnection can retain a locally-open socket after the peer sent FIN.
    ready, _, _ = select.select([pinned], [], [], 0)
    if ready and pinned.recv(1, socket.MSG_PEEK) == b'':
        native.reject()


def exchange(invocation, credential, port, *, clock=time.monotonic,
             cancelled=lambda: False, connection_factory=http.client.HTTPConnection,
             nonce_factory=lambda: os.urandom(32).hex()):
    """One proved socket and one invocation write, without mutation retries.

    The watchdog only cancels this helper's own socket. Native parent/session
    cancellation still requires a live proof; no process is signalled here.
    """
    if type(port) is not int or not 1 <= port <= 65535:
        native.reject()
    conn = connection_factory('127.0.0.1', port, timeout=max(.001, invocation.deadline - clock()))
    stop = threading.Event()
    guard = None
    pinned = None
    try:
        if cancelled() or clock() >= invocation.deadline:
            native.reject()
        conn.connect()
        pinned = conn.sock
        if pinned is None:
            native.reject()
        conn.auto_open = False  # A connection object must never reconnect after proof.

        def watch():
            while not stop.wait(.025):
                if cancelled() or clock() >= invocation.deadline:
                    try:
                        pinned.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    try:
                        pinned.close()
                    except OSError:
                        pass
                    return
        guard = threading.Thread(target=watch, daemon=True)
        guard.start()
        request = native.challenge_request(credential, nonce_factory(), invocation.invocation_id)
        headers = {'Content-Type': 'application/json', 'Host': '127.0.0.1:' + str(port)}
        conn.request('POST', native.CHALLENGE_PATH, native.json_bytes(request), headers)
        response = conn.getresponse()
        if response.status != 200 or response.will_close:
            native.reject()
        challenge = native.parse_json(_response(response, 2048), 2048)
        native.verify_challenge(credential, request, challenge, port)
        _same_socket(conn, pinned)
        if cancelled() or clock() >= invocation.deadline:
            native.reject()
        raw, signature = native.invocation_request(credential, challenge, request, invocation, port)
        conn.request('POST', native.INVOCATION_PATH, raw, dict(headers, **{'X-Watch-Native-Signature': signature}))
        response = conn.getresponse()
        raw = _response(response, native.MAX_RESPONSE)
        signatures = response.headers.get_all('X-Watch-Native-Signature', [])
        if len(signatures) != 1:
            native.reject()
        return native.verify_response(credential, challenge, request, invocation, port, response.status,
                                      raw, signatures[0], clock=clock, cancelled=cancelled)
    finally:
        stop.set()
        conn.close()
        if guard:
            guard.join(.2)


def run_hook(stdin, stdout, *, credential_path=None, port=None, enabled=False,
             clock=time.monotonic, cancelled=lambda: False, parent_alive=None,
             credential_loader=native.load_credential, connection_factory=http.client.HTTPConnection,
             invocation_id_factory=lambda: str(uuid.uuid4()), timeout=75):
    """Explicitly injected fixture entry point; disabled unless caller opts in.

    An enabled caller must additionally satisfy all native.GATES before any real
    hook installation. Credentials register callbacks only; owner decisions belong
    to the root's separate authenticated store. On every failure stdout is empty.
    """
    if enabled is not True:
        return 'disabled'
    try:
        if not native._finite(timeout) or not 0 < timeout <= 75 or credential_path is None:
            native.reject()
        parent_pid = os.getppid()
        alive = parent_alive or (lambda: os.getppid() == parent_pid)
        stopped = lambda: cancelled() or not alive()
        deadline = clock() + timeout
        if stopped():
            native.reject()
        raw = stdin.read(native.MAX_INPUT + 1)
        payload = native.parse_json(raw)
        invocation = native.HookInvocation.create(payload, invocation_id_factory(), deadline, clock)
        credential = credential_loader(credential_path)
        out = exchange(invocation, credential, port, clock=clock, cancelled=stopped,
                       connection_factory=connection_factory)
        if stopped() or clock() >= deadline:
            native.reject()
        if out:
            stdout.write(out)
            stdout.flush()
            return 'returned_to_hook_unconfirmed'
        return 'relinquished_to_native'
    except (native.InputError, OSError, ValueError, TypeError, http.client.HTTPException):
        return 'relinquished_to_native'


def main():
    # No activation CLI until every root-owned integration and native gate passes.
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
