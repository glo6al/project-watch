"""Disabled Claude hook adapter primitives. No routes, installation, or import-time I/O.

A signed hook invocation is not owner authorization. The caller must authenticate
owner decisions separately. Returned bytes are not evidence of native consumption.
"""
import copy
import hashlib
import hmac
import json
import math
import os
import re
import stat
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

MAX_INPUT = 256 * 1024
MAX_RESPONSE = 128 * 1024
MAX_ANSWER_UNITS = 32768
PURPOSE = 'watch-native-input-hook-v1'
CHALLENGE_PATH = '/native-input/v1/hook/challenge'
INVOCATION_PATH = '/native-input/v1/hook/invocation'
HEX = re.compile(r'[0-9a-f]{64}\Z')
ID = re.compile(r'[A-Za-z0-9_.:-]{1,200}\Z')
GATES = ('legacy_hook_migration_and_owner_key_rotation', 'observer_privacy',
         'native_hot_activation', 'native_callback_cancellation', 'native_consumption_proof')


class InputError(ValueError):
    """Fixed diagnostics only: exception text must never contain native payloads."""


def reject():
    raise InputError('Native input is unavailable.')


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            reject()
        out[key] = value
    return out


def parse_json(raw, limit=MAX_INPUT):
    try:
        if not isinstance(raw, bytes) or not 0 < len(raw) <= limit:
            reject()
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                          parse_constant=lambda _: reject())
    except (ValueError, UnicodeError, RecursionError, TypeError):
        reject()


def json_bytes(value):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False,
                          separators=(',', ':')).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        reject()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _exact(value, keys, optional=()):
    if not isinstance(value, dict) or not set(keys) <= set(value) or set(value) - set(keys) - set(optional):
        reject()


def _text(value, maximum=MAX_INPUT, empty=False):
    if not isinstance(value, str) or (not empty and not value) or len(value) > maximum:
        reject()
    try:
        value.encode('utf-8')
    except UnicodeError:
        reject()
    return value


def _ident(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        reject()
    return value


def _uuid(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            reject()
    except (ValueError, AttributeError):
        reject()
    return value


def _nonce(value):
    if not isinstance(value, str) or not HEX.fullmatch(value):
        reject()
    return value


def utf16_units(value):
    _text(value, empty=True)
    return len(value.encode('utf-16-le')) // 2


def encode_multiselect(values):
    """Native quote/join format, including labels containing comma-space or quotes."""
    return ', '.join(json.dumps(v, ensure_ascii=False) if ', ' in v or '"' in v else v
                     for v in values)


def _questions(value):
    _exact(value, ('questions',), ('metadata',))
    if 'metadata' in value and not isinstance(value['metadata'], dict):
        reject()
    qs = value['questions']
    if not isinstance(qs, list) or not 1 <= len(qs) <= 4:
        reject()
    names = set()
    for q in qs:
        _exact(q, ('question', 'header', 'options'), ('multiSelect',))
        name = _text(q['question'])
        _text(q['header'])
        if name in names or type(q.get('multiSelect', False)) is not bool:
            reject()
        names.add(name)
        opts = q['options']
        if not isinstance(opts, list) or not 2 <= len(opts) <= 4:
            reject()
        labels = set()
        for opt in opts:
            _exact(opt, ('label', 'description'), ('preview',))
            label = _text(opt['label'])
            _text(opt['description'], empty=True)
            if 'preview' in opt:
                _text(opt['preview'], empty=True)
            if label in labels:
                reject()
            labels.add(label)


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _form_schema(schema):
    """Staged flat MCP subset: strings, finite numbers, booleans, simple enums.

    Formats, arrays, oneOf, nested schemas and secret fields are deliberately
    unsupported until their native and privacy proofs exist. Never weaken them.
    """
    _exact(schema, ('type', 'properties'), ('required',))
    props = schema['properties']
    if schema['type'] != 'object' or not isinstance(props, dict) or not 1 <= len(props) <= 32:
        reject()
    required = schema.get('required', [])
    if not isinstance(required, list) or any(not isinstance(k, str) for k in required) or len(set(required)) != len(required) or not set(required) <= set(props):
        reject()
    for key, p in props.items():
        _text(key, 200)
        if not isinstance(p, dict):
            reject()
        typ = p.get('type')
        allowed = {'type', 'title', 'description', 'default'}
        if typ == 'string':
            allowed |= {'minLength', 'maxLength', 'enum'}
        elif typ in ('number', 'integer'):
            allowed |= {'minimum', 'maximum'}
        elif typ != 'boolean':
            reject()
        if set(p) - allowed:
            reject()
        for name in ('title', 'description'):
            if name in p:
                _text(p[name], empty=True)
        for name in ('minLength', 'maxLength'):
            if name in p and (type(p[name]) is not int or p[name] < 0):
                reject()
        if p.get('minLength', 0) > p.get('maxLength', MAX_INPUT):
            reject()
        for name in ('minimum', 'maximum'):
            if name in p and not _finite(p[name]):
                reject()
        if p.get('minimum', -math.inf) > p.get('maximum', math.inf):
            reject()
        if 'enum' in p:
            enum = p['enum']
            if not isinstance(enum, list) or not enum or len(enum) > 64 or any(not isinstance(v, str) for v in enum) or len(set(enum)) != len(enum):
                reject()
            for v in enum:
                _text(v, empty=True)
        if 'default' in p:
            _form_value(p['default'], p)


def _form_value(value, prop):
    typ = prop['type']
    if typ == 'boolean':
        if type(value) is not bool:
            reject()
    elif typ == 'string':
        _text(value, empty=True)
        # MCP length constraints use Unicode code points, not UTF-8 byte size.
        if not prop.get('minLength', 0) <= len(value) <= prop.get('maxLength', MAX_INPUT):
            reject()
        if 'enum' in prop and value not in prop['enum']:
            reject()
    elif not _finite(value) or (typ == 'integer' and type(value) is not int) or value < prop.get('minimum', -math.inf) or value > prop.get('maximum', math.inf):
        reject()


@dataclass(frozen=True, repr=False)
class HookInvocation:
    """Immutable original callback snapshot. payload/binding accessors return copies."""
    invocation_id: str
    deadline: float
    kind: str
    _payload: bytes = field(repr=False)
    _binding: bytes = field(repr=False)

    @classmethod
    def create(cls, payload, invocation_id, deadline, clock=time.monotonic):
        _uuid(invocation_id)
        if not _finite(deadline) or not clock() < deadline <= clock() + 75:
            reject()
        raw = json_bytes(payload)
        p = parse_json(raw)
        if not isinstance(p, dict):
            reject()
        _uuid(p.get('session_id'))
        for key in ('prompt_id', 'agent_id'):
            if key in p:
                _ident(p[key])
        event = p.get('hook_event_name')
        if event == 'PreToolUse':
            if p.get('tool_name') != 'AskUserQuestion':
                reject()
            _ident(p.get('tool_use_id'))
            _questions(p.get('tool_input'))
            kind = 'claude_question'
        elif event == 'Elicitation':
            if p.get('mode', 'form') != 'form':
                reject()
            _text(p.get('mcp_server_name'), 200)
            _text(p.get('message'), empty=True)
            _form_schema(p.get('requested_schema'))
            kind = 'mcp_form'
        else:
            reject()
        # Preserve known callback identity; do not invent a tool ID for a form.
        binding = {k: p[k] for k in ('session_id', 'prompt_id', 'agent_id', 'tool_use_id',
                                     'hook_event_name', 'tool_name', 'mcp_server_name') if k in p}
        binding.update(invocation_id=invocation_id, payload_digest=digest(raw), kind=kind)
        return cls(invocation_id, deadline, kind, raw, json_bytes(binding))

    def __repr__(self):
        return '<HookInvocation redacted>'

    @property
    def payload(self):
        return parse_json(self._payload)

    @property
    def binding(self):
        return parse_json(self._binding)

    @property
    def payload_digest(self):
        return digest(self._payload)

    @property
    def binding_digest(self):
        return digest(self._binding)

    def native_output(self, decision):
        """Pure serialization/validation only. Does not authorize or deliver an answer."""
        if not isinstance(decision, dict):
            reject()
        if decision == {'kind': 'return_to_native'}:
            return b''
        if self.kind == 'mcp_form':
            if decision.get('kind') in ('mcp_form_decline', 'mcp_form_cancel'):
                _exact(decision, ('kind',))
                action = decision['kind'].rsplit('_', 1)[1]
                out = {'hookSpecificOutput': {'hookEventName': 'Elicitation', 'action': action}}
            else:
                _exact(decision, ('kind', 'content'))
                if decision['kind'] != 'mcp_form_accept':
                    reject()
                content = decision['content']
                schema = self.payload['requested_schema']
                if not isinstance(content, dict) or set(content) - set(schema['properties']) or not set(schema.get('required', [])) <= set(content):
                    reject()
                for key, value in content.items():
                    _form_value(value, schema['properties'][key])
                out = {'hookSpecificOutput': {'hookEventName': 'Elicitation', 'action': 'accept', 'content': copy.deepcopy(content)}}
        else:
            _exact(decision, ('kind', 'answers'), ('skip_all',))
            if decision['kind'] != 'claude_question_answers' or type(decision.get('skip_all', False)) is not bool:
                reject()
            questions = self.payload['tool_input']['questions']
            answers, seen = {}, set()
            rows = decision['answers']
            if not isinstance(rows, list) or len(rows) != len(questions):
                reject()
            for row in rows:
                _exact(row, ('question_index',), ('option_indices', 'other', 'skip'))
                idx = row['question_index']
                if type(idx) is not int or not 0 <= idx < len(questions) or idx in seen:
                    reject()
                seen.add(idx)
                if row.get('skip') is True:
                    _exact(row, ('question_index', 'skip'))
                    continue
                if 'skip' in row:
                    reject()
                indices = row.get('option_indices', [])
                q = questions[idx]
                if not isinstance(indices, list) or any(type(v) is not int or not 0 <= v < len(q['options']) for v in indices) or len(set(indices)) != len(indices):
                    reject()
                values = [q['options'][v]['label'] for v in indices]
                if 'other' in row:
                    values.append(_text(row['other'], empty=False))
                if not values or (not q.get('multiSelect', False) and len(values) != 1):
                    reject()
                answers[q['question']] = encode_multiselect(values) if q.get('multiSelect', False) else values[0]
            if (not answers) != decision.get('skip_all', False):
                reject()
            original = self.payload['tool_input']
            out = {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'allow',
                                         'updatedInput': dict(original, answers=answers)}}
        raw = json_bytes(out)
        if len(raw) > MAX_RESPONSE or utf16_units(json_bytes(decision).decode('utf-8')) > MAX_ANSWER_UNITS:
            reject()
        return raw


class OriginalCallback:
    """One existing callback, not a global store or authentication endpoint.

    owner_authorized is an INTERNAL root-owned authorization result, never read
    from native payloads, peer credentials, protocol envelopes or browser forms.
    Call take_output only inside a verified helper context; it claims no delivery.
    """
    def __init__(self, invocation, clock=time.monotonic, cancelled=lambda: False):
        self.invocation = invocation
        self.clock, self.cancelled = clock, cancelled
        self._lock = threading.Lock()
        self._state = 'pending'
        self._output = None
        self._submission = None
        self._decision_mac = None
        self._idempotency_key = os.urandom(32)

    @property
    def state(self):
        with self._lock:
            self._expire()
            return self._state

    def _expire(self):
        if self._state in ('pending', 'reserved') and (self.cancelled() or self.clock() >= self.invocation.deadline):
            self._state = 'cancelled' if self.cancelled() else 'expired'
            self._output = None

    def reserve_owner_decision(self, decision, submission_id, *, owner_authorized=False):
        if owner_authorized is not True:
            reject()
        _uuid(submission_id)
        output = self.invocation.native_output(decision)
        stamp = hmac.new(self._idempotency_key, json_bytes(decision), hashlib.sha256).digest()
        with self._lock:
            self._expire()
            if self._submission is not None:
                if self._submission == submission_id and hmac.compare_digest(self._decision_mac, stamp) and self._state in ('reserved', 'output_taken', 'relinquished_to_native'):
                    return self._state
                reject()
            if self._state != 'pending':
                reject()
            self._submission, self._decision_mac = submission_id, stamp
            self._output, self._state = output, 'reserved'
            return self._state

    def take_output(self):
        with self._lock:
            self._expire()
            if self._state != 'reserved':
                reject()
            out, self._output = self._output, None
            self._state = 'output_taken' if out else 'relinquished_to_native'
            return out

    def close(self, reason='disconnected'):
        if reason not in ('cancelled', 'expired', 'disconnected', 'relinquished_to_native'):
            reject()
        with self._lock:
            if self._state in ('pending', 'reserved'):
                self._output, self._state = None, reason


@dataclass(frozen=True)
class HookCredential:
    credential_id: str
    secret: bytes = field(repr=False)

    def __post_init__(self):
        _uuid(self.credential_id)
        if not isinstance(self.secret, bytes) or len(self.secret) != 32:
            reject()


def load_credential(path):
    """Read an explicitly supplied credential only; never creates or discovers one.

    POSIX ownership/mode checks are required. Windows remains unavailable until
    an equivalent file-identity/ACL validator is supplied by a future integration.
    """
    if os.name == 'nt':
        reject()
    try:
        p = Path(path)
        if not p.is_absolute():
            reject()
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        parent = os.open(str(p.parent), flags | os.O_DIRECTORY)
        try:
            st = os.fstat(parent)
            if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
                reject()
            fd = os.open(p.name, flags, dir_fd=parent)
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600 or st.st_size > 2048:
                    reject()
                with os.fdopen(fd, 'rb', closefd=False) as stream:
                    raw = stream.read(2049)
            finally:
                os.close(fd)
        finally:
            os.close(parent)
        data = parse_json(raw, 2048)
        _exact(data, ('version', 'purpose', 'credential_id', 'secret'))
        if type(data['version']) is not int or data['version'] != 1 or data['purpose'] != PURPOSE:
            reject()
        return HookCredential(data['credential_id'], bytes.fromhex(_nonce(data['secret'])))
    except (OSError, ValueError, TypeError, AttributeError):
        reject()


def mac(credential, domain, *parts):
    return hmac.new(credential.secret, json_bytes([domain] + list(parts)), hashlib.sha256).hexdigest()


def challenge_request(credential, nonce, invocation_id):
    return {'version': 1, 'credential_id': credential.credential_id,
            'client_nonce': _nonce(nonce), 'invocation_id': _uuid(invocation_id)}


def verify_challenge(credential, request, response, port):
    _exact(response, ('version', 'run_id', 'server_nonce', 'proof'))
    if type(response['version']) is not int or response['version'] != 1:
        reject()
    _uuid(response['run_id'])
    _nonce(response['server_nonce'])
    expected = mac(credential, 'watch-native-input-server-v1', port, CHALLENGE_PATH,
                   request, response['run_id'], response['server_nonce'])
    if not isinstance(response['proof'], str) or not hmac.compare_digest(expected, response['proof']):
        reject()


def invocation_request(credential, challenge, request, invocation, port):
    body = {'version': 1, 'credential_id': credential.credential_id,
            'run_id': challenge['run_id'], 'client_nonce': request['client_nonce'],
            'server_nonce': challenge['server_nonce'], 'invocation_id': invocation.invocation_id,
            'payload': invocation.payload}
    raw = json_bytes(body)
    return raw, mac(credential, 'watch-native-input-request-v1', port, 'POST', INVOCATION_PATH, digest(raw))


class HookProtocol:
    """Injected server-side protocol, no HTTP routes and NO owner-answer operation.

    Each connection token must identify the actual accepted socket, not a remote
    address or a reused HTTP client object. Challenges are one-use and run-bound.
    """
    def __init__(self, credential, port, *, clock=time.monotonic, run_id=None, nonce_factory=None):
        if type(port) is not int or not 1 <= port <= 65535:
            reject()
        self.credential, self.port, self.clock = credential, port, clock
        self.run_id = _uuid(run_id or str(uuid.uuid4()))
        self.nonce_factory = nonce_factory or (lambda: os.urandom(32).hex())
        self._challenges, self._used, self._nonces = {}, set(), set()
        self._lock = threading.Lock()

    def issue_challenge(self, request, connection_token):
        _exact(request, ('version', 'credential_id', 'client_nonce', 'invocation_id'))
        if type(request['version']) is not int or request['version'] != 1 or request['credential_id'] != self.credential.credential_id:
            reject()
        _nonce(request['client_nonce'])
        _uuid(request['invocation_id'])
        nonce = _nonce(self.nonce_factory())
        with self._lock:
            self._challenges = {k: v for k, v in self._challenges.items() if self.clock() < v[0]}
            if len(self._challenges) >= 128 or len(self._nonces) >= 2048 or request['client_nonce'] in self._nonces or request['invocation_id'] in self._used or connection_token in self._challenges:
                reject()
            self._challenges[connection_token] = (self.clock() + 10, copy.deepcopy(request), nonce)
            self._nonces.add(request['client_nonce'])
        return {'version': 1, 'run_id': self.run_id, 'server_nonce': nonce,
                'proof': mac(self.credential, 'watch-native-input-server-v1', self.port,
                             CHALLENGE_PATH, request, self.run_id, nonce)}

    def accept_invocation(self, raw, signature, connection_token):
        data = parse_json(raw, MAX_INPUT + 4096)
        _exact(data, ('version', 'credential_id', 'run_id', 'client_nonce', 'server_nonce', 'invocation_id', 'payload'))
        expected = mac(self.credential, 'watch-native-input-request-v1', self.port, 'POST', INVOCATION_PATH, digest(raw))
        if not isinstance(signature, str) or not hmac.compare_digest(expected, signature):
            reject()
        with self._lock:
            held = self._challenges.pop(connection_token, None)
            if held is None:
                reject()
            expiry, req, nonce = held
            if (self.clock() >= expiry or type(data['version']) is not int or data['version'] != 1 or
                    data['credential_id'] != self.credential.credential_id or data['run_id'] != self.run_id or
                    data['client_nonce'] != req['client_nonce'] or data['server_nonce'] != nonce or
                    data['invocation_id'] != req['invocation_id'] or data['invocation_id'] in self._used):
                reject()
            invocation = HookInvocation.create(data['payload'], data['invocation_id'], self.clock() + 75, self.clock)
            self._used.add(invocation.invocation_id)
        return invocation

    def response(self, invocation, request_nonce, decision=None):
        """Serialize an ALREADY owner-authorized decision supplied by the root store.

        Not an HTTP operation; possession of a hook credential cannot choose it.
        None means no decision. No delivery or consumption evidence is produced.
        """
        _nonce(request_nonce)
        if self.clock() >= invocation.deadline:
            reject()
        if decision is not None:
            invocation.native_output(decision)
        data = {'version': 1, 'run_id': self.run_id, 'invocation_id': invocation.invocation_id,
                'binding_digest': invocation.binding_digest, 'payload_digest': invocation.payload_digest,
                'outcome': 'answer' if decision is not None else 'return_to_native'}
        if decision is not None:
            data['decision'] = copy.deepcopy(decision)
        raw = json_bytes(data)
        if len(raw) > MAX_RESPONSE:
            reject()
        signature = mac(self.credential, 'watch-native-input-response-v1', self.port, self.run_id,
                        request_nonce, 200, digest(raw))
        return raw, signature


def verify_response(credential, challenge, request, invocation, port, status, raw, signature,
                    *, clock=time.monotonic, cancelled=lambda: False):
    """Return native output only after full authenticated original-callback checks."""
    expected = mac(credential, 'watch-native-input-response-v1', port, challenge['run_id'],
                   request['client_nonce'], status, digest(raw))
    if status != 200 or not isinstance(signature, str) or not hmac.compare_digest(expected, signature):
        reject()
    data = parse_json(raw, MAX_RESPONSE)
    _exact(data, ('version', 'run_id', 'invocation_id', 'binding_digest', 'payload_digest', 'outcome'), ('decision',))
    if (type(data['version']) is not int or data['version'] != 1 or data['run_id'] != challenge['run_id'] or
            data['invocation_id'] != invocation.invocation_id or data['binding_digest'] != invocation.binding_digest or
            data['payload_digest'] != invocation.payload_digest):
        reject()
    if clock() >= invocation.deadline or cancelled():
        reject()
    if data['outcome'] == 'return_to_native' and 'decision' not in data:
        return b''
    if data['outcome'] != 'answer' or 'decision' not in data:
        reject()
    out = invocation.native_output(data['decision'])
    if clock() >= invocation.deadline or cancelled():
        reject()
    return out
