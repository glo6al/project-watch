"""Owner-only WATCH gateway to the local native navigation connector.

No peer or model tool can invoke this adapter. Connection capabilities stay in
private files and HTTP headers; public state contains only availability facts.
"""
import http.client
import json
import re
import threading
import time
import urllib.parse
import uuid

import antigravity_navigation as navigation

STATES = frozenset(('unsupported', 'unsupported_version', 'setup_needed', 'disconnected',
                    'waiting_app', 'waiting_panel', 'ready', 'unavailable'))
MESSAGES = {
    'unsupported': 'Antigravity navigation currently requires macOS.',
    'unsupported_version': 'This Antigravity version is not supported by WATCH Navigation.',
    'setup_needed': 'Connect Antigravity to install WATCH Navigation once.',
    'disconnected': 'Antigravity navigation is disconnected.',
    'waiting_app': 'Open Antigravity to connect WATCH Navigation.',
    'waiting_panel': 'Open WATCH Navigation from Extensions in any existing Antigravity chat.',
    'ready': 'Antigravity navigation is connected.',
    'unavailable': 'Antigravity navigation is unavailable. Refresh or reconnect it.',
}
DISPATCHED = 'Opening requested in Antigravity; selection is not confirmed.'
UNCERTAIN = 'The opening request could not be confirmed. It will not be sent again automatically.'
UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z')
GENERATION = re.compile(r'[0-9a-f]{32}\Z')
MAX_RESPONSE = 4096


class OpenError(Exception):
    def __init__(self, code='unavailable', status=503, rejected=False):
        self.rejected = rejected
        self.code = code if code in STATES else 'unavailable'
        self.status = status
        super().__init__(MESSAGES[self.code])


class Adapter:
    def __init__(self, home, clock=time.monotonic, wall=time.time, sleep=time.sleep):
        self.home, self.clock, self.wall, self.sleep = home, clock, wall, sleep
        self._action = threading.Lock()
        self._configuration = threading.Lock()
        self._cache_lock = threading.Lock()
        self._cache = None
        self._epoch = 0

    def close(self):
        self.invalidate()

    def invalidate(self):
        with self._cache_lock:
            self._epoch += 1
            self._cache = None

    @staticmethod
    def _same(first, second):
        return isinstance(first, dict) and first == second

    def _connection(self, expected=None):
        try:
            connection = navigation.read_connection(self.home)
            if navigation.validate_connection(connection, self.home) is not True:
                raise OpenError()
        except navigation.NavigationError as error:
            code = error.code if error.code in ('unsupported', 'unsupported_version', 'disconnected', 'waiting_app', 'waiting_panel') else 'unavailable'
            raise OpenError(code) from None
        except (OSError, ValueError, TypeError):
            raise OpenError('waiting_app') from None
        if expected is not None and not self._same(connection, expected):
            raise OpenError('unavailable')
        return connection

    def _request(self, connection, method, path, body=None, timeout=1):
        # Re-read and verify ownership, live processes/listener and authorization
        # immediately before sending the capability. Never use proxy/redirect APIs.
        self._connection(connection)
        parsed = urllib.parse.urlsplit(connection['url'])
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.path
                or parsed.query or parsed.fragment or parsed.username or parsed.password
                or not parsed.port or connection['url'] != 'http://127.0.0.1:%d' % parsed.port):
            raise OpenError()
        client = http.client.HTTPConnection('127.0.0.1', parsed.port, timeout=max(.01, min(timeout, 1)))
        timer = None
        try:
            # A socket timeout alone can be extended indefinitely by trickled
            # headers. Close this one socket at an absolute transport deadline.
            client.connect()
            transport_socket = client.sock
            def expire():
                try:
                    transport_socket.shutdown(2)
                except (OSError, AttributeError):
                    pass
            timer = threading.Timer(max(.01, min(timeout, 1)), expire)
            timer.daemon = True
            timer.start()
            payload = json.dumps(body, separators=(',', ':')).encode() if body is not None else None
            client.request(method, path, payload, {'Authorization': 'Bearer ' + connection['owner_token'],
                           **({'Content-Type': 'application/json'} if payload is not None else {})})
            response = client.getresponse()
            if response.status != 200:
                raise OpenError('waiting_panel' if response.status in (409, 503) else 'unavailable',
                                status=response.status if response.status in (400, 403, 404, 409, 429, 503) else 503,
                                rejected=method == 'POST' and response.status in (400, 403, 404, 409, 429, 503))
            if response.getheader('Content-Type', '').split(';')[0] != 'application/json':
                raise OpenError()
            raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise OpenError()
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError()
                    result[key] = value
                return result
            out = json.loads(raw, object_pairs_hook=unique)
            if not isinstance(out, dict) or out.get('protocol') != 1 or type(out.get('protocol')) is not int or out.get('generation') != connection['generation']:
                raise OpenError()
            self._connection(connection)
            return out
        except (OSError, ValueError, TypeError, http.client.HTTPException):
            raise OpenError() from None
        finally:
            if timer is not None:
                timer.cancel()
            client.close()

    def _live(self, connection, timeout=1):
        out = self._request(connection, 'GET', '/owner/status', timeout=timeout)
        if (set(out) != {'protocol', 'generation', 'connected', 'pending'}
                or type(out['connected']) is not bool or type(out['pending']) is not bool):
            raise OpenError()
        if not out['connected']:
            raise OpenError('waiting_panel')
        return out

    def _eligible(self, aid, agent):
        return (isinstance(aid, str) and aid.startswith('a:') and UUID.fullmatch(aid[2:]) is not None
                and isinstance(agent, dict) and agent.get('runtime') == 'Antigravity'
                and not agent.get('observed') and navigation.existing_app_chat(self.home, aid[2:]))

    def status(self, agents, force=False):
        # Cache only connector readiness, never source eligibility or private data.
        with self._cache_lock:
            epoch, cached = self._epoch, self._cache
        now = self.clock()
        if not force and cached and now - cached[0] < 2:
            public = dict(cached[1])
        else:
            public = dict(supported=False, installed=False, authorized=False, available=False,
                          state='unavailable', message=MESSAGES['unavailable'], generation='')
            try:
                local = navigation.local_state(self.home)
                for name in ('supported', 'installed', 'authorized'):
                    if type(local.get(name)) is not bool:
                        raise OpenError()
                    public[name] = local[name]
                state = local.get('state')
                state = state if state in STATES else 'unavailable'
                public['state'] = state
                if public['supported'] and public['installed'] and public['authorized']:
                    connection = self._connection()
                    if not isinstance(connection.get('generation'), str) or not GENERATION.fullmatch(connection['generation']):
                        raise OpenError()
                    self._live(connection)
                    public.update(available=True, state='ready', generation=connection['generation'])
            except OpenError as error:
                public.update(available=False, state=error.code, generation='')
            except (navigation.NavigationError, OSError, ValueError, TypeError):
                public.update(available=False, state='unavailable', generation='')
            public['message'] = MESSAGES[public['state']]
            with self._cache_lock:
                if epoch == self._epoch:
                    self._cache = (self.clock(), dict(public))
                else:
                    public.update(available=False, state='unavailable', message=MESSAGES['unavailable'], generation='')
        eligible = []
        if public['available']:
            try:
                eligible = sorted(aid for aid, row in agents.items() if self._eligible(aid, row))
            except (navigation.NavigationError, OSError, ValueError, TypeError):
                public.update(available=False, state='unavailable', message=MESSAGES['unavailable'], generation='')
        return dict(public, eligible_agents=eligible if public['available'] else [])

    def setup(self, repo, agents):
        if not self._action.acquire(False):
            raise OpenError('unavailable', 409)
        try:
            with self._configuration:
                navigation.install(self.home, repo)
                self.invalidate()
                return self.status(agents, force=True)
        except (navigation.NavigationError, OSError, ValueError, TypeError):
            raise OpenError() from None
        finally:
            self._action.release()

    def disconnect(self, agents):
        # Revoke even while an open is in flight; its post-dispatch checks then
        # prevent activation. Do not wait for an owner request to finish first.
        self.invalidate()
        try:
            with self._configuration:
                navigation.disconnect(self.home)
                self.invalidate()
                return self.status(agents, force=True)
        except (navigation.NavigationError, OSError, ValueError, TypeError):
            raise OpenError() from None

    def open(self, aid, agent):
        if not self._action.acquire(False):
            raise OpenError('unavailable', 409)
        submitted = False
        try:
            with self._cache_lock:
                epoch = self._epoch
            if not self._eligible(aid, agent):
                raise OpenError('unavailable', 404)
            connection = self._connection()
            self._live(connection)
            request_id = str(uuid.uuid4())
            deadline = self.clock() + 5
            expiry = int(self.wall() * 1000) + 4900
            body = dict(request_id=request_id, conversation_id=aid[2:], expires_at_ms=expiry)
            submitted = True                 # a lost response never permits POST replay
            row = self._request(connection, 'POST', '/owner/open', body, timeout=min(1, deadline-self.clock()))
            while True:
                if (set(row) != {'protocol', 'generation', 'request_id', 'conversation_id', 'state', 'confirmed'}
                        or row['request_id'] != request_id or row['conversation_id'] != aid[2:]
                        or row['confirmed'] is not False or row['state'] not in ('queued', 'claimed', 'called', 'failed', 'expired', 'unknown')):
                    raise OpenError()
                if row['state'] in ('called', 'failed', 'expired', 'unknown'):
                    break
                remaining = deadline - self.clock()
                if remaining <= .05 or self.wall() * 1000 >= expiry:
                    raise OpenError()
                self.sleep(min(.1, remaining))
                row = self._request(connection, 'GET', '/owner/status?request_id=' + request_id, timeout=min(1, deadline-self.clock()))
            if row['state'] != 'called' or self.clock() >= deadline or self.wall() * 1000 >= expiry:
                raise OpenError()
            self._connection(connection)
            self._live(connection, timeout=min(1, deadline-self.clock()))
            if (self.clock() >= deadline or self.wall() * 1000 >= expiry
                    or not self._eligible(aid, agent)):
                raise OpenError()
            with self._cache_lock:
                if epoch != self._epoch:
                    raise OpenError()
            # This helper verifies the exact still-running native identity again;
            # it activates no URL, file, prompt or new app instance.
            requested = navigation.foreground(connection['runtime'], self.home)
            return dict(status='dispatched', confirmed=False, foreground_requested=requested is True, message=DISPATCHED)
        except (OpenError, navigation.NavigationError, OSError, ValueError, TypeError) as error:
            if submitted and not (isinstance(error, OpenError) and error.rejected):
                return dict(status='uncertain', confirmed=False, foreground_requested=False, message=UNCERTAIN)
            if isinstance(error, OpenError):
                raise error
            raise OpenError() from None
        finally:
            self.invalidate()
            self._action.release()
