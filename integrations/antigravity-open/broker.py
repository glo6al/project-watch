"""Disposable native Antigravity navigation proof; standard library only.

Importing this module does no I/O. There are no prompt, agent, model, MCP,
arbitrary RPC, browser automation, or native debugging endpoints.
"""
import hmac
import html
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import signal
import sqlite3
import stat
import time
import urllib.parse
import uuid

SIDECAR_ID = 'watch-open-prototype'
COMMAND_SECONDS, LEASE_SECONDS = 5, 8
MAX_BODY, MAX_REQUESTS = 2048, 64
TERMINAL = frozenset(('called', 'failed', 'expired', 'unknown'))
HERE = Path(__file__).resolve().parent


class ProtocolError(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def canonical_uuid(value, request=False):
    try:
        parsed = uuid.UUID(value)
        return isinstance(value, str) and str(parsed) == value and (not request or parsed.version == 4)
    except (ValueError, TypeError, AttributeError):
        return False


def exact(value, names):
    return isinstance(value, dict) and set(value) == set(names)


def native_parent(value):
    try:
        u = urllib.parse.urlsplit(value)
        return (u.scheme == 'https' and u.hostname == '127.0.0.1' and u.port is not None
                and 1 <= u.port <= 65535 and value == 'https://127.0.0.1:%d' % u.port)
    except (ValueError, TypeError, AttributeError):
        return False


def regular(path):
    try:
        info = os.lstat(path)
        return (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                and os.path.abspath(path) == os.path.realpath(path))
    except OSError:
        return False


class Broker:
    def __init__(self, source_root, allowed_ids, owner_token, ui_token, parent_origin,
                 clock=time.monotonic, wall=time.time):
        self.root = Path(os.path.abspath(source_root))
        if (self.root.name != 'antigravity' or self.root.parent.name != '.gemini'
                or str(self.root) != os.path.realpath(self.root)):
            raise ValueError('Only a canonical Antigravity App source is supported.')
        if (not isinstance(allowed_ids, (list, tuple, set, frozenset)) or not 1 <= len(allowed_ids) <= 2
                or not all(canonical_uuid(v) for v in allowed_ids)):
            raise ValueError('Register one or two existing App conversations.')
        self.allowed = frozenset(allowed_ids)
        if not all(isinstance(v, str) and 16 <= len(v) <= 256 and v.isascii() and not any(c.isspace() for c in v)
                   for v in (owner_token, ui_token)) or hmac.compare_digest(owner_token, ui_token):
            raise ValueError('Separate owner and native UI capabilities are required.')
        if not native_parent(parent_origin):
            raise ValueError('The observed native parent origin must be pinned.')
        self.owner_token, self.ui_token, self.parent_origin = owner_token, ui_token, parent_origin
        self.clock, self.wall = clock, wall
        self.ui_origin = None
        self.page = None
        self.requests = {}

    def _target(self, target):
        if target not in self.allowed or not canonical_uuid(target):
            return False
        database = self.root / 'conversation_summaries.db'
        conn = None
        try:
            if str(self.root) != os.path.realpath(self.root) or not regular(database):
                return False
            for suffix in ('-wal', '-shm'):
                p = Path(str(database) + suffix)
                if os.path.lexists(p) and not regular(p):
                    return False
            if Path(str(database) + '-wal').exists() and not Path(str(database) + '-shm').exists():
                return False
            conn = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=.05)
            conn.execute('PRAGMA query_only=ON')
            deadline = time.monotonic() + .1
            conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            rows = conn.execute('SELECT conversation_id FROM conversation_summaries '
                                'WHERE conversation_id=? LIMIT 2', (target,)).fetchall()
            return rows == [(target,)]
        except (OSError, ValueError, sqlite3.Error):
            return False
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def public(row):
        return dict(request_id=row['request_id'], conversation_id=row['conversation_id'],
                    state=row['state'], confirmed=False)

    def _advance(self):
        now = self.clock()
        if self.page and now >= self.page['expires']:
            self.page = None
        for row in self.requests.values():
            if row['state'] in ('queued', 'claimed') and (now >= row['expires'] or not self.page
                    or row['lease'] != self.page['lease']):
                row['state'] = 'expired' if row['state'] == 'queued' else 'unknown'

    def _page(self, page_id, lease):
        self._advance()
        if (not self.page or self.page['page_id'] != page_id or not isinstance(lease, str)
                or not hmac.compare_digest(self.page['lease'], lease)):
            raise ProtocolError(409, 'The native UI lease is unavailable.')
        self.page['expires'] = self.clock() + LEASE_SECONDS

    def owner_open(self, request_id, conversation_id):
        if not canonical_uuid(request_id, request=True) or not canonical_uuid(conversation_id):
            raise ProtocolError(400, 'Valid exact request and conversation identifiers are required.')
        self._advance()
        previous = self.requests.get(request_id)
        if previous:
            if previous['conversation_id'] != conversation_id:
                raise ProtocolError(409, 'That request identifier already has a different target.')
            return self.public(previous)
        if not self._target(conversation_id):
            raise ProtocolError(404, 'The registered existing App conversation is unavailable.')
        self._advance()
        if not self.page:
            raise ProtocolError(503, 'The registered native UI is not connected.')
        if any(r['state'] in ('queued', 'claimed') for r in self.requests.values()):
            raise ProtocolError(409, 'A navigation request is already unresolved.')
        if len(self.requests) >= MAX_REQUESTS:
            raise ProtocolError(429, 'This disposable connector has reached its request limit.')
        row = dict(request_id=request_id, conversation_id=conversation_id, state='queued',
                   expires=self.clock() + COMMAND_SECONDS, lease=self.page['lease'])
        self.requests[request_id] = row
        return self.public(row)

    def status(self, request_id=None):
        self._advance()
        if request_id is None:
            return dict(connected=bool(self.page), pending=any(r['state'] in ('queued', 'claimed') for r in self.requests.values()))
        if not canonical_uuid(request_id, request=True):
            raise ProtocolError(400, 'A valid request identifier is required.')
        row = self.requests.get(request_id)
        if row is None:
            raise ProtocolError(404, 'That request is not retained by this connector.')
        return self.public(row)

    def hello(self, page_id, parent_origin):
        if not canonical_uuid(page_id, request=True) or parent_origin != self.parent_origin:
            raise ProtocolError(400, 'The native UI identity or parent is invalid.')
        self._advance()
        if self.page and self.page['page_id'] != page_id:
            raise ProtocolError(409, 'Another native UI already holds this connector.')
        if self.page is None:
            self.page = dict(page_id=page_id, lease=secrets.token_hex(32), expires=0)
        self.page['expires'] = self.clock() + LEASE_SECONDS
        return dict(page_id=page_id, lease=self.page['lease'], lease_ms=LEASE_SECONDS * 1000)

    def poll(self, page_id, lease):
        self._page(page_id, lease)
        for row in self.requests.values():
            if row['state'] == 'queued':
                if not self._target(row['conversation_id']):
                    row['state'] = 'expired'
                    return dict(command=None)
                self._advance()
                if row['state'] != 'queued':
                    return dict(command=None)
                row['state'] = 'claimed'  # Before HTTP output: losing that output never redelivers.
                remaining = max(1, min(5000, int((row['expires'] - self.clock()) * 1000)))
                return dict(command=dict(request_id=row['request_id'], conversation_id=row['conversation_id'],
                                         expires_at_ms=int(self.wall() * 1000) + remaining, remaining_ms=remaining))
        return dict(command=None)

    def receipt(self, page_id, lease, request_id, conversation_id, state):
        self._page(page_id, lease)
        row = self.requests.get(request_id) if isinstance(request_id, str) else None
        if (not row or row['lease'] != lease or row['conversation_id'] != conversation_id
                or state not in ('called', 'failed') or row['state'] not in ('claimed', state)):
            raise ProtocolError(409, 'The receipt does not match a current claimed navigation.')
        row['state'] = state
        return self.public(row)

    def bye(self, page_id, lease):
        self._page(page_id, lease)
        self.page = None
        self._advance()
        return dict(closed=True)

    def close(self):
        self.page = None
        self._advance()


def _json(raw):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError('duplicate key')
            out[key] = value
        return out
    return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))


class BoundedReader:
    """One absolute request deadline and byte ceiling, including headers."""
    def __init__(self, stream, connection):
        self.stream, self.connection = stream, connection
        self.deadline, self.remaining = time.monotonic() + 2, 16384 + MAX_BODY

    def _call(self, method, size):
        remaining_time = self.deadline - time.monotonic()
        if remaining_time <= 0 or self.remaining <= 0:
            raise TimeoutError('Request bounds exceeded.')
        self.connection.settimeout(remaining_time)
        limit = self.remaining + 1 if size < 0 else min(size, self.remaining + 1)
        out = getattr(self.stream, method)(limit)
        self.remaining -= len(out)
        if self.remaining < 0:
            raise TimeoutError('Request bounds exceeded.')
        return out

    def readline(self, size=-1):
        return self._call('readline', size)

    def read(self, size=-1):
        return self._call('read', size)

    def close(self):
        return self.stream.close()


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version, sys_version = 'WATCH Open Test', ''

    def log_message(self, *args):
        pass

    def send_error(self, code, *args, **kwargs):
        self.send(code, dict(error='Unsupported or invalid request.'))

    def setup(self):
        super().setup()
        self.connection.settimeout(2)
        self.rfile = BoundedReader(self.rfile, self.connection)

    def send(self, status, value, content_type='application/json'):
        raw = json.dumps(value).encode() if content_type == 'application/json' else value
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; connect-src 'self'; frame-ancestors " + self.server.broker.parent_origin)
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(raw)
        except OSError:
            pass

    def _common(self):
        hosts = self.headers.get_all('Host') or []
        port = self.server.server_port
        if len(self.path) > 2048:
            raise ProtocolError(400, 'Invalid request path.')
        url = urllib.parse.urlsplit(self.path)
        native_route = (not (url.scheme or url.netloc or url.fragment)
                        and ((self.command == 'GET' and (url.path == '/'
                              or (url.path == '/connector.js' and not url.query)))
                             or (self.command == 'POST' and not url.query
                                 and url.path in ('/ui/hello', '/ui/poll', '/ui/receipt', '/ui/bye'))))
        forwarded = False
        if native_route and len(hosts) == 1 and hosts[0].isascii() and hosts[0].startswith('localhost:'):
            forwarded_port = hosts[0][len('localhost:'):]
            forwarded = (1 <= len(forwarded_port) <= 5 and forwarded_port.isdigit()
                         and forwarded_port[0] != '0' and int(forwarded_port) <= 65535)
        if len(hosts) != 1 or (hosts[0] not in ('127.0.0.1:%d' % port, 'localhost:%d' % port)
                               and not forwarded):
            raise ProtocolError(403, 'Forbidden.')
        if self.headers.get('Transfer-Encoding') is not None:
            raise ProtocolError(400, 'Unsupported request framing.')
        return url

    def _auth(self, role):
        broker = self.server.broker
        if role == 'owner':
            if self.headers.get_all('Origin') or len(self.headers.get_all('Authorization') or []) != 1:
                raise ProtocolError(403, 'Forbidden.')
            provided, expected = self.headers.get('Authorization'), 'Bearer ' + broker.owner_token
        else:
            origins = self.headers.get_all('Origin') or []
            if (origins not in ([], [broker.ui_origin])
                    or len(self.headers.get_all('X-Sidecar-Token') or []) != 1):
                raise ProtocolError(403, 'Forbidden.')
            provided, expected = self.headers.get('X-Sidecar-Token'), broker.ui_token
        if not isinstance(provided, str) or not hmac.compare_digest(provided.encode(), expected.encode()):
            raise ProtocolError(403, 'Forbidden.')

    def do_GET(self):
        try:
            url = self._common()
            if url.fragment or url.scheme or url.netloc:
                raise ProtocolError(400, 'Invalid request path.')
            if url.path == '/owner/status':
                self._auth('owner')
                query = urllib.parse.parse_qs(url.query, keep_blank_values=True, strict_parsing=True)
                if set(query) - {'request_id'} or any(len(v) != 1 for v in query.values()):
                    raise ProtocolError(400, 'Invalid status query.')
                return self.send(200, self.server.broker.status(query.get('request_id', [None])[0]))
            if url.path == '/connector.js' and not url.query:
                return self.send(200, (HERE / 'connector.js').read_bytes(), 'application/javascript; charset=utf-8')
            if url.path == '/':
                query = urllib.parse.parse_qs(url.query, keep_blank_values=True, strict_parsing=True)
                if (set(query) - {'token', 'conversationId'} or 'token' not in query
                        or any(len(values) != 1 for values in query.values())
                        or not hmac.compare_digest(query['token'][0].encode(), self.server.broker.ui_token.encode())):
                    raise ProtocolError(403, 'Forbidden.')
                if 'conversationId' in query:
                    target = query['conversationId'][0]
                    if not canonical_uuid(target) or target not in self.server.broker.allowed:
                        raise ProtocolError(403, 'Forbidden.')
                page = (HERE / 'index.html').read_text(encoding='utf-8')
                page = page.replace('{{NATIVE_PARENT}}', html.escape(self.server.broker.parent_origin, quote=True))
                page = page.replace('{{NATIVE_ORIGIN}}', html.escape(self.server.broker.ui_origin, quote=True))
                return self.send(200, page.encode(), 'text/html; charset=utf-8')
            raise ProtocolError(404, 'Not found.')
        except ProtocolError as error:
            self.send(error.status, dict(error=str(error)))
        except (ValueError, UnicodeError, OSError):
            self.send(400, dict(error='Invalid request.'))

    def do_POST(self):
        try:
            url = self._common()
            if url.query or url.fragment or url.scheme or url.netloc:
                raise ProtocolError(400, 'Invalid request path.')
            routes = {'/owner/open': ('owner', ('request_id', 'conversation_id'), 'owner_open'),
                      '/ui/hello': ('ui', ('page_id', 'parent_origin'), 'hello'),
                      '/ui/poll': ('ui', ('page_id', 'lease'), 'poll'),
                      '/ui/receipt': ('ui', ('page_id', 'lease', 'request_id', 'conversation_id', 'state'), 'receipt'),
                      '/ui/bye': ('ui', ('page_id', 'lease'), 'bye')}
            if url.path not in routes:
                raise ProtocolError(404, 'Not found.')
            role, names, method = routes[url.path]
            self._auth(role)
            lengths = self.headers.get_all('Content-Length') or []
            if len(lengths) != 1 or not lengths[0].isdigit() or not 1 <= int(lengths[0]) <= MAX_BODY:
                raise ProtocolError(400, 'Invalid request length.')
            if self.headers.get_all('Content-Type') != ['application/json']:
                raise ProtocolError(400, 'JSON content is required.')
            raw = self.rfile.read(int(lengths[0]))
            if len(raw) != int(lengths[0]):
                raise ProtocolError(400, 'Incomplete request.')
            body = _json(raw)
            if not exact(body, names):
                raise ProtocolError(400, 'Invalid request fields.')
            result = getattr(self.server.broker, method)(**body)
            self.send(200, result)
        except ProtocolError as error:
            self.send(error.status, dict(error=str(error)))
        except (ValueError, UnicodeError, OSError, TypeError):
            self.send(400, dict(error='Invalid request.'))


def make_server(broker, port=0):
    class QuietServer(HTTPServer):
        allow_reuse_address = False

        def handle_error(self, request, client_address):
            # No raw request, credential, or local diagnostic is logged.
            pass
    server = QuietServer(('127.0.0.1', port), Handler)
    server.broker = broker
    broker.ui_origin = 'plugin://%s-%d' % (SIDECAR_ID, server.server_port)
    return server


def _private_read(path):
    if not regular(path):
        raise ValueError('The private prototype configuration is unavailable.')
    info = os.stat(path)
    if os.name == 'nt' or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 4096:
        raise ValueError('The private prototype configuration is not protected.')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError('The private prototype configuration changed.')
        return _json(os.read(fd, 4097))
    finally:
        os.close(fd)


def main():
    # Disposable macOS test setup only; no installation or enabling is performed.
    if os.name == 'nt':
        raise ValueError('This native desktop prototype has not been enabled on Windows.')
    private = Path.home() / '.live-room' / 'antigravity-open-prototype'
    info = os.lstat(private)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
            or str(private) != os.path.realpath(private)):
        raise ValueError('A protected prototype directory is required.')
    config = _private_read(private / 'config.json')
    if not exact(config, ('allowed_ids', 'parent_origin')) or not isinstance(config['allowed_ids'], list):
        raise ValueError('The bounded prototype configuration is invalid.')
    port_text = os.environ.get('ANTIGRAVITY_SIDECAR_WEB_PORT', '')
    if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
        raise ValueError('The native sidecar web port is unavailable.')
    owner = secrets.token_hex(32)
    broker = Broker(Path.home() / '.gemini' / 'antigravity', config['allowed_ids'], owner,
                    os.environ.get('ANTIGRAVITY_SIDECAR_UI_TOKEN', ''), config['parent_origin'])
    server = make_server(broker, int(port_text))
    receipt = private / 'connection.json'
    generation = secrets.token_hex(16)
    fd = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(dict(url='http://127.0.0.1:%d' % server.server_port, owner_token=owner,
                           generation=generation), f)
        def stop(*_):
            raise SystemExit()
        signal.signal(signal.SIGTERM, stop)
        server.serve_forever(poll_interval=.25)
    finally:
        broker.close()
        server.server_close()
        try:
            current = _private_read(receipt)
            if current.get('generation') == generation:
                receipt.unlink()
        except (OSError, ValueError):
            pass


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError):
        raise SystemExit('WATCH Open Test could not start. Check its private setup; no action was performed.')
