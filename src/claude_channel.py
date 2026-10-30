"""Private local Claude channel discovery and bounded HTTP; no import-time I/O."""
import hashlib
import http.client
import json
import os
import re
import socket
import stat
import threading
import uuid

START_HINT = 'Start a new Claude session with python3 scripts/watch_claude.py; already-open sessions cannot attach.'
MAX_RESPONSE = 100000


class ChannelError(Exception):
    pass


def is_uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _private(st, directory=False):
    kind = stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode) and st.st_nlink == 1
    if not kind or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise ChannelError('Claude channel discovery must be private and owned by you. ' + START_HINT)


def _directory(path):
    """Walk directories by fd, rejecting links in every component."""
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in os.path.abspath(path).split(os.sep):
            if not part:
                continue
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def discover(root, session_id):
    if not is_uuid(session_id):
        raise ChannelError('Invalid Claude session identifier.')
    root_fd = session_fd = fd = None
    try:
        root_fd = _directory(root)
        _private(os.fstat(root_fd), True)
        session_fd = os.open(session_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        _private(os.fstat(session_fd), True)
        fd = os.open('connection.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=session_fd)
        _private(os.fstat(fd))
        raw = os.read(fd, 8193)
        if len(raw) > 8192:
            raise ChannelError('Claude channel discovery is too large.')
        d = json.loads(raw)
        if (not isinstance(d, dict) or type(d.get('version')) is not int or d['version'] != 1 or d.get('session_id') != session_id
                or not is_uuid(d.get('instance_id')) or type(d.get('port')) is not int or not 1 <= d['port'] <= 65535
                or not isinstance(d.get('token'), str) or not re.fullmatch(r'[0-9a-f]{64}', d['token'])):
            raise ChannelError('Claude channel discovery has invalid identity or connection fields. ' + START_HINT)
        return {k: d[k] for k in ('version', 'session_id', 'instance_id', 'port', 'token')}
    except (OSError, ValueError, TypeError):
        raise ChannelError('No readable private Claude channel is connected. ' + START_HINT) from None
    finally:
        for opened in (fd, session_fd, root_fd):
            if opened is not None:
                os.close(opened)


def identity(descriptor):
    return {k: descriptor[k] for k in ('session_id', 'instance_id', 'port')} | {
        'token_hash': hashlib.sha256(descriptor['token'].encode()).hexdigest()}


def pinned(root, expected):
    d = discover(root, expected['session_id'])
    if identity(d) != expected:
        raise ChannelError('The Claude channel instance changed. WATCH will not redirect or resend this request.')
    return d


def any_configured(root):
    try:
        # Capability reporting only; sends always revalidate their exact descriptor.
        with os.scandir(root) as entries:
            for n, item in enumerate(entries):
                if n >= 256:
                    break
                if is_uuid(item.name):
                    try:
                        discover(root, item.name)
                        return True
                    except ChannelError:
                        pass
    except OSError:
        pass
    return False


def exchange(descriptor, request_id, content=None, timeout=3):
    """No proxies/redirects; after an attempted HTTP write all errors are uncertain."""
    if not is_uuid(request_id):
        raise ChannelError('Invalid request identifier.')
    connection = http.client.HTTPConnection('127.0.0.1', descriptor['port'], timeout=timeout)
    connected = [None]
    def interrupt():
        sock = connected[0] or connection.sock
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
    timer = threading.Timer(timeout, interrupt)
    timer.daemon = True
    timer.start()
    try:
        headers = {'Authorization': 'Bearer ' + descriptor['token'], 'Content-Type': 'application/json'}
        body = None if content is None else json.dumps({'session_id': descriptor['session_id'], 'instance_id': descriptor['instance_id'], 'request_id': request_id, 'content': content}, ensure_ascii=False).encode('utf-8')
        connection.request('GET' if content is None else 'POST', '/requests/' + request_id if content is None else '/requests', body, headers)
        connected[0] = connection.sock
        response = connection.getresponse()
        raw = response.read(MAX_RESPONSE + 1)
        if response.status not in (200, 202) or len(raw) > MAX_RESPONSE:
            raise ChannelError('Claude channel response could not confirm this request.')
        d = json.loads(raw)
        if (not isinstance(d, dict) or any(d.get(k) != value for k, value in
                (('session_id', descriptor['session_id']), ('instance_id', descriptor['instance_id']), ('request_id', request_id)))
                or d.get('state') not in ('transport_written', 'answered', 'unknown')
                or not isinstance(d.get('reply', ''), str) or len(d.get('reply', '')) > 16000
                or not isinstance(d.get('error', ''), str) or len(d.get('error', '')) > 2000):
            raise ChannelError('Claude returned a mismatched or invalid request record.')
        return d
    except (OSError, http.client.HTTPException, ValueError, TypeError):
        raise ChannelError('Claude channel delivery could not be confirmed; WATCH will not resend.') from None
    finally:
        timer.cancel()
        connection.close()


def envelope(request_id, question):
    return ('WATCH_REQUEST ' + request_id + '\nThe owner asks through the Officer of the Watch:\n' + question +
            '\nUse this channel\'s reply tool with request_id ' + request_id + ' and your answer in text. '
            'Preserve the owner\'s scope and authorization: an explicit work instruction authorizes that work under your existing permissions; '
            'a status question asks only for information. Do not infer additional tasks or permissions. '
            'For a progress question, inspect your own current state.')
