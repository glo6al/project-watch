"""Bounded private owner observations. No dispatch, persistence or import-time I/O.

Existing room-key authentication is an owner-read scope, not a new OS security
boundary. Browser focus cancellation does not cancel a bounded native read.
"""
from copy import deepcopy
import hashlib
import hmac
import json
import math
import re
import socket
import threading
import time
import urllib.parse
import uuid

PARTS = ('model_settings', 'native_inputs')


class ReadError(ValueError):
    def __init__(self, status, reason):
        self.status, self.reason = status, reason
        super().__init__(reason)


def target(agent_id, row):
    if type(agent_id) is not str or not re.fullmatch(r'[xc]:[0-9a-f-]{36}', agent_id):
        raise ReadError(404, 'unknown_agent')
    try:
        if str(uuid.UUID(agent_id[2:])) != agent_id[2:]:
            raise ValueError()
    except ValueError:
        raise ReadError(404, 'unknown_agent') from None
    provider = 'codex' if agent_id.startswith('x:') else 'claude'
    if (type(row) is not dict or row.get('runtime') != ('Codex' if provider == 'codex' else 'Claude')
            or row.get('observed') or row.get('stale') or (provider == 'claude' and row.get('sub'))
            or any(row.get(k, 'local') != 'local' for k in ('host_id', 'hostId'))):
        raise ReadError(404, 'unknown_agent')
    return (agent_id, provider, agent_id[2:])


def _part(name):
    capability = ({'read': False, 'selectModel': False, 'selectEffort': False,
                   'reason': 'not_read', 'scope': 'next_turn'} if name == PARTS[0]
                  else {'observe': False, 'answer': False, 'reason': 'not_read'})
    value = {'state': 'unavailable', 'read_active': False, 'expires_at': None,
             'stale': True, 'capability': capability}
    if name == PARTS[0]:
        value.update(settings=None, catalog=None)
    else:
        value.update(observed_at=None, requests=[], request_count=None)
    return value


def _scalar(value):
    return not any(0xD800 <= ord(c) <= 0xDFFF for c in value)


def _units(value):
    return len(value.encode('utf-16-le', errors='surrogatepass')) // 2


def _model(value, now):
    if type(value) is not dict or value.get('state') != 'observed':
        raise ValueError()
    settings = value.get('settings')
    if type(settings) is not dict:
        raise ValueError()
    result = {}
    for key, maximum, nullable in [('model', 256, False), ('effort', 64, True),
                                   ('modelProvider', 128, True)]:
        v = settings.get(key)
        if v is None and nullable:
            result[key] = None
        elif (type(v) is str and _scalar(v) and 0 < _units(v) <= maximum and v == v.strip()
              and not any(ord(c) < 32 or ord(c) == 127 for c in v)):
            result[key] = v
        else:
            raise ValueError()
    stamp = settings.get('observedAt')
    if (type(stamp) not in (int, float) or not math.isfinite(stamp)
            or stamp > now + 1 or stamp < now - 15 or settings.get('source') != 'native_desktop_settings'
            or settings.get('mode') not in (None, 'default', 'plan')):
        raise ValueError()
    result.update(mode=settings['mode'], source='native_desktop_settings', observedAt=stamp)
    return result


def _questions(descriptors, identity):
    """Validate adapter descriptors again and retain only inert private fields."""
    import codex_native_input as native
    if type(descriptors) not in (tuple, list) or len(descriptors) > native.MAX_REQUESTS:
        raise ValueError()
    rows, group, seen, size, presented = [], None, set(), 0, 0
    for d in descriptors:
        if type(d) is not native.NativeRequestDescriptor or d.binding.conversation_id != identity[2] or d.binding.host_id != 'local':
            raise ValueError()
        for value in (d.binding.owner_client_id, d.binding.client_id, d.binding.connection_epoch):
            native._uuid(value)
        if type(d.observation_incarnation) is not str or not d.observation_incarnation:
            raise ValueError()
        current = (d.binding, d.observation_incarnation, d.revision)
        if group is not None and current != group:
            raise ValueError()
        group = current
        if type(d.revision) is not int or d.revision < 0 or d.native_id in seen:
            raise ValueError()
        seen.add(d.native_id)
        raw = d.raw_request()
        encoded = native._json(raw)
        size += len(encoded)
        if (size > native.MAX_PROJECTION or hashlib.sha256(encoded.encode()).hexdigest() != d.schema_hash
                or native.typed_id(raw.get('id')) != d.native_id or raw.get('method') != d.method
                or type(raw.get('params')) is not dict
                or raw['params'].get('threadId', identity[2]) != identity[2]):
            raise ValueError()
        presentation, reason = None, 'unsupported_method'
        if d.method == native.QUESTION:
            reason = 'unsupported_schema'
            try:
                if raw.keys() != {'id', 'method', 'params'}:
                    raise ValueError()
                secret = native._question_schema(raw['params'])
                if secret or d.secret:
                    reason = 'secret_native_only'
                elif d.supported_schema:
                    presentation = {'questions': [
                        {'id': 'q%d' % i, 'header': q['header'], 'question': q['question'],
                         'options': deepcopy(q.get('options')), 'isOther': q.get('isOther', False)}
                        for i, q in enumerate(raw['params']['questions'])]}
                    count = _units(json.dumps(presentation, ensure_ascii=False, separators=(',', ':')))
                    fits = count <= 262144 and presented + count <= native.MAX_PROJECTION - 65536
                    for q in presentation['questions']:
                        fits = (fits and _scalar(q['header']) and _scalar(q['question'])
                                and _units(q['header']) <= 4096 and _units(q['question']) <= 65536)
                        for option in q['options'] or []:
                            fits = (fits and _scalar(option['label']) and _scalar(option['description'])
                                    and _units(option['label']) <= 65536 and _units(option['description']) <= 65536)
                    if fits:
                        presented += count
                        reason = 'native_generation_unproven'
                    else:
                        presentation, reason = None, 'oversized_native_only'
            except (ValueError, TypeError, KeyError):
                pass
        rows.append((d.native_id, d.schema_hash, {'kind': 'question' if d.method == native.QUESTION else 'native_request',
                     'presentation': presentation, 'capability': {'answer': False, 'reason': reason}}))
    return rows, group


class NativeObservationService:
    def __init__(self, resolve_agent, model_reader=None, input_reader=None, *, clock=time.monotonic, wall=time.time):
        self.resolve_agent, self.model_reader, self.input_reader = resolve_agent, model_reader, input_reader
        self.clock, self.wall = clock, wall
        self.lock = threading.Condition(threading.RLock())
        self.entries, self.jobs, self.queue, self.threads = {}, {}, [], []
        self.closed = False

    def start(self):
        with self.lock:
            if self.closed or self.threads:
                return
            for name in (PARTS[1], PARTS[0], PARTS[0]):
                worker = threading.Thread(target=self._worker, args=(name,), daemon=True,
                                          name='watch-native-read-' + name)
                self.threads.append(worker)
                worker.start()

    def _identity(self, aid):
        if type(aid) is not str:
            raise ReadError(404, 'unknown_agent')
        return target(aid, self.resolve_agent(aid))

    def _expire(self):
        now = self.clock()
        for aid, e in list(self.entries.items()):
            if now - e['used'] >= 60 and not any(k[0] == aid for k in self.jobs):
                del self.entries[aid]
                continue
            if now >= e.get('retained_until', float('inf')):
                p = e['view'][PARTS[1]]
                p.update(requests=[], request_count=None, state='unavailable', stale=True, read_active=False)
                p['capability'].update(observe=False, reason='observation_expired')
                e['handles'].clear()
                e.pop('retained_until', None)
                e['view']['revision'] += 1
            for name in PARTS:
                p = e['view'][name]
                if p['expires_at'] is not None and now >= e['until'].get(name, float('inf')) and (not p['stale'] or p['read_active']):
                    self._stale(e, name)

    def _stale(self, e, name):
        p = e['view'][name]
        p.update(stale=True, read_active=False)
        if name == PARTS[1] and p['request_count'] is not None:
            p['state'] = 'stale_observation'
            for r in p['requests']:
                r.update(stale=True, state='stale_observation')
        e['view']['revision'] += 1

    def _snapshot(self, e):
        v = deepcopy(e['view'])
        parts = [v[n] for n in PARTS]
        stamps = [p['expires_at'] for p in parts if p['expires_at'] is not None]
        v['expires_at'] = min(stamps) if stamps else None
        v['stale'] = any(p['stale'] for p in parts)
        v['inputs'] = v.pop('native_inputs')
        return v

    def snapshot(self, aid):
        try:
            identity = self._identity(aid)
        except ReadError:
            self.invalidate_agent(aid)
            raise
        with self.lock:
            self._expire()
            e = self.entries.get(aid)
            if self.closed or e is None or e['identity'] != identity:
                self.invalidate_agent(aid)
                raise ReadError(404, 'not_observed')
            e['used'] = self.clock()
            return self._snapshot(e)

    def request_read(self, aid, include):
        if (type(include) is not list or not include
                or any(type(n) is not str or n not in PARTS for n in include)
                or len(include) != len(set(include))):
            raise ReadError(400, 'invalid_request')
        try:
            identity = self._identity(aid)
        except ReadError:
            self.invalidate_agent(aid)
            raise
        with self.lock:
            if self.closed or not self.threads:
                raise ReadError(503, 'unavailable')
            self._expire()
            if any(k[0] == aid and k[1] in include
                   and (j['epoch'] is None or self.clock() >= j['deadline'])
                   for k, j in self.jobs.items()):
                raise ReadError(429, 'capacity')
            e = self.entries.get(aid)
            if e is None:
                if len(self.entries) >= 8:
                    raise ReadError(429, 'capacity')
                view = {'version': 1, 'agent_id': aid, 'provider': identity[1],
                        'observation_epoch': str(uuid.uuid4()), 'revision': 1, 'observed_at': None,
                        PARTS[0]: _part(PARTS[0]), PARTS[1]: _part(PARTS[1])}
                e = {'identity': identity, 'view': view, 'used': self.clock(), 'until': {}, 'last': {}, 'handles': {}}
                self.entries[aid] = e
            wanted = [n for n in include if (aid, n) not in self.jobs and self.clock() >= e['until'].get(n, -1)]
            if any(self.clock() - e['last'].get(n, -100) < 5 for n in wanted):
                raise ReadError(429, 'rate_limit')
            if len(self.queue) + len(wanted) > 4:
                raise ReadError(429, 'capacity')
            active = any(k[0] == aid for k in self.jobs)
            if wanted and not active:
                e['view']['observation_epoch'] = str(uuid.uuid4())
                e['handles'] = {}
            for name in wanted:
                e['last'][name] = self.clock()
                p = e['view'][name]
                if identity[1] == 'claude':
                    p.update(_part(name))
                    p['capability']['reason'] = 'adapter_unavailable'
                    e['until'][name] = self.clock() + 5
                    continue
                deadline = self.clock() + 20
                job = {'identity': identity, 'epoch': e['view']['observation_epoch'], 'token': str(uuid.uuid4()),
                       'deadline': deadline, 'name': name, 'group': None}
                self.jobs[(aid, name)] = job
                self.queue.append(job)
                p.update(state='reading', read_active=True, stale=True, expires_at=self.wall() + 20)
                e['until'][name] = deadline
            e['used'] = self.clock()
            e['view']['revision'] += 1
            self.lock.notify_all()
            return (202 if any((aid, n) in self.jobs for n in include) else 200), self._snapshot(e)

    def _valid(self, job):
        aid, _, _ = job['identity']
        e = self.entries.get(aid)
        try:
            current = self._identity(aid)
        except ReadError:
            current = None
        if e is not None and e['identity'] != current:
            self.invalidate_agent(aid)
            return None
        if (self.closed or e is None or e['identity'] != current
                or e['view']['observation_epoch'] != job['epoch']
                or self.jobs.get((aid, job['name'])) is not job or self.clock() >= job['deadline']):
            return None
        return e

    def _observe(self, job, descriptors):
        rows, group = _questions(descriptors, job['identity'])
        with self.lock:
            e = self._valid(job)
            if e is None:
                return
            if group is not None:
                old = job['group']
                if old is not None and (group[:2] != old[:2] or group[2] < old[2]):
                    raise ValueError()
                fingerprint = tuple((r[0], r[1]) for r in rows)
                if old == group and job.get('fingerprint') != fingerprint:
                    raise ValueError()
                job['group'] = group
                job['fingerprint'] = fingerprint
            p, mapped = e['view'][PARTS[1]], []
            present = {(r[0], r[1]) for r in rows}
            e['handles'] = {k: v for k, v in e['handles'].items() if k in present}
            for native_id, schema_hash, row in rows:
                key = (native_id, schema_hash)
                handle = e['handles'].setdefault(key, str(uuid.uuid4()))
                row.update(handle=handle, revision=1, state='observed', observed_at=self.wall(), stale=False)
                mapped.append(row)
            e['retained_until'] = self.clock() + 60
            p.update(state='observed', observed_at=self.wall(), stale=False, requests=mapped, request_count=len(mapped))
            p['capability'].update(observe=True, answer=False, reason='native_generation_unproven')
            e['view']['observed_at'] = self.wall()
            e['view']['revision'] += 1

    def _worker(self, name):
        while True:
            with self.lock:
                while not self.closed and not any(j['name'] == name for j in self.queue):
                    self.lock.wait()
                if self.closed:
                    return
                job = next(j for j in self.queue if j['name'] == name)
                self.queue.remove(job)
                e = self._valid(job)
                if e is not None and name == PARTS[1]:
                    job['deadline'] = min(job['deadline'], self.clock() + 10)
                    e['until'][name] = job['deadline']
                    e['view'][name]['expires_at'] = self.wall() + max(0, job['deadline'] - self.clock())
            try:
                if e is None:
                    continue
                if name == PARTS[1]:
                    reader = self.input_reader
                    if reader is None:
                        from codex_native_input import subscribe_exact
                        reader = subscribe_exact
                    reader(job['identity'][2], lambda ds: self._observe(job, ds), seconds=10)
                else:
                    reader = self.model_reader
                    if reader is None:
                        from codex_model_settings import read_settings
                        reader = read_settings
                    settings = _model(reader(job['identity'][2]), self.wall())
                    with self.lock:
                        e = self._valid(job)
                        if e is not None:
                            p = e['view'][name]
                            p.update(state='observed', settings=settings, read_active=False, stale=False,
                                     expires_at=settings['observedAt'] + 15)
                            p['capability'].update(read=True, selectModel=False, selectEffort=False, reason='catalog_unavailable')
                            e['until'][name] = self.clock() + max(0, settings['observedAt'] + 15 - self.wall())
                            e['view']['observed_at'] = self.wall()
                            e['view']['revision'] += 1
            except Exception:
                # Deliberately no exception text or native payload logging.
                pass
            finally:
                with self.lock:
                    e = self._valid(job)
                    if e is not None:
                        p = e['view'][name]
                        if name == PARTS[1] or p['state'] != 'observed':
                            self._stale(e, name)
                            if p['state'] == 'reading':
                                p['state'] = 'unavailable'
                                p['capability']['reason'] = 'native_read_unavailable'
                            e['until'][name] = self.clock() + 5
                            p['expires_at'] = self.wall()
                    key = (job['identity'][0], name)
                    if self.jobs.get(key) is job:
                        del self.jobs[key]
                    self.lock.notify_all()

    def invalidate_agent(self, aid):
        if type(aid) is not str:
            return
        with self.lock:
            self.entries.pop(aid, None)
            # Running jobs keep their physical slots until adapter cleanup.
            queued = [j for j in self.queue if j['identity'][0] == aid]
            self.queue[:] = [j for j in self.queue if j['identity'][0] != aid]
            for key, job in list(self.jobs.items()):
                if key[0] == aid:
                    job['epoch'] = None
                    if job in queued:
                        del self.jobs[key]

    def close(self):
        with self.lock:
            self.closed = True
            self.entries.clear()
            self.queue.clear()
            self.lock.notify_all()
        deadline = time.monotonic() + 1
        for worker in self.threads:
            worker.join(max(0, deadline - time.monotonic()))


def handle_http(handler, service, owner_key, port):
    """Strict new-namespace boundary; authenticate before consuming any body."""
    handler.close_connection = True
    def reply(status, value):
        # Bound both header and payload writes even if the owner stops reading.
        handler.connection.settimeout(2)
        try:
            return handler.send(status, json.dumps(value, allow_nan=False).encode(), 'application/json; charset=utf-8')
        except (BrokenPipeError, ConnectionResetError, TimeoutError, socket.timeout):
            return None
    def headers(name):
        return handler.headers.get_all(name, [])
    hosts, origins, keys = headers('Host'), headers('Origin'), headers('X-Live-Room-Key')
    local = ['127.0.0.1:%d' % port, 'localhost:%d' % port]
    if (len(hosts) != 1 or hosts[0] not in local or len(origins) > 1
            or (origins and origins[0] not in ['http://' + x for x in local])
            or len(keys) != 1 or not owner_key or not hmac.compare_digest(keys[0].encode(), owner_key.encode())):
        return reply(403, {'error': 'forbidden'})
    try:
        u = urllib.parse.urlsplit(handler.path)
        if u.query or u.fragment or u.scheme or u.netloc:
            raise ReadError(400, 'invalid_request')
        if service is None:
            raise ReadError(503, 'unavailable')
        if handler.command == 'POST' and u.path == '/native/v1/read':
            lengths = headers('Content-Length')
            if (headers('Transfer-Encoding') or len(lengths) != 1
                    or not re.fullmatch(r'[0-9]+', lengths[0]) or len(lengths[0]) > 6
                    or not 0 < int(lengths[0]) <= 4096):
                raise ReadError(400, 'invalid_request')
            def pairs(items):
                d = {}
                for k, v in items:
                    if k in d:
                        raise ValueError()
                    d[k] = v
                return d
            try:
                deadline = time.monotonic() + 2
                raw = bytearray()
                while len(raw) < int(lengths[0]):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError()
                    handler.connection.settimeout(remaining)
                    # read1 performs at most one underlying read; read(n) can
                    # keep resetting an idle timeout while bytes trickle in.
                    chunk = handler.rfile.read1(int(lengths[0]) - len(raw))
                    if not chunk:
                        raise ValueError()
                    raw.extend(chunk)
                data = json.loads(raw, object_pairs_hook=pairs)
            except (ValueError, UnicodeError, OSError, RecursionError):
                raise ReadError(400, 'invalid_request') from None
            if type(data) is not dict or data.keys() != {'agent_id', 'include'}:
                raise ReadError(400, 'invalid_request')
            status, view = service.request_read(data['agent_id'], data['include'])
            return reply(status, view)
        if handler.command == 'GET' and u.path.startswith('/native/v1/agents/'):
            encoded = u.path[len('/native/v1/agents/'):]
            if re.search(r'%(?![0-9A-Fa-f]{2})', encoded):
                raise ReadError(400, 'invalid_request')
            aid = urllib.parse.unquote(encoded, errors='strict')
            return reply(200, service.snapshot(aid))
        raise ReadError(404, 'unknown_route')
    except ReadError as exc:
        return reply(exc.status, {'error': exc.reason})
    except (ValueError, TypeError, UnicodeError, OverflowError):
        return reply(400, {'error': 'invalid_request'})
