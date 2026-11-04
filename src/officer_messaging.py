"""Explicit screen-authorized Codex questions. Importing this module does no I/O.

Queue acceptance is not delivery. Only the exact outgoing user envelope followed
by a matching completed turn can produce an answer. Uncertain sends are never retried.
"""
import json
import math
from datetime import datetime
import os
import re
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid
import claude_channel
import claude_mod
import peer_bridge

CLI = '/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex'
ACTIVE = ('sending', 'queued', 'awaiting_reply', 'unknown', 'expired')
MAX_RECORDS, MAX_PENDING, MAX_LINE, MAX_READ = 256, 8, 1_000_000, 2_000_000
TTL, RETENTION = 900, 86400
CLAUDE_OWNER_UNAVAILABLE = ("Exact named-chat delivery requires the WATCH official mod connected in this Claude chat. "
                           "Connect it before sending. Nothing was sent.")


class MessagingError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def valid_uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def validate_question(question):
    if not isinstance(question, str) or not question.strip() or len(question) > 2000 or '\x00' in question:
        raise MessagingError(400, 'Write a question between one and two thousand characters.')
    try:
        question.encode('utf-8')
    except UnicodeError:
        raise MessagingError(400, 'The question contains invalid text.')


def validate_target(agent, agents):
    if not isinstance(agent, str) or agent[:2] not in ('x:', 'c:') or not valid_uuid(agent[2:]):
        raise MessagingError(400, 'Select an exact local chat identifier. New named-chat delivery currently supports Codex only.')
    a = agents.get(agent)
    if not a:
        raise MessagingError(404, 'That agent is no longer in the room.')
    expected = 'Claude' if agent.startswith('c:') else 'Codex'
    if a.get('runtime') != expected or a.get('observed') or (expected == 'Claude' and a.get('sub')):
        raise MessagingError(400, 'That target is not an eligible local parent chat. New named-chat delivery currently supports Codex only.')
    return str(a.get('name') or agent)[:120]


def envelope(request_id, question, version=2):
    # Version one remains byte-exact for replies to messages sent before this fix.
    guidance = ('This is a question, not permission to change files or send messages. For a progress question, inspect your own current state.'
                if version == 1 else
                'This message relays the owner\'s request. Preserve its scope and authorization: '
                'an explicit work instruction authorizes that work under your existing permissions; '
                'a status question asks only for information. Do not infer additional tasks or permissions. '
                'For a progress question, inspect your own current state.')
    return ('WATCH_REQUEST ' + request_id + '\nThe owner asks through the Officer of the Watch:\n' + question +
            '\nReply in your final answer with WATCH_REPLY ' + request_id + ' on its own line, followed by your answer. '
            + guidance)


def outgoing_message(row):
    return row.get('envelope') or envelope(row['request_id'], row['question'], version=1)


def public(row):
    result = {k: row.get(k, '') for k in ('request_id', 'agent', 'name', 'question', 'state', 'reply', 'error')}
    if isinstance(row.get('delivery_warning'), str) and row['delivery_warning']:
        result['delivery_warning'] = row['delivery_warning']
    return result


class MessagingStore:
    def __init__(self, state_dir, sessions_dir, cli=CLI, channel_root=None):
        self.sessions_dir, self.cli = os.path.realpath(sessions_dir), cli
        self.channel_root = channel_root or os.path.join(state_dir, "claude-channels")
        self.codex_available, self.worker_failed = None, False
        self.lock, self.stop = threading.RLock(), threading.Event()
        self.thread, self.available, self.reason = None, False, 'Messaging has not started.'
        os.makedirs(state_dir, mode=0o700, exist_ok=True)
        st = os.lstat(state_dir)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise OSError('Messaging state directory must be private and owned by you.')
        # A second WATCH process must not launch the same persisted request. The
        # lifetime lock also protects recovery from marking another worker unknown.
        import fcntl
        self.owner_fd = os.open(os.path.join(state_dir, 'officer-messages.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        self.db = None
        try:
            st = os.fstat(self.owner_fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_nlink != 1 or st.st_mode & 0o077:
                raise OSError('Messaging lock must be a private regular file.')
            fcntl.flock(self.owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            path = os.path.join(state_dir, 'officer-messages.sqlite3')
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_nlink != 1 or st.st_mode & 0o077:
                    raise OSError('Messaging database must be a private regular file.')
            finally:
                os.close(fd)
            self.db = sqlite3.connect(path, check_same_thread=False, timeout=peer_bridge.DB_TIMEOUT)
            peer_bridge.shared_schema(self.db)
            self.db.execute('CREATE TABLE IF NOT EXISTS controls (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS expired_controls (id TEXT PRIMARY KEY)')
            self.db.execute('CREATE TABLE IF NOT EXISTS request_fulfillments (request_id TEXT PRIMARY KEY, control_request_id TEXT NOT NULL, confirmed_at REAL NOT NULL)')
            for (raw,) in self.db.execute('SELECT data FROM controls').fetchall():
                control = json.loads(raw)
                if control['state'] == 'sending':
                    control.update(state='unknown', error='WATCH restarted during control dispatch. It will not retry.')
                    self.db.execute('UPDATE controls SET data=? WHERE id=?', (json.dumps(control), control['request_id']))
            self.db.execute('CREATE TABLE IF NOT EXISTS asks (id TEXT PRIMARY KEY, user_text TEXT NOT NULL, created REAL NOT NULL, decision TEXT)')
            self.mods = claude_mod.ClaudeMod(self)
            for row in self._rows():
                if row.get('transport') == claude_mod.TRANSPORT:
                    continue
                if row['state'] == 'sending':
                    peer_meta = peer_bridge.transport_metadata(self.db, transport_ids=(row['request_id'],)).get(row['request_id'])
                    if peer_meta is not None and peer_meta['launched_at'] is None:
                        row.update(state='failed', settled=True, error='WATCH restarted before peer dispatch was authorized. Nothing was sent.')
                    else:
                        row.update(state='unknown', error='WATCH restarted during sending. Delivery is unknown; it will not resend.')
                    self._save(row)
            self._reconcile_fulfillments()
            self.db.commit()
            self.peers = peer_bridge.PeerBridge(self)
            self.shared = peer_bridge.SharedBridge(self)
        except BaseException:
            if self.db is not None:
                self.db.close()
            os.close(self.owner_fd)
            self.owner_fd = None
            raise

    def claim_ask(self, request_id, user_text):
        """Claim interpretation once. A crash while planning never reruns a model."""
        if not valid_uuid(request_id):
            raise MessagingError(400, 'A valid request identifier is required.')
        if not isinstance(user_text, str) or not user_text.strip() or len(user_text) > 4000 or '\x00' in user_text:
            raise MessagingError(400, 'The latest request must contain between one and four thousand characters.')
        try:
            user_text.encode('utf-8')
        except UnicodeError:
            raise MessagingError(400, 'The request contains invalid text.')
        with self.lock:
            self._reject_expired_control(request_id)
            old = self.db.execute('SELECT user_text, decision FROM asks WHERE id=?', (request_id,)).fetchone()
            if old:
                if old[0] != user_text:
                    raise MessagingError(409, 'That request identifier belongs to different text.')
                if old[1] is None:
                    raise MessagingError(409, 'Request interpretation already started; check status. It will not be resent.')
                return json.loads(old[1])
            now = time.time()
            with self.db:
                self.db.execute('DELETE FROM asks WHERE created < ?', (now - RETENTION,))
                count, recent = self.db.execute('SELECT COUNT(*), COALESCE(SUM(created > ?),0) FROM asks', (now - 60,)).fetchone()
                if count >= MAX_RECORDS or recent >= 20:
                    raise MessagingError(429, 'Too many Officer requests are being kept. Try again later.')
                self.db.execute('INSERT INTO asks VALUES (?,?,?,NULL)', (request_id, user_text, now))
            return None

    def decide_ask(self, request_id, decision):
        """Durably freeze routing before submit; no changed plan on a retry."""
        if not valid_uuid(request_id) or not isinstance(decision, dict) or decision.get('kind') not in ('relay', 'none', 'clarify', 'control'):
            raise MessagingError(400, 'Invalid request decision.')
        try:
            encoded = json.dumps(decision, sort_keys=True)
        except (ValueError, TypeError):
            raise MessagingError(400, 'Invalid request decision.')
        if len(encoded) > 16000:
            raise MessagingError(400, 'The request decision is too large.')
        with self.lock:
            old = self.db.execute('SELECT decision FROM asks WHERE id=?', (request_id,)).fetchone()
            if old is None:
                raise MessagingError(409, 'The request must be claimed before deciding.')
            if old[0] is not None:
                if old[0] != encoded:
                    raise MessagingError(409, 'The request already has a different decision.')
                return json.loads(old[0])
            with self.db:
                self.db.execute('UPDATE asks SET decision=? WHERE id=? AND decision IS NULL', (encoded, request_id))
            return decision

    def _reject_expired_control(self, request_id):
        # IDs alone survive retention: an old HTTP retry must never target a new turn.
        if self.db.execute('SELECT 1 FROM expired_controls WHERE id=?', (request_id,)).fetchone():
            raise MessagingError(409, 'This control request has expired and cannot be replayed. Issue a new explicit request; nothing was sent.')

    def _rows(self):
        rows = []
        stored = self.db.execute('SELECT id,data FROM requests').fetchall()
        peer_meta = peer_bridge.transport_metadata(self.db, transport_ids=(ident for ident, _ in stored))
        peer_ids = set(peer_meta)
        for ident, raw in stored:
            try:
                row = json.loads(raw)
                if not isinstance(row, dict) or row.get('request_id') != ident:
                    raise ValueError()
                if ident in peer_ids and (row.get('origin') != 'peer' or
                        (peer_meta[ident]['version'] == 2 and row.get('bridge_version') != 2)):
                    raise ValueError()
                if row.get('origin') == 'peer':
                    if (not self._timestamp(row.get('created')) or row.get('state') not in
                            ('sending', 'queued', 'awaiting_reply', 'quarantined', 'unknown', 'expired', 'failed', 'answered')
                            or not isinstance(row.get('agent'), str) or row['agent'][:2] not in ('x:', 'c:')
                            or not valid_uuid(row['agent'][2:]) or type(row.get('launched')) is not bool):
                        raise ValueError()
                rows.append(row)
            except (ValueError, TypeError):
                # Corrupt peer payloads must not take down owner reply tracking.
                if ident in peer_ids:
                    continue
                raise
        return rows

    def _save(self, row):
        try:
            self.db.execute('INSERT OR REPLACE INTO requests VALUES (?,?)', (row['request_id'], json.dumps(row)))
            self.db.commit()
        except sqlite3.OperationalError:
            self.db.rollback()
            raise

    def probe(self):
        if sys.platform != 'darwin':
            return False, 'Agent messaging currently requires Codex desktop on macOS.'
        try:
            version = subprocess.run([self.cli, '--version'], capture_output=True, timeout=5, text=True)
            help_result = subprocess.run([self.cli, 'queue', '--help'], capture_output=True, timeout=5, text=True)
            if version.returncode or not re.search(r'codex\S*\s+\d+\.\d+', version.stdout) or help_result.returncode or not all(s in help_result.stdout for s in ('--thread', '--message')):
                return False, 'The bundled Codex executable does not advertise the required queue command.'
            return True, ''
        except (OSError, subprocess.SubprocessError):
            return False, 'The bundled Codex executable is unavailable.'

    def start(self):
        self.codex_available, self.reason = self.probe()
        self.available = self.codex_available or claude_channel.any_configured(self.channel_root)
        if self.shared.enabled:
            try:
                self.shared.ensure_directory()
            except Exception:
                # Discovery failure must not disable owner messaging. RPC retains
                # its normal retry path if setup becomes available later.
                pass
        self.thread = threading.Thread(target=self._run, daemon=True, name='watch-agent-messages')
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(15)
        directory_thread = self.shared.directory_thread
        if directory_thread:
            directory_thread.join(2)
        if self.shared.readiness_thread:
            self.shared.readiness_thread.join(2)
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise RuntimeError('Messaging worker is still stopping; ownership retained.')
            self.db.close()
            if self.owner_fd is not None:
                os.close(self.owner_fd)
                self.owner_fd = None

    def _metadata(self, path, ident):
        if os.path.realpath(path) != path or not path.startswith(self.sessions_dir + os.sep):
            raise MessagingError(400, 'The session log is not a plain local transcript.')
        flags = os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, 'rb') as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise MessagingError(400, 'The session log is not a regular transcript.')
            raw = f.readline(MAX_LINE + 1)
        if not raw.endswith(b'\n') or len(raw) > MAX_LINE or not stat.S_ISREG(st.st_mode):
            raise MessagingError(400, 'The session identity is not readable yet.')
        try:
            data = json.loads(raw)
            meta = data['payload']
        except (ValueError, KeyError, TypeError):
            raise MessagingError(400, 'The session identity is not readable.')
        if data.get('type') != 'session_meta' or not isinstance(meta, dict) or meta.get('id') != ident:
            raise MessagingError(400, 'The transcript identity does not match the selected chat.')
        if meta.get('thread_source') == 'guardian_review' or meta.get('originator') == 'codex_exec' or meta.get('source') == 'exec':
            raise MessagingError(400, 'Review and exec sessions cannot receive questions.')
        return st

    def _target_log(self, ident):
        matches, pending, visited = [], [self.sessions_dir], 0
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    visited += 1
                    if visited > 20000:
                        raise MessagingError(503, 'Too many session entries to safely resolve this chat.')
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(entry.path)
                    elif entry.name.endswith(ident + '.jsonl'):
                        matches.append(entry.path)
                        if len(matches) > 1:
                            raise MessagingError(409, 'The chat must have exactly one local transcript before sending.')
        if len(matches) != 1:
            raise MessagingError(409, 'The chat must have exactly one local transcript before sending.')
        path = matches[0]
        return path, self._metadata(path, ident)

    def snapshot(self):
        claude_ready = (self.mods.any_ready() or claude_channel.any_configured(self.channel_root)) and not self.worker_failed
        codex_ready = (self.available if self.codex_available is None else self.codex_available) and not self.worker_failed
        with self.lock:
            now = time.time()
            controls = [json.loads(raw) for (raw,) in self.db.execute('SELECT data FROM controls')]
            retained = [r for r in controls if r['state'] == 'sending' or now - r['created'] <= RETENTION]
            return {'requests': [self._public_request(r) for r in sorted((r for r in self._rows() if r.get('origin') != 'peer'), key=lambda x: x['created'])],
                    'controls': [self._public_control(r) for r in sorted(retained, key=lambda x: (x['created'], x['request_id']))],
                    'available': codex_ready or claude_ready, 'reason': '' if codex_ready or claude_ready else self.reason,
                    'capabilities': {'codex': codex_ready, 'claude': claude_ready}}

    def owner_capabilities(self, agent, agents):
        """Advisory eligibility only; never starts native observation or resolves a turn."""
        reason = ''
        try:
            validate_target(agent, agents)
        except MessagingError as error:
            reason = str(error)
        codex = not reason and agent.startswith('x:')
        queue_reason = reason
        claude_setup = None
        if isinstance(agent, str) and agent.startswith('c:'):
            status = self.mods.setup_status(agents)
            ready, entry, _ = self.mods.capability(agent, agents.get(agent, {}))
            state = ('connected' if ready else 'setup_needed' if not status['authorized']
                     else 'disconnected' if entry else 'waiting')
            claude_setup = dict(authorized=status['authorized'], state=state, peer_receive=status['peer_receive'])
        if not reason and not codex:
            queue_reason = ('' if ready else 'Waiting for this Claude chat to load or reconnect the WATCH plugin.'
                            if status['authorized'] else 'Set up Claude once in WATCH to connect eligible local chats.')
        elif codex and (self.worker_failed or not (self.available if self.codex_available is None else self.codex_available)):
            queue_reason = self.reason or 'Codex queue transport is unavailable.'
        if codex and not queue_reason:
            try:
                self._target_log(agent[2:])
            except MessagingError as error:
                queue_reason = str(error)
            except (OSError, ValueError, TypeError):
                queue_reason = 'The local transcript identity is unavailable or unreadable.'
        control_reason = reason
        if not reason and (not codex or agents[agent].get('sub') or agents[agent].get('parent')):
            control_reason = 'Live controls support local parent Codex desktop chats only.'
        elif not reason and sys.platform != 'darwin':
            control_reason = 'Codex desktop controls require macOS.'
        result = {'agent': agent, 'actions': {
            'queue': {'available': not bool(queue_reason), 'reason': queue_reason, 'mode': 'queue'},
            'steer': {'supported': not bool(control_reason), 'reason': control_reason, 'mode': 'new_instruction'},
            'interrupt': {'supported': not bool(control_reason), 'reason': control_reason, 'mode': 'interrupt'},
            'promote_queued': {'available': False, 'reason': 'Promoting an existing queued message is unavailable.', 'mode': 'queued_promotion'}}}
        if claude_setup is not None:
            result['claude_setup'] = claude_setup
        return result

    def owner_action(self, action, agent, text, request_id, agents):
        """The authenticated owner gateway. Replay checks precede new-send eligibility.

        The ledger methods own atomic duplicate/conflict checks and dispatch preflight;
        preview capabilities are never caller authority and never gate a durable replay.
        """
        if action == 'queue':
            return self.submit(agent, text, request_id, agents)
        if action in ('steer', 'interrupt'):
            return self.submit_control(action, agent, text, request_id, agents)
        raise MessagingError(400, 'Unsupported owner action. Promoting queued messages is unavailable.')

    def submit(self, agent, question, request_id, agents):
        validate_question(question)
        if not valid_uuid(request_id):
            raise MessagingError(400, 'A valid request identifier is required.')
        with self.lock:
            self._reject_expired_control(request_id)
            rows = self._rows()
            old = next((r for r in rows if r['request_id'] == request_id), None)
            if old:
                if old.get('origin') == 'peer':
                    raise MessagingError(409, 'That identifier is reserved for a different origin.')
                if old['agent'] != agent or old['question'] != question:
                    raise MessagingError(409, 'That request identifier belongs to a different question.')
                return self._public_request(old)
            if self.db.execute('SELECT 1 FROM controls WHERE id=?', (request_id,)).fetchone():
                raise MessagingError(409, 'That request identifier already belongs to a live control.')
            name = validate_target(agent, agents)
            queue = self.owner_capabilities(agent, agents)['actions']['queue']
            if not queue['available']:
                raise MessagingError(503, queue['reason'])
            now = time.time()
            rows = [r for r in self._prune(rows, now) if r.get('origin') != 'peer']
            active = [r for r in rows if r['state'] in ACTIVE]
            # An unresolved request is not a lock on the whole conversation.
            # A distinct explicit instruction gets its own delivery/reply identity.
            if any(r['agent'] == agent and r['question'] == question for r in active):
                raise MessagingError(409, 'This same message is still unresolved for that agent and may still run. WATCH will not send it twice; no new message was sent.')
            if len(active) >= MAX_PENDING:
                raise MessagingError(429, 'Too many unresolved message deliveries are being tracked. Older messages may still run; no new message was sent.')
            if len(rows) >= MAX_RECORDS:
                raise MessagingError(429, 'Message history is at its storage limit. No new message was sent.')
            if sum(r['created'] > now - 60 for r in rows) >= 10:
                raise MessagingError(429, 'Too many messages were submitted in the last minute. No new message was sent.')
            row = dict(request_id=request_id, agent=agent, name=name, question=question, state='sending', reply='', error='',
                       created=now, launched=False)
            if any(r['agent'] == agent for r in active):
                row['delivery_warning'] = 'When this message was submitted, older messages to this agent were unresolved and could still run. This request did not cancel them.'
            if agent.startswith('c:'):
                ready, enrollment, _ = self.mods.capability(agent, agents[agent])
                if not ready:
                    raise MessagingError(503, CLAUDE_OWNER_UNAVAILABLE)
                row.update(runtime='claude', transport=claude_mod.TRANSPORT,
                           mod_enrollment=enrollment['id'], mod_project=enrollment['project'],
                           envelope=envelope(request_id, question))
            else:
                path, st = self._target_log(agent[2:])
                # Start only after a complete record; never bind a partial old line.
                with open(path, 'rb') as f:
                    if st.st_size:
                        f.seek(st.st_size - 1)
                        if f.read(1) != b'\n':
                            raise MessagingError(409, 'The chat log is being written. Try again shortly.')
                row.update(runtime='codex', path=path, inode=st.st_ino, device=st.st_dev, cursor=st.st_size,
                           current_turn='', bound_turn='', final='', envelope=envelope(request_id, question))
            self._save(row)  # Durable before a worker can launch the external process.
            return self._public_request(row)

    @staticmethod
    def _public_control(row):
        result = {k: row.get(k, '') for k in ('request_id', 'action', 'agent', 'name', 'instruction', 'state', 'error')}
        result['created'] = row['created'] if MessagingStore._timestamp(row.get('created')) else 0
        return result

    def _control_target(self, agent, agents, action='steer'):
        validate_target(agent, agents)
        if not agent.startswith('x:') or agents[agent].get('sub') or agents[agent].get('parent'):
            raise MessagingError(400, 'Live controls currently support local parent Codex desktop chats only.')
        path, identity = self._target_log(agent[2:])
        if action == 'steer' and time.time() - identity.st_mtime > 300:
            raise MessagingError(409, 'The chat log is stale; I cannot establish its current active turn.')
        with open(path, 'rb') as f:
            current = os.fstat(f.fileno())
            if (current.st_ino, current.st_dev) != (identity.st_ino, identity.st_dev):
                raise MessagingError(409, 'The chat transcript changed during control preparation.')
            meta = json.loads(f.readline(MAX_LINE + 1))['payload']
            if not stat.S_ISREG(current.st_mode) or not isinstance(meta, dict) or meta.get('id') != agent[2:]:
                raise MessagingError(409, 'The chat transcript identity changed during control preparation.')
            if meta.get('parent_thread_id') or meta.get('agent_path') not in (None, '', '/root'):
                raise MessagingError(400, 'Live controls support parent chats only.')
            offset = max(0, current.st_size - MAX_READ)
            f.seek(offset)
            if offset:
                f.readline(MAX_LINE + 1)
            raw = f.read(MAX_READ + 1)
        if not raw.endswith(b'\n') or len(raw) > MAX_READ:
            raise MessagingError(409, 'The current turn is not safely readable yet.')
        active, ended = set(), set()
        for line in raw.splitlines():
            if len(line) > MAX_LINE:
                raise MessagingError(409, 'The current turn record is too large to verify.')
            try:
                item = json.loads(line)
            except ValueError:
                raise MessagingError(409, 'The current turn log is malformed.') from None
            payload = item.get('payload') if isinstance(item, dict) else None
            if not isinstance(payload, dict):
                continue
            kind, turn = payload.get('type'), payload.get('turn_id')
            if item.get('type') == 'event_msg' and kind in ('task_complete', 'turn_aborted', 'task_aborted'):
                if not valid_uuid(turn):
                    raise MessagingError(409, 'A terminal turn record is ambiguous; no control was sent.')
                else:
                    active.discard(turn); ended.add(turn)
            elif (item.get('type') == 'event_msg' and kind == 'task_started') or item.get('type') == 'turn_context':
                if not valid_uuid(turn):
                    raise MessagingError(409, 'The active turn identifier is missing or invalid.')
                if turn not in ended:
                    active.add(turn)
        after = self._metadata(path, agent[2:])
        if (after.st_ino, after.st_dev, after.st_size, after.st_mtime_ns) != (current.st_ino, current.st_dev, current.st_size, current.st_mtime_ns):
            raise MessagingError(409, 'The chat changed during control preparation; no control was sent.')
        if len(active) != 1:
            raise MessagingError(409, 'I cannot establish one active turn for that chat. No control was sent.')
        cwd = meta.get('cwd')
        if not isinstance(cwd, str) or not os.path.isabs(cwd) or '\x00' in cwd:
            raise MessagingError(409, 'The chat working directory is not recorded safely.')
        return next(iter(active)), cwd, {'path': path, 'inode': after.st_ino, 'device': after.st_dev, 'size': after.st_size, 'mtime_ns': after.st_mtime_ns}

    def submit_control(self, action, agent, instruction, request_id, agents, execute=None):
        if action not in ('steer', 'interrupt') or not valid_uuid(request_id) or not isinstance(agent, str):
            raise MessagingError(400, 'Invalid control request.')
        if action == 'steer':
            validate_question(instruction)
        elif instruction != '':
            raise MessagingError(400, 'An interrupt cannot include another instruction.')
        with self.lock:
            self._reject_expired_control(request_id)
            existing = self.db.execute('SELECT data FROM controls WHERE id=?', (request_id,)).fetchone()
            if existing:
                old = json.loads(existing[0])
                if (old['action'], old['agent'], old['instruction']) != (action, agent, instruction):
                    raise MessagingError(409, 'That request identifier belongs to a different control.')
                return self._public_control(old)
            if self.db.execute('SELECT 1 FROM requests WHERE id=?', (request_id,)).fetchone():
                raise MessagingError(409, 'That request identifier already belongs to a queued message.')
            now = time.time()
            rows = [json.loads(r[0]) for r in self.db.execute('SELECT data FROM controls')]
            retained = [r for r in rows if now - r['created'] <= RETENTION or r['state'] == 'sending']
            if any(r['agent'] == agent and r['state'] == 'sending' for r in retained):
                raise MessagingError(409, 'A control for that chat is still being dispatched. Wait for its result.')
            if len(retained) >= MAX_RECORDS or sum(r['created'] > now - 60 for r in retained) >= 10:
                raise MessagingError(429, 'Too many live controls; try again later.')
            with self.db:
                for old in rows:
                    if old not in retained:
                        self.db.execute('INSERT OR IGNORE INTO expired_controls VALUES (?)', (old['request_id'],))
                        self.db.execute('DELETE FROM asks WHERE id=?', (old['request_id'],))
                        self.db.execute('DELETE FROM controls WHERE id=?', (old['request_id'],))
            try:
                turn, cwd, transcript = self._control_target(agent, agents, action)
            except (MessagingError, OSError, ValueError, KeyError, TypeError) as error:
                # Freeze preflight failure too: replay must not resolve a newer turn.
                row = dict(request_id=request_id, action=action, agent=agent, instruction=instruction,
                           name=str(agents.get(agent, {}).get('name') or agent)[:120], state='failed',
                           error=str(error) if isinstance(error, MessagingError) else 'The active turn could not be verified. No control was sent.', created=now)
                with self.db:
                    self.db.execute('INSERT INTO controls VALUES (?,?)', (request_id, json.dumps(row)))
                return self._public_control(row)
            row = dict(request_id=request_id, action=action, agent=agent, instruction=instruction,
                       name=str(agents[agent].get('name') or agent)[:120], expected_turn_id=turn, cwd=cwd, transcript=transcript,
                       state='sending', error='', created=now)
            with self.db:
                self.db.execute('INSERT INTO controls VALUES (?,?)', (request_id, json.dumps(row)))
        # The bridge may block; no room or store lock spans its I/O. Once reserved,
        # every exception is uncertain and must never trigger another dispatch.
        try:
            check = self._metadata(transcript['path'], agent[2:])
            unchanged = (check.st_ino, check.st_dev, check.st_size, check.st_mtime_ns) == (transcript['inode'], transcript['device'], transcript['size'], transcript['mtime_ns'])
        except (OSError, MessagingError):
            unchanged = False
        try:
            if not unchanged:
                outcome = {'state': 'failed', 'error': 'The chat transcript changed before dispatch. No control was sent.'}
            else:
                if execute is None:
                    from codex_desktop_control import execute
                outcome = execute(action, agent[2:], instruction=instruction, expected_turn_id=turn, cwd=cwd)
            state = outcome.get('state') if isinstance(outcome, dict) else None
            allowed = ('acknowledged', 'failed', 'unknown') if action == 'steer' else ('interrupted', 'no_op', 'failed', 'unknown')
            if state not in allowed:
                row.update(state='unknown', error='The desktop control result could not be confirmed. It will not be retried.')
            else:
                row.update(state=state, error=str(outcome.get('error') or '')[:500])
        except Exception:
            row.update(state='unknown', error='Desktop control status is unknown. It will not be retried.')
        with self.lock:
            with self.db:
                row['completed_at'] = time.time()
                self.db.execute('UPDATE controls SET data=? WHERE id=?', (json.dumps(row), request_id))
                if row['action'] == 'interrupt' and row['state'] == 'interrupted':
                    self._reconcile_fulfillments()
        return self._public_control(row)

    def _public_request(self, row):
        result = public(row)
        evidence = self.db.execute('SELECT control_request_id FROM request_fulfillments WHERE request_id=?',
                                   (row['request_id'],)).fetchone()
        if evidence:
            result['fulfillment'] = {'state': 'fulfilled', 'intent': 'pause', 'via': 'interrupt',
                                     'control_request_id': evidence[0]}
        return result

    @staticmethod
    def _timestamp(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

    @staticmethod
    def _pure_pause(text, row, owner=False):
        """A deliberately narrow grammar, never a general intent classifier.

        Both authenticated owner text and its exact frozen relay payload must
        qualify. In particular a queued/deferred owner request cannot inherit
        immediate intent from a shortened payload.
        """
        if not isinstance(text, str):
            return False
        text = text.strip()
        effect = r'(?:pause(?: (?:your|its|the)(?: current)? work)?|stop (?:working|(?:your|its|the)(?: current)? work)|halt(?: (?:your|its|the)(?: current)? work)?|interrupt(?: (?:your|its|the)(?: current)? work)?)'
        ending = r'(?: now)?[.!]?'
        if not owner:
            return re.fullmatch(effect + ending, text, re.IGNORECASE) is not None
        targets = [row.get('name'), row.get('agent'), str(row.get('agent', ''))[2:]]
        for target in targets:
            if not isinstance(target, str) or not target.strip():
                continue
            target = re.escape(target)
            patterns = [r'(?:tell|ask) ' + target + r' to ' + effect,
                        r'(?:pause|stop|halt|interrupt) ' + target + r"(?:'s)?(?: (?:current )?work)?"]
            if any(re.fullmatch(pattern + ending, text, re.IGNORECASE) for pattern in patterns):
                return True
        return False

    def _legacy_control_completion(self, control):
        """Recover only a timestamped abort of the exact pinned turn, locally.

        Read at most MAX_READ bytes after the saved complete-record boundary.
        A witness found within that budget suffices even if the log grew larger.
        Transcript content supplies evidence only, never instructions.
        """
        transcript = control.get('transcript')
        if not isinstance(transcript, dict):
            return None
        try:
            path, offset = transcript['path'], transcript['size']
            if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
                return None
            st = self._metadata(path, control['agent'][2:])
            identity = (transcript['inode'], transcript['device'])
            if (st.st_ino, st.st_dev) != identity or st.st_size < offset:
                return None
            with open(path, 'rb') as f:
                current = os.fstat(f.fileno())
                if (current.st_ino, current.st_dev) != identity or current.st_size < offset:
                    return None
                if offset:
                    f.seek(offset - 1)
                    if f.read(1) != b'\n':
                        return None
                f.seek(offset)
                raw = f.read(MAX_READ)
            for line in raw.split(b'\n')[:-1]:
                if len(line) > MAX_LINE:
                    return None
                event = json.loads(line)
                if not isinstance(event, dict):
                    continue
                payload = event.get('payload')
                if (event.get('type') != 'event_msg' or not isinstance(payload, dict)
                        or payload.get('type') != 'turn_aborted' or payload.get('turn_id') != control['expected_turn_id']):
                    continue
                stamp = event.get('timestamp')
                if not isinstance(stamp, str):
                    return None
                parsed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    return None
                ended = parsed.timestamp()
                return ended if self._timestamp(ended) and ended >= control['created'] else None
        except (MessagingError, OSError, KeyError, TypeError, ValueError, OverflowError):
            return None
        return None

    def _reconcile_fulfillments(self):
        """Annotate historical pause satisfaction, without changing delivery.

        Caller owns the store lock (or startup ownership) and transaction. The
        separate table cannot be overwritten by tick's in-flight request copy.
        Unknown routing anywhere inside the authority interval fails closed.
        """
        requests = self._rows()
        controls = [json.loads(raw) for (raw,) in self.db.execute('SELECT data FROM controls')]
        asks = {rid: (text, created, decision) for rid, text, created, decision in
                self.db.execute('SELECT id, user_text, created, decision FROM asks')}
        fulfilled = {rid for (rid,) in self.db.execute('SELECT request_id FROM request_fulfillments')}
        completions = {}
        for control in controls:
            if (control.get('action') != 'interrupt' or control.get('state') != 'interrupted'
                    or not valid_uuid(control.get('request_id')) or not valid_uuid(control.get('expected_turn_id'))
                    or not self._timestamp(control.get('created'))):
                continue
            ended = control.get('completed_at')
            if ended is None:
                ended = self._legacy_control_completion(control)
            if self._timestamp(ended) and ended >= control['created']:
                completions[control['request_id']] = ended
        for row in requests:
            rid, agent = row['request_id'], row.get('agent')
            source = asks.get(rid)
            if (row.get('origin') == 'peer' or rid in fulfilled or not valid_uuid(rid) or not source or not isinstance(agent, str) or not agent.startswith('x:')
                    or not valid_uuid(agent[2:]) or not self._timestamp(row.get('created'))
                    or not self._timestamp(source[1]) or source[1] > row['created']):
                continue
            try:
                decision = json.loads(source[2])
            except (TypeError, ValueError):
                continue
            if (not isinstance(decision, dict) or decision.get('kind') != 'relay'
                    or decision.get('agent') != agent or decision.get('question') != row.get('question')
                    or not self._pure_pause(source[0], row, owner=True) or not self._pure_pause(row.get('question'), row)):
                continue
            for control in sorted(controls, key=lambda c: completions.get(c.get('request_id'), float('inf'))):
                cid = control.get('request_id')
                ended = completions.get(cid)
                if (ended is None or control.get('agent') != agent or control['created'] <= row['created']
                        or control['created'] <= source[1]
                        or (row.get('bound_turn') and row['bound_turn'] != control['expected_turn_id'])):
                    continue
                transcript = control.get('transcript', {})
                if (not isinstance(transcript, dict) or row.get('path') != transcript.get('path')
                        or row.get('inode') != transcript.get('inode') or row.get('device') != transcript.get('device')):
                    continue
                blocked = False
                for other_id, (_, created, raw_decision) in asks.items():
                    if other_id in (rid, cid):
                        continue
                    if not self._timestamp(created):
                        blocked = True; break
                    if not source[1] <= created <= ended:
                        continue
                    try:
                        other = json.loads(raw_decision)
                    except (TypeError, ValueError):
                        blocked = True; break
                    if not isinstance(other, dict) or other.get('kind') not in ('relay', 'control', 'none', 'clarify'):
                        blocked = True; break
                    if other['kind'] in ('relay', 'control'):
                        target = other.get('agent')
                        if (not isinstance(target, str) or target[:2] not in ('x:', 'c:')
                                or not valid_uuid(target[2:]) or target == agent):
                            blocked = True; break
                        try:
                            if other['kind'] == 'relay':
                                validate_question(other.get('question'))
                            elif other.get('action') == 'steer':
                                validate_question(other.get('instruction'))
                            elif other.get('action') != 'interrupt' or other.get('instruction') != '':
                                blocked = True; break
                        except MessagingError:
                            blocked = True; break
                    elif other['kind'] == 'clarify' and (not isinstance(other.get('text'), str)
                                                         or not other['text'].strip()):
                        blocked = True; break
                if blocked:
                    continue
                # Launched peer input remains interference even if its payload
                # was malformed and isolated by _rows(). Reservation may predate
                # the authority interval, so compare durable launch time instead.
                peer_launches = peer_bridge.transport_metadata(self.db, agent_id=agent, launched_since=source[1])
                if any(not self._timestamp(meta['launched_at']) or source[1] <= meta['launched_at'] <= ended
                       for meta in peer_launches.values()):
                    continue
                # Direct message/control endpoints may have no asks provenance.
                if any(other.get('request_id') not in (rid, cid) and other.get('agent') == agent
                       and (not self._timestamp(other.get('created')) or source[1] <= other['created'] <= ended)
                       for other in requests + controls):
                    continue
                self.db.execute('INSERT OR IGNORE INTO request_fulfillments VALUES (?,?,?)', (rid, cid, ended))
                break

    def _prune(self, rows, now):
        for r in rows:
            if now - r['created'] > RETENTION:
                self.db.execute('DELETE FROM request_fulfillments WHERE request_id=?', (r['request_id'],))
                self.db.execute('DELETE FROM requests WHERE id=?', (r['request_id'],))
        self.db.execute("DELETE FROM requests WHERE id IN (SELECT transport_id FROM shared_bridge_requests WHERE created < ?)", (now - RETENTION,))
        self.db.execute("UPDATE shared_bridge_requests SET preview='' WHERE created < ? AND preview<>''", (now - RETENTION,))
        self.db.commit()
        return [r for r in rows if now - r['created'] <= RETENTION]

    def _send(self, row):
        if row.get('transport') == claude_mod.TRANSPORT:
            return 'unknown', 'Official-mod requests are dispatched only by their atomic claim.'
        if row.get('runtime') == 'claude':
            return self._send_claude(row)
        try:
            st = self._metadata(row['path'], row['agent'][2:])
            if (st.st_ino, st.st_dev) != (row['inode'], row['device']) or st.st_size < row['cursor']:
                raise MessagingError(400, 'The transcript changed before sending.')
        except (OSError, MessagingError):
            return 'failed', 'The target transcript changed before sending; nothing was sent.'
        try:
            proc = subprocess.Popen([self.cli, 'queue', '--thread', row['agent'][2:], '--message', outgoing_message(row)],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            return 'failed', 'Codex could not be launched; nothing was sent.'
        try:
            code = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except (OSError, subprocess.SubprocessError):
                pass
            return 'unknown', 'Sending timed out. Delivery is unknown; WATCH will not resend.'
        except (OSError, subprocess.SubprocessError):
            return 'unknown', 'Sending could not be confirmed after launch. WATCH will not resend.'
        if code:
            return 'unknown', 'Codex returned an error after launch. Delivery is unknown; WATCH will not resend.'
        return 'queued', ''

    def _apply_claude(self, row, record):
        if record['state'] == 'answered':
            row.update(state='answered', reply=record.get('reply', ''), error='')
            if row.get('origin') != 'peer' and record.get('peer_input_seen') is True:
                row['delivery_warning'] = (row.get('delivery_warning', '') + ' ' + peer_bridge.PEER_WARNING).strip()
        elif record['state'] == 'unknown':
            row.update(state='unknown', error='Claude could not confirm delivery. WATCH will not resend.')
        elif row['state'] not in ('expired', 'unknown'):
            row.update(state='queued', error='')

    def _send_claude(self, row):
        if row.get('origin') == 'peer':
            return 'failed', claude_channel.PEER_IDENTITY_REASON + ' Nothing was sent.'
        try:
            descriptor = claude_channel.pinned(self.channel_root, row['channel'])
        except claude_channel.ChannelError:
            return 'failed', 'The Claude channel disappeared or changed before sending; nothing was sent. ' + claude_channel.START_HINT
        try:
            record = (claude_channel.exchange(descriptor, row['request_id'], row['envelope'], origin='peer')
                      if row.get('origin') == 'peer' else claude_channel.exchange(descriptor, row['request_id'], row['envelope']))
            self._apply_claude(row, record)
            return row['state'], row['error']
        except claude_channel.ChannelError:
            return 'unknown', 'Claude delivery could not be confirmed after sending. WATCH will not resend.'

    def _poll_claude(self, row):
        try:
            descriptor = claude_channel.pinned(self.channel_root, row['channel'])
            record = (claude_channel.exchange(descriptor, row['request_id'], origin='peer')
                      if row.get('origin') == 'peer' else claude_channel.exchange(descriptor, row['request_id']))
            self._apply_claude(row, record)
        except claude_channel.ChannelError:
            row.update(state='unknown', error='The original Claude channel or its reply is unavailable. WATCH will not redirect or resend.')
        if row['state'] in ('queued', 'awaiting_reply') and time.time() - row['created'] > TTL:
            row.update(state='expired', error='No matching reply yet. WATCH will keep checking without resending.')

    def _consume(self, row, data):
        p = data.get('payload')
        if not isinstance(p, dict):
            return
        typ = data.get('type')
        if typ == 'event_msg' and p.get('type') == 'task_started':
            row['current_turn'] = p.get('turn_id') if isinstance(p.get('turn_id'), str) else ''
            if row.get('origin') == 'peer' and row['current_turn']:
                starts = row.setdefault('peer_turn_starts', [])
                if row['current_turn'] not in starts:
                    if len(starts) >= 128:
                        row['peer_context_overflow'] = True
                    starts[:] = starts[-127:] + [row['current_turn']]
        elif typ == 'turn_context' and isinstance(p.get('turn_id'), str):
            row['current_turn'] = p['turn_id']
        elif typ == 'response_item' and p.get('type') == 'message':
            content = p.get('content', [])
            text = ''.join(c.get('text', '') for c in content if isinstance(c, dict) and isinstance(c.get('text'), str)) if isinstance(content, list) else ''
            # A delayed response_item can belong to an older turn even while a
            # newer task is current. Explicit message metadata is authoritative.
            nested = p.get('internal_chat_message_metadata_passthrough')
            if nested is not None and not isinstance(nested, dict):
                return
            explicit = []
            for source in (p, nested or {}):
                if 'turn_id' in source:
                    value = source['turn_id']
                    if not isinstance(value, str) or not value or len(value) > 128:
                        return
                    explicit.append(value)
            if len(set(explicit)) > 1:
                return
            turn = explicit[0] if explicit else row['current_turn']
            if row.get('origin') == 'peer' and p.get('role') == 'user' and text != outgoing_message(row):
                if turn and turn == row.get('bound_turn'):
                    row['peer_mixed_input'] = True
                foreign = row.setdefault('peer_foreign_turns', [])
                if not turn:
                    row['peer_mixed_input'] = True
                elif turn not in foreign:
                    if len(foreign) >= 128:
                        row['peer_context_overflow'] = True
                    foreign[:] = foreign[-127:] + [turn]
            if p.get('role') == 'user' and text == outgoing_message(row) and turn:
                if row.get('origin') == 'peer':
                    if (turn not in row.get('peer_turn_starts', []) or turn in row.get('peer_foreign_turns', [])
                            or row.get('peer_context_overflow') or row.get('bound_turn') == turn):
                        row['peer_mixed_input'] = True
                if row['bound_turn'] != turn:
                    row['final'], row['final_turn'] = '', ''
                row['bound_turn'] = turn
                row['state'] = 'awaiting_reply'
            elif p.get('role') == 'assistant' and p.get('phase') == 'final_answer' and turn and turn == row['bound_turn']:
                row['final'], row['final_turn'] = text[:16000], turn
        elif typ == 'event_msg' and p.get('type') == 'task_complete' and p.get('turn_id') and p['turn_id'] == row['bound_turn']:
            answer = p.get('last_agent_message') or (row['final'] if row.get('final_turn') == row['bound_turn'] else '')
            marker = 'WATCH_REPLY ' + row['request_id']
            if row.get('origin') == 'peer':
                row['settled'] = True
                markers = [line for line in answer.splitlines() if 'WATCH_REPLY' in line] if isinstance(answer, str) else []
                if markers != [marker]:
                    row.update(state='unknown', reply='', tracking_stopped=True, error='Peer reply has missing or ambiguous correlation markers.')
                    return
                row.update(state='quarantined', complete_offset=row['cursor'], quarantined_at=time.time(),
                           candidate_reply='\n'.join(line for line in answer.splitlines() if line != marker).strip()[:16000], error='')
                return
            if isinstance(answer, str) and marker in answer.splitlines():
                row.update(state='answered', reply='\n'.join(line for line in answer.splitlines() if line != marker).strip()[:16000], error='')

    def _poll(self, row):
        if row.get('transport') == claude_mod.TRANSPORT:
            return
        if row.get('runtime') == 'claude':
            return self._poll_claude(row)
        try:
            st = self._metadata(row['path'], row['agent'][2:])
            if (st.st_ino, st.st_dev) != (row['inode'], row['device']) or st.st_size < row['cursor']:
                row.update(state='unknown', error='The transcript changed. Delivery is unknown; WATCH will not resend.')
                return
            with open(row['path'], 'rb') as f:
                f.seek(row['cursor'])
                data = f.read(MAX_READ)
            lines = data.split(b'\n')
            for raw in lines[:-1]:
                row['cursor'] += len(raw) + 1
                if len(raw) > MAX_LINE:
                    if row.get('origin') == 'peer':
                        row['peer_mixed_input'] = True
                    continue
                try:
                    event = json.loads(raw)
                    if isinstance(event, dict):
                        self._consume(row, event)
                except (ValueError, TypeError):
                    if row.get('origin') == 'peer':
                        row['peer_mixed_input'] = True
                if row['state'] in ('answered', 'quarantined') or row.get('tracking_stopped'):
                    break
            if len(lines[-1]) > MAX_LINE:
                row.update(state='unknown', error='A transcript record exceeded the safe limit; reply tracking stopped.')
                row['tracking_stopped'] = True
        except (OSError, MessagingError):
            row['error'] = 'The transcript is unavailable; WATCH will keep checking for a reply.'
        if row['state'] in ('queued', 'awaiting_reply') and time.time() - row['created'] > TTL:
            row.update(state='expired', error='No matching reply yet. The question may still be queued; WATCH will not resend.')

    def tick(self):
        # A competing owner CLI transaction is transient; never kill the owner worker.
        with self.lock:
            try:
                with self.db:
                    self.mods.maintain()
                rows = self._prune(self._rows(), time.time())
            except sqlite3.OperationalError:
                self.db.rollback()
                return
        for row in rows:
            if row.get('transport') == claude_mod.TRANSPORT:
                continue  # HTTP claims/events exclusively own these rows; no stale tick writeback.
            if self.stop.is_set():
                break
            peer = row.get('origin') == 'peer'
            was_answered = row['state'] == 'answered'
            try:
                if row['state'] == 'sending' and not row['launched']:
                    if peer:
                        if row.get('bridge_version') == 2:
                            self.shared.authorize(row)
                        elif self.shared.enabled:
                            raise peer_bridge.PeerError(409, 'Legacy peer dispatch is disabled in shared mode.')
                        else:
                            self.peers.authorize(row)
                    else:
                        with self.lock:
                            row['launched'] = True
                            self._save(row)
                    row['state'], row['error'] = self._send(row)
                elif row['state'] == 'sending' and row['launched']:
                    # A post-launch persistence failure cannot authorize a second send.
                    row.update(state='unknown', error='Delivery outcome could not be persisted. WATCH will not resend.')
                if row['state'] in ACTIVE and row['state'] != 'sending' and not row.get('tracking_stopped'):
                    self._poll(row)
            except sqlite3.OperationalError:
                with self.lock:
                    self.db.rollback()
                continue
            except peer_bridge.PeerRetry:
                # A partial target tail is temporary; authorization has not committed.
                continue
            except Exception as error:
                if not peer:
                    raise
                reason = 'Peer delivery could not be safely completed. WATCH will not resend.'
                if not row.get('launched'):
                    reason = (str(error) if isinstance(error, peer_bridge.PeerError) else reason) + ' Nothing was sent.'
                row.update(state='unknown' if row.get('launched') else 'failed', reply='', candidate_reply='', tracking_stopped=True,
                           error=reason)
            with self.lock:
                try:
                    # Codex candidates are private until all owner cursors catch up.
                    if not was_answered and row['state'] == 'answered':
                        self.peers.owner_warning(row)
                    self._save(row)
                except sqlite3.OperationalError:
                    self.db.rollback()
        with self.lock:
            try:
                current = self._rows()
                self.peers.reconcile(current)
                for row in current:
                    self._save(row)
            except sqlite3.OperationalError:
                self.db.rollback()

    def _run(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except sqlite3.OperationalError:
                with self.lock:
                    self.db.rollback()
            except Exception:
                # No exception text: it can contain user content or local paths.
                self.worker_failed = True
                self.available, self.reason = False, 'Reply tracking encountered an error. Restart WATCH to recover safely.'
                return
            self.stop.wait(1)
