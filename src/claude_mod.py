"""Scoped owner and shared-v2 peer official-mod delivery. No native credentials or native IPC.

SQLite reservations precede returning a claim. Claims never replay; only status
and completion events may replay. The official host owns native input execution.
"""
import hashlib
import hmac
import json
import os
import peer_bridge
import secrets
import time
import uuid

LEASE, PAIR_TTL, TTL, RETENTION = 12, 300, 900, 86400
TRANSPORT = 'claude_mod'
PEER_UNAVAILABLE = 'Agent receiving requires explicit WATCH setup and a compatible live official plugin in this exact Claude chat.'
MAX_ENROLLMENTS = 256


class ModError(Exception):
    def __init__(self, status, message, code='invalid_request'):
        self.status = status
        self.code = code
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


def project_text(value):
    return isinstance(value, str) and 0 < len(value) <= 4096 and os.path.isabs(value) and '\x00' not in value


def project(value):
    return project_text(value) and os.path.realpath(value) == value


def local_parent(agent, item):
    """Local observation is required; registration never creates room members."""
    return (isinstance(agent, str) and agent.startswith('c:') and ident(agent[2:])
            and isinstance(item, dict) and item.get('runtime') == 'Claude'
            and not any(item.get(k) for k in ('observed', 'sub', 'parent')))


def eligible(agent, item):
    return local_parent(agent, item) and project_text(item.get('cwd'))


class ClaudeMod:
    def __init__(self, store):
        self.store, self.db = store, store.db
        self.room_snapshot = None
        self.db.execute('''CREATE TABLE IF NOT EXISTS claude_mod_pairs (
            code_hash TEXT PRIMARY KEY, agent TEXT NOT NULL, project TEXT NOT NULL, expires REAL NOT NULL)''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS claude_mod_enrollments (
            id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, agent TEXT NOT NULL,
            project TEXT NOT NULL, generation TEXT, last_seen REAL NOT NULL, active INTEGER NOT NULL)''')
        columns = {r[1] for r in self.db.execute('PRAGMA table_info(claude_mod_enrollments)')}
        for name, kind in (('grant_id', 'TEXT'), ('client_id', 'TEXT'), ('connection_seq', 'INTEGER'),
                           ('peer_protocol', 'INTEGER NOT NULL DEFAULT 0'),
                           ('reply_notice_protocol', 'INTEGER NOT NULL DEFAULT 0')):
            if name not in columns:
                self.db.execute(f'ALTER TABLE claude_mod_enrollments ADD COLUMN {name} {kind}')
        self.db.execute('CREATE INDEX IF NOT EXISTS claude_mod_agents ON claude_mod_enrollments(agent,active)')
        self.db.execute('CREATE UNIQUE INDEX IF NOT EXISTS claude_mod_active_agent ON claude_mod_enrollments(agent) WHERE active=1')
        self.db.execute('''CREATE TABLE IF NOT EXISTS claude_mod_setup_codes (
            code_hash TEXT PRIMARY KEY, expires REAL NOT NULL)''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS claude_mod_grants (
            id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, created REAL NOT NULL, active INTEGER NOT NULL)''')
        for table in ('claude_mod_setup_codes', 'claude_mod_grants'):
            columns = {r[1] for r in self.db.execute(f'PRAGMA table_info({table})')}
            if 'peer_receive' not in columns:
                self.db.execute(f'ALTER TABLE {table} ADD COLUMN peer_receive INTEGER NOT NULL DEFAULT 0')
        # Connections cannot survive a WATCH process restart. Original receipts do.
        self.db.execute('UPDATE claude_mod_enrollments SET generation=NULL,last_seen=0')
        for row in self.rows():
            if row.get('launched') and row['state'] not in ('answered', 'failed'):
                row.update(state='unknown', error='WATCH restarted after a Claude mod claim. It will not resubmit.')
                self.put(row)
        self.db.commit()

    def rows(self):
        return [r for r in self.store._rows() if r.get('transport') == TRANSPORT
                and (r.get('origin') in (None, 'owner') or self.peer_row(r))]

    def peer_row(self, row):
        # Origin in a mutable JSON payload alone never authorizes shared receiving.
        if (row.get('origin') != 'peer' or row.get('bridge_version') != 2
                or row.get('runtime') != 'claude' or row.get('transport') != TRANSPORT
                or row.get('mod_project') != row.get('target_project')):
            return False
        ledger = self.db.execute('SELECT target FROM shared_bridge_requests WHERE transport_id=?',
                                 (row['request_id'],)).fetchone()
        return bool(ledger and ledger[0] == row.get('agent') and ident(row.get('mod_enrollment')))

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
        self.db.execute('DELETE FROM claude_mod_setup_codes WHERE expires<=?', (now,))
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
        if not eligible(agent, item) or not project(item['cwd']):
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
        result = dict(version=1, native_id=entry['agent'][2:], generation=entry['generation'], heartbeat_interval_ms=2000, lease_seconds=LEASE, peer_protocol=entry['peer_protocol'],
                      reply_notice_protocol=entry['reply_notice_protocol'], supported_reply_notice_protocol=1)
        if entry['grant_id']:
            result.update(grant_id=entry['grant_id'], client_id=entry['client_id'], connection_seq=entry['connection_seq'])
        return result

    def get(self, eid):
        fields = ('id', 'agent', 'project', 'generation', 'last_seen', 'active', 'grant_id', 'client_id', 'connection_seq', 'peer_protocol', 'reply_notice_protocol')
        row = self.db.execute('SELECT '+','.join(fields)+' FROM claude_mod_enrollments WHERE id=?', (eid,)).fetchone()
        return dict(zip(fields, row)) if row else None

    def setup_code(self, port, peer_receive=False):
        if type(peer_receive) is not bool:
            raise ModError(400, 'Invalid Claude peer receiving consent.')
        code, expiry = secrets.token_urlsafe(24), time.time() + PAIR_TTL
        with self.store.lock, self.db:
            self.maintain()
            if self.db.execute('SELECT COUNT(*) FROM claude_mod_setup_codes').fetchone()[0] >= 32:
                raise ModError(429, 'Too many pending Claude setup codes.')
            self.db.execute('INSERT INTO claude_mod_setup_codes (code_hash,expires,peer_receive) VALUES (?,?,?)',
                            (digest(code), expiry, int(peer_receive)))
        return dict(version=1, port=port, pair_code=code, expires_at=expiry, command=f'/watch-setup {port}:{code}', peer_receive=peer_receive)

    def setup_status(self, agents=None):
        with self.store.lock, self.db:
            self.maintain()
            grant = self.db.execute('SELECT peer_receive FROM claude_mod_grants WHERE active=1').fetchone()
            authorized = bool(grant)
            entries = [self.get(r[0]) for r in self.db.execute('SELECT id FROM claude_mod_enrollments WHERE active=1 AND grant_id IS NOT NULL AND generation IS NOT NULL')]
            connected = sum(1 for e in entries if not self.store.worker_failed and e['last_seen'] > time.time() - LEASE
                            and self.db.execute('SELECT 1 FROM claude_mod_grants WHERE id=? AND active=1', (e['grant_id'],)).fetchone()
                            and not self.room_veto(e['agent'], e['project']) and
                            (agents is None or e['agent'] not in agents or eligible(e['agent'], agents[e['agent']])
                             and agents[e['agent']]['cwd'] == e['project']))
            return dict(version=1, authorized=authorized, connected_chats=connected, peer_receive=bool(grant and grant[0] == 1))

    def _revoke(self):
        entries = [r[0] for r in self.db.execute('SELECT id FROM claude_mod_enrollments WHERE active=1 AND grant_id IS NOT NULL')]
        for eid in entries:
            self.uncertain(eid, 'Automatic Claude access was revoked. Earlier input may still run; nothing was resubmitted.')
        for row in self.rows():
            if row.get('mod_enrollment') in entries and not row.get('launched') and row['state'] == 'sending':
                row.update(state='failed', error='Automatic Claude access was revoked before claim. Nothing was submitted.')
                self.put(row)
        self.db.execute('UPDATE claude_mod_grants SET active=0')
        self.db.execute('UPDATE claude_mod_enrollments SET active=0,generation=NULL,last_seen=0 WHERE grant_id IS NOT NULL')
        self.db.execute('DELETE FROM claude_mod_setup_codes')

    def revoke(self):
        with self.store.lock, self.db:
            self._revoke()
        return dict(version=1, revoked=True)

    def setup(self, data, port):
        schema(data, ('code',))
        code = data['code']
        if not isinstance(code, str) or not 16 <= len(code) <= 100:
            raise ModError(403, 'Claude setup code is expired or used.', 'access_denied')
        row = self.db.execute('SELECT expires,peer_receive FROM claude_mod_setup_codes WHERE code_hash=?', (digest(code),)).fetchone()
        if not row or row[0] <= time.time():
            raise ModError(403, 'Claude setup code is expired or used.', 'access_denied')
        # Issuing a code is harmless; only its explicit consumption replaces access.
        self._revoke()
        gid, grant = str(uuid.uuid4()), secrets.token_hex(32)
        self.db.execute('INSERT INTO claude_mod_grants (id,token_hash,created,active,peer_receive) VALUES (?,?,?,1,?)',
                        (gid, digest(grant), time.time(), row[1]))
        return dict(version=1, grant_id=gid, grant=grant, port=port)

    def registration_access(self, data, token):
        fields = ('native_id', 'project', 'client_id', 'connection_seq')
        schema(data, fields + tuple(k for k in ('peer_protocol', 'reply_notice_protocol') if isinstance(data, dict) and k in data))
        if type(data.get('reply_notice_protocol', 0)) is not int or data.get('reply_notice_protocol', 0) not in (0, 1):
            raise ModError(400, 'Unsupported reply-notice protocol.')
        protocol = data.get('peer_protocol', 0)
        if type(protocol) is not int or protocol not in (0, 1):
            raise ModError(400, 'Unsupported Claude peer protocol.')
        if not isinstance(token, str) or len(token) != 64:
            raise ModError(403, 'Claude registration access is invalid.', 'access_denied')
        grant = self.db.execute('SELECT id,token_hash FROM claude_mod_grants WHERE active=1 AND token_hash=?', (digest(token),)).fetchone()
        if not grant or not hmac.compare_digest(grant[1], digest(token)):
            raise ModError(403, 'Claude registration access is invalid.', 'access_denied')
        seq = data['connection_seq']
        if (not ident(data['native_id']) or not ident(data['client_id']) or not project_text(data['project'])
                or type(seq) is not int or not 0 <= seq <= 9007199254740991):
            raise ModError(400, 'Invalid Claude registration identity.', 'identity_rejected')
        return grant

    @staticmethod
    def metadata_item(proof):
        return dict(runtime='Claude', cwd=proof['project'], name=proof['name'])

    def register(self, data, token, agents, proof=None):
        grant = self.registration_access(data, token)
        protocol, seq = data.get('peer_protocol', 0), data['connection_seq']
        aid, gid = 'c:'+data['native_id'], grant[0]
        item = agents.get(aid) if isinstance(agents, dict) else None
        if isinstance(agents, dict) and aid in agents:
            if not local_parent(aid, item):
                raise ModError(409, 'This is not an eligible local parent Claude chat and project.', 'identity_rejected')
            if not item.get('cwd'):
                raise ModError(409, 'Waiting for WATCH to observe this local Claude project.', 'not_observed')
            if not eligible(aid, item) or item['cwd'] != data['project']:
                raise ModError(409, 'This is not an eligible local parent Claude chat and project.', 'identity_rejected')
        elif (not proof or proof['agent'] != aid or proof['project'] != data['project']
              or not self.store.shared.metadata_current(proof)):
            raise ModError(409, 'Waiting for verified local Claude parent metadata.', 'not_observed')
        active = self.db.execute('SELECT id FROM claude_mod_enrollments WHERE agent=? AND active=1', (aid,)).fetchone()
        entry = self.get(active[0]) if active else None
        if entry and not entry['grant_id']:
            raise ModError(409, 'This chat retains its manual WATCH connection.', 'manual_conflict')
        if entry and (entry['grant_id'] != gid or entry['project'] != data['project']):
            raise ModError(409, 'This chat retains a different WATCH enrollment.', 'identity_rejected')
        payload = json.dumps(['watch-claude-session-v1', data['native_id'], data['project']], separators=(',', ':'), ensure_ascii=True)
        service_token = hmac.new(token.encode('ascii'), payload.encode('utf-8'), hashlib.sha256).hexdigest()
        if entry:
            same_client = entry['client_id'] == data['client_id']
            if same_client and seq < entry['connection_seq']:
                raise ModError(409, 'This registration episode is stale.', 'stale_connection')
            if not same_client and entry['generation']:
                raise ModError(409, 'Another Claude host currently owns this chat connection.', 'host_conflict')
            if same_client and seq == entry['connection_seq'] and (protocol != entry['peer_protocol'] or data.get('reply_notice_protocol', 0) != entry['reply_notice_protocol']):
                raise ModError(409, 'This registration episode has a different peer protocol.', 'stale_connection')
            if not (same_client and seq == entry['connection_seq'] and entry['generation']):
                self.uncertain(entry['id'], 'Claude reconnected after an earlier claim. Delivery remains unknown; it will not resubmit.')
                self.db.execute('UPDATE claude_mod_enrollments SET generation=?,client_id=?,connection_seq=?,last_seen=?,peer_protocol=?,reply_notice_protocol=? WHERE id=?',
                                (str(uuid.uuid4()), data['client_id'], seq, time.time(), protocol, data.get('reply_notice_protocol', 0), entry['id']))
        else:
            if self.db.execute('SELECT COUNT(*) FROM claude_mod_enrollments WHERE active=1').fetchone()[0] >= MAX_ENROLLMENTS:
                raise ModError(429, 'Too many active Claude connections.', 'connection_limit')
            eid = str(uuid.uuid4())
            self.db.execute('''INSERT INTO claude_mod_enrollments
                (id,token_hash,agent,project,generation,last_seen,active,grant_id,client_id,connection_seq,peer_protocol,reply_notice_protocol)
                VALUES (?,?,?,?,?,?,1,?,?,?,?,?)''', (eid, digest(service_token), aid, data['project'], str(uuid.uuid4()),
                                               time.time(), gid, data['client_id'], seq, protocol, data.get('reply_notice_protocol', 0)))
            entry = self.get(eid)
        entry = self.get(entry['id'])
        return dict(self.session(entry), enrollment_id=entry['id'], token=service_token)

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
        if entry['grant_id'] and data.get('client_id') != entry['client_id']:
            raise ModError(409, 'Another Claude runtime owns this connection.', 'host_conflict')

    def fields(self, entry, fields):
        return fields + (('client_id',) if entry['grant_id'] else ())

    def room_veto(self, agent, project):
        """Trusted pure in-memory callback; no discovery or filesystem work."""
        current = self.room_snapshot() if self.room_snapshot else {}
        return agent in current and (not local_parent(agent, current[agent]) or current[agent].get('cwd') != project)

    def capability(self, agent, item=None):
        with self.store.lock, self.db:
            self.maintain()
            row = self.db.execute('SELECT id FROM claude_mod_enrollments WHERE agent=? AND active=1', (agent,)).fetchone()
            entry = self.get(row[0]) if row else None
            ready = bool(entry and entry['generation'] and entry['last_seen'] > time.time() - LEASE and not self.store.worker_failed)
            if entry and self.room_veto(agent, entry['project']):
                ready = False
            if item is not None and entry and (not eligible(agent, item) or item.get('cwd') != entry['project']):
                ready = False
            return ready, entry, ('' if ready else 'Connect the WATCH official mod in this exact existing Claude chat.')

    def peer_ready(self, entry, item=None):
        """Transaction-owned facts only: no lock, filesystem, maintenance or commit."""
        if (not entry or not entry['active'] or not entry['grant_id'] or entry['peer_protocol'] != 1
                or not entry['generation'] or entry['last_seen'] <= time.time() - LEASE or self.store.worker_failed
                or self.room_veto(entry['agent'], entry['project'])):
            return False
        grant = self.db.execute('SELECT peer_receive FROM claude_mod_grants WHERE id=? AND active=1',
                                (entry['grant_id'],)).fetchone()
        return bool(grant and grant[0] == 1 and (item is None or
                    local_parent(entry['agent'], item) and item.get('cwd') == entry['project']))

    def peer_capability(self, agent, item):
        with self.store.lock, self.db:
            self.maintain()
            row = self.db.execute('SELECT id FROM claude_mod_enrollments WHERE agent=? AND active=1', (agent,)).fetchone()
            entry = self.get(row[0]) if row else None
            ready = self.peer_ready(entry, item)
            return ready, entry, ('' if ready else 'Agent receiving requires explicit WATCH setup and a compatible live official plugin in this exact Claude chat.')

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
        if self.db.execute('SELECT COUNT(*) FROM claude_mod_enrollments WHERE active=1').fetchone()[0] >= MAX_ENROLLMENTS:
            raise ModError(429, 'Too many active Claude connections.', 'connection_limit')
        eid, token, generation = str(uuid.uuid4()), secrets.token_hex(32), str(uuid.uuid4())
        self.db.execute('INSERT INTO claude_mod_enrollments (id,token_hash,agent,project,generation,last_seen,active) VALUES (?,?,?,?,?,?,1)', (eid, digest(token), pair[0], pair[1], generation, time.time()))
        self.db.execute('DELETE FROM claude_mod_pairs WHERE code_hash=?', (digest(data['code']),))
        result = self.session(self.get(eid))
        return dict(result, enrollment_id=eid, token=token)

    def statuses(self, entry):
        notices = self.store.reply_delivery.statuses(entry) if hasattr(self.store, 'reply_delivery') and entry['reply_notice_protocol'] == 1 else []
        return notices + [dict(request_id=r['request_id'], attempt_id=r['mod_attempt_id'], state=r['state'], turn_id=r.get('mod_turn', ''), error=r.get('error', ''))
                for r in sorted(self.rows(), key=lambda r: r['created']) if r.get('mod_enrollment') == entry['id'] and r.get('mod_attempt_id')][-32:]

    def validate_poll(self, entry, data):
        schema(data, self.fields(entry, ('generation', 'native_id', 'project', 'accept')))
        self.native(entry, data)
        if data['project'] != entry['project'] or type(data['accept']) is not bool:
            raise ModError(409, 'The enrolled Claude project does not match.')

    def poll_call(self, data, token, agents):
        # Preparation can read files/directory, so it cannot run under our SQLite
        # lock or transaction. It conveys evidence, never permission to dispatch.
        with self.store.lock, self.db:
            self.maintain()
            entry = self.authenticate(token)
            self.validate_poll(entry, data)
            new_rows = [r for r in self.rows() if data['accept'] and r.get('mod_enrollment') == entry['id']
                        and not r.get('launched') and r['state'] == 'sending']
            candidates = [r for r in new_rows if self.peer_row(r)]
            notices = self.store.reply_delivery.candidates(entry['agent']) if hasattr(self.store, 'reply_delivery') and entry['reply_notice_protocol'] == 1 and data['accept'] else []
        snapshot = agents() if callable(agents) else agents
        proof = None
        if (new_rows or notices) and (not isinstance(snapshot, dict) or entry['agent'] not in snapshot):
            try:
                proof = self.store.shared.metadata_target(entry['agent'], entry['project'])
            except Exception:
                pass  # Heartbeats and original receipts do not require new-claim evidence.
        prepared = {}
        for row in candidates:
            try:
                prepared[row['request_id']] = self.store.shared.prepare_mod_claim(row)
            except Exception:
                prepared[row['request_id']] = None
        notice_proofs = {}
        for row in notices:
            try:
                notice_proofs[row['request_id']] = self.store.reply_delivery.prepare(row['source']['agent_id'], row['source'])
            except Exception:
                pass
        with self.store.lock, self.db:
            snapshot = agents() if callable(agents) else agents  # pure current room vetoes
            snapshot = dict(snapshot) if isinstance(snapshot, dict) else {}
            if entry['agent'] not in snapshot and proof and self.store.shared.metadata_current(proof):
                snapshot[entry['agent']] = self.metadata_item(proof)
            self.maintain()
            # Connection or grant replacement during preparation invalidates this
            # poll; retained original attempts can still recover after reconnect.
            entry = self.authenticate(token)
            return self.poll(entry, data, snapshot, prepared, notice_proofs)

    def poll(self, entry, data, agents, prepared, notice_proofs=None):
        self.validate_poll(entry, data)
        self.db.execute('UPDATE claude_mod_enrollments SET last_seen=? WHERE id=?', (time.time(), entry['id']))
        rows = self.rows()
        busy = any(r.get('mod_enrollment') == entry['id'] and r.get('launched') and r['state'] in ('queued', 'awaiting_reply') for r in rows)
        busy = busy or self.store.reply_delivery.busy(entry['id'])
        claim = None
        item = agents.get(entry['agent']) if isinstance(agents, dict) else None
        can_claim = local_parent(entry['agent'], item) and item.get('cwd') == entry['project']
        if data['accept'] and can_claim and not busy and not self.store.worker_failed:
            for row in sorted(rows, key=lambda r: r['created']):
                if row.get('mod_enrollment') != entry['id'] or row.get('launched') or row['state'] != 'sending':
                    continue
                # Deadline checked inside the same serialized reservation transaction.
                if row['created'] + TTL <= time.time():
                    row.update(state='failed', error='This message expired before claim. Nothing was submitted.')
                    self.put(row)
                    continue
                if self.peer_row(row):
                    if row['request_id'] not in prepared:
                        continue  # A competing reservation was not prepared by this poll.
                    evidence = prepared[row['request_id']]
                    try:
                        if evidence is None:
                            raise ValueError('Claude peer preflight failed.')
                        self.store.shared.authorize_mod_claim_in_transaction(row, entry, evidence, item)
                    except (ValueError, peer_bridge.PeerError):
                        row.update(state='failed', error='Claude peer authorization changed before claim. Nothing was submitted.')
                        self.put(row)
                        continue
                attempt = str(uuid.uuid4())
                row.update(launched=True, state='queued', mod_attempt_id=attempt, mod_generation=entry['generation'], error='')
                self.put(row)
                claim = dict(request_id=row['request_id'], attempt_id=attempt, native_id=entry['agent'][2:], generation=entry['generation'], envelope=row['envelope'], expires_at=row['created']+TTL)
                if row.get('origin') == 'peer':
                    claim['origin'] = 'peer'
                break
        if claim is None and data['accept'] and can_claim and not busy:
            claim = self.store.reply_delivery.claim_in_transaction(entry, notice_proofs or {})
        return dict(version=1, native_id=entry['agent'][2:], generation=entry['generation'], peer_protocol=entry['peer_protocol'], reply_notice_protocol=entry['reply_notice_protocol'], claim=claim, requests=self.statuses(entry))

    def event(self, entry, data):
        if hasattr(self.store, 'reply_delivery'):
            self.native(entry, data)
            try:
                result = self.store.reply_delivery.event_in_transaction(entry, data)
            except peer_bridge.PeerError as error:
                raise ModError(error.status, str(error)) from None
            if result is not None:
                return result
        kind = data.get('kind')
        extras = {'submitted': (), 'started': ('turn_id',), 'answered': ('turn_id', 'answer'), 'dropped': (), 'unknown': ()}
        if kind not in extras:
            raise ModError(400, 'Invalid Claude request event.')
        schema(data, self.fields(entry, ('generation', 'native_id', 'request_id', 'attempt_id', 'kind') + extras[kind]))
        self.native(entry, data)
        row = next((r for r in self.rows() if r['request_id'] == data['request_id']), None)
        if (not row or row.get('mod_enrollment') != entry['id'] or row.get('mod_project') != entry['project']
                or row.get('mod_attempt_id') != data['attempt_id'] or not ident(data['attempt_id'])
                or row['agent'] != entry['agent'] or not row.get('launched')):
            raise ModError(409, 'Claude request does not match its original claim.')
        if self.peer_row(row):
            launched = self.db.execute('SELECT launched FROM shared_bridge_requests WHERE transport_id=?',
                                       (row['request_id'],)).fetchone()
            if not launched or launched[0] is None or not ident(row.get('mod_generation')):
                raise ModError(409, 'Claude peer claim has no original launch receipt.')
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
        if kind == 'answered':
            self.store.peers.owner_warning(row)
        self.put(row)
        return dict(request_id=row['request_id'], state=row['state'], turn_id=row.get('mod_turn', ''))

    def call(self, method, data, token='', agents=None, port=None):
        if callable(agents):
            self.room_snapshot = agents
        if method == 'poll':
            return self.poll_call(data, token, agents)
        if method == 'register':
            with self.store.lock, self.db:
                self.maintain()
                self.registration_access(data, token)
            if not project(data['project']):
                raise ModError(409, 'The Claude project is not canonical.', 'identity_rejected')
            snapshot = agents() if callable(agents) else agents
            aid = 'c:' + data['native_id']
            proof = None
            if not isinstance(snapshot, dict) or aid not in snapshot:
                try:
                    proof = self.store.shared.metadata_target(aid, data['project'])
                except Exception:
                    raise ModError(409, 'Waiting for verified local Claude parent metadata.', 'not_observed') from None
            with self.store.lock, self.db:
                self.maintain()
                current = agents() if callable(agents) else agents  # pure room snapshot only
                return self.register(data, token, current, proof)
        agents = agents() if callable(agents) else agents
        with self.store.lock, self.db:
            self.maintain()
            if method == 'enroll':
                return self.enroll(data)
            if method == 'setup':
                return self.setup(data, port)
            entry = self.authenticate(token)
            if method == 'connect':
                if entry['grant_id']:
                    raise ModError(409, 'Automatic Claude connections reconnect through registration.', 'register_required')
                schema(data, ('native_id', 'project'))
                self.native(entry, data, connected=False)
                if data['project'] != entry['project']:
                    raise ModError(409, 'The enrolled Claude project does not match.')
                self.uncertain(entry['id'], 'Claude reconnected after an earlier claim. Delivery remains unknown; it will not resubmit.')
                generation = str(uuid.uuid4())
                self.db.execute('UPDATE claude_mod_enrollments SET generation=?,last_seen=? WHERE id=?', (generation, time.time(), entry['id']))
                return self.session(self.get(entry['id']))
            if method == 'event':
                return self.event(entry, data)
            if method == 'disconnect':
                schema(data, self.fields(entry, ('generation', 'native_id')))
                self.native(entry, data)
                self.uncertain(entry['id'], 'The Claude mod disconnected. Delivery remains unknown; nothing was resubmitted.')
                self.db.execute('UPDATE claude_mod_enrollments SET generation=NULL,last_seen=0 WHERE id=?', (entry['id'],))
                return {'disconnected': True}
            raise ModError(404, 'Unknown Claude mod operation.')


def handle_http(handler, store, agents):
    """Dedicated purpose-scoped service routes; owner key is never accepted."""
    handler.connection.settimeout(10)
    try:
        port = handler.server.server_address[1]
        method = handler.path.removeprefix('/claude-mod/')
        if (method not in ('setup', 'register', 'enroll', 'connect', 'poll', 'event', 'disconnect')
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
        if method not in ('setup', 'enroll') and (len(auth) != 1 or not auth[0].startswith('Bearer ')):
            raise ModError(403, 'Claude mod service access is required.')
        if method in ('setup', 'enroll') and auth:
            raise ModError(403, 'Claude setup and pairing use one-time codes only.')
        result = store.mods.call(method, data, auth[0][7:] if auth else '', agents=agents, port=port)
        status = 200
    except ModError as error:
        status, result = error.status, {'error': str(error), 'error_code': error.code}
    except Exception:
        status, result = 400, {'error': 'Claude mod request could not be accepted.'}
    handler.close_connection = True
    return handler.send(status, json.dumps(result).encode(), 'application/json')
