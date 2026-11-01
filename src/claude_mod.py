"""Owner-only official-mod delivery. No native credentials or native IPC.

SQLite reservations precede returning a claim. Claims never replay; only status
and completion events may replay. The official host owns native input execution.
"""
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid

LEASE, PAIR_TTL, TTL, RETENTION = 12, 300, 900, 86400
TRANSPORT = 'claude_mod'


class ModError(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def ident(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def schema(data, required):
    if not isinstance(data, dict) or set(data) != set(required):
        raise ModError(400, 'Invalid Claude mod request fields.')


def project(value):
    return (isinstance(value, str) and bool(value) and os.path.isabs(value)
            and os.path.realpath(value) == value and len(value) <= 4096)


class ClaudeMod:
    def __init__(self, store):
        self.store, self.db = store, store.db
        self.db.execute('''CREATE TABLE IF NOT EXISTS claude_mod_pairs (
            code_hash TEXT PRIMARY KEY, agent TEXT NOT NULL, project TEXT NOT NULL, expires REAL NOT NULL)''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS claude_mod_enrollments (
            id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, agent TEXT NOT NULL,
            project TEXT NOT NULL, generation TEXT, last_seen REAL NOT NULL, active INTEGER NOT NULL)''')
        self.db.execute('CREATE INDEX IF NOT EXISTS claude_mod_agents ON claude_mod_enrollments(agent,active)')
        # Connections cannot survive a WATCH process restart. Original receipts do.
        self.db.execute('UPDATE claude_mod_enrollments SET generation=NULL,last_seen=0')
        for row in self.rows():
            if row.get('launched') and row['state'] not in ('answered', 'failed'):
                row.update(state='unknown', error='WATCH restarted after a Claude mod claim. It will not resubmit.')
                self.put(row)
        self.db.commit()

    def rows(self):
        return [r for r in self.store._rows() if r.get('transport') == TRANSPORT and r.get('origin') in (None, 'owner')]

    def put(self, row):
        self.db.execute('INSERT OR REPLACE INTO requests VALUES (?,?)', (row['request_id'], json.dumps(row)))

    def uncertain(self, enrollment, reason):
        for row in self.rows():
            if row.get('mod_enrollment') == enrollment and row.get('launched') and row['state'] in ('sending', 'queued', 'awaiting_reply'):
                row.update(state='unknown', error=reason)
                self.put(row)

    def maintain(self):
        now = time.time()
        self.db.execute('DELETE FROM claude_mod_pairs WHERE expires<=?', (now,))
        for eid, in self.db.execute('SELECT id FROM claude_mod_enrollments WHERE generation IS NOT NULL AND last_seen<?', (now - LEASE,)).fetchall():
            self.uncertain(eid, 'The Claude mod disconnected. Delivery is unknown; nothing was resubmitted.')
            self.db.execute('UPDATE claude_mod_enrollments SET generation=NULL WHERE id=?', (eid,))
        for row in self.rows():
            if row['state'] in ('answered', 'failed'):
                continue
            if not row.get('launched') and now >= row['created'] + TTL:
                row.update(state='failed', error='This message expired before a Claude mod claimed it. Nothing was submitted.')
                self.put(row)
            elif row.get('launched') and row['state'] in ('queued', 'awaiting_reply') and now >= row['created'] + TTL:
                row.update(state='expired', error='No matching reply yet. Native input may still run; WATCH will not resubmit.')
                self.put(row)

    def pair(self, agent, agents, port):
        item = agents.get(agent) if isinstance(agent, str) else None
        if (not item or not agent.startswith('c:') or not ident(agent[2:]) or item.get('runtime') != 'Claude'
                or item.get('observed') or item.get('sub') or item.get('parent') or not project(item.get('cwd'))):
            raise ModError(400, 'Choose an existing local parent Claude chat with a known project.')
        code, expiry = secrets.token_urlsafe(24), time.time() + PAIR_TTL
        with self.store.lock, self.db:
            self.maintain()
            if self.db.execute('SELECT COUNT(*) FROM claude_mod_pairs').fetchone()[0] >= 32:
                raise ModError(429, 'Too many pending Claude pairing codes.')
            self.db.execute('DELETE FROM claude_mod_pairs WHERE agent=?', (agent,))
            self.db.execute('INSERT INTO claude_mod_pairs VALUES (?,?,?,?)', (digest(code), agent, item['cwd'], expiry))
        return dict(agent=agent, port=port, pair_code=code, expires_at=expiry, command=f'/watch-connect {port}:{code}')

    def session(self, entry):
        return dict(version=1, native_id=entry['agent'][2:], generation=entry['generation'], heartbeat_interval_ms=2000, lease_seconds=LEASE)

    def get(self, eid):
        row = self.db.execute('SELECT id,agent,project,generation,last_seen,active FROM claude_mod_enrollments WHERE id=?', (eid,)).fetchone()
        return dict(zip(('id', 'agent', 'project', 'generation', 'last_seen', 'active'), row)) if row else None

    def authenticate(self, token):
        if not isinstance(token, str) or len(token) != 64:
            raise ModError(403, 'Claude mod service access is invalid.')
        row = self.db.execute('SELECT id,token_hash FROM claude_mod_enrollments WHERE active=1 AND token_hash=?', (digest(token),)).fetchone()
        if not row or not hmac.compare_digest(row[1], digest(token)):
            raise ModError(403, 'Claude mod service access is invalid.')
        return self.get(row[0])

    def native(self, entry, data, connected=True):
        if data.get('native_id') != entry['agent'][2:]:
            raise ModError(409, 'The enrolled Claude conversation does not match.')
        if connected and (not entry['generation'] or data.get('generation') != entry['generation']):
            raise ModError(409, 'Reconnect the Claude mod before receiving more messages.')

    def capability(self, agent, item=None):
        with self.store.lock, self.db:
            self.maintain()
            row = self.db.execute('SELECT id FROM claude_mod_enrollments WHERE agent=? AND active=1', (agent,)).fetchone()
            entry = self.get(row[0]) if row else None
            ready = bool(entry and entry['generation'] and entry['last_seen'] > time.time() - LEASE and not self.store.worker_failed)
            if item is not None and entry and item.get('cwd') != entry['project']:
                ready = False
            return ready, entry, ('' if ready else 'Connect the WATCH official mod in this exact existing Claude chat.')

    def any_ready(self):
        with self.store.lock, self.db:
            self.maintain()
            return not self.store.worker_failed and bool(self.db.execute('SELECT 1 FROM claude_mod_enrollments WHERE active=1 AND generation IS NOT NULL AND last_seen>?', (time.time()-LEASE,)).fetchone())

    def enroll(self, data):
        schema(data, ('code', 'native_id', 'project'))
        if not isinstance(data['code'], str) or len(data['code']) > 100 or not ident(data['native_id']) or not project(data['project']):
            raise ModError(400, 'Invalid Claude enrollment identity.')
        pair = self.db.execute('SELECT agent,project,expires FROM claude_mod_pairs WHERE code_hash=?', (digest(data['code']),)).fetchone()
        if not pair or pair[2] <= time.time() or pair[:2] != ('c:'+data['native_id'], data['project']):
            raise ModError(403, 'Claude pairing code is expired, used, or does not match this chat.')
        for eid, in self.db.execute('SELECT id FROM claude_mod_enrollments WHERE agent=? AND active=1', (pair[0],)).fetchall():
            self.uncertain(eid, 'This Claude enrollment was replaced. Earlier input may still run; nothing was resubmitted.')
        self.db.execute('UPDATE claude_mod_enrollments SET active=0,generation=NULL WHERE agent=?', (pair[0],))
        eid, token, generation = str(uuid.uuid4()), secrets.token_hex(32), str(uuid.uuid4())
        self.db.execute('INSERT INTO claude_mod_enrollments VALUES (?,?,?,?,?,?,1)', (eid, digest(token), pair[0], pair[1], generation, time.time()))
        self.db.execute('DELETE FROM claude_mod_pairs WHERE code_hash=?', (digest(data['code']),))
        result = self.session(self.get(eid))
        return dict(result, enrollment_id=eid, token=token)

    def statuses(self, entry):
        return [dict(request_id=r['request_id'], attempt_id=r['mod_attempt_id'], state=r['state'], turn_id=r.get('mod_turn', ''), error=r.get('error', ''))
                for r in sorted(self.rows(), key=lambda r: r['created']) if r.get('mod_enrollment') == entry['id'] and r.get('mod_attempt_id')][-32:]

    def poll(self, entry, data):
        schema(data, ('generation', 'native_id', 'project', 'accept'))
        self.native(entry, data)
        if data['project'] != entry['project'] or type(data['accept']) is not bool:
            raise ModError(409, 'The enrolled Claude project does not match.')
        self.db.execute('UPDATE claude_mod_enrollments SET last_seen=? WHERE id=?', (time.time(), entry['id']))
        rows = self.rows()
        busy = any(r.get('mod_enrollment') == entry['id'] and r.get('launched') and r['state'] in ('queued', 'awaiting_reply') for r in rows)
        claim = None
        if data['accept'] and not busy and not self.store.worker_failed:
            for row in sorted(rows, key=lambda r: r['created']):
                if row.get('mod_enrollment') != entry['id'] or row.get('launched') or row['state'] != 'sending':
                    continue
                # Deadline checked inside the same serialized reservation transaction.
                if row['created'] + TTL <= time.time():
                    row.update(state='failed', error='This message expired before claim. Nothing was submitted.')
                    self.put(row)
                    continue
                attempt = str(uuid.uuid4())
                row.update(launched=True, state='queued', mod_attempt_id=attempt, mod_generation=entry['generation'], error='')
                self.put(row)
                claim = dict(request_id=row['request_id'], attempt_id=attempt, native_id=entry['agent'][2:], generation=entry['generation'], envelope=row['envelope'], expires_at=row['created']+TTL)
                break
        return dict(version=1, native_id=entry['agent'][2:], generation=entry['generation'], claim=claim, requests=self.statuses(entry))

    def event(self, entry, data):
        kind = data.get('kind')
        extras = {'submitted': (), 'started': ('turn_id',), 'answered': ('turn_id', 'answer'), 'dropped': (), 'unknown': ()}
        if kind not in extras:
            raise ModError(400, 'Invalid Claude request event.')
        schema(data, ('generation', 'native_id', 'request_id', 'attempt_id', 'kind') + extras[kind])
        self.native(entry, data)
        row = next((r for r in self.rows() if r['request_id'] == data['request_id']), None)
        if (not row or row.get('origin') == 'peer' or row.get('mod_enrollment') != entry['id']
                or row.get('mod_attempt_id') != data['attempt_id'] or not ident(data['attempt_id'])
                or row['agent'] != entry['agent'] or not row.get('launched')):
            raise ModError(409, 'Claude request does not match its original claim.')
        turn = data.get('turn_id')
        if kind in ('started', 'answered'):
            if not isinstance(turn, str) or not 1 <= len(turn) <= 128 or any(ord(c)<32 for c in turn):
                raise ModError(400, 'Invalid Claude turn identifier.')
            if row.get('mod_turn') and row['mod_turn'] != turn:
                raise ModError(409, 'Claude request is already bound to another turn.')
        reply = None
        if kind == 'answered':
            answer, marker = data['answer'], 'WATCH_REPLY ' + row['request_id']
            if not isinstance(answer, str) or len(answer) > 16000 or '\x00' in answer:
                raise ModError(400, 'Invalid Claude reply.')
            if [line for line in answer.splitlines() if 'WATCH_REPLY' in line] != [marker]:
                raise ModError(409, 'Claude reply has missing or ambiguous correlation markers.')
            reply = '\n'.join(line for line in answer.splitlines() if line != marker).strip()
            if row['state'] == 'answered' and row.get('reply') != reply:
                raise ModError(409, 'A different Claude reply was already recorded.')
        # A completion can beat both submit settlement and its started event.
        if kind == 'answered':
            row.update(state='answered', reply=reply, error='', mod_turn=turn)
        elif row['state'] != 'answered':
            if kind == 'started' and row['state'] != 'failed':
                row.update(state='awaiting_reply', mod_turn=turn, error='')
            elif kind == 'submitted' and row['state'] == 'queued':
                pass
            elif kind == 'dropped' and row['state'] not in ('awaiting_reply', 'expired'):
                row.update(state='failed', error='Native Claude input was dropped before it started. It was not resubmitted.')
            elif kind == 'unknown' and row['state'] != 'failed':
                row.update(state='unknown', error='Claude submission could not be confirmed. Nothing was resubmitted.')
        self.put(row)
        return dict(request_id=row['request_id'], state=row['state'], turn_id=row.get('mod_turn', ''))

    def call(self, method, data, token=''):
        with self.store.lock, self.db:
            self.maintain()
            if method == 'enroll':
                return self.enroll(data)
            entry = self.authenticate(token)
            if method == 'connect':
                schema(data, ('native_id', 'project'))
                self.native(entry, data, connected=False)
                if data['project'] != entry['project']:
                    raise ModError(409, 'The enrolled Claude project does not match.')
                self.uncertain(entry['id'], 'Claude reconnected after an earlier claim. Delivery remains unknown; it will not resubmit.')
                generation = str(uuid.uuid4())
                self.db.execute('UPDATE claude_mod_enrollments SET generation=?,last_seen=? WHERE id=?', (generation, time.time(), entry['id']))
                return self.session(self.get(entry['id']))
            if method == 'poll':
                return self.poll(entry, data)
            if method == 'event':
                return self.event(entry, data)
            if method == 'disconnect':
                schema(data, ('generation', 'native_id'))
                self.native(entry, data)
                self.uncertain(entry['id'], 'The Claude mod disconnected. Delivery remains unknown; nothing was resubmitted.')
                self.db.execute('UPDATE claude_mod_enrollments SET generation=NULL,last_seen=0 WHERE id=?', (entry['id'],))
                return {'disconnected': True}
            raise ModError(404, 'Unknown Claude mod operation.')


def handle_http(handler, store):
    """Dedicated purpose-scoped service routes; owner key is never accepted."""
    handler.connection.settimeout(10)
    try:
        port = handler.server.server_address[1]
        method = handler.path.removeprefix('/claude-mod/')
        if (method not in ('enroll', 'connect', 'poll', 'event', 'disconnect')
                or handler.headers.get_all('Host') != [f'127.0.0.1:{port}']
                or handler.headers.get('Origin') is not None or handler.headers.get('Transfer-Encoding') is not None
                or len(handler.headers.get_all('Content-Length') or []) != 1):
            raise ModError(403, 'Invalid Claude mod service request.')
        size = int(handler.headers.get('Content-Length', ''))
        if size < 2 or size > (80000 if method == 'event' else 16384):
            raise ModError(400, 'Invalid Claude mod request size.')
        raw = handler.rfile.read(size)
        if len(raw) != size:
            raise ModError(400, 'Incomplete Claude mod request.')
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ModError(400, 'Invalid Claude mod request.')
        auth = handler.headers.get_all('Authorization') or []
        if method != 'enroll' and (len(auth) != 1 or not auth[0].startswith('Bearer ')):
            raise ModError(403, 'Claude mod service access is required.')
        result = store.mods.call(method, data, auth[0][7:] if auth else '')
        status = 200
    except ModError as error:
        status, result = error.status, {'error': str(error)}
    except Exception:
        status, result = 400, {'error': 'Claude mod request could not be accepted.'}
    handler.close_connection = True
    return handler.send(status, json.dumps(result).encode(), 'application/json')
