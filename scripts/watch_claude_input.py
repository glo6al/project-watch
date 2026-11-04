#!/usr/bin/env python3
"""Disabled native-input helper. Import run_hook for explicitly enabled fixtures.

There is intentionally no CLI activation switch, default credential path, hook
installer or credential creation. Native cancellation/privacy/rotation proofs and
root-owned integration must precede a production activation path.
"""
import http.client
import io
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


class _ResponseReader(io.RawIOBase):
    """A response borrows its exchange's socket; file close never closes it."""
    def __init__(self, owner):
        super().__init__()
        self.owner = owner

    def readable(self):
        return True

    def readinto(self, buffer):
        if self.closed:
            raise ValueError('Response reader is closed.')
        return self.owner.readinto(buffer)


class _CancellableSocket:
    """Only the invocation thread performs connected I/O or owns the raw close."""
    def __init__(self, raw, check):
        self._raw, self._check = raw, check
        self.detached = False

    @property
    def raw(self):
        return self._raw

    def fileno(self):
        return self.raw.fileno()

    def close(self):
        # HTTPConnection can detach before a final Connection: close body is read.
        self.detached = True

    def makefile(self, mode):
        if mode != 'rb' or self.detached:
            native.reject()
        return io.BufferedReader(_ResponseReader(self))

    def _wait(self, writing=False):
        while True:
            remaining = self._check()
            try:
                readable, writable, _ = select.select([] if writing else [self.raw],
                                                      [self.raw] if writing else [], [], min(.025, remaining))
            except InterruptedError:
                continue
            self._check()
            if (writable if writing else readable):
                return

    def readinto(self, buffer):
        if not len(buffer):
            return 0
        while True:
            self._wait()
            self._check()
            try:
                return self.raw.recv_into(buffer)
            except (BlockingIOError, InterruptedError):
                continue

    def sendall(self, data):
        if self.detached:
            native.reject()
        data, offset = memoryview(data), 0
        while offset < len(data):
            self._wait(writing=True)
            self._check()
            try:
                sent = self.raw.send(data[offset:])
            except (BlockingIOError, InterruptedError):
                continue
            if sent <= 0:
                native.reject()
            offset += sent


def _same_socket(connection, pinned, actual):
    pinned._check()
    if connection.sock is not pinned or pinned.raw is not actual or pinned.detached or actual.fileno() < 0:
        native.reject()
    ready, _, _ = select.select([actual], [], [], 0)
    if ready:
        try:
            if actual.recv(1, socket.MSG_PEEK) == b'':
                native.reject()
        except (BlockingIOError, InterruptedError):
            pass
    pinned._check()


def exchange(invocation, credential, port, *, clock=time.monotonic,
             cancelled=lambda: False, connection_factory=http.client.HTTPConnection,
             nonce_factory=lambda: os.urandom(32).hex()):
    """One proved socket and one invocation write, without mutation retries.

    The watchdog latches cancellation; this thread owns all socket I/O and close.
    Native parent/session cancellation still requires a live proof.
    """
    if type(port) is not int or not 1 <= port <= 65535:
        native.reject()
    conn = connection_factory('127.0.0.1', port, timeout=min(75.0, max(.001, invocation.deadline - clock())))
    stop = threading.Event()
    aborted = threading.Event()
    guard = None
    pinned = actual = response = None
    def remaining():
        if aborted.is_set() or cancelled():
            native.reject()
        left = invocation.deadline - clock()
        if left <= 0:
            native.reject()
        return left
    try:
        if cancelled() or clock() >= invocation.deadline:
            native.reject()
        conn.connect()
        actual = conn.sock
        if actual is None:
            native.reject()
        actual.setblocking(False)
        pinned = _CancellableSocket(actual, remaining)
        conn.sock = pinned
        conn.auto_open = False  # A proved connection must never reconnect.

        def watch():
            try:
                while not stop.wait(.025):
                    if cancelled() or clock() >= invocation.deadline:
                        aborted.set()
                        break
            except Exception:
                aborted.set()  # A failed callback must fail the exchange closed.
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
        response.close()
        response = None
        _same_socket(conn, pinned, actual)
        if cancelled() or clock() >= invocation.deadline:
            native.reject()
        raw, signature = native.invocation_request(credential, challenge, request, invocation, port)
        conn.request('POST', native.INVOCATION_PATH, raw, dict(headers, **{'X-Watch-Native-Signature': signature}))
        response = conn.getresponse()
        raw = _response(response, native.MAX_RESPONSE)
        signatures = response.headers.get_all('X-Watch-Native-Signature', [])
        if len(signatures) != 1:
            native.reject()
        output = native.verify_response(credential, challenge, request, invocation, port, response.status,
                                        raw, signatures[0], clock=clock, cancelled=cancelled)
    finally:
        stop.set()
        try:
            try:
                if response is not None:
                    response.close()
            finally:
                conn.close()
        finally:
            try:
                if actual is not None:
                    actual.close()
            finally:
                if guard:
                    guard.join(.2)
    if aborted.is_set() or (guard and guard.is_alive()):
        native.reject()
    return output


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
        def stopped():
            try:
                return cancelled() or not alive()
            except Exception:
                native.reject()
        def now():
            try:
                return clock()
            except Exception:
                native.reject()
        deadline = now() + timeout
        if stopped():
            native.reject()
        raw = stdin.read(native.MAX_INPUT + 1)
        payload = native.parse_json(raw)
        invocation = native.HookInvocation.create(payload, invocation_id_factory(), deadline, now)
        credential = credential_loader(credential_path)
        out = exchange(invocation, credential, port, clock=now, cancelled=stopped,
                       connection_factory=connection_factory)
        if stopped() or now() >= deadline:
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
