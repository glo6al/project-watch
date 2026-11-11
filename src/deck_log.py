"""Opt-in local evidence archive. No native-log reading, network, or import-time I/O.

Only projected prose and structural facts cross this boundary. Defensive redaction
cannot identify every secret in arbitrary prose. Source logs and OS backups are
outside this archive's deletion boundary.
"""
import collections
import ctypes
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import stat
import threading
import time

DEFAULTS = dict(enabled=False, paused=False, retention_days=30,
                excluded_chats=[], excluded_projects=[])
LIMITS = dict(queue=256, events=50000, records=100000, bytes=64 * 1024 * 1024,
              batch_bytes=32768, projections=32, prose_bytes=2048, fallback_rows=1000)
HEX = re.compile(r'[0-9a-f]{64}\Z')
CHAT = re.compile(r'[xck]:[A-Za-z0-9_.:-]{1,196}\Z')
KINDS = frozenset(('message', 'user', 'say', 'operation', 'read', 'write', 'edit',
                   'patch', 'run', 'search', 'web', 'tool', 'result', 'turn_start',
                   'turn_end', 'compact', 'queued', 'dequeued', 'waiting', 'ask',
                   'task', 'task_upd', 'spawn', 'assign', 'see'))
OPERATIONS = frozenset(('', 'read', 'write', 'edit', 'patch', 'run', 'search', 'web',
                        'tool', 'spawn', 'task', 'delete', 'check', 'compact', 'see'))
CANDIDATE_KEYS = frozenset(('kind', 'role', 'text', 'operation', 'operation_id',
                           'phase', 'outcome', 'file_ref', 'chat_name', 'project_name', 'source', 'certainty'))
SECRET = re.compile(r'(?i)(?:\b(?:api[_-]?key|password|passwd|secret|access[_-]?token|authorization)[\"\']?\s*[:=]|\bBearer\s+\S+|sk-(?:ant-)?[\w-]{12,}|gh[pousr]_[\w]{20,}|AKIA[A-Z0-9]{16}|-----BEGIN [^-]*PRIVATE KEY-----)')
MACHINE = re.compile(r'<(?:task-notification|system-reminder|environment_context|tool_result|svg|html|script)\b', re.I)
LOCAL_CAPABILITY = re.compile(r'(?i)(?:https?://(?:localhost|127(?:\.\d{1,3}){3}|\[::1\])(?::\d+)?(?:[^\s<>]*)|(?<![\w:/])/?fs/[^\s<>]+)')



def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _text(value, maximum=120):
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 for c in value):
        return ''
    try:
        value.encode('utf-8')
    except UnicodeError:
        return ''
    if SECRET.search(value) or MACHINE.search(value) or LOCAL_CAPABILITY.search(value):
        return ''
    return value.strip()


def _prose(value, maximum):
    if not isinstance(value, str) or len(value.encode('utf-8', 'replace')) > 16384:
        return '[content omitted]', True
    if SECRET.search(value) or MACHINE.search(value) or value.lstrip().startswith(('{', '[')):
        return '[content omitted]', True
    clean = LOCAL_CAPABILITY.sub('[local link omitted]', value)
    clean = re.sub(r'```[\s\S]*?(?:```|\Z)|~~~[\s\S]*?(?:~~~|\Z)', '[code omitted]', clean)
    clean = re.sub(r'`[^`\n]*`', '[code omitted]', clean)
    clean = re.sub(r'https?://[^\s/@]+(?::[^\s/@]*)?@\S+', '[credentialed URL omitted]', clean, flags=re.I)
    clean = re.sub(r'(?m)^(?:diff --git|@@|\+\+\+|--- |[+-]).*$', '[diff omitted]', clean)
    clean = re.sub(r'(?<!\w)(?:[A-Za-z]:[\\/]|(?<![\w:/])/(?!/)|\\\\)[^\s<>]*', '[path omitted]', clean)
    clean = ''.join(c for c in clean if ord(c) >= 32 or c in '\n\t').strip()
    clean = clean.encode('utf-8', 'replace').decode('utf-8')
    encoded = clean.encode('utf-8')
    if len(encoded) > maximum:
        clean = encoded[:maximum].decode('utf-8', 'ignore')
    return clean, clean != value


def _relative(value):
    if not isinstance(value, str) or not value or len(value) > 300 or SECRET.search(value) or LOCAL_CAPABILITY.search(value):
        return ''
    try:
        value.encode('utf-8')
    except UnicodeError:
        return ''
    value = value.replace('\\', '/')
    if value.startswith(('/', '~')) or ':' in value or any(x in ('', '.', '..') for x in value.split('/')) or any(ord(c) < 32 for c in value):
        return ''
    return value


def _windows_security(path, directory=False, create=False):
    """Protected current-user/SYSTEM DACL, checked with actual Win32 APIs."""
    from ctypes import wintypes as w
    adv = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    adv.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    adv.GetTokenInformation.argtypes = [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD)]
    adv.ConvertSidToStringSidW.argtypes = [w.LPVOID, ctypes.POINTER(w.LPWSTR)]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [w.LPCWSTR, w.DWORD, ctypes.POINTER(w.LPVOID), ctypes.POINTER(w.DWORD)]
    adv.GetNamedSecurityInfoW.argtypes = [w.LPWSTR, ctypes.c_int, w.DWORD, ctypes.POINTER(w.LPVOID), ctypes.POINTER(w.LPVOID), ctypes.POINTER(w.LPVOID), ctypes.POINTER(w.LPVOID), ctypes.POINTER(w.LPVOID)]
    adv.GetSecurityDescriptorControl.argtypes = [w.LPVOID, ctypes.POINTER(w.WORD), ctypes.POINTER(w.DWORD)]
    adv.GetAclInformation.argtypes = [w.LPVOID, w.LPVOID, w.DWORD, ctypes.c_int]
    adv.GetAce.argtypes = [w.LPVOID, w.DWORD, ctypes.POINTER(w.LPVOID)]
    adv.EqualSid.argtypes = [w.LPVOID, w.LPVOID]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CloseHandle.restype = w.BOOL
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.LocalFree.argtypes = [w.LPVOID]
    token, needed, sid_text = w.HANDLE(), w.DWORD(), w.LPWSTR()
    if not adv.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise OSError('private storage unavailable')
    try:
        adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        buf = ctypes.create_string_buffer(needed.value)
        if not adv.GetTokenInformation(token, 1, buf, needed, ctypes.byref(needed)):
            raise OSError('private storage unavailable')
        user_sid = ctypes.cast(buf, ctypes.POINTER(w.LPVOID))[0]
        if not adv.ConvertSidToStringSidW(user_sid, ctypes.byref(sid_text)):
            raise OSError('private storage unavailable')
        try:
            sddl = 'O:%sD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;%s)' % (sid_text.value, sid_text.value)
        finally:
            kernel.LocalFree(sid_text)
        if create:
            descriptor = w.LPVOID()
            if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
                raise OSError('private storage unavailable')
            class SA(ctypes.Structure):
                _fields_ = [('size', w.DWORD), ('descriptor', w.LPVOID), ('inherit', w.BOOL)]
            sa = SA(ctypes.sizeof(SA), descriptor, False)
            try:
                if directory:
                    kernel.CreateDirectoryW.argtypes = [w.LPCWSTR, ctypes.POINTER(SA)]
                    if not kernel.CreateDirectoryW(path, ctypes.byref(sa)):
                        raise OSError('private storage unavailable')
                else:
                    kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.POINTER(SA), w.DWORD, w.DWORD, w.HANDLE]
                    kernel.CreateFileW.restype = w.HANDLE
                    handle = kernel.CreateFileW(path, 0xC0000000, 0, ctypes.byref(sa), 1, 0x80, None)
                    if handle == w.HANDLE(-1).value:
                        raise OSError('private storage unavailable')
                    kernel.CloseHandle(handle)
            finally:
                kernel.LocalFree(descriptor)
        owner, dacl, sd = w.LPVOID(), w.LPVOID(), w.LPVOID()
        if adv.GetNamedSecurityInfoW(path, 1, 5, ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(sd)):
            raise OSError('private storage unavailable')
        try:
            control, revision = w.WORD(), w.DWORD()
            if not adv.EqualSid(owner, user_sid):
                raise OSError('nonprivate storage owner')
            if not dacl or not adv.GetSecurityDescriptorControl(sd, ctypes.byref(control), ctypes.byref(revision)) or (not directory and not control.value & 0x1000):
                raise OSError('private storage unavailable')
            class ACLINFO(ctypes.Structure):
                _fields_ = [('count', w.DWORD), ('used', w.DWORD), ('free', w.DWORD)]
            info = ACLINFO()
            if not adv.GetAclInformation(dacl, ctypes.byref(info), ctypes.sizeof(info), 2):
                raise OSError('private storage unavailable')
            for i in range(info.count):
                ace = w.LPVOID()
                if not adv.GetAce(dacl, i, ctypes.byref(ace)):
                    raise OSError('private storage unavailable')
                data = ctypes.string_at(ace, 8)
                if data[0] != 0:  # only plain access-allowed ACEs
                    raise OSError('private storage unavailable')
                ace_sid = w.LPVOID(ace.value + 8)
                value = w.LPWSTR()
                if not adv.ConvertSidToStringSidW(ace_sid, ctypes.byref(value)):
                    raise OSError('private storage unavailable')
                try:
                    # Python's private mkdir DACL uses OWNER RIGHTS (S-1-3-4).
                    # It names this user because the owner was checked above.
                    allowed = ('S-1-5-18', 'S-1-5-32-544', 'S-1-3-4') if directory else ('S-1-5-18',)
                    if not adv.EqualSid(ace_sid, user_sid) and value.value not in allowed:
                        raise OSError('private storage unavailable')
                finally:
                    kernel.LocalFree(value)
        finally:
            kernel.LocalFree(sd)
    finally:
        kernel.CloseHandle(token)


class DeckLog:
    def __init__(self, state_dir, *, clock=time.time, limits=None):
        self.clock, self.limits = clock, dict(LIMITS, **(limits or {}))
        self.path = os.path.join(os.path.abspath(state_dir), 'deck-log.sqlite3')
        self.root = os.path.dirname(self.path)
        self.lock = threading.RLock()
        self.wake = threading.Condition(threading.RLock())
        self.inflight = 0
        self.queue = collections.deque()
        self.settings = dict(DEFAULTS)
        self.epoch = secrets.token_hex(16)
        self.active = False
        self.available = False
        self.reason = ''
        self.fts = False
        self.closed = False
        self.db = None
        self.key = secrets.token_bytes(32)
        self.dropped = 0
        self.last_gap = ''
        self.last_purge = self.clock()
        self.thread = None
        if os.path.lexists(self.path):
            self._initialize()
        else:
            try:
                if os.path.lexists(self.root):
                    self._private(self.root, True)
                self.available = True
            except OSError:
                self.reason = 'private_storage_unavailable'

    def _initialize(self):
        try:
            self._private_open()
            self._schema()
            self.available = True
            self._purge()
            self._gap('offline_or_not_recording')
            self.thread = threading.Thread(target=self._writer, name='deck-log-writer', daemon=True)
            self.thread.start()
        except (OSError, sqlite3.Error, ValueError, TypeError):
            self.available = False
            self.reason = 'private_storage_unavailable'
            if self.db:
                self.db.close()
                self.db = None

    def _private(self, path, directory=False):
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode) or getattr(st, 'st_file_attributes', 0) & 0x400:
            raise OSError('linked storage')
        if directory != stat.S_ISDIR(st.st_mode) or (not directory and (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1)):
            raise OSError('invalid storage')
        if os.name == 'nt':
            _windows_security(path, directory)
        elif st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != (0o700 if directory else 0o600):
            raise OSError('nonprivate storage')
        return st.st_dev, st.st_ino

    def _private_open(self):
        ancestor = os.path.dirname(self.root)
        while ancestor != os.path.dirname(ancestor):
            st = os.lstat(ancestor)
            if stat.S_ISLNK(st.st_mode) or getattr(st, 'st_file_attributes', 0) & 0x400:
                raise OSError('linked storage parent')
            ancestor = os.path.dirname(ancestor)
        if not os.path.lexists(self.root):
            if os.name == 'nt':
                _windows_security(self.root, True, True)
            else:
                os.mkdir(self.root, 0o700)
        self.root_identity = self._private(self.root, True)
        if not os.path.lexists(self.path):
            if os.name == 'nt':
                _windows_security(self.path, False, True)
            else:
                fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
                os.close(fd)
        self.file_identity = self._private(self.path)
        self.db = sqlite3.connect(self.path, timeout=0.1, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA journal_mode=DELETE')
        self.db.execute('PRAGMA secure_delete=ON')
        self.db.execute('PRAGMA temp_store=MEMORY')
        self.db.execute('PRAGMA max_page_count=%d' % max(32, self.limits['bytes'] // self.db.execute('PRAGMA page_size').fetchone()[0]))

    def _verify(self):
        if self._private(self.root, True) != self.root_identity or self._private(self.path) != self.file_identity:
            raise OSError('storage replaced')
        for suffix in ('-journal', '-wal', '-shm'):
            if os.path.lexists(self.path + suffix):
                self._private(self.path + suffix)

    def _schema(self):
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS source_records(key TEXT PRIMARY KEY,stream TEXT NOT NULL,chat_id TEXT NOT NULL,project_id TEXT NOT NULL,source_ts REAL NOT NULL,observed_ts REAL NOT NULL,age_ts REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,record_key TEXT NOT NULL REFERENCES source_records(key) ON DELETE CASCADE,slot TEXT NOT NULL,provider TEXT NOT NULL,chat_id TEXT NOT NULL,chat_name TEXT NOT NULL,project_id TEXT NOT NULL,project_name TEXT NOT NULL,source_ts REAL NOT NULL,observed_ts REAL NOT NULL,time_basis TEXT NOT NULL,kind TEXT NOT NULL,role TEXT NOT NULL,text TEXT NOT NULL,operation TEXT NOT NULL,operation_id TEXT NOT NULL,phase TEXT NOT NULL,outcome TEXT NOT NULL,file_ref TEXT NOT NULL,redacted INTEGER NOT NULL,certainty TEXT NOT NULL DEFAULT 'recorded');
        CREATE INDEX IF NOT EXISTS events_time ON events(source_ts,id);
        CREATE INDEX IF NOT EXISTS events_scope ON events(chat_id,project_id);
        CREATE TABLE IF NOT EXISTS erased_records(key TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS erase_floors(scope TEXT PRIMARY KEY,cutoff REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS coverage(id INTEGER PRIMARY KEY,reason TEXT NOT NULL,started REAL NOT NULL,ended REAL NOT NULL,count INTEGER NOT NULL);
        ''')
        existing = {r['key']: json.loads(r['value']) for r in self.db.execute('SELECT * FROM settings')}
        if existing.get('schema_version', 1) not in (1, 2):
            raise ValueError('unsupported archive schema')
        if 'certainty' not in {r[1] for r in self.db.execute('PRAGMA table_info(events)')}:
            self.db.execute("ALTER TABLE events ADD COLUMN certainty TEXT NOT NULL DEFAULT 'recorded'")
        self._save_setting('schema_version', 2)
        for key in DEFAULTS:
            if key in existing:
                self.settings[key] = existing[key]
        self._validate_settings(self.settings)
        self.key = bytes.fromhex(existing.get('identity_key', self.key.hex()))
        if len(self.key) != 32:
            raise ValueError('invalid archive identity')
        self._save_setting('identity_key', self.key.hex())
        for key, value in self.settings.items():
            self._save_setting(key, value)
        try:
            if self.limits.get('fts', True):
                self.db.execute('CREATE VIRTUAL TABLE IF NOT EXISTS event_fts USING fts5(id UNINDEXED,text)')
                self.fts = True
        except sqlite3.OperationalError:
            self.fts = False
        self.db.commit()

    def _save_setting(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, json.dumps(value)))

    def _gap(self, reason, count=1):
        now = self.clock()
        row = self.db.execute('SELECT * FROM coverage ORDER BY id DESC LIMIT 1').fetchone()
        if row and row['reason'] == reason:
            self.db.execute('UPDATE coverage SET ended=?,count=count+? WHERE id=?', (now, count, row['id']))
        else:
            if row:
                self.db.execute('UPDATE coverage SET ended=? WHERE id=?', (now, row['id']))
            self.db.execute('INSERT INTO coverage(reason,started,ended,count) VALUES (?,?,?,?)', (reason, now, now, count))
        self.db.execute('DELETE FROM coverage WHERE id NOT IN (SELECT id FROM coverage ORDER BY id DESC LIMIT 200)')
        self.db.commit()

    @staticmethod
    def _validate_settings(value):
        if type(value['enabled']) is not bool or type(value['paused']) is not bool or type(value['retention_days']) is not int or not 1 <= value['retention_days'] <= 365:
            raise ValueError('invalid settings')
        for field in ('excluded_chats', 'excluded_projects'):
            if not isinstance(value[field], list) or len(value[field]) > 1000 or any(not isinstance(x, str) or not x or len(x) > 200 for x in value[field]):
                raise ValueError('invalid exclusions')
        if any(not CHAT.fullmatch(x) for x in value['excluded_chats']) or any(not HEX.fullmatch(x) for x in value['excluded_projects']):
            raise ValueError('invalid exclusion identity')

    def opaque_identity(self, namespace, *parts):
        if namespace not in ('project', 'stream', 'record', 'operation') or len(parts) > 16:
            raise ValueError('invalid identity')
        data = bytearray(namespace.encode())
        for part in parts:
            if isinstance(part, bytes):
                tag, raw = b'b', part
            elif isinstance(part, str):
                tag, raw = b's', part.encode('utf-8', 'surrogatepass')
            elif type(part) is int:
                tag, raw = b'i', str(part).encode()
            else:
                raise ValueError('invalid identity')
            if len(raw) > 32768:
                raise ValueError('invalid identity')
            data.extend(tag + str(len(raw)).encode() + b':' + raw)
        return hmac.new(self.key, data, hashlib.sha256).hexdigest()

    def _status(self):
        try:
            coverage = [dict(r) for r in self.db.execute('SELECT * FROM coverage ORDER BY id DESC LIMIT 200')] if self.db else []
        except sqlite3.Error:
            coverage = []
            self.active = False
            self.reason = 'history_read_unavailable'
        if coverage and self.active:
            coverage[0]['ended'] = self.clock()
        return dict(available=self.available, reason=self.reason, settings={k: list(v) if isinstance(v, list) else v for k, v in self.settings.items()},
                    enabled=self.settings['enabled'], paused=self.settings['paused'],
                    recording=bool(self.available and self.active), capture_epoch=self.epoch,
                    state='recording' if self.active else 'arming' if self.settings['enabled'] and not self.settings['paused'] else 'paused' if self.settings['paused'] else 'off',
                    search_mode='fts5' if self.fts else 'bounded_plain', fallback_rows=self.limits['fallback_rows'],
                    queued=len(self.queue), dropped=self.dropped,
                    coverage=coverage,
                    limits=dict(self.limits))

    def status(self):
        with self.lock:
            return self._status()

    def configure(self, *, enabled=None, paused=None, retention_days=None, excluded_chats=None, excluded_projects=None):
        patch = {k: v for k, v in locals().items() if k != 'self' and v is not None}
        with self.lock:
            value = dict(self.settings, **patch)
            self._validate_settings(value)
            value = {k: list(v) if isinstance(v, list) else v for k, v in value.items()}
            if not self.available:
                return self._status()
            if self.db is None:
                self._initialize()
                if not self.available:
                    return self._status()
            self.active = False
            self.epoch = secrets.token_hex(16)
            with self.wake:
                self.queue.clear()
            try:
                self._verify()
                for key, val in value.items():
                    self._save_setting(key, val)
                self.db.commit()
                self.settings = value
                self._purge()
                self._gap('policy_changed')
                self.reason = ''
            except (OSError, sqlite3.Error):
                self.db.rollback()
                self.reason = 'settings_storage_unavailable'
            return self._status()

    def activate(self, capture_epoch):
        with self.lock:
            if self.available and not self.reason and not self.closed and capture_epoch == self.epoch and self.settings['enabled'] and not self.settings['paused']:
                try:
                    self._verify()
                    self._gap('recording_started')
                    self.active = True
                except (OSError, sqlite3.Error):
                    self.active = False
                    self.reason = 'settings_storage_unavailable'
            return self._status()

    def _prepare(self, record, candidates):
        fields = {'key', 'stream', 'provider', 'chat_id', 'project_id', 'source_ts', 'time_basis'}
        if not isinstance(record, dict) or set(record) != fields or not isinstance(candidates, list) or len(candidates) > self.limits['projections']:
            raise ValueError('invalid_record')
        if not all(isinstance(record[k], str) and HEX.fullmatch(record[k]) for k in ('key', 'stream')):
            raise ValueError('invalid_provenance')
        prefixes = {'codex': 'x:', 'claude': 'c:', 'cursor': 'k:'}
        provider, chat, project = record['provider'], record['chat_id'], record['project_id']
        if provider not in prefixes or not isinstance(chat, str) or not chat.startswith(prefixes[provider]) or not CHAT.fullmatch(chat) or not isinstance(project, str) or (project and not HEX.fullmatch(project)) or record['time_basis'] not in ('source', 'observed') or not _finite(record['source_ts']):
            raise ValueError('invalid_scope')
        for candidate in candidates:
            if not isinstance(candidate, dict) or set(candidate) - CANDIDATE_KEYS:
                raise ValueError('invalid_projection')
            if any(key != 'source' and (not isinstance(value, str) or len(value) > 16384) for key, value in candidate.items()):
                raise ValueError('invalid_projection')
        if len(json.dumps([record, candidates], ensure_ascii=True).encode()) > self.limits['batch_bytes']:
            raise ValueError('record_too_large')
        now = self.clock()
        rows, slots = [], set()
        for c in candidates:
            if not isinstance(c, dict) or set(c) - CANDIDATE_KEYS or c.get('kind') not in KINDS:
                raise ValueError('invalid_projection')
            source = c.get('source')
            if not isinstance(source, dict) or set(source) != {'slot'} or not isinstance(source['slot'], str) or not re.fullmatch(r'[A-Za-z0-9:_-]{1,80}', source['slot']) or source['slot'] in slots:
                raise ValueError('invalid_slot')
            slots.add(source['slot'])
            role = c.get('role', '')
            if role not in ('', 'user', 'assistant') or c.get('operation', '') not in OPERATIONS or c.get('phase', '') not in ('', 'started', 'result', 'observation') or c.get('outcome', 'unknown') not in ('success', 'failure', 'unknown', 'pending') or c.get('certainty', 'recorded') not in ('recorded', 'inferred', 'probably'):
                raise ValueError('invalid_outcome')
            text, redacted = ('', False)
            if c['kind'] in ('message', 'user', 'say', 'assign'):
                text, redacted = _prose(c.get('text', ''), self.limits['prose_bytes'])
            operation_id = c.get('operation_id', '')
            if operation_id and (not isinstance(operation_id, str) or not HEX.fullmatch(operation_id)):
                raise ValueError('invalid_operation_identity')
            name = _text(c.get('chat_name', ''))
            project_name = _text(c.get('project_name', ''))
            # Names cannot become an alternate channel for private absolute paths.
            if '/' in name or '\\' in name:
                name = ''
            if '/' in project_name or '\\' in project_name:
                project_name = ''
            event_id = hmac.new(self.key, (record['key'] + ':' + source['slot']).encode(), hashlib.sha256).hexdigest()
            rows.append((event_id, record['key'], source['slot'], provider, chat, name, project, project_name,
                         record['source_ts'], now, record['time_basis'], c['kind'], role, text, c.get('operation', ''),
                         operation_id, c.get('phase', ''), c.get('outcome', 'unknown'), _relative(c.get('file_ref', '')), int(redacted), c.get('certainty', 'recorded')))
        return dict(record), rows, now

    def offer_record(self, record, candidates, *, capture_epoch):
        try:
            prepared = self._prepare(record, candidates)
        except (ValueError, TypeError, OverflowError, RecursionError):
            self.dropped += 1
            self.last_gap = 'invalid_or_oversized_record'
            return dict(status='dropped', reason=self.last_gap)
        if not self.wake.acquire(False):
            self.dropped += 1
            self.last_gap = 'writer_busy'
            return dict(status='dropped', reason='writer_busy')
        try:
            if not self.available or not self.active or self.closed or capture_epoch != self.epoch:
                return dict(status='ignored', reason='not_recording')
            if len(self.queue) >= self.limits['queue']:
                self.dropped += 1
                self.last_gap = 'queue_full'
                return dict(status='dropped', reason='queue_full')
            self.queue.append((self.epoch, prepared))
            self.wake.notify()
            return dict(status='queued', reason='')
        finally:
            self.wake.release()

    def _scope_hash(self, kind, identity):
        return hmac.new(self.key, (kind + ':' + identity).encode(), hashlib.sha256).hexdigest()

    def _write(self, epoch, prepared):
        if epoch != self.epoch or not self.active:
            return
        record, rows, observed = prepared
        if record['chat_id'] in self.settings['excluded_chats'] or record['project_id'] in self.settings['excluded_projects']:
            return
        age = min(record['source_ts'], observed)
        if age < self.clock() - self.settings['retention_days'] * 86400:
            return
        scopes = ('all', self._scope_hash('chat', record['chat_id']), self._scope_hash('project', record['project_id']))
        if any(age <= r[0] for scope in scopes for r in self.db.execute('SELECT cutoff FROM erase_floors WHERE scope=?', (scope,))):
            return
        self._verify()
        if self.db.execute('SELECT 1 FROM erased_records WHERE key=?', (record['key'],)).fetchone():
            return
        self.db.execute('BEGIN IMMEDIATE')
        try:
            # F1: deduplicate the COMPLETE source record before considering any slots.
            cursor = self.db.execute('INSERT OR IGNORE INTO source_records VALUES (?,?,?,?,?,?,?)',
                                     (record['key'], record['stream'], record['chat_id'], record['project_id'], record['source_ts'], observed, age))
            if cursor.rowcount == 0:
                self.db.rollback()
                return
            if self.db.execute('SELECT COUNT(*) FROM events').fetchone()[0] + len(rows) > self.limits['events'] or self.db.execute('SELECT (SELECT COUNT(*) FROM source_records)+(SELECT COUNT(*) FROM erased_records)').fetchone()[0] > self.limits['records']:
                raise OverflowError('storage_full')
            self.db.executemany('INSERT INTO events VALUES (' + ','.join('?' * 21) + ')', rows)
            if self.fts:
                self.db.executemany('INSERT INTO event_fts(id,text) VALUES (?,?)', [(r[0], ' '.join(str(r[i]) for i in (13, 11, 14, 17, 18, 5, 7, 20))) for r in rows])
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def _writer(self):
        while True:
            with self.wake:
                if not self.queue and not self.closed:
                    self.wake.wait(0.2)
                if self.closed:
                    return
                item = self.queue.popleft() if self.queue else None
                if item:
                    self.inflight += 1
            try:
                with self.lock:
                    if self.clock() - self.last_purge >= 3600:
                        self._purge()
                        self.last_purge = self.clock()
                    if self.last_gap:
                        self._gap(self.last_gap)
                        self.last_gap = ''
                    if item:
                        self._write(*item)
            except (sqlite3.Error, OSError, OverflowError):
                with self.lock:
                    self.active = False
                    self.reason = 'storage_full_or_unavailable'
                    with self.wake:
                        self.queue.clear()
                    try:
                        self._gap(self.reason)
                    except sqlite3.Error:
                        pass
            finally:
                with self.wake:
                    if item:
                        self.inflight -= 1
                    self.wake.notify_all()

    def _cleanup(self):
        # Recreate instead of relying on optional recent FTS5 secure-delete support.
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='event_fts'").fetchone():
            self.db.execute('DROP TABLE event_fts')
        if self.fts:
            self.db.execute('CREATE VIRTUAL TABLE event_fts USING fts5(id UNINDEXED,text)')
            self.db.execute("INSERT INTO event_fts(id,text) SELECT id,text||' '||kind||' '||operation||' '||outcome||' '||file_ref||' '||chat_name||' '||project_name||' '||certainty FROM events")
        self.db.commit()
        self.db.execute('VACUUM')
        self._verify()

    def _purge(self):
        cutoff = self.clock() - self.settings['retention_days'] * 86400
        count = self.db.execute('SELECT COUNT(*) FROM source_records WHERE age_ts<?', (cutoff,)).fetchone()[0]
        if count:
            self.db.execute('INSERT OR IGNORE INTO erased_records SELECT key FROM source_records WHERE age_ts<?', (cutoff,))
            self.db.execute('DELETE FROM source_records WHERE age_ts<?', (cutoff,))
            self.db.execute("INSERT INTO erase_floors VALUES ('all',?) ON CONFLICT(scope) DO UPDATE SET cutoff=MAX(cutoff,excluded.cutoff)", (cutoff,))
            self._cleanup()

    def delete(self, *, chat_id=None, project_id=None, all=False):
        if (chat_id is not None and not _text(chat_id, 200)) or (project_id is not None and not _text(project_id, 200)):
            raise ValueError('invalid deletion scope')
        if sum((chat_id is not None, project_id is not None, all is True)) != 1:
            raise ValueError('one deletion scope required')
        with self.lock:
            if not self.available:
                return dict(ok=False, reason=self.reason)
            self._verify()
            self.active = False
            self.epoch = secrets.token_hex(16)
            with self.wake:
                self.queue.clear()
            scope = 'all' if all else self._scope_hash('chat' if chat_id is not None else 'project', chat_id if chat_id is not None else project_id)
            now = self.clock()
            if self.db.execute('SELECT COUNT(*) FROM erase_floors').fetchone()[0] >= 2000:
                # Bounded anti-resurrection ledger; a global floor is conservative.
                self.db.execute('DELETE FROM erase_floors')
                self.db.execute('INSERT INTO erase_floors VALUES (?,?)', ('all', now))
            self.db.execute('INSERT OR REPLACE INTO erase_floors VALUES (?,?)', (scope, now))
            if all:
                self.db.execute('INSERT OR IGNORE INTO erased_records SELECT key FROM source_records')
                self.db.execute('DELETE FROM source_records')
                self.db.execute('DELETE FROM coverage')
            else:
                field = 'chat_id' if chat_id is not None else 'project_id'
                self.db.execute('INSERT OR IGNORE INTO erased_records SELECT key FROM source_records WHERE ' + field + '=?', (chat_id if chat_id is not None else project_id,))
                self.db.execute('DELETE FROM source_records WHERE ' + field + '=?', (chat_id if chat_id is not None else project_id,))
            try:
                self._cleanup()
                self._gap('history_deleted')
                self.reason = ''
            except (sqlite3.Error, OSError):
                self.active = False
                self.reason = 'deletion_cleanup_failed'
                return dict(ok=False, reason=self.reason, capture_epoch=self.epoch, rearm_required=True)
            return dict(ok=True, capture_epoch=self.epoch, rearm_required=True)

    def flush(self, timeout=2.0):
        end = time.monotonic() + timeout
        with self.wake:
            while (self.queue or self.inflight) and time.monotonic() < end:
                self.wake.wait(max(0, end - time.monotonic()))
            return not self.queue and not self.inflight and not self.reason

    def close(self, timeout=2.0):
        self.flush(timeout)
        with self.wake:
            self.active = False
            self.closed = True
            self.queue.clear()
            self.wake.notify_all()
        if self.thread:
            self.thread.join(timeout)
        if self.thread and self.thread.is_alive():
            return
        with self.lock:
            if self.db:
                try:
                    self._gap('watch_stopped')
                except sqlite3.Error:
                    pass
                self.db.close()
                self.db = None

    def _readable(self):
        if not self.available or self.db is None:
            return False
        try:
            self._verify()
            self._purge()
            return True
        except (sqlite3.Error, OSError):
            self.active = False
            self.reason = 'history_read_unavailable'
            return False

    @staticmethod
    def _event(row):
        value = dict(row)
        value['citation'] = 'deck:' + value['id']
        value['historical'] = True
        return value

    def search(self, *, query='', chat_id=None, project_id=None, since=None, until=None, limit=50, cursor=None):
        if not isinstance(query, str) or len(query) > 256:
            raise ValueError('invalid query')
        limit = max(1, min(100, int(limit)))
        with self.lock:
            if not self._readable():
                return dict(events=[], cursor=None, search_mode='unavailable', coverage=[], reason=self.reason)
            where, args = [], []
            for field, val, op in (('chat_id', chat_id, '='), ('project_id', project_id, '='), ('source_ts', since, '>='), ('source_ts', until, '<')):
                if val is not None:
                    where.append(field + op + '?')
                    args.append(val)
            if cursor:
                if not isinstance(cursor, str) or not HEX.fullmatch(cursor):
                    raise ValueError('invalid cursor')
                row = self.db.execute('SELECT source_ts FROM events WHERE id=?', (cursor,)).fetchone()
                if row:
                    where.append('(source_ts<? OR (source_ts=? AND id<?))')
                    args.extend((row[0], row[0], cursor))
            if query.strip():
                if self.fts:
                    terms = re.findall(r'\w+', query, re.UNICODE)[:12]
                    if not terms:
                        return dict(events=[], cursor=None, search_mode='fts5', coverage=self._status()['coverage'])
                    where.append('id IN (SELECT id FROM event_fts WHERE event_fts MATCH ?)')
                    args.append(' AND '.join('"' + t.replace('"', '""') + '"' for t in terms))
                else:
                    where.append('id IN (SELECT id FROM events ORDER BY source_ts DESC,id DESC LIMIT ?)')
                    args.append(self.limits['fallback_rows'])
                    where.append("instr(lower(text||' '||kind||' '||operation||' '||outcome||' '||file_ref||' '||chat_name||' '||project_name||' '||certainty),lower(?))>0")
                    args.append(query)
            sql = 'SELECT * FROM events' + (' WHERE ' + ' AND '.join(where) if where else '') + ' ORDER BY source_ts DESC,id DESC LIMIT ?'
            rows = self.db.execute(sql, args + [limit + 1]).fetchall()
            return dict(events=[self._event(r) for r in rows[:limit]], cursor=rows[limit - 1]['id'] if len(rows) > limit else None,
                        search_mode='fts5' if self.fts else 'bounded_plain', coverage=self._status()['coverage'])

    def get_event(self, event_id):
        if not isinstance(event_id, str) or not HEX.fullmatch(event_id):
            return None
        with self.lock:
            if not self._readable():
                return None
            row = self.db.execute('SELECT * FROM events WHERE id=?', (event_id,)).fetchone()
            return self._event(row) if row else None

    def chats(self, *, limit=100, cursor=None):
        with self.lock:
            if not self._readable():
                return dict(chats=[], cursor=None)
            rows = self.db.execute('SELECT chat_id,provider,chat_name,project_id,project_name,MAX(source_ts) AS last_ts FROM events WHERE chat_id>? GROUP BY chat_id,provider ORDER BY chat_id LIMIT ?', (cursor or '', min(100, max(1, int(limit))) + 1)).fetchall()
            cap = min(100, max(1, int(limit)))
            return dict(chats=[dict(r) for r in rows[:cap]], cursor=rows[cap - 1]['chat_id'] if len(rows) > cap else None)

    def projects(self, *, limit=100, cursor=None):
        with self.lock:
            if not self._readable():
                return dict(projects=[], cursor=None)
            cap = min(100, max(1, int(limit)))
            rows = self.db.execute('SELECT project_id,MAX(project_name) AS project_name,MAX(source_ts) AS last_ts FROM events WHERE project_id>? GROUP BY project_id ORDER BY project_id LIMIT ?', (cursor or '', cap + 1)).fetchall()
            return dict(projects=[dict(r) for r in rows[:cap]], cursor=rows[cap - 1]['project_id'] if len(rows) > cap else None)

    def resolve_chat(self, label_or_id, *, provider=None, project_id=None):
        with self.lock:
            if not self._readable():
                return dict(error='not_found', candidates=[])
            exact = self.db.execute('SELECT 1 FROM events WHERE chat_id=? LIMIT 1', (label_or_id,)).fetchone()
            where, args = (['chat_id=?'], [label_or_id]) if exact else (['lower(chat_name)=lower(?)'], [label_or_id])
            for field, value in (('provider', provider), ('project_id', project_id)):
                if value is not None:
                    where.append(field + '=?')
                    args.append(value)
            rows = self.db.execute('SELECT DISTINCT chat_id,provider,chat_name,project_id,project_name FROM events WHERE ' + ' AND '.join(where) + ' LIMIT 101', args).fetchall()
            ids = {r['chat_id'] for r in rows}
            if len(ids) == 1:
                return dict(chat_id=next(iter(ids)), candidates=[dict(r) for r in rows])
            return dict(error='ambiguous' if ids else 'not_found', candidates=[dict(r) for r in rows[:100]])

    def session_day_summary(self, day, *, chat_id=None, project_id=None):
        try:
            parsed = time.strptime(day, '%Y-%m-%d')
            start = time.mktime((parsed.tm_year, parsed.tm_mon, parsed.tm_mday, 0, 0, 0, 0, 0, -1))
            following = time.localtime(start + 36 * 3600)
            end = time.mktime((following.tm_year, following.tm_mon, following.tm_mday, 0, 0, 0, 0, 0, -1))
        except (TypeError, ValueError, OverflowError):
            raise ValueError('invalid day') from None
        with self.lock:
            if not self._readable():
                return dict(day=day, events=[], counts={}, complete=False, coverage=[])
            where, args = ['source_ts>=?', 'source_ts<?'], [start, end]
            for field, value in (('chat_id', chat_id), ('project_id', project_id)):
                if value is not None:
                    where.append(field + '=?')
                    args.append(value)
            rows = self.db.execute('SELECT * FROM events WHERE ' + ' AND '.join(where) + ' ORDER BY source_ts,id LIMIT 1001', args).fetchall()
            selected = rows[:1000]
            outcomes, outcome_certainty = {}, {}
            for r in selected:
                if r['operation_id']:
                    # Only an explicit result can replace an operation's pending state.
                    if r['phase'] == 'result' or r['operation_id'] not in outcomes:
                        outcomes[r['operation_id']] = r['outcome'] if r['phase'] == 'result' else 'pending'
                        outcome_certainty[r['operation_id']] = r['certainty']
            return dict(day=day, digest_type='deterministic_local_day', events=[self._event(r) for r in selected],
                        counts=dict(collections.Counter(r['kind'] for r in selected)), outcomes=outcomes,
                        outcome_certainty=outcome_certainty, certainty_counts=dict(collections.Counter(r['certainty'] for r in selected)),
                        truncated=len(rows) > 1000, complete=False,
                        qualification='Historical evidence only; inferred/probably observations retain that qualification. Gaps, omitted content, and unobserved outcomes may remain.', coverage=self._status()['coverage'])
