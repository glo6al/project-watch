"""Durable execution ownership, separate from expiring message content.

Verified completion/drop or explicit owner retirement releases ownership. Retirement
is policy only: it does not establish any provider outcome. Connection loss and
elapsed retention never imply that a provider discarded its native input. No I/O on import.
"""
import hashlib
import json
import math
import uuid
import time

TERMINAL = ('completed', 'dropped', 'retired')
RETIRE_REASON = 'owner_requested_clear_unresolved_attempt'
RETIRE_MESSAGE = ('This attempt was retired by the owner. Provider outcome remains unknown; '
                  'late replies are withheld. Nothing was cancelled or resent.')
MAX_RESERVATIONS = 200000  # Both immutable ledgers are individually capped at 100000.
MAX_EVENTS, EVENT_RETENTION = 2048, 86400


def schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS peer_execution (
        request_id TEXT PRIMARY KEY, agent TEXT NOT NULL, created REAL NOT NULL,
        execution TEXT NOT NULL, capture TEXT NOT NULL, evidence TEXT NOT NULL)''')
    db.execute('CREATE INDEX IF NOT EXISTS peer_execution_agent ON peer_execution(agent,execution)')
    index = db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='peer_execution_active_created'").fetchone()
    if index and "'retired'" not in index[0]:
        db.execute('DROP INDEX peer_execution_active_created')
    db.execute("CREATE INDEX IF NOT EXISTS peer_execution_active_created ON peer_execution(created) WHERE execution NOT IN ('completed','dropped','retired')")
    db.execute('''CREATE TABLE IF NOT EXISTS peer_execution_events (
        event_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, kind TEXT NOT NULL,
        reason TEXT NOT NULL, received REAL NOT NULL, occurred REAL,
        before_state TEXT NOT NULL, after_state TEXT NOT NULL, fingerprint TEXT NOT NULL)''')


def metadata(db, request_id=None):
    result = {}
    suffix, params = (' WHERE transport_id=?', (request_id,)) if request_id is not None else ('', ())
    for tid, target, created, launched in db.execute('SELECT transport_id,target,created,launched FROM shared_bridge_requests' + suffix, params):
        result[tid] = (target, created, launched, 2)
    for tid, runtime, native, created, launched in db.execute('''SELECT r.transport_id,p.runtime,p.session,r.created,r.launched
            FROM peer_requests r LEFT JOIN peer_principals p ON p.id=r.target''' + (' WHERE r.transport_id=?' if request_id is not None else ''), params):
        result.setdefault(tid, ((('c:' if runtime == 'claude' else 'x:') + native) if native else '', created, launched, 1))
    return result


def get(db, request_id):
    r = db.execute('SELECT agent,created,execution,capture,evidence FROM peer_execution WHERE request_id=?', (request_id,)).fetchone()
    return dict(request_id=request_id, agent=r[0], created=r[1], execution=r[2], capture=r[3], evidence=json.loads(r[4])) if r else None


def is_retired(db, request_id):
    row = get(db, request_id)
    return bool(row and row['execution'] == 'retired')


def project_retired(db, request_id, row):
    """Suppress mutable payload state without asserting provider completion."""
    if not is_retired(db, request_id):
        return False
    row.update(state='unknown', reply='', candidate_reply='', tracking_stopped=True,
               capture_disposition='withheld', error=RETIRE_MESSAGE)
    row.pop('settled', None)
    row.pop('execution_outcome', None)
    return True


def retire(db, transport_id, owner_reason, *, expected_agent=None):
    """Offline owner action; caller holds exclusive service lock and transaction.

    Preserve immutable admission/launch and attempt receipts. No native I/O and
    no completion claim. The explicit reason is a metadata code, never user text.
    """
    if owner_reason != RETIRE_REASON:
        raise ValueError('Retirement requires the explicit owner decision code.')
    identities = db.execute('SELECT target,created,launched FROM shared_bridge_requests WHERE transport_id=?',
                            (transport_id,)).fetchall()
    legacy = db.execute('SELECT transport_id FROM peer_requests WHERE transport_id=?', (transport_id,)).fetchall()
    if len(identities) + len(legacy) != 1:
        raise ValueError('Retirement requires one exact immutable admission.')
    meta = metadata(db, transport_id).get(transport_id)
    if (not meta or not meta[0] or meta[2] is None
            or expected_agent is not None and meta[0] != expected_agent):
        raise ValueError('Retirement requires the original launched recipient identity.')
    schema(db)
    old = get(db, transport_id)
    if old and (old['agent'] != meta[0] or old['created'] != meta[1]):
        raise ValueError('Retirement identity conflicts with its immutable admission.')
    if old and old['execution'] == 'retired':
        if old['evidence'].get('owner_retirement', {}).get('reason') != owner_reason:
            raise ValueError('Retirement conflicts with its original owner decision.')
        return old
    if old and old['execution'] in TERMINAL:
        raise ValueError('A verified terminal attempt cannot be retired.')
    raw = db.execute('SELECT data FROM requests WHERE id=?', (transport_id,)).fetchone()
    try:
        row = json.loads(raw[0]) if raw else None
    except (ValueError, TypeError):
        row = None
    if not valid_payload(transport_id, row, meta):
        row = dict(request_id=transport_id, agent=meta[0], state='unknown')
    reconcile(db, row, meta)
    old = get(db, transport_id)
    if old['execution'] in TERMINAL or old['capture'] == 'captured':
        raise ValueError('A verified terminal attempt cannot be retired.')
    evidence = dict(old['evidence'])
    evidence['owner_retirement'] = dict(reason=owner_reason, decided_at=time.time(),
                                        source='offline_owner_decision', provider_outcome='unknown')
    db.execute("UPDATE peer_execution SET execution='retired',capture='withheld',evidence=? WHERE request_id=?",
               (json.dumps(evidence, sort_keys=True), transport_id))
    if raw:
        project_retired(db, transport_id, row)
        db.execute('UPDATE requests SET data=? WHERE id=?', (json.dumps(row), transport_id))
    record_event(db, transport_id, 'owner_retirement', owner_reason, old['execution'], 'retired')
    return get(db, transport_id)


def reconcile(db, row, meta=None, *, missing_payload=False):
    """Called inside the caller's request-write transaction. No payload retained."""
    tid = row.get('request_id')
    if not isinstance(tid, str):
        return
    old = get(db, tid)
    if meta is None:
        meta = metadata(db, tid).get(tid)
    if meta is None:
        return  # Owner requests have a separate lifecycle.
    target, created, launched, version = meta
    if not target or row.get('agent', target) != target:
        raise ValueError('Peer execution identity requires owner recovery before migration.')
    if old and old['agent'] != target:
        raise ValueError('Peer execution identity conflicts with its immutable admission.')
    if old and old['execution'] == 'retired':
        project_retired(db, tid, row)
        # Some callers persist JSON before reconciliation; overwrite that retained
        # copy too. Never recreate an expired payload merely to project retirement.
        db.execute('UPDATE requests SET data=? WHERE id=?', (json.dumps(row), tid))
        return
    if not missing_payload and not valid_payload(tid, row, meta):
        row = dict(request_id=tid, agent=target, state='unknown')

    if not old and db.execute('SELECT COUNT(*) FROM peer_execution').fetchone()[0] >= MAX_RESERVATIONS:
        raise ValueError('Peer execution history is full; owner maintenance is required.')
    evidence = dict(old['evidence']) if old else {}
    for key in ('mod_enrollment', 'mod_attempt_id', 'mod_generation', 'mod_turn', 'bound_turn', 'lifecycle_protocol',
                'completion_event_id', 'completion_fingerprint', 'answer_event_id', 'answer_fingerprint'):
        value = row.get(key)
        if value is not None and value != '':
            if key in evidence and evidence[key] not in ('', None) and evidence[key] != value:
                raise ValueError('Peer execution receipt conflicts with its original attempt.')
            evidence[key] = value
    if row.get('mod_project'):
        evidence['project_digest'] = hashlib.sha256(row['mod_project'].encode()).hexdigest()
    if launched is not None:
        evidence['launched'] = True
    if row.get('reply') and row.get('state') == 'answered':
        value = hashlib.sha256(row['reply'].encode()).hexdigest()
        if evidence.get('answer_digest') not in (None, value):
            raise ValueError('Peer execution answer conflicts with its original reply.')
        evidence['answer_digest'] = value
    execution = old['execution'] if old else ('uncertain' if launched is not None else 'reserved')
    capture = old['capture'] if old else 'pending'
    if execution not in TERMINAL:
        if row.get('execution_outcome') == 'dropped' or row.get('state') == 'failed' and launched is None:
            execution = 'dropped'
        elif row.get('settled') is True or row.get('execution_outcome') == 'completed' or row.get('state') == 'answered':
            execution = 'completed'
        elif not row.get('state') and launched is None:
            execution = 'dropped'  # Immutable ledger proves dispatch was never authorized.
        elif row.get('state') in ('unknown', 'expired') or not row.get('state'):
            execution = 'uncertain'
        elif row.get('bound_turn') or row.get('mod_turn') or row.get('state') == 'awaiting_reply':
            execution = 'running'
        elif launched is not None:
            execution = 'submitted'
    if capture != 'withheld':
        if row.get('capture_disposition') == 'withheld' or row.get('peer_mixed_input') is True or row.get('peer_context_overflow') is True:
            capture = 'withheld'
        elif row.get('state') == 'answered':
            capture = 'captured'
        elif capture != 'captured' and row.get('capture_disposition') == 'unusable':
            capture = 'unusable'
    db.execute('INSERT OR REPLACE INTO peer_execution VALUES (?,?,?,?,?,?)',
               (tid, target, created, execution, capture, json.dumps(evidence, sort_keys=True)))


def valid_payload(tid, row, meta):
    """A mutable JSON blob is not proof merely because a field is truthy."""
    target, created, launched, version = meta
    if (not isinstance(row, dict) or row.get('request_id') != tid or row.get('agent') != target
            or row.get('origin') != 'peer' or type(row.get('bridge_version', 1)) is not int
            or row.get('bridge_version', 1) != version
            or type(row.get('created')) not in (int, float) or not math.isfinite(row['created'])
            or row['created'] != created or type(row.get('launched')) is not bool
            or row['launched'] != (launched is not None)
            or row.get('state') not in ('sending', 'queued', 'awaiting_reply', 'quarantined', 'unknown', 'expired', 'failed', 'answered')
            or row.get('runtime') != ('claude' if target.startswith('c:') else 'codex')):
        return False
    for flag in ('settled', 'peer_mixed_input', 'peer_context_overflow', 'tracking_stopped'):
        if flag in row and type(row[flag]) is not bool:
            return False
    if (row.get('execution_outcome', '') not in ('', 'completed', 'dropped')
            or row.get('capture_disposition', '') not in ('', 'pending', 'captured', 'unusable', 'withheld')
            or type(row.get('lifecycle_protocol', 0)) is not int or row.get('lifecycle_protocol', 0) not in (0, 2)):
        return False
    for name in ('mod_enrollment', 'mod_attempt_id', 'mod_generation', 'completion_event_id', 'answer_event_id'):
        if name in row:
            try:
                if not isinstance(row[name], str) or str(uuid.UUID(row[name])) != row[name]: return False
            except (ValueError, AttributeError):
                return False
    for name in ('mod_project', 'target_project'):
        if name in row and (not isinstance(row[name], str) or not 0 < len(row[name]) <= 4096 or '\x00' in row[name]):
            return False
    if 'mod_project' in row and row.get('mod_project') != row.get('target_project'):
        return False
    for name in ('mod_project', 'target_project', 'mod_turn', 'bound_turn', 'reply'):
        value = row.get(name)
        if isinstance(value, str):
            try:
                value.encode('utf-8')
            except UnicodeError:
                return False
    for name in ('mod_turn', 'bound_turn'):
        if name in row and (not isinstance(row[name], str) or len(row[name]) > 128 or any(ord(c) < 32 for c in row[name])):
            return False
    for name in ('completion_fingerprint', 'answer_fingerprint'):
        if name in row and (not isinstance(row[name], str) or len(row[name]) != 64 or any(c not in '0123456789abcdef' for c in row[name])):
            return False
    if launched is not None and row['runtime'] == 'claude':
        if (row.get('transport') != 'claude_mod'
                or not all(row.get(k) for k in ('mod_enrollment', 'mod_attempt_id', 'mod_generation'))
                or not isinstance(row.get('mod_project'), str) or not row['mod_project']
                or row['mod_project'] != row.get('target_project')):
            return False
    if row.get('execution_outcome') == 'dropped':
        return row['state'] == 'failed'
    if row['state'] == 'failed' and launched is None:
        return True  # Immutable ledger proves no native dispatch was authorized.
    completed = row.get('settled') is True or row.get('execution_outcome') == 'completed' or row['state'] in ('answered', 'quarantined')
    if completed:
        if launched is None or not row['launched']:
            return False
        if row['runtime'] == 'claude':
            if (row.get('transport') != 'claude_mod' or not row.get('mod_turn')
                    or not all(row.get(k) for k in ('mod_enrollment', 'mod_attempt_id', 'mod_generation'))
                    or not isinstance(row.get('mod_project'), str) or not row['mod_project']
                    or row['mod_project'] != row.get('target_project')):
                return False
        elif not row.get('bound_turn'):
            return False
    if row['state'] == 'answered' and (not isinstance(row.get('reply'), str) or len(row['reply']) > 16000 or '\x00' in row['reply']):
        return False
    return True


def migrate(db):
    for tid, meta in metadata(db).items():
        raw = db.execute('SELECT data FROM requests WHERE id=?', (tid,)).fetchone()
        try:
            row = json.loads(raw[0]) if raw else None
        except (ValueError, TypeError):
            row = None
        if raw is not None and not valid_payload(tid, row, meta):
            # Bind uncertainty to the immutable ledger ID; never trust an ID or
            # terminal assertion supplied by malformed mutable request JSON.
            row = dict(request_id=tid, agent=meta[0], state='unknown')
        elif raw is None:
            row = dict(request_id=tid)  # Only an absent never-launched row is provably unsubmitted.
        reconcile(db, row, meta, missing_payload=raw is None)


def blockers(db, agent, *, excluding=None):
    rows = db.execute("SELECT request_id,execution,created FROM peer_execution WHERE agent=? AND execution NOT IN ('completed','dropped','retired')", (agent,)).fetchall()
    return [r for r in rows if r[0] != excluding]


def capacity_error(db, agent, now, ttl):
    blocked = db.execute("SELECT request_id,execution,created FROM peer_execution WHERE agent=? AND execution NOT IN ('completed','dropped','retired') ORDER BY created LIMIT 1", (agent,)).fetchall()
    if blocked:
        tid, state, created = blocked[0]
        if state == 'uncertain' or created <= now - ttl:
            return 'Earlier delivery requires recovery for this recipient (' + tid + '). The new request was not accepted; nothing was resent.'
        return 'This recipient has an active request (' + tid + '). The new request was not accepted.'
    count = db.execute("SELECT COUNT(*) FROM peer_execution WHERE execution NOT IN ('completed','dropped','retired') AND created>?", (now - ttl,)).fetchone()[0]
    if count >= 4:
        return 'Four peer deliveries are active. The new request was not accepted.'
    return None


def record_event(db, request_id, kind, reason, before, after, *, event_id=None, occurred=None, fingerprint=''):
    now = time.time()
    db.execute('DELETE FROM peer_execution_events WHERE received<?', (now - EVENT_RETENTION,))
    if event_id is None:
        # Duplicate legacy reports do not fill the local metadata journal.
        event_id = hashlib.sha256(json.dumps([request_id, kind, reason, before, after]).encode()).hexdigest()
    db.execute('INSERT OR IGNORE INTO peer_execution_events VALUES (?,?,?,?,?,?,?,?,?)',
               (event_id, request_id, kind, reason, now, occurred, before, after, fingerprint))
    db.execute('''DELETE FROM peer_execution_events WHERE event_id IN
        (SELECT event_id FROM peer_execution_events ORDER BY received DESC LIMIT -1 OFFSET ?)''', (MAX_EVENTS,))


def downgrade_ready(db):
    """Administrative preflight: preserve unresolved v2 and owner policy evidence."""
    # Earlier lifecycle implementations do not recognize this terminal policy and
    # can resurrect its reservation or publish a late answer on migration.
    if db.execute("SELECT 1 FROM peer_execution WHERE execution='retired' LIMIT 1").fetchone():
        return False
    if any(json.loads(evidence).get('lifecycle_protocol') == 2 for (evidence,) in db.execute(
            "SELECT evidence FROM peer_execution WHERE execution NOT IN ('completed','dropped','retired')")):
        return False
    for (raw,) in db.execute('SELECT data FROM requests'):
        try:
            row = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(row, dict) and row.get('lifecycle_protocol') == 2 and row.get('launched') and not row.get('settled'):
            return False
    return True
