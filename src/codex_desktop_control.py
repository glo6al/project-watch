"""Version-gated local desktop control; no router startup or delivery retries.

This internal desktop protocol is not a public API. Steering targets the turn
active when the owner handles it (the desktop may retarget a turn race).
Interruption pins a turn but retains normal user-stop descendant cleanup.
"""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import socket
import stat
import struct
import sys
import threading
import time
import uuid

_APP = Path('/Applications/ChatGPT.app')
_VERSION = '26.1016.62342'
_BUILD = '21477'
_ASAR_SHA256 = '5b5cf52887cfd4de19cb5862155a8ef6cf58830c0a2fc592b72060cfcc3be869'
_MAX_ASAR = 1024 * 1024 * 1024
_HASH_SECONDS = 8
_TIMEOUT = 15
_MAX_FRAME = 1024 * 1024
_MAX_TOTAL = 4 * 1024 * 1024
_MAX_FRAMES = 100
_build_cache = None
_build_lock = threading.Lock()


class _Invalid(Exception):
    pass


def _require(condition):
    if not condition:
        raise _Invalid()


def _id(value):
    _require(isinstance(value, str) and str(uuid.UUID(value)) == value)
    return value


def _signature(meta):
    return (meta.st_dev, meta.st_ino, meta.st_size, meta.st_mtime_ns, meta.st_ctime_ns)


def _file(path, maximum):
    """Open only a bounded ordinary file, not a final symlink or device."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        meta = os.fstat(fd)
        _require(stat.S_ISREG(meta.st_mode) and 0 < meta.st_size <= maximum)
        return os.fdopen(fd, 'rb'), meta
    except Exception:
        os.close(fd)
        raise


def _verify_build():
    global _build_cache
    _require(sys.platform == 'darwin')
    info_path = _APP / 'Contents/Info.plist'
    archive_path = _APP / 'Contents/Resources/app.asar'
    with _build_lock:
        info_file, info_meta = _file(info_path, 1024 * 1024)
        with info_file:
            info = plistlib.loads(info_file.read(1024 * 1024 + 1))
        _require(info.get('CFBundleShortVersionString') == _VERSION
                 and str(info.get('CFBundleVersion')) == _BUILD)
        archive, archive_meta = _file(archive_path, _MAX_ASAR)
        with archive:
            key = (str(_APP), _signature(info_meta), _signature(archive_meta), _ASAR_SHA256)
            if _build_cache == key:
                return
            digest = hashlib.sha256()
            deadline = time.monotonic() + _HASH_SECONDS
            length = 0
            while True:
                _require(time.monotonic() < deadline)
                chunk = archive.read(1024 * 1024)
                if not chunk:
                    break
                length += len(chunk)
                _require(length <= _MAX_ASAR)
                digest.update(chunk)
            _require(length == archive_meta.st_size
                     and _signature(os.fstat(archive.fileno())) == _signature(archive_meta)
                     and digest.hexdigest() == _ASAR_SHA256)
            _build_cache = key


def _endpoint():
    return Path.home() / '.codex/ipc/ipc.sock'


def _verify_endpoint(path):
    _require(path.resolve() == path)
    identities = []
    for item, kind, mode in ((path.parent, stat.S_ISDIR, 0o700),
                             (path, stat.S_ISSOCK, 0o600)):
        meta = item.lstat()
        _require(kind(meta.st_mode) and meta.st_uid == os.getuid()
                 and stat.S_IMODE(meta.st_mode) == mode)
        identities.append((meta.st_dev, meta.st_ino))
    return identities


def preflight():
    """Read-only capability check, including installed build and socket privacy."""
    try:
        _verify_build()
        _verify_endpoint(_endpoint())
        return {'available': True, 'error': ''}
    except Exception:
        return {'available': False, 'error': 'Compatible local Codex desktop control is unavailable.'}


class _Connection:
    def __init__(self, path):
        self.sock = None
        self.deadline = time.monotonic() + _TIMEOUT
        self.frames = 0
        self.total = 0
        self.mutation_attempted = False
        identity = _verify_endpoint(path)
        try:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._remaining()
            self.sock.connect(str(path))
            _require(_verify_endpoint(path) == identity)
        except Exception:
            self.close()
            raise

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def _remaining(self):
        remaining = self.deadline - time.monotonic()
        _require(remaining > 0)
        self.sock.settimeout(remaining)

    def _send(self, message, mutation=False):
        data = json.dumps(message, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
        _require(0 < len(data) <= _MAX_FRAME)
        self._remaining()
        # Anything from this point may have been partially delivered.
        if mutation:
            self.mutation_attempted = True
        self.sock.sendall(struct.pack('<I', len(data)) + data)

    def _read(self, size):
        result = bytearray()
        while len(result) < size:
            self._remaining()
            part = self.sock.recv(size - len(result))
            _require(bool(part))
            result.extend(part)
        return bytes(result)

    def request(self, method, params, client, version, owner=None):
        request_id = str(uuid.uuid4())
        message = {'type': 'request', 'requestId': request_id, 'sourceClientId': client,
                   'version': version, 'method': method, 'params': params, 'timeoutMs': 5000}
        if owner is not None:
            message['targetClientId'] = owner
        self._send(message, mutation=owner is not None)
        while True:
            self.frames += 1
            _require(self.frames <= _MAX_FRAMES)
            size = struct.unpack('<I', self._read(4))[0]
            self.total += size + 4
            _require(0 < size <= _MAX_FRAME and self.total <= _MAX_TOTAL)
            response = json.loads(self._read(size))
            _require(isinstance(response, dict))
            kind = response.get('type')
            if kind == 'broadcast':
                continue
            if kind == 'client-discovery-request':
                self._send({'type': 'client-discovery-response',
                            'requestId': _id(response.get('requestId')),
                            'response': {'canHandle': False}})
                continue
            if kind == 'request':
                self._send({'type': 'response', 'requestId': _id(response.get('requestId')),
                            'resultType': 'error', 'error': 'no-handler-for-request'})
                continue
            _require(kind == 'response' and response.get('requestId') == request_id
                     and response.get('resultType') == 'success' and response.get('method') == method)
            handled = _id(response.get('handledByClientId'))
            _require(owner is None or handled == owner)
            _require(isinstance(response.get('result'), dict))
            return response


def execute(action, conversation_id, instruction='', expected_turn_id=None, cwd=None):
    """One attempt only. The caller must persist intent before calling this.

    The caller also verifies fresh active-turn state. This adapter neither reads
    history nor starts/resumes sessions, and never converts failure into queueing.
    Returned errors intentionally omit private request or provider details.
    """
    connection = None
    try:
        _require(action in ('steer', 'interrupt'))
        _id(conversation_id)
        if action == 'interrupt':
            _id(expected_turn_id)
            _require(instruction == '')
        else:
            _require(isinstance(instruction, str) and bool(instruction.strip()) and len(instruction) <= 4000)
            if expected_turn_id is not None:
                _id(expected_turn_id)  # Validated only; the desktop steering interface cannot pin it.
            _require(cwd is None or (isinstance(cwd, str) and os.path.isabs(cwd)
                                    and len(cwd) <= 4096 and '\x00' not in cwd))
        _verify_build()
        connection = _Connection(_endpoint())
        init = connection.request('initialize', {'clientType': 'watch-officer-control'}, 'initializing-client', 0)
        client = _id(init['result'].get('clientId'))
        _require(init['handledByClientId'] == client)
        owner = connection.request('thread-owner-discovery',
                                   {'hostId': 'local', 'conversationId': conversation_id}, client, 1)
        owner_id = _id(owner['handledByClientId'])
        _require(owner_id != client and owner['result'].get('supportsUntrustedAppInput') is True)
        if action == 'steer':
            message_id = str(uuid.uuid4())
            directory = cwd or '/'
            restore = {'id': message_id, 'text': instruction, 'cwd': directory,
                       'createdAt': int(time.time() * 1000),
                       'context': {'prompt': instruction, 'addedFiles': [], 'fileAttachments': [],
                                   'ideContext': None, 'imageAttachments': [], 'workspaceRoots': [directory]}}
            response = connection.request('thread-follower-steer-turn',
                {'conversationId': conversation_id,
                 'input': [{'type': 'text', 'text': instruction, 'text_elements': []}],
                 'restoreMessage': restore, 'attachments': [], 'clientUserMessageId': message_id},
                client, 1, owner_id)
            result = response['result'].get('result')
            _require(isinstance(result, dict))
            _id(result.get('turnId'))
            return {'state': 'acknowledged', 'error': ''}
        response = connection.request('thread-follower-interrupt-turn',
            {'conversationId': conversation_id, 'expectedTurnId': expected_turn_id, 'mode': 'user-stop'},
            client, 4, owner_id)
        result = response['result']
        _require(result.get('ok') is True and 'interruptedTurnId' in result)
        if result.get('goalPauseError') is not None:
            return {'state': 'unknown', 'error': 'Codex reported a partial interruption; check the conversation.'}
        interrupted = result['interruptedTurnId']
        if interrupted is None:
            return {'state': 'no_op', 'error': 'The selected turn was no longer active; nothing was interrupted.'}
        _require(_id(interrupted) == expected_turn_id)
        return {'state': 'interrupted', 'error': ''}
    except Exception:
        if connection is not None and connection.mutation_attempted:
            return {'state': 'unknown', 'error': 'Control outcome is unknown. Check the conversation; do not automatically retry.'}
        return {'state': 'failed', 'error': 'Codex control could not be sent. Verify desktop compatibility and the active conversation.'}
    finally:
        if connection is not None:
            connection.close()
