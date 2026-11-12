"""Owner-authorized native Antigravity navigation; standard library only.

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
import time
import urllib.parse
import uuid
import sys

# A normal global sidecar loads the same reviewed helper beside this file.
if not (Path(__file__).resolve().parent / 'antigravity_navigation.py').exists():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
import antigravity_navigation as navigation

SIDECAR_ID = navigation.ID
COMMAND_SECONDS, LEASE_SECONDS, RETENTION_SECONDS = 5, 8, 30
MAX_BODY, MAX_REQUESTS = 2048, 2048
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


class Broker:
    def __init__(self, home, owner_token, ui_token, runtime, generation=None,
                 clock=time.monotonic, wall=time.time):
        self.home = Path(home)
        if not all(type(v) is str and 16 <= len(v) <= 256 and v.isascii()
                   and not any(c.isspace() for c in v) for v in (owner_token, ui_token)):
            raise ValueError('Separate owner and native UI capabilities are required.')
        if hmac.compare_digest(owner_token, ui_token) or not native_parent(runtime.get('origin')):
            raise ValueError('The native connection is invalid.')
        self.generation = generation or secrets.token_hex(16)
        if (type(self.generation) is not str or len(self.generation) != 32
                or any(c not in '0123456789abcdef' for c in self.generation)):
            raise ValueError('The native generation is invalid.')
        self.owner_token, self.ui_token = owner_token, ui_token
        self.runtime, self.parent_origin = dict(runtime), runtime['origin']
        self.clock, self.wall = clock, wall
        self.ui_origin = None
        self.page = None
        self.requests = {}

    def _authorized(self):
        return navigation.authorized(self.home)

    def _target(self, target):
        return self._authorized() and navigation.existing_app_chat(self.home, target)

    def public(self, row):
        return dict(protocol=1, generation=self.generation,
                    request_id=row['request_id'], conversation_id=row['conversation_id'],
                    state=row['state'], confirmed=False)

    def _advance(self):
        now = self.clock()
        if self.page and (now >= self.page['expires'] or not self._authorized()):
            self.page = None
        for row in self.requests.values():
            if row['state'] in ('queued', 'claimed') and (now >= row['expires'] or not self.page
                    or row['lease'] != self.page['lease']):
                row['state'] = 'expired' if row['state'] == 'queued' else 'unknown'
        self.requests = {key: row for key, row in self.requests.items()
                         if row['state'] not in TERMINAL or now < row['expires'] + RETENTION_SECONDS}

    def _page(self, page_id, lease):
        self._advance()
        if (not self.page or self.page['page_id'] != page_id or not isinstance(lease, str)
                or not hmac.compare_digest(self.page['lease'], lease)):
            raise ProtocolError(409, 'The native connection is unavailable.')
        self.page['expires'] = self.clock() + LEASE_SECONDS

    def owner_open(self, request_id, conversation_id, expires_at_ms):
        if not canonical_uuid(request_id, request=True) or not canonical_uuid(conversation_id):
            raise ProtocolError(400, 'Valid exact request and conversation identifiers are required.')
        if type(expires_at_ms) is not int or not 0 < expires_at_ms <= 9007199254740991:
            raise ProtocolError(400, 'A bounded navigation expiry is required.')
        self._advance()
        if not self._authorized():
            raise ProtocolError(403, 'Antigravity navigation is disconnected.')
        previous = self.requests.get(request_id)
        if previous:
            if previous['conversation_id'] != conversation_id:
                raise ProtocolError(409, 'That request identifier already has a different target.')
            # The originally accepted expiry is immutable, including retries with
            # a later expiry. A terminal/uncertain request is never requeued.
            return self.public(previous)
        remaining = expires_at_ms - int(self.wall() * 1000)
        if not 0 < remaining <= COMMAND_SECONDS * 1000:
            raise ProtocolError(400, 'This navigation request has expired or is too far ahead.')
        if not self._target(conversation_id):
            raise ProtocolError(404, 'That existing Antigravity desktop chat is unavailable.')
        try:
            navigation.verify_runtime(self.runtime, self.home)
        except navigation.NavigationError:
            raise ProtocolError(503, 'The native desktop connection is unavailable.') from None
        self._advance()
        if not self.page:
            raise ProtocolError(503, 'Open WATCH Navigation from Extensions in an existing Antigravity chat.')
        if any(r['state'] in ('queued', 'claimed') for r in self.requests.values()):
            raise ProtocolError(409, 'A navigation request is already unresolved.')
        if len(self.requests) >= MAX_REQUESTS:
            raise ProtocolError(429, 'Navigation is temporarily busy. Try again shortly.')
        row = dict(request_id=request_id, conversation_id=conversation_id, state='queued',
                   expires=self.clock() + remaining / 1000, expires_at_ms=expires_at_ms,
                   lease=self.page['lease'])
        self.requests[request_id] = row
        return self.public(row)

    def status(self, request_id=None):
        self._advance()
        if request_id is None:
            return dict(protocol=1, generation=self.generation, connected=bool(self.page),
                        pending=any(r['state'] in ('queued', 'claimed') for r in self.requests.values()))
        if not canonical_uuid(request_id, request=True):
            raise ProtocolError(400, 'A valid request identifier is required.')
        row = self.requests.get(request_id)
        if row is None:
            raise ProtocolError(404, 'That request is no longer retained.')
        return self.public(row)

    def hello(self, page_id, parent_origin):
        if not canonical_uuid(page_id, request=True) or parent_origin != self.parent_origin:
            raise ProtocolError(400, 'The native connection identity is invalid.')
        self._advance()
        if not self._authorized():
            raise ProtocolError(403, 'Antigravity navigation is disconnected.')
        if self.page and self.page['page_id'] != page_id:
            return dict(page_id=page_id, passive=True, lease_ms=LEASE_SECONDS * 1000)
        if self.page is None:
            self.page = dict(page_id=page_id, lease=secrets.token_hex(32), expires=0)
        self.page['expires'] = self.clock() + LEASE_SECONDS
        return dict(page_id=page_id, passive=False, lease=self.page['lease'], lease_ms=LEASE_SECONDS * 1000)

    def poll(self, page_id, lease):
        self._page(page_id, lease)
        for row in self.requests.values():
            if row['state'] == 'queued':
                if not self._target(row['conversation_id']):
                    row['state'] = 'expired'
                    return dict(command=None)
                try:
                    navigation.verify_runtime(self.runtime, self.home)
                except navigation.NavigationError:
                    row['state'] = 'expired'
                    return dict(command=None)
                self._advance()
                if row['state'] != 'queued':
                    return dict(command=None)
                row['state'] = 'claimed'  # Consume before HTTP output: never redeliver.
                remaining = max(1, min(5000, int((row['expires'] - self.clock()) * 1000),
                                       row['expires_at_ms'] - int(self.wall() * 1000)))
                if row['expires_at_ms'] <= int(self.wall() * 1000):
                    row['state'] = 'unknown'
                    return dict(command=None)
                return dict(command=dict(request_id=row['request_id'], conversation_id=row['conversation_id'],
                                         expires_at_ms=row['expires_at_ms'], remaining_ms=remaining))
        return dict(command=None)

    def receipt(self, page_id, lease, request_id, conversation_id, state):
        self._page(page_id, lease)
        row = self.requests.get(request_id) if isinstance(request_id, str) else None
        if (not row or row['lease'] != lease or row['conversation_id'] != conversation_id
                or state not in ('called', 'failed') or row['state'] not in ('claimed', state)):
            raise ProtocolError(409, 'The receipt does not match a current navigation request.')
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
    server_version, sys_version = 'WATCH Navigation', ''

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
                # Python 3.9 rejects an empty query in strict mode. Absence is
                # valid here; nonempty queries still require strict field syntax.
                query = urllib.parse.parse_qs(url.query, keep_blank_values=True, strict_parsing=True) if url.query else {}
                if set(query) - {'request_id'} or any(len(v) != 1 for v in query.values()):
                    raise ProtocolError(400, 'Invalid status query.')
                return self.send(200, self.server.broker.status(query.get('request_id', [None])[0]))
            if url.path == '/connector.js' and not url.query:
                return self.send(200, (HERE / 'connector.js').read_bytes(), 'application/javascript; charset=utf-8')
            if url.path == '/':
                query = urllib.parse.parse_qs(url.query, keep_blank_values=True, strict_parsing=True) if url.query else {}
                if (set(query) - {'token', 'conversationId'} or 'token' not in query
                        or any(len(values) != 1 for values in query.values())
                        or not hmac.compare_digest(query['token'][0].encode(), self.server.broker.ui_token.encode())):
                    raise ProtocolError(403, 'Forbidden.')
                if 'conversationId' in query:
                    target = query['conversationId'][0]
                    if (not canonical_uuid(target)
                            or not navigation.existing_app_chat(self.server.broker.home, target)):
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
            routes = {'/owner/open': ('owner', ('request_id', 'conversation_id', 'expires_at_ms'), 'owner_open'),
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



def main():
    if sys.platform != 'darwin':
        raise navigation.NavigationError('unsupported')
    home = Path.home()
    try:
        if not navigation._owned_before(home):
            return
        navigation._installed(home, navigation._manifest(home))
    except (OSError, navigation.NavigationError):
        return  # An unowned/changed package cannot create a background connection.
    with navigation.process_lock(home):
        runtime = navigation.runtime_for_backend(os.getppid(), home)
        port_text = os.environ.get('ANTIGRAVITY_SIDECAR_WEB_PORT', '')
        if not port_text.isascii() or not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
            raise navigation.NavigationError()
        owner = secrets.token_hex(32)
        broker = Broker(home, owner, os.environ.get('ANTIGRAVITY_SIDECAR_UI_TOKEN', ''), runtime)
        server = make_server(broker, int(port_text))
        receipt = navigation.private_dir(home) / 'connection.json'
        before, _ = navigation.snapshot(receipt, private=True)
        current = navigation.process(os.getpid())
        value = dict(protocol=1, generation=broker.generation, url='http://127.0.0.1:%d' % server.server_port,
                     owner_token=owner, broker_pid=os.getpid(), broker_start=current['start'], runtime=runtime)
        try:
            navigation.replace(receipt, navigation.encoded(value), before)
            def stop(*_):
                raise SystemExit()
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            server.serve_forever(poll_interval=.25)
        finally:
            broker.close()
            server.server_close()
            try:
                if navigation.private_json(home, 'connection.json').get('generation') == broker.generation:
                    raw, _ = navigation.snapshot(receipt, private=True)
                    navigation.unlink(receipt, raw)
            except (OSError, navigation.NavigationError):
                pass


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, navigation.NavigationError):
        raise SystemExit('WATCH Navigation could not start. Reconnect it from WATCH.')
