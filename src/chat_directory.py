"""Bounded, metadata-only discovery of existing native chats.

No import-time I/O, transcript output, transport calls, or server dependency.
Refreshes advance an incremental scan outside the messaging store's lock.
Transport readiness is supplied by the caller and never inferred from log age.
"""
import base64
import copy
import hashlib
import json
import math
import os
import re
import stat
import threading
import time
import uuid

MAX_LINE = 262144
_SKIP_STRING = re.compile(rb'["\\\n]')
MAX_TITLE = 240
MAX_TIMESTAMP = 253402300799
PUBLIC = ('agent_id', 'native_id', 'provider', 'name', 'project_id', 'project_label',
          'last_seen', 'capability', 'reason', 'identity_revision', 'supported_transport')


def validate_policy(policy):
    """Return the normalized, JSON-safe one-service discovery policy."""
    if not isinstance(policy, dict):
        raise ValueError('Invalid directory eligibility policy')
    result = ChatDirectory._policy(policy)
    return {k: list(v) if isinstance(v, tuple) else v for k, v in result.items()}


class DirectoryError(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def _uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _text(value, limit):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit and not any(ord(c) < 32 or ord(c) == 127 for c in value)


def _timestamp(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 < value <= MAX_TIMESTAMP


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _open(root, path):
    """Open a regular file beneath a canonical root without following links."""
    if os.path.commonpath((root, path)) != root or os.path.abspath(path) != path:
        raise OSError('Nonlocal metadata path')
    fd = os.open(os.path.sep, os.O_RDONLY | os.O_DIRECTORY)
    try:
        parts = os.path.abspath(path).split(os.sep)
        for part in parts[1:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        result = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        st = os.fstat(result)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            os.close(result)
            raise OSError('Not a plain metadata file')
        return result, st
    finally:
        os.close(fd)


class _MetadataRecord:
    """Stream a JSONL object's top-level scalar metadata, skipping content values.

    Native messages can contain megabytes of text in one record. Keeping only
    selected scalars avoids retaining or decoding that text, while still finding
    metadata after it. A malformed/oversized metadata value fails closed.
    """
    fields = frozenset(('type', 'cwd', 'sessionId', 'customTitle', 'isSidechain', 'isSubagent'))

    def __init__(self):
        self.depth, self.phase = 0, 'start'
        self.string = self.escape = self.error = False
        self.kind, self.key, self.buffer = None, None, bytearray()
        self.values = {}
        self.active = False

    def _capture(self, byte):
        if self.kind is not None:
            if len(self.buffer) < 16384:
                self.buffer.append(byte)
            else:
                self.error = True

    def _value(self):
        if self.kind == 'value':
            try:
                self.values[self.key] = json.loads(self.buffer)
            except (ValueError, TypeError, UnicodeError):
                self.error = True
        self.kind, self.buffer = None, bytearray()

    def feed(self, raw, consume):
        position = 0
        while position < len(raw):
            if self.string and self.kind is None and not self.escape:
                # Unretained string text cannot change the parser until a quote,
                # escape or record boundary. Find those bytes without a Python
                # iteration for every byte of transcript content.
                match = _SKIP_STRING.search(raw, position)
                if match is None:
                    break
                position = match.start()
            byte = raw[position]
            position += 1
            if byte == 10:
                if self.active:
                    consume(self.values if self.phase == 'closed' and not self.error and not self.string else None)
                self.__init__()
                continue
            if not self.active and byte in (9, 13, 32):
                continue
            self.active = True
            if self.string:
                self._capture(byte)
                if self.escape:
                    self.escape = False
                elif byte == 92:
                    self.escape = True
                elif byte == 34:
                    self.string = False
                    if self.depth == 1 and self.kind == 'key':
                        try:
                            self.key = json.loads(self.buffer)
                        except (ValueError, TypeError, UnicodeError):
                            self.error, self.key = True, None
                        self.kind, self.buffer, self.phase = None, bytearray(), 'colon'
                    elif self.depth == 1 and self.phase == 'value':
                        self._value()
                        self.phase = 'after'
                continue
            if byte in (9, 13, 32):
                if self.depth == 1 and self.phase == 'scalar':
                    self._capture(byte)
                continue
            if self.phase == 'start':
                if byte == 123:
                    self.depth, self.phase = 1, 'key'
                else:
                    self.error = True
                continue
            if self.phase == 'closed':
                self.error = True
                continue
            if self.depth > 1:
                if byte == 34:
                    self.string, self.kind = True, None
                elif byte in (123, 91):
                    self.depth += 1
                elif byte in (125, 93):
                    self.depth -= 1
                continue
            if byte == 34:
                self.string, self.escape = True, False
                if self.phase == 'key':
                    self.kind = 'key'
                elif self.phase == 'value':
                    self.kind = 'value' if self.key in self.fields else None
                else:
                    self.error = True
                    self.kind = None
                self.buffer = bytearray()
                self._capture(byte)
            elif byte == 58 and self.phase == 'colon':
                self.phase = 'value'
            elif byte in (123, 91) and self.phase == 'value':
                if self.key in self.fields:
                    self.error = True
                self.depth += 1
                self.phase = 'after'
            elif byte in (44, 125):
                if self.phase == 'scalar':
                    self._value()
                    self.phase = 'after'
                if byte == 44 and self.phase == 'after':
                    self.phase = 'key'
                elif byte == 125 and self.phase in ('key', 'after'):
                    self.phase, self.depth = 'closed', 0
                else:
                    self.error = True
            elif self.phase == 'value':
                self.phase = 'scalar'
                self.kind = 'value' if self.key in self.fields else None
                self.buffer = bytearray()
                self._capture(byte)
            elif self.phase == 'scalar':
                self._capture(byte)
            else:
                self.error = True


class ChatDirectory:
    def __init__(self, codex_root, claude_root, *, policy=None, live_snapshot=None,
                 codex_sessions=None, codex_index=None, now=time.time,
                 cache_seconds=5, max_files=256, max_bytes=2097152,
                 max_scan_seconds=0.05, max_entries=100000):
        self.codex_root = os.path.realpath(os.fspath(codex_root))
        self.codex_sessions = os.path.realpath(codex_sessions or os.path.join(self.codex_root, 'sessions'))
        self.codex_index = os.path.abspath(codex_index or os.path.join(self.codex_root, 'session_index.jsonl'))
        self.claude_root = os.path.realpath(os.fspath(claude_root))
        self.policy = self._policy(policy or {})
        self.live_snapshot = live_snapshot or (lambda: {})
        self.now, self.cache_seconds = now, cache_seconds
        if (type(max_files) is not int or max_files < 1 or type(max_bytes) is not int or max_bytes < 1
                or max_scan_seconds <= 0 or type(max_entries) is not int or max_entries < 1):
            raise ValueError('Directory scan bounds must be positive')
        self.max_files, self.max_bytes = max_files, max_bytes
        self.max_scan_seconds, self.max_entries = max_scan_seconds, max_entries
        self.lock, self.refresh_lock = threading.RLock(), threading.Lock()
        self.index_lock = threading.Lock()
        self.file_locks = tuple(threading.RLock() for _ in range(64))
        self._index_generation = self._completed_index_generation = 0
        self._pass_dirty = False
        self._files, self._titles, self._entries = {}, {}, {}
        self._index = None
        self._walk = None
        self._walk_errors = set()
        self._seen = set()
        self._pending = None
        self._complete = False
        self.work_pending = True
        self._refreshed = self.now()
        self._last_attempt = None
        self._reasons = ['Metadata discovery is still in progress; retry later without polling.']
        self._published_status = {'complete': False, 'reasons': list(self._reasons), 'refreshed_at': self._refreshed}

    @staticmethod
    def _policy(policy):
        providers = policy.get('providers', ['codex', 'claude'])
        days = policy.get('history_days', 14)
        if (not isinstance(providers, (list, tuple)) or not providers
                or any(p not in ('codex', 'claude') for p in providers) or len(set(providers)) != len(providers)
                or not (days == 'all' or type(days) is int and 1 <= days <= 36500)):
            raise ValueError('Invalid directory eligibility policy')
        result = {'providers': tuple(providers), 'history_days': days}
        for key in ('project_allow', 'project_deny'):
            values = policy.get(key, [])
            if not isinstance(values, (list, tuple)) or len(values) > 1000 or any(not _text(p, 4096) or not os.path.isabs(p) for p in values):
                raise ValueError('Invalid directory project policy')
            result[key] = tuple(os.path.realpath(p) for p in values)
        return result

    @staticmethod
    def _within(project, root):
        try:
            return os.path.commonpath((project, root)) == root
        except ValueError:
            return False

    def _eligible(self, project, last_seen):
        p = self.policy
        return (not p['project_allow'] or any(self._within(project, root) for root in p['project_allow'])) and not any(
            self._within(project, root) for root in p['project_deny']) and (
            p['history_days'] == 'all' or _timestamp(last_seen) and last_seen >= self.now() - p['history_days'] * 86400)

    def _paths(self):
        for provider, root in (('codex', self.codex_sessions), ('claude', self.claude_root)):
            if provider not in self.policy['providers']:
                continue
            stack = [(root, 0)]
            while stack:
                folder, depth = stack.pop()
                try:
                    with os.scandir(folder) as items:
                        for item in items:
                            candidate = None
                            if not item.is_symlink() and not item.name.startswith('.'):
                                if item.is_dir(follow_symlinks=False):
                                    if item.name not in ('subagents', 'archived_sessions', 'archived', 'exec', 'review') and depth < (3 if provider == 'codex' else 1):
                                        stack.append((item.path, depth + 1))
                                elif item.is_file(follow_symlinks=False) and item.name.endswith('.jsonl'):
                                    ident = item.name[:-6] if provider == 'claude' else item.name[-42:-6]
                                    recent = (self.policy['history_days'] == 'all' or item.stat(follow_symlinks=False).st_mtime >=
                                              self.now() - self.policy['history_days'] * 86400)
                                    if recent and _uuid(ident) and (provider != 'claude' or depth == 1):
                                        candidate = provider, item.path, ident
                            # Traversal steps have their own bound, not a double file charge.
                            yield candidate
                except OSError:
                    if os.path.lexists(folder):
                        self._walk_errors.add('Some metadata directories could not be read.')

    def _file_lock(self, path):
        return self.file_locks[hash(path) % len(self.file_locks)]

    def _state(self, root, path, states=None):
        states = self._files if states is None else states
        fd, st = _open(root, path)
        state = states.get(path)
        if state is None and len(states) >= self.max_entries:
            os.close(fd)
            raise OSError('Metadata cache entry limit reached')
        token = (st.st_dev, st.st_ino)
        stamp = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
        if (not state or state['token'] != token or st.st_size < state['offset']
                or (st.st_size == state['offset'] and st.st_mtime_ns != state['mtime'])):
            state = {'token': token, 'offset': 0, 'pending': b'', 'skip': False,
                     'uncertain': False, 'meta': {}, 'invalid': False, 'done': False}
            states[path] = state
        state['changed'] = state.get('stamp') != stamp
        state.update(size=st.st_size, mtime=st.st_mtime_ns, last_seen=st.st_mtime, stamp=stamp)
        return fd, st, state

    @staticmethod
    def _lines(state, raw, consume):
        for chunk in raw.splitlines(keepends=True):
            if state['skip']:
                if chunk.endswith(b'\n'):
                    state['skip'] = False
                continue
            state['pending'] += chunk
            if len(state['pending']) > MAX_LINE:
                state['uncertain'] = True
                state['pending'] = b''
                state['skip'] = not chunk.endswith(b'\n')
                continue
            if chunk.endswith(b'\n'):
                line, state['pending'] = state['pending'], b''
                consume(line)

    def _read_index(self, budget):
        with self.index_lock:
            return self._read_index_locked(budget)

    def _read_index_locked(self, budget):
        self._index_changed = False
        if 'codex' not in self.policy['providers']:
            return budget, True
        try:
            fd, st = _open(self.codex_root, self.codex_index)
        except FileNotFoundError:
            self._index_changed = self._index is not None
            self._index_generation += int(self._index_changed)
            self._titles = {}
            self._index = None
            return budget, True
        except OSError:
            self._scan_errors.add('Codex title index could not be read.')
            return budget, False
        try:
            token = (st.st_dev, st.st_ino)
            s = self._index
            self._index_changed = not s or s['token'] != token or s['observed_size'] != st.st_size or s['mtime'] != st.st_mtime_ns
            self._index_generation += int(self._index_changed)
            if (not s or s['token'] != token or st.st_size < s['observed_size']
                    or st.st_mtime_ns != s['mtime'] and st.st_size <= s['observed_size']):
                s = self._index = {'token': token, 'offset': 0, 'pending': b'', 'skip': False, 'uncertain': False}
                self._titles = {}
            s['mtime'] = st.st_mtime_ns
            s['observed_size'] = st.st_size
            os.lseek(fd, s['offset'], os.SEEK_SET)
            raw = os.read(fd, min(budget, max(0, st.st_size - s['offset'])))
            s['offset'] += len(raw)
            def consume(line):
                try:
                    d = json.loads(line)
                    if isinstance(d, dict) and _uuid(d.get('id')):
                        title = d.get('thread_name')
                        if _text(title, MAX_TITLE):
                            if d['id'] in self._titles or len(self._titles) < self.max_entries:
                                self._titles[d['id']] = title.strip()
                            else:
                                s['uncertain'] = True
                        else:
                            self._titles.pop(d['id'], None)
                except (ValueError, TypeError):
                    s['uncertain'] = True
            self._lines(s, raw, consume)
            done = s['offset'] >= st.st_size and not s['pending'] and not s['skip'] and not s['uncertain']
            return budget - len(raw), done
        finally:
            os.close(fd)

    def _read_chat(self, provider, path, ident, budget):
        with self._file_lock(path):
            return self._read_chat_locked(provider, path, ident, budget)

    def _read_chat_locked(self, provider, path, ident, budget, states=None, force_head=False):
        states = self._files if states is None else states
        root = self.codex_sessions if provider == 'codex' else self.claude_root
        try:
            fd, st, s = self._state(root, path, states)
        except OSError:
            states.pop(path, None)
            if os.path.lexists(path):
                self._walk_errors.add('Some chat metadata files could not be read safely.')
            return budget, True
        try:
            if provider == 'codex':
                # Native identity is exclusively the first record, never a later transcript assertion.
                if s['done'] and not s.get('partial') and not s['changed'] and not force_head:
                    return budget, True
                if s['done'] and not s.get('partial'):
                    s['head'] = b''
                s['uncertain'] = False
                head = s.get('head', b'')
                os.lseek(fd, len(head), os.SEEK_SET)
                with os.fdopen(os.dup(fd), 'rb') as source:
                    part = source.readline(min(MAX_LINE + 1 - len(head), budget))
                raw = head + part
                budget -= len(part)
                if not raw.endswith(b'\n') and len(raw) <= MAX_LINE:
                    at_eof = len(raw) >= st.st_size
                    s.update(head=raw, offset=len(raw), done=at_eof, partial=at_eof, invalid=False)
                    return budget, at_eof
                s['partial'] = False
                try:
                    d = json.loads(raw)
                    meta = d.get('payload') if isinstance(d, dict) else None
                    source = meta.get('source') if isinstance(meta, dict) else None
                    valid = (raw.endswith(b'\n') and len(raw) <= MAX_LINE and d.get('type') == 'session_meta'
                             and isinstance(meta, dict) and meta.get('id') == ident
                             and meta.get('thread_source') != 'guardian_review' and meta.get('originator') != 'codex_exec'
                             and not isinstance(source, dict) and source not in ('exec', 'review', 'subagent')
                             and not meta.get('parent_thread_id') and meta.get('agent_path', '') in ('', '/root')
                             and not meta.get('is_subagent') and not meta.get('is_archived'))
                    s['invalid'] = not valid
                    if not raw.endswith(b'\n') or len(raw) > MAX_LINE:
                        s['uncertain'] = True
                    s['meta'] = {'project': meta.get('cwd')} if valid else {}
                except (ValueError, TypeError, AttributeError):
                    s['invalid'] = True
                    s['uncertain'] = True
                s['offset'], s['done'] = st.st_size, True
                s.pop('head', None)
                return budget, True
            os.lseek(fd, s['offset'], os.SEEK_SET)
            raw = os.read(fd, min(budget, 524288, max(0, st.st_size - s['offset'])))
            s['offset'] += len(raw)
            def consume(d):
                if d is None:
                    s['uncertain'] = True
                    return
                if d.get('isSidechain') or d.get('isSubagent'):
                    s['invalid'] = True
                if d.get('sessionId') is not None and d['sessionId'] != ident:
                    s['invalid'] = True
                if d.get('type') == 'custom-title':
                    title = d.get('customTitle')
                    s['meta']['name'] = title.strip() if _text(title, MAX_TITLE) else None
                if isinstance(d.get('cwd'), str) and 'project' not in s['meta']:
                    s['meta']['project'] = d['cwd']
            parser = s.setdefault('parser', _MetadataRecord())
            parser.feed(raw, consume)
            s['done'] = s['offset'] >= st.st_size
            s['partial'] = s['done'] and parser.active
            return budget - len(raw), s['done']
        finally:
            os.close(fd)

    def _live(self):
        try:
            live = self.live_snapshot()
            return live if isinstance(live, dict) else {}
        except Exception:
            return {}

    def _entry(self, provider, path, ident, s, live, fresh_project=False):
        meta = s['meta']
        title = self._titles.get(ident) if provider == 'codex' else meta.get('name')
        project = meta.get('project')
        if (s['invalid'] or not _text(title, MAX_TITLE) or not _text(project, 4096) or not os.path.isabs(project)):
            return None
        project_cache = s.get('project_cache')
        if fresh_project or not project_cache or project_cache[0] != project:
            project_cache = s['project_cache'] = (project, os.path.realpath(project))
        project = project_cache[1]
        if not self._eligible(project, s['last_seen']):
            return None
        aid = ('x:' if provider == 'codex' else 'c:') + ident
        fallback = '__codex_transport__' if provider == 'codex' else '__claude_transport__'
        facts = live.get(aid, live.get(fallback, {}))
        facts = facts if isinstance(facts, dict) else {}
        capability = facts.get('capability')
        if capability not in ('reachable', 'unverified', 'unavailable'):
            capability = 'unverified' if provider == 'codex' else 'unavailable'
        reason = facts.get('reason')
        if not _text(reason, 300) and not (capability == 'reachable' and reason == ''):
            reason = ('Native transport has not been verified for this chat.' if provider == 'codex'
                      else 'No compatible live Claude channel is connected.')
        if s['uncertain']:
            capability, reason = 'unavailable', 'Chat contains unverifiable metadata; identity and exact name cannot be confirmed.'
        elif not s['done'] or s['pending'] or s['skip'] or s.get('partial'):
            capability, reason = 'unavailable', 'Chat metadata scan is incomplete; identity is not verified.'
        if provider == 'codex' and self._index and self._index['uncertain']:
            capability, reason = 'unavailable', 'Codex title metadata is unverifiable; exact name and revision cannot be confirmed.'
        instance = facts.get('instance_id') if provider == 'claude' else None
        if provider == 'claude' and capability == 'reachable' and not _uuid(instance):
            capability, reason = 'unavailable', 'Claude channel instance identity is not verified.'
        if provider == 'claude' and capability == 'reachable' and facts.get('project') != project:
            capability, reason = 'unavailable', 'Claude channel project does not match the selected chat.'
        identity = {'path': path, 'device': s['token'][0], 'inode': s['token'][1]}
        if _uuid(instance):
            identity['instance_id'] = instance
        base_key = (provider, ident, project, title, s['token'], instance, path)
        base_cache = s.get('base_cache')
        if not base_cache or base_cache[0] != base_key:
            base = {'agent_id': aid, 'native_id': ident, 'provider': provider, 'name': title,
                         'project_id': _hash(['watch-project-v1', project]), 'project_label': os.path.basename(project) or project,
                         'project': project, 'identity': identity,
                         'supported_transport': 'codex_queue' if provider == 'codex' else 'claude_channel',
                         'identity_revision': _hash([provider, ident, project, title, identity])}
            base_cache = s['base_cache'] = (base_key, base)
        result = dict(base_cache[1], capability=capability, reason=reason)
        if _timestamp(s['last_seen']):
            result['last_seen'] = s['last_seen']
        return result

    def _publish(self, live):
        entries, duplicates = {}, set()
        for number, (path, s) in enumerate(self._files.items()):
            if number and number % 128 == 0:
                # Let pinned validation complete its short filesystem syscalls
                # even while a large first publish fills its metadata caches.
                time.sleep(0.001)
            provider, ident = s.get('provider'), s.get('ident')
            if not provider:
                continue
            entry = self._entry(provider, path, ident, s, live)
            if entry:
                if entry['agent_id'] in entries:
                    duplicates.add(entry['agent_id'])
                else:
                    entries[entry['agent_id']] = entry
        for aid in duplicates:
            entries[aid]['capability'] = 'unavailable'
            entries[aid]['reason'] = 'Multiple local metadata files claim this native chat identity.'
        labels = {}
        for e in entries.values():
            labels.setdefault(e['project_label'], set()).add(e['project_id'])
        for e in entries.values():
            if len(labels[e['project_label']]) > 1:
                e['project_label'] += ' [' + e['project_id'][:8] + ']'
        with self.lock:
            self._entries = entries
            self._published_status = {'complete': self._complete, 'reasons': list(self._reasons), 'refreshed_at': self._refreshed}

    @staticmethod
    def _fingerprint(state):
        if state is None:
            return None
        return (state['token'], tuple(sorted(state['meta'].items())), state['invalid'],
                state['uncertain'], state.get('partial', False))

    def refresh(self, force=False):
        """Advance at most one bounded batch. Repeated incomplete calls make progress."""
        with self.refresh_lock:
            now = self.now()
            if not force and not self.work_pending and self._last_attempt is not None and now - self._last_attempt < self.cache_seconds:
                return self.status()
            self._last_attempt = now
            self._scan_errors = set()
            deadline, budget = time.monotonic() + self.max_scan_seconds, self.max_bytes
            index_budget = max(1, budget // 2)
            index_left, index_done = self._read_index(index_budget)
            budget -= index_budget - index_left
            if self._walk is None:
                self._walk, self._seen = self._paths(), set()
                self._walk_errors = set()
                self._walk_count = 0
                self._pass_dirty = False
                self._pass_index_generation = self._index_generation
            self._pass_dirty |= self._index_generation != self._completed_index_generation
            finished = False
            visited = set()
            while len(visited) < self.max_files or self._pending is not None and self._pending[1] in visited:
                if budget <= 0 or time.monotonic() >= deadline:
                    break
                if self._pending is None:
                    try:
                        candidate = next(self._walk)
                        self._walk_count += 1
                    except StopIteration:
                        finished = True
                        self._walk = None
                        break
                    if self._walk_count > self.max_entries * 4:
                        self._scan_errors.add('Directory traversal limit reached; narrow the eligibility policy.')
                        self._walk.close()
                        self._walk = None
                        break
                    if candidate is None:
                        continue
                    self._pending = candidate
                provider, path, ident = self._pending
                self._seen.add(path)
                if len(self._seen) > self.max_entries:
                    self._scan_errors.add('Directory entry limit reached; narrow the eligibility policy.')
                    self._walk.close()
                    self._walk = None
                    self._pending = None
                    break
                before = self._files.get(path)
                fingerprint = self._fingerprint(before)
                visited.add(path)
                budget, done = self._read_chat(provider, path, ident, budget)
                after = self._files.get(path)
                self._pass_dirty |= fingerprint != self._fingerprint(after) or not done
                if path in self._files:
                    self._files[path].update(provider=provider, ident=ident)
                if done:
                    self._pending = None
                # A pending file may consume the remaining batch budget. The
                # next iteration checks both the byte and monotonic deadlines.
            confirmation_pending = False
            if finished:
                # A native title may have arrived after its folder was visited,
                # including while the iterator was finishing this very batch.
                # Check the index stamp without spending another parse budget.
                _, final_index_done = self._read_index(0)
                index_done = index_done and final_index_done
                confirmation_pending = self._index_generation != self._pass_index_generation
                self._pass_dirty |= any(p not in self._seen for p in self._files)
                self._files = {p: s for p, s in self._files.items() if p in self._seen}
            metadata_complete = all(s['done'] and not s['uncertain'] and not s['pending'] and not s['skip'] and not s.get('partial')
                                    for s in self._files.values())
            reasons = sorted(self._scan_errors)
            reasons.extend(sorted(self._walk_errors))
            if not index_done:
                if self._index and self._index['uncertain']:
                    reasons.append('1 Codex title index contains unverifiable records; exact names and revisions cannot be confirmed.')
                else:
                    reasons.append('Codex title metadata is still incomplete; retry later without polling.')
            if not finished and (not self._complete or self._pass_dirty):
                reasons.append('Metadata discovery is still in progress; retry later without polling.')
            unverifiable = sum(bool(s['uncertain']) for s in self._files.values())
            if unverifiable:
                # A later valid title cannot establish what an unreadable record
                # said about another title, native identity or initial project.
                reasons.append(str(unverifiable) + ' chat metadata file(s) contain unverifiable records or exceed safe parsing bounds; name uniqueness cannot be confirmed.')
            elif not metadata_complete:
                reasons.append('Some chat metadata is still incomplete; retry later without polling.')
            if confirmation_pending:
                reasons.append('Chat titles changed during discovery; a confirmation pass is pending. Retry later without polling.')
            safe = index_done and metadata_complete and not self._scan_errors and not self._walk_errors
            self._complete = safe and not confirmation_pending and (finished or self._complete and not self._pass_dirty)
            if self._complete:
                reasons = []
                if finished:
                    self._completed_index_generation = self._index_generation
            self.work_pending = (self._walk is not None or self._pending is not None or
                                 bool(self._index and self._index['offset'] < self._index['observed_size']) or confirmation_pending)
            self._reasons, self._refreshed = reasons, now
            self._publish(self._live())
            return self.status()

    def status(self):
        with self.lock:
            return copy.deepcopy(self._published_status)

    def list_agents(self, query=None, provider=None, project_id=None, cursor=None, limit=50):
        if (query is not None and (not isinstance(query, str) or len(query) > MAX_TITLE)
                or provider is not None and provider not in ('codex', 'claude')
                or project_id is not None and (not isinstance(project_id, str) or len(project_id) != 64)
                or type(limit) is not int or not 1 <= limit <= 50):
            raise DirectoryError(400, 'Invalid directory filter or page size.')
        self.refresh()
        with self.lock:
            snapshot = list(self._entries.values())
            directory_status = copy.deepcopy(self._published_status)
        entries = [e for e in snapshot if (not query or query.casefold() in e['name'].casefold())
                   and (not provider or e['provider'] == provider) and (not project_id or e['project_id'] == project_id)]
        entries.sort(key=lambda e: (e['name'].casefold(), e['provider'], e['project_id'], e['agent_id']))
        signature = _hash([query, provider, project_id, [(e['agent_id'], e['identity_revision']) for e in entries]])
        offset = 0
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) > 256:
                    raise ValueError()
                d = json.loads(base64.urlsafe_b64decode(cursor.encode() + b'=' * (-len(cursor) % 4)))
                if not isinstance(d, list) or len(d) != 2 or d[0] != signature or type(d[1]) is not int or not 0 <= d[1] <= len(entries):
                    raise ValueError()
                offset = d[1]
            except (ValueError, TypeError, UnicodeError):
                raise DirectoryError(409, 'Directory page changed; restart discovery.') from None
        page, wire_size = [], 0
        for e in entries[offset:offset + limit]:
            public = {k: copy.deepcopy(e[k]) for k in PUBLIC if k in e}
            # Reserve room for status/cursor and the outer RPC wrapper. ASCII escaping
            # also bounds the server's default json.dumps wire representation.
            item_size = len(json.dumps(public).encode()) + 2
            if page and wire_size + item_size > 46000:
                break
            page.append(public)
            wire_size += item_size
        end = offset + len(page)
        next_cursor = base64.urlsafe_b64encode(json.dumps([signature, end], separators=(',', ':')).encode()).decode().rstrip('=') if end < len(entries) else None
        return {'agents': page, 'next_cursor': next_cursor, 'directory': directory_status}

    def resolve(self, agent_id, identity_revision, require_reachable=True):
        """Freshly validate one pinned path; never enumerate directories here."""
        if (not isinstance(agent_id, str) or agent_id[:2] not in ('x:', 'c:') or not _uuid(agent_id[2:])
                or not isinstance(identity_revision, str) or len(identity_revision) != 64):
            raise DirectoryError(400, 'Invalid selected chat identity.')
        with self.lock:
            old = copy.deepcopy(self._entries.get(agent_id))
        if not old or old['identity_revision'] != identity_revision:
            raise DirectoryError(409, 'Selected chat identity changed; discover and select it again.')
        _, index_done = self._read_index(self.max_bytes)
        path = old['identity']['path']
        with self._file_lock(path):
            # Local validation cannot wait behind a whole-directory publish, nor
            # mutate the scanner's parser/offset snapshot midway through a record.
            local = {path: copy.deepcopy(self._files[path])} if path in self._files else {}
            _, done = self._read_chat_locked(old['provider'], path, old['native_id'], self.max_bytes, local, True)
            state = local.get(path)
            fresh = self._entry(old['provider'], path, old['native_id'], state, self._live(), fresh_project=True) if state else None
            if not index_done or not done or not fresh or fresh['identity_revision'] != identity_revision:
                with self.lock:
                    self._complete = False
                    self._reasons = ['Selected chat metadata changed; refresh discovery before selecting it again.']
                    self._last_attempt = None
                    self._published_status = {'complete': False, 'reasons': list(self._reasons), 'refreshed_at': self._refreshed}
                if path in self._files:
                    self._files[path].pop('project_cache', None)
                raise DirectoryError(409, 'Selected chat identity changed or is not verified; discover and select it again.')
            if old['reason'].startswith('Multiple local metadata'):
                raise DirectoryError(409, old['reason'])
            if fresh['project_id'] == old['project_id']:
                fresh['project_label'] = old['project_label']
            if require_reachable and fresh['capability'] != 'reachable':
                raise DirectoryError(409, 'Selected chat is ' + fresh['capability'] + ': ' + fresh['reason'])
            return copy.deepcopy(fresh)

    def revalidate(self, target):
        return self.resolve(target['agent_id'], target['identity_revision'])


def refresh_delay(elapsed, work_pending):
    """Bound background scan duty near half a core; uncertain idle scans back off."""
    return max(0.01, elapsed) if work_pending else 1.0
