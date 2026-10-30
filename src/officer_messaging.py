"""Explicit screen-authorized Codex questions. Importing this module does no I/O.

Queue acceptance is not delivery. Only the exact outgoing user envelope followed
by a matching completed turn can produce an answer. Uncertain sends are never retried.
"""
import glob
import json
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

CLI = '/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex'
ACTIVE = ('sending', 'queued', 'awaiting_reply', 'unknown', 'expired')
MAX_RECORDS, MAX_PENDING, MAX_LINE, MAX_READ = 256, 8, 1_000_000, 2_000_000
TTL, RETENTION = 900, 86400


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
        raise MessagingError(400, 'Messaging supports local Codex chats and channel-connected Claude sessions only.')
    a = agents.get(agent)
    if not a:
        raise MessagingError(404, 'That agent is no longer in the room.')
    expected = 'Claude' if agent.startswith('c:') else 'Codex'
    if a.get('runtime') != expected or a.get('observed') or (expected == 'Claude' and a.get('sub')):
        raise MessagingError(400, 'Only local Codex chats and parent Claude sessions can receive messages. ' + claude_channel.START_HINT)
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
    return {k: row.get(k, '') for k in ('request_id', 'agent', 'name', 'question', 'state', 'reply', 'error')}


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
            self.db = sqlite3.connect(path, check_same_thread=False)
            self.db.execute('CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS asks (id TEXT PRIMARY KEY, user_text TEXT NOT NULL, created REAL NOT NULL, decision TEXT)')
            for row in self._rows():
                if row['state'] == 'sending':
                    row.update(state='unknown', error='WATCH restarted during sending. Delivery is unknown; it will not resend.')
                    self._save(row)
            self.db.commit()
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
        if not valid_uuid(request_id) or not isinstance(decision, dict) or decision.get('kind') not in ('relay', 'none', 'clarify'):
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

    def _rows(self):
        return [json.loads(x[0]) for x in self.db.execute('SELECT data FROM requests')]

    def _save(self, row):
        self.db.execute('INSERT OR REPLACE INTO requests VALUES (?,?)', (row['request_id'], json.dumps(row)))
        self.db.commit()

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
        self.thread = threading.Thread(target=self._run, daemon=True, name='watch-agent-messages')
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(15)
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
        with open(path, 'rb') as f:
            raw = f.readline(MAX_LINE + 1)
            st = os.fstat(f.fileno())
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
        matches = []
        for n, path in enumerate(glob.iglob(os.path.join(self.sessions_dir, '**', '*.jsonl'), recursive=True)):
            if n >= 20000:
                raise MessagingError(503, 'Too many session logs to safely resolve this chat.')
            if path.endswith(ident + '.jsonl'):
                matches.append(path)
        if len(matches) != 1:
            raise MessagingError(409, 'The chat must have exactly one local transcript before sending.')
        path = matches[0]
        return path, self._metadata(path, ident)

    def snapshot(self):
        claude_ready = claude_channel.any_configured(self.channel_root) and not self.worker_failed
        codex_ready = (self.available if self.codex_available is None else self.codex_available) and not self.worker_failed
        with self.lock:
            return {'requests': [public(r) for r in sorted(self._rows(), key=lambda x: x['created'])],
                    'available': codex_ready or claude_ready, 'reason': '' if codex_ready or claude_ready else self.reason,
                    'capabilities': {'codex': codex_ready, 'claude': claude_ready}}

    def submit(self, agent, question, request_id, agents):
        validate_question(question)
        if not valid_uuid(request_id):
            raise MessagingError(400, 'A valid request identifier is required.')
        with self.lock:
            rows = self._rows()
            old = next((r for r in rows if r['request_id'] == request_id), None)
            if old:
                if old['agent'] != agent or old['question'] != question:
                    raise MessagingError(409, 'That request identifier belongs to a different question.')
                return public(old)
            name = validate_target(agent, agents)
            if self.worker_failed or (agent.startswith('x:') and not (self.available if self.codex_available is None else self.codex_available)):
                raise MessagingError(503, self.reason)
            now = time.time()
            rows = self._prune(rows, now)
            active = [r for r in rows if r['state'] in ACTIVE]
            if any(r['agent'] == agent for r in active):
                raise MessagingError(409, 'A question for this agent is still unresolved. WATCH will not risk sending it twice.')
            if len(active) >= MAX_PENDING or len(rows) >= MAX_RECORDS or sum(r['created'] > now - 60 for r in rows) >= 10:
                raise MessagingError(429, 'The messaging queue is full. Try again later.')
            row = dict(request_id=request_id, agent=agent, name=name, question=question, state='sending', reply='', error='',
                       created=now, launched=False)
            if agent.startswith('c:'):
                try:
                    descriptor = claude_channel.discover(self.channel_root, agent[2:])
                except claude_channel.ChannelError as error:
                    raise MessagingError(503, str(error)) from None
                row.update(runtime='claude', channel=claude_channel.identity(descriptor),
                           envelope=claude_channel.envelope(request_id, question))
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
            return public(row)

    def _prune(self, rows, now):
        for r in rows:
            if now - r['created'] > RETENTION:
                self.db.execute('DELETE FROM requests WHERE id=?', (r['request_id'],))
        self.db.commit()
        return [r for r in rows if now - r['created'] <= RETENTION]

    def _send(self, row):
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
        elif record['state'] == 'unknown':
            row.update(state='unknown', error='Claude could not confirm delivery. WATCH will not resend.')
        elif row['state'] not in ('expired', 'unknown'):
            row.update(state='queued', error='')

    def _send_claude(self, row):
        try:
            descriptor = claude_channel.pinned(self.channel_root, row['channel'])
        except claude_channel.ChannelError:
            return 'failed', 'The Claude channel disappeared or changed before sending; nothing was sent. ' + claude_channel.START_HINT
        try:
            record = claude_channel.exchange(descriptor, row['request_id'], row['envelope'])
            self._apply_claude(row, record)
            return row['state'], row['error']
        except claude_channel.ChannelError:
            return 'unknown', 'Claude delivery could not be confirmed after sending. WATCH will not resend.'

    def _poll_claude(self, row):
        try:
            descriptor = claude_channel.pinned(self.channel_root, row['channel'])
            record = claude_channel.exchange(descriptor, row['request_id'])
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
            if p.get('role') == 'user' and text == outgoing_message(row) and turn:
                if row['bound_turn'] != turn:
                    row['final'], row['final_turn'] = '', ''
                row['bound_turn'] = turn
                row['state'] = 'awaiting_reply'
            elif p.get('role') == 'assistant' and p.get('phase') == 'final_answer' and turn and turn == row['bound_turn']:
                row['final'], row['final_turn'] = text[:16000], turn
        elif typ == 'event_msg' and p.get('type') == 'task_complete' and p.get('turn_id') and p['turn_id'] == row['bound_turn']:
            answer = p.get('last_agent_message') or (row['final'] if row.get('final_turn') == row['bound_turn'] else '')
            marker = 'WATCH_REPLY ' + row['request_id']
            if isinstance(answer, str) and marker in answer.splitlines():
                row.update(state='answered', reply='\n'.join(line for line in answer.splitlines() if line != marker).strip()[:16000], error='')

    def _poll(self, row):
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
                    continue
                try:
                    event = json.loads(raw)
                    if isinstance(event, dict):
                        self._consume(row, event)
                except (ValueError, TypeError):
                    pass
                if row['state'] == 'answered':
                    break
            if len(lines[-1]) > MAX_LINE:
                row.update(state='unknown', error='A transcript record exceeded the safe limit; reply tracking stopped.')
                row['tracking_stopped'] = True
        except (OSError, MessagingError):
            row['error'] = 'The transcript is unavailable; WATCH will keep checking for a reply.'
        if row['state'] in ('queued', 'awaiting_reply') and time.time() - row['created'] > TTL:
            row.update(state='expired', error='No matching reply yet. The question may still be queued; WATCH will not resend.')

    def tick(self):
        with self.lock:
            rows = self._prune(self._rows(), time.time())
        for row in rows:
            if self.stop.is_set():
                break
            if row['state'] == 'sending' and not row['launched']:
                with self.lock:
                    row['launched'] = True
                    self._save(row)
                row['state'], row['error'] = self._send(row)
            if row['state'] in ACTIVE and row['state'] != 'sending' and not row.get('tracking_stopped'):
                self._poll(row)
            with self.lock:
                self._save(row)

    def _run(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:
                # No exception text: it can contain user content or local paths.
                self.worker_failed = True
                self.available, self.reason = False, 'Reply tracking encountered an error. Restart WATCH to recover safely.'
                return
            self.stop.wait(1)
