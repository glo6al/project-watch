"""Unintegrated, read-only Codex native-input adapter.

No connections or workers start on import. Descriptors contain PRIVATE owner
input: never place them in room events, peer APIs or Officer context. Live answer
submission is deliberately impossible here: a desktop owner may survive a native
backend restart and reuse a native request ID. An IPC epoch is NOT that backend's
generation. Source-kind transcript privacy is a separate activation prerequisite.

The version-gated desktop stream uses Immer patches (array paths, add/remove/
replace), not JSON Pointer. Only a small request projection is retained. Native
approvals are observed but unsupported; history is never approval evidence here.
"""
from __future__ import annotations

from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import stat
import struct
import time
import uuid

import codex_desktop_control

QUESTION = 'item/tool/requestUserInput'
MAX_FRAME = 32 * 1024 * 1024
MAX_INITIAL_BYTES = 40 * 1024 * 1024
MAX_NODES = 200_000
MAX_DEPTH = 64
MAX_PROJECTION = 1024 * 1024
MAX_REQUESTS = 32
MAX_PATCHES = 2048
_RETAINED = {'id', 'hostId', 'requests', 'threadRuntimeStatus'}


class InvalidObservation(ValueError):
    """A payload-free failure, safe to report without leaking native content."""


def _require(ok, reason='invalid_native_input'):
    if not ok:
        raise InvalidObservation(reason)


def _uuid(value):
    try:
        _require(type(value) is str and str(uuid.UUID(value)) == value)
    except (ValueError, AttributeError, TypeError):
        raise InvalidObservation('invalid_identity') from None
    return value


def _text(value, maximum=65536, nonempty=False):
    _require(type(value) is str and len(value) <= maximum
             and (not nonempty or bool(value)))
    return value


def _integer(value):
    return type(value) is int and 0 <= value <= 2**53 - 1


def typed_id(value):
    """JSON number and string identities remain distinct; booleans are not IDs."""
    if type(value) is int and -(2**53 - 1) <= value <= 2**53 - 1:
        return ('integer', value)
    if type(value) is str and 0 < len(value) <= 1024:
        return ('string', value)
    raise InvalidObservation('invalid_request_identity')


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False)


def _bounded_tree(value):
    # Iterative traversal avoids recursion on hostile in-memory test transports.
    stack, count = [(value, 0)], 0
    while stack:
        item, depth = stack.pop()
        count += 1
        _require(count <= MAX_NODES and depth <= MAX_DEPTH, 'decoded_bounds')
        if type(item) is dict:
            for key, child in item.items():
                _require(type(key) is str)
                stack.append((key, depth + 1))
                stack.append((child, depth + 1))
        elif type(item) is list:
            stack.extend((child, depth + 1) for child in item)
        else:
            _require(item is None or type(item) in (str, bool, int, float))
            if type(item) is float:
                _require(math.isfinite(item))


def decode_frame(raw):
    """Check encoded structure before JSON allocation, then decoded structure."""
    _require(type(raw) is bytes and 0 < len(raw) <= MAX_FRAME, 'frame_bounds')
    # Count token starts conservatively (keys also count). Strings may contain
    # escaped delimiters, which must not affect structural depth/token budgets.
    depth = tokens = 0
    quoted = escaped = primitive = False
    for c in raw:
        if quoted:
            if escaped:
                escaped = False
            elif c == 92:
                escaped = True
            elif c == 34:
                quoted = False
            continue
        if c == 34:
            quoted = True
            primitive = False
            tokens += 1
        elif c in (123, 91):
            depth += 1
            tokens += 1
            primitive = False
        elif c in (125, 93):
            depth -= 1
            primitive = False
        elif c in (32, 9, 10, 13, 44, 58):
            primitive = False
        elif not primitive:
            primitive = True
            tokens += 1
        _require(0 <= depth <= MAX_DEPTH and tokens <= MAX_NODES, 'encoded_bounds')
    _require(not quoted and depth == 0)

    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, 'duplicate_json_key')
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(InvalidObservation()))
    except (ValueError, UnicodeError, RecursionError):
        raise InvalidObservation('invalid_json') from None
    _bounded_tree(value)
    _require(type(value) is dict)
    return value


class FrameBudget:
    """Initial acquisition and sliding live-stream budgets are independent."""
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.started = clock()
        self.initial = True
        self.total = self.count = 0
        self.window = deque()

    def charge(self, size):
        _require(type(size) is int and 0 < size <= MAX_FRAME, 'frame_bounds')
        now = self.clock()
        self.total += size + 4
        self.count += 1
        if self.initial:
            _require(now - self.started <= 10 and self.total <= MAX_INITIAL_BYTES
                     and self.count <= 100, 'initial_budget')
        while self.window and now - self.window[0][0] >= 10:
            self.window.popleft()
        self.window.append((now, size + 4))
        _require(len(self.window) <= 200
                 and sum(n for _, n in self.window) <= MAX_INITIAL_BYTES, 'stream_budget')

    def snapshot_received(self):
        self.initial = False


@dataclass(frozen=True)
class Binding:
    conversation_id: str
    owner_client_id: str
    client_id: str
    connection_epoch: str
    host_id: str = 'local'

    def __post_init__(self):
        for value in (self.conversation_id, self.owner_client_id, self.client_id,
                      self.connection_epoch):
            _uuid(value)
        _require(self.host_id == 'local', 'unsupported_host')


@dataclass(frozen=True)
class NativeRequestDescriptor:
    binding: Binding = field(repr=False)
    native_id: tuple = field(repr=False)
    method: str = field(repr=False)
    revision: int
    schema_hash: str
    observation_incarnation: str
    raw_json: str = field(repr=False)
    supported_schema: bool = False
    reason: str = 'unsupported_method'
    secret: bool = False
    turn_id: str | None = field(default=None, repr=False)
    item_id: str | None = field(default=None, repr=False)

    def raw_request(self):
        """Private copy; callers cannot mutate the observation's authority."""
        return json.loads(self.raw_json)


def _question_schema(params):
    _require(type(params) is dict)
    required = {'threadId', 'turnId', 'itemId', 'isBlocking', 'questions'}
    _require(required <= params.keys())
    _require(params.keys() <= required | {'autoResolutionMs'}, 'unsupported_schema')
    for key in ('threadId', 'turnId', 'itemId'):
        _text(params[key], 1024, True)
    _require(type(params['isBlocking']) is bool)
    auto = params.get('autoResolutionMs')
    _require(auto is None or _integer(auto))
    questions = params['questions']
    _require(type(questions) is list and 1 <= len(questions) <= 16)
    ids, secret = set(), False
    for question in questions:
        _require(type(question) is dict)
        _require({'id', 'header', 'question'} <= question.keys())
        _require(question.keys() <= {'id', 'header', 'question', 'options', 'isOther', 'isSecret'},
                 'unsupported_schema')
        qid = _text(question['id'], 1024, True)
        _require(qid not in ids, 'duplicate_question_id')
        ids.add(qid)
        _text(question['header'])
        _text(question['question'], nonempty=True)
        for flag in ('isOther', 'isSecret'):
            _require(type(question.get(flag, False)) is bool)
        secret |= question.get('isSecret', False)
        options = question.get('options')
        if options is not None:
            _require(type(options) is list and 1 <= len(options) <= 64)
            labels = set()
            for option in options:
                _require(type(option) is dict and option.keys() == {'label', 'description'})
                label = _text(option['label'], nonempty=True)
                _text(option['description'])
                _require(label not in labels, 'duplicate_option')
                labels.add(label)
    return secret


def _descriptors(projection, binding, revision, incarnation):
    _require(projection.get('id') == binding.conversation_id
             and projection.get('hostId') == binding.host_id, 'state_identity')
    requests = projection.get('requests')
    _require(type(requests) is list and len(requests) <= MAX_REQUESTS)
    _require(len(_json(projection)) <= MAX_PROJECTION, 'projection_bounds')
    result, ids = [], set()
    for request in requests:
        _require(type(request) is dict and {'id', 'method', 'params'} <= request.keys())
        identity = typed_id(request['id'])
        _require(identity not in ids, 'duplicate_native_id')
        ids.add(identity)
        method = _text(request['method'], 1024, True)
        params = request['params']
        _require(type(params) is dict)
        for identity_key in ('turnId', 'itemId'):
            if identity_key in params:
                _text(params[identity_key], 1024, True)
        # Unsupported methods may omit threadId, but may never name another chat.
        _require(params.get('threadId', binding.conversation_id) == binding.conversation_id,
                 'request_thread_identity')
        supported, secret, reason = False, False, 'unsupported_method'
        if method == QUESTION:
            secret = any(type(q) is dict and q.get('isSecret') is True
                         for q in params.get('questions', [])
                         ) if type(params.get('questions')) is list else False
            try:
                _require(request.keys() <= {'id', 'method', 'params'}, 'unsupported_schema')
                secret = _question_schema(params)
                supported, reason = not secret, 'secret_native_only' if secret else 'live_dispatch_disabled'
            except InvalidObservation:
                reason = 'unsupported_schema'
        raw = _json(request)
        result.append(NativeRequestDescriptor(binding, identity, method, revision,
                      hashlib.sha256(raw.encode()).hexdigest(), incarnation, raw, supported, reason,
                      secret, params.get('turnId'), params.get('itemId')))
    return tuple(result)


def _patch_retained(projection, patch):
    op, path = patch.get('op'), patch.get('path')
    _require(op in ('add', 'remove', 'replace') and type(path) is list
             and len(path) <= MAX_DEPTH, 'unsupported_patch')
    _require(patch.keys() == ({'op', 'path'} if op == 'remove' else {'op', 'path', 'value'}),
             'unsupported_patch')
    _require(all((type(p) is str and p not in ('__proto__', 'constructor', 'prototype'))
                 or (type(p) is int and p >= 0) for p in path), 'unsupported_path')
    if not path:
        _require(op == 'replace' and type(patch['value']) is dict, 'unsupported_root_patch')
        return {key: deepcopy(value) for key, value in patch['value'].items() if key in _RETAINED}
    _require(type(path[0]) is str, 'unsupported_path')
    if path[0] not in _RETAINED:
        # Nonretained top-level subtrees cannot alter requests or identity. Their
        # data/path shape has already been bounded and validated, not executed.
        return projection
    target = projection
    for component in path[:-1]:
        if type(target) is dict:
            _require(type(component) is str and component in target, 'missing_path')
        elif type(target) is list:
            _require(type(component) is int and component < len(target), 'missing_path')
        else:
            raise InvalidObservation('missing_path')
        target = target[component]
    key = path[-1]
    if type(target) is list:
        _require(type(key) is int and 0 <= key <= len(target), 'invalid_array_index')
        _require(op == 'add' or key < len(target), 'missing_path')
        if op == 'add':
            target.insert(key, deepcopy(patch['value']))
        elif op == 'remove':
            target.pop(key)
        else:
            target[key] = deepcopy(patch['value'])
    else:
        _require(type(target) is dict and type(key) is str, 'missing_path')
        _require(op == 'add' or key in target, 'missing_path')
        if op == 'remove':
            del target[key]
        else:
            target[key] = deepcopy(patch['value'])
    return projection


class NativeInputObserver:
    """Exact-owner revision reducer. A rejected matching frame revokes state."""
    def __init__(self, binding):
        self.binding = binding
        self.revision = None
        self.available = False
        self._projection = {}
        self._descriptors = ()
        self._closed = False
        self._incarnation = str(uuid.uuid4())

    @property
    def descriptors(self):
        return self._descriptors

    def invalidate(self):
        self._incarnation = str(uuid.uuid4())
        self.available = False
        self.revision = None
        self._projection = {}
        self._descriptors = ()

    def close(self):
        self.invalidate()
        self._closed = True

    def consume(self, message):
        _require(not self._closed, 'closed')
        try:
            _bounded_tree(message)
            _require(type(message) is dict)
        except InvalidObservation:
            self.invalidate()
            raise
        b = self.binding
        if message.get('type') == 'broadcast':
            lifecycle = message.get('params')
            if (message.get('method') == 'ipc-connection-reset'
                    or (message.get('method') == 'client-status-changed'
                        and type(lifecycle) is dict
                        and lifecycle.get('clientId') == b.owner_client_id
                        and lifecycle.get('status') == 'disconnected')):
                self.close()
                raise InvalidObservation('owner_disconnected')
        # The router stamps sourceClientId. Unrelated broadcasts cannot revive
        # or poison this exact subscription and their content is never retained.
        if (message.get('type') != 'broadcast'
                or message.get('sourceClientId') != b.owner_client_id):
            return False
        params = message.get('params')
        if type(params) is not dict or params.get('conversationId') != b.conversation_id:
            return False
        if params.get('hostId') != b.host_id:
            return False
        try:
            _require(message.get('method') == 'thread-stream-state-changed'
                     and type(message.get('version')) is int and message['version'] == 11,
                     'unsupported_stream')
            # Router delivery is socket-targeted; some envelopes also preserve
            # targets. If present, they must explicitly include this client.
            targets = message.get('targetClientIds', params.get('targetClientIds'))
            _require(targets is None or (type(targets) is list and b.client_id in targets),
                     'wrong_recipient')
            change = params.get('change')
            _require(type(change) is dict and _integer(change.get('revision')))
            revision = change['revision']
            if change.get('type') == 'snapshot':
                # A second unsolicited snapshot is not proof of native generation.
                _require(self.revision is None, 'unexpected_snapshot')
                state = change.get('conversationState')
                _require(type(state) is dict)
                projection = {k: deepcopy(v) for k, v in state.items() if k in _RETAINED}
            else:
                _require(change.get('type') == 'patches' and self.available
                         and _integer(change.get('baseRevision'))
                         and change['baseRevision'] == self.revision
                         and revision == self.revision + 1, 'revision_gap')
                patches = change.get('patches')
                _require(type(patches) is list and len(patches) <= MAX_PATCHES)
                accepted = change.get('acceptedTextChanges')
                _require(accepted is None or type(accepted) is list)
                projection = deepcopy(self._projection)
                for patch in patches:
                    _require(type(patch) is dict)
                    projection = _patch_retained(projection, patch)
            descriptors = _descriptors(projection, b, revision, self._incarnation)
        except (InvalidObservation, TypeError, ValueError, RecursionError):
            self.invalidate()
            raise InvalidObservation('invalid_stream_state') from None
        self._projection, self._descriptors = projection, descriptors
        self.revision, self.available = revision, True
        return True

    def validate_response(self, descriptor, response):
        """Offline schema validation, NOT permission or ability to dispatch.

        Initially one nonempty answer per question. Skip, empty-answer and
        multiselect semantics remain unsupported until native evidence exists.
        """
        _require(self.available and descriptor in self._descriptors, 'stale_descriptor')
        _require(descriptor.supported_schema and not descriptor.secret, 'unsupported_question')
        _bounded_tree(response)
        _require(type(response) is dict and response.keys() == {'answers'})
        answers = response['answers']
        questions = descriptor.raw_request()['params']['questions']
        _require(type(answers) is dict and answers.keys() == {q['id'] for q in questions})
        for question in questions:
            answer = answers[question['id']]
            _require(type(answer) is dict and answer.keys() == {'answers'})
            values = answer['answers']
            _require(type(values) is list and len(values) == 1, 'unsupported_cardinality')
            value = _text(values[0], nonempty=True)
            if question.get('options') and not question.get('isOther', False):
                _require(value in {o['label'] for o in question['options']}, 'unsupported_choice')
        _require(len(_json(response)) <= MAX_PROJECTION)
        return deepcopy(response)

    def dispatch_reserved(self, descriptor, decision):
        """No flag, injected sender, or fallback can enable native mutation."""
        return {'attempted': False, 'status': 'not_attempted',
                'reason': 'native_generation_unproven', 'consumed': False}


class _SocketTransport:
    """Private framing transport; only the observation driver constructs it."""
    def __init__(self, clock=time.monotonic):
        _require(codex_desktop_control.preflight()['available'], 'desktop_unavailable')
        self.clock, self.sock = clock, None
        path = Path.home() / '.codex/ipc/ipc.sock'

        def identity():
            _require(path.resolve() == path, 'socket_identity')
            result = []
            for p, kind, mode in ((path.parent, stat.S_ISDIR, 0o700),
                                  (path, stat.S_ISSOCK, 0o600)):
                info = p.lstat()
                _require(kind(info.st_mode) and info.st_uid == os.getuid()
                         and stat.S_IMODE(info.st_mode) == mode, 'socket_identity')
                result.append((info.st_dev, info.st_ino))
            return result
        before = identity()
        try:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(2)
            self.sock.connect(str(path))
            _require(identity() == before, 'socket_replaced')
        except Exception:
            self.close()
            raise

    def send(self, message):
        allowed = {'initialize', 'thread-owner-discovery', 'thread-stream-following-changed'}
        _require(message.get('method') in allowed or message.get('type') in
                 ('client-discovery-response', 'response'), 'observation_only')
        raw = _json(message).encode()
        _require(len(raw) <= 16384)
        self.sock.sendall(struct.pack('<I', len(raw)) + raw)

    def receive(self, budget, deadline):
        def read(size):
            data = bytearray()
            while len(data) < size:
                remaining = deadline - self.clock()
                _require(remaining > 0, 'deadline')
                self.sock.settimeout(remaining)
                part = self.sock.recv(size - len(data))
                _require(bool(part), 'disconnected')
                data.extend(part)
            return bytes(data)
        size = struct.unpack('<I', read(4))[0]
        budget.charge(size)  # Before allocating or reading the advertised body.
        return decode_frame(read(size))

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None


def _observe(transport, conversation_id, on_observation, clock, seconds):
    """Internal injection point for offline transport/lifecycle tests."""
    conversation_id = _uuid(conversation_id)
    _require(type(seconds) in (int, float) and 0 < seconds <= 10)
    deadline, budget = clock() + seconds, FrameBudget(clock)
    observer = None
    client, owner = 'initializing-client', None

    def receive():
        while True:
            message = transport.receive(budget, deadline)
            if message.get('type') == 'client-discovery-request':
                transport.send({'type': 'client-discovery-response',
                                'requestId': _uuid(message.get('requestId')),
                                'response': {'canHandle': False}})
            elif message.get('type') == 'request':
                transport.send({'type': 'response', 'requestId': _uuid(message.get('requestId')),
                                'resultType': 'error', 'error': 'no-handler-for-request'})
            else:
                return message

    def request(method, params, version):
        rid = str(uuid.uuid4())
        transport.send({'type': 'request', 'requestId': rid, 'sourceClientId': client,
                        'version': version, 'method': method, 'params': params,
                        'timeoutMs': 5000})
        while True:
            reply = receive()
            if reply.get('type') == 'broadcast':
                continue
            _require(reply.get('type') == 'response' and reply.get('requestId') == rid
                     and reply.get('method') == method and reply.get('resultType') == 'success',
                     'invalid_handshake')
            _uuid(reply.get('handledByClientId'))
            _require(type(reply.get('result')) is dict)
            return reply

    def follow(value):
        transport.send({'type': 'broadcast', 'sourceClientId': client, 'version': 1,
                        'method': 'thread-stream-following-changed', 'targetClientIds': [owner],
                        'params': {'conversationId': conversation_id, 'hostId': 'local',
                                   'following': value}})
    try:
        initialized = request('initialize', {'clientType': 'watch-native-input-observer'}, 0)
        client = _uuid(initialized['result'].get('clientId'))
        _require(initialized['handledByClientId'] == client, 'invalid_handshake')
        found = request('thread-owner-discovery', {'conversationId': conversation_id,
                                                  'hostId': 'local'}, 1)
        owner = _uuid(found['handledByClientId'])
        _require(owner != client and found['result'].get('supportsUntrustedAppInput') is True,
                 'invalid_owner')
        observer = NativeInputObserver(Binding(conversation_id, owner, client, str(uuid.uuid4())))
        follow(True)
        while clock() < deadline:
            message = receive()
            if observer.consume(message):
                budget.snapshot_received()
                on_observation(observer.descriptors)
    finally:
        if observer is not None:
            observer.close()
        try:
            if client and owner:
                follow(False)
        finally:
            transport.close()


def subscribe_exact(conversation_id, on_observation, *, seconds=10):
    """Explicit bounded read-only subscription for one known local chat.

    Synchronous, no reconnect/replay, no full-history retention. Exceptions are
    payload-free. Descriptors become stale when this function returns. A future
    service integration must revoke its owner handles on any termination.
    """
    _uuid(conversation_id)
    _require(type(seconds) in (int, float) and 0 < seconds <= 10)
    transport = None
    try:
        transport = _SocketTransport()
        _observe(transport, conversation_id, on_observation, time.monotonic, seconds)
    except Exception:
        raise InvalidObservation('observation_stopped') from None
    finally:
        if transport is not None:
            transport.close()
