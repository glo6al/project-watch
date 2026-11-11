"""Private Officer API usage metadata, independent of logs and message delivery.

No prompts, replies, tool results, provider error text, or credentials are stored.
Token counts are provider-reported usage. Dollar figures are dated estimates,
never invoices. Failures are fixed status values, never reasons to repeat a call.
"""
from datetime import date
from decimal import Decimal, InvalidOperation
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
import uuid

MAX_BYTES = 5 * 1024 * 1024
RETENTION_DAYS = 90
PRICING_PATH = Path(__file__).resolve().parents[1] / 'officer' / 'PRICING.json'
PRICE_SOURCE = 'https://platform.claude.com/docs/en/about-claude/pricing'
MODEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z')
KEY_SHAPE = re.compile(r'sk-ant-[A-Za-z0-9_-]{20,}')
COUNTERS = ('input_tokens', 'output_tokens', 'cache_creation_input_tokens',
            'cache_read_input_tokens', 'cache_5m_input_tokens', 'cache_1h_input_tokens')
STAGES = frozenset(('dispatch', 'answer'))


def _model(value):
    return value if isinstance(value, str) and MODEL.fullmatch(value) and not KEY_SHAPE.search(value) and not value.lower().startswith(('sk-', 'ghp_', 'gho_')) else None


def _count(value):
    return value if type(value) is int and 0 <= value <= 10**12 else None


def _pricing():
    """A bounded repository rate snapshot; malformed/missing rates mean unknown."""
    try:
        before = PRICING_PATH.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > 16384 or getattr(before, 'st_file_attributes', 0) & 0x400:
            return None
        fd = os.open(PRICING_PATH, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        try:
            if not os.path.samestat(before, os.fstat(fd)):
                return None
            raw = os.read(fd, 16385)
        finally:
            os.close(fd)
        value = json.loads(raw.decode('utf-8'))
        if set(value) != {'schema_version', 'verified_date', 'source', 'currency', 'per_tokens', 'models'}:
            return None
        if value['schema_version'] != 1 or value['source'] != PRICE_SOURCE or value['currency'] != 'USD' or value['per_tokens'] != 1000000:
            return None
        if date.fromisoformat(value['verified_date']).isoformat() != value['verified_date'] or not isinstance(value['models'], dict):
            return None
        for model, row in value['models'].items():
            if not _model(model) or not isinstance(row, dict) or set(row) != {'input', 'output', 'cache_5m', 'cache_1h', 'cache_read', 'max_total_input_tokens'}:
                return None
            for key in ('input', 'output', 'cache_5m', 'cache_1h', 'cache_read'):
                if not isinstance(row[key], str) or len(row[key]) > 16:
                    return None
                number = Decimal(row[key])
                if not number.is_finite() or not 0 <= number <= 100000:
                    return None
            if row['max_total_input_tokens'] is not None and (_count(row['max_total_input_tokens']) is None or row['max_total_input_tokens'] == 0):
                return None
        return value
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, InvalidOperation):
        return None


def _usage(response):
    """Project only validated numeric/enumerated metadata from an API response."""
    response = response if isinstance(response, dict) else {}
    usage = response.get('usage')
    usage = usage if isinstance(usage, dict) else {}
    creation = usage.get('cache_creation')
    creation = creation if isinstance(creation, dict) else {}
    counts = {key: _count(usage.get(key)) for key in COUNTERS[:4]}
    counts['cache_5m_input_tokens'] = _count(creation.get('ephemeral_5m_input_tokens'))
    counts['cache_1h_input_tokens'] = _count(creation.get('ephemeral_1h_input_tokens'))
    complete = all(value is not None for value in counts.values())
    if complete and counts['cache_creation_input_tokens'] != counts['cache_5m_input_tokens'] + counts['cache_1h_input_tokens']:
        complete = False
    known = {'input_tokens', 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens',
             'cache_creation', 'output_tokens_details', 'service_tier', 'inference_geo', 'speed', 'server_tool_use'}
    no_extras = not (set(usage) - known)
    details = usage.get('output_tokens_details', {})
    if not isinstance(details, dict) or set(details) - {'thinking_tokens'}:
        no_extras = False
    elif details and (_count(details.get('thinking_tokens')) is None or counts['output_tokens'] is None or details['thinking_tokens'] > counts['output_tokens']):
        no_extras = False
    extras = usage.get('server_tool_use', {})
    if not isinstance(extras, dict) or set(extras) - {'web_search_requests', 'web_fetch_requests'} or any(_count(value) != 0 for value in extras.values()):
        no_extras = False
    if set(creation) - {'ephemeral_5m_input_tokens', 'ephemeral_1h_input_tokens'}:
        no_extras = False
    standard = usage.get('service_tier') == 'standard' and usage.get('inference_geo') == 'global' and usage.get('speed') == 'standard'
    return _model(response.get('model')), counts, complete, bool(standard and no_extras)


def _estimate(pricing, model, counts, eligible):
    if not eligible or not pricing or model not in pricing['models']:
        return None
    rates = pricing['models'][model]
    total_input = counts['input_tokens'] + counts['cache_creation_input_tokens'] + counts['cache_read_input_tokens']
    if rates['max_total_input_tokens'] is not None and total_input > rates['max_total_input_tokens']:
        return None
    value = sum(Decimal(counts[counter]) * Decimal(rates[rate]) for counter, rate in
                (('input_tokens', 'input'), ('output_tokens', 'output'),
                 ('cache_5m_input_tokens', 'cache_5m'), ('cache_1h_input_tokens', 'cache_1h'),
                 ('cache_read_input_tokens', 'cache_read'))) / Decimal(1000000)
    return str(value)


class UsageLedger:
    def __init__(self, state_dir, *, clock=time.time, max_bytes=MAX_BYTES, retention_days=RETENTION_DAYS):
        self.root = os.path.abspath(os.fspath(state_dir))
        self.path = os.path.join(self.root, 'officer-usage.sqlite3')
        self.clock, self.lock, self.db = clock, threading.RLock(), None
        self.available, self.tracking_incomplete, self.reason = False, False, ''
        self.max_bytes = min(MAX_BYTES, max(131072, int(max_bytes)))
        self.retention_days = min(RETENTION_DAYS, max(1, int(retention_days)))
        self.pricing = _pricing()
        try:
            self._open()
            self.available = True
            self._prune()
        except (OSError, ValueError, sqlite3.Error):
            self._fail()

    def _fail(self):
        self.available, self.tracking_incomplete = False, True
        self.reason = 'usage_storage_unavailable'
        if self.db is not None:
            try:
                self.db.rollback()
                self.db.close()
            except sqlite3.Error:
                pass
            self.db = None

    def _private(self, path, directory=False):
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise OSError('linked usage storage')
        if directory != stat.S_ISDIR(info.st_mode) or (not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1)):
            raise OSError('invalid usage storage')
        if os.name == 'nt':
            from deck_log import _windows_security  # Security helper only; no archive is constructed.
            _windows_security(path, directory)
        elif info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600):
            raise OSError('nonprivate usage storage')
        return info.st_dev, info.st_ino

    def _verify(self):
        if self._private(self.root, True) != self.root_identity or self._private(self.path) != self.file_identity:
            raise OSError('usage storage replaced')
        total = os.lstat(self.path).st_size
        for suffix in ('-journal', '-wal', '-shm'):
            path = self.path + suffix
            if os.path.lexists(path):
                self._private(path)
                if suffix != '-journal':
                    raise OSError('unexpected usage sidecar')
                total += os.lstat(path).st_size
        if total > self.max_bytes:
            raise OSError('usage storage limit')

    def _open(self):
        parent = os.path.dirname(self.root)
        while parent != os.path.dirname(parent):
            info = os.lstat(parent)
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise OSError('linked usage parent')
            parent = os.path.dirname(parent)
        if not os.path.lexists(self.root):
            if os.name == 'nt':
                from deck_log import _windows_security
                _windows_security(self.root, True, True)
            else:
                os.mkdir(self.root, 0o700)
        self.root_identity = self._private(self.root, True)
        if not os.path.lexists(self.path):
            if os.name == 'nt':
                from deck_log import _windows_security
                _windows_security(self.path, False, True)
            else:
                fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
                os.close(fd)
        self.file_identity = self._private(self.path)
        self._verify()  # Reject unsafe pre-existing sidecars before SQLite recovery.
        self.db = sqlite3.connect(self.path, timeout=0.1, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        # A rollback journal can contain the old version of every DB page plus
        # small page headers. Keep the DB below half the aggregate cap, with a
        # 64 KiB reserve for journal headers/page overhead; WAL is never enabled.
        self.db.execute('PRAGMA journal_mode=DELETE')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA secure_delete=ON')
        self.db.execute('PRAGMA temp_store=MEMORY')
        page = self.db.execute('PRAGMA page_size').fetchone()[0]
        pages = (self.max_bytes - 65536) // (2 * page)
        if pages < 4 or self.db.execute('PRAGMA page_count').fetchone()[0] > pages:
            raise OSError('usage storage limit')
        self.db.execute('PRAGMA max_page_count=%d' % pages)
        self.db.execute('''CREATE TABLE IF NOT EXISTS calls (
            id TEXT PRIMARY KEY, started REAL NOT NULL, ended REAL,
            requested_model TEXT NOT NULL, returned_model TEXT, stage TEXT NOT NULL,
            status TEXT NOT NULL, input_tokens INTEGER, output_tokens INTEGER,
            cache_creation_input_tokens INTEGER, cache_read_input_tokens INTEGER,
            cache_5m_input_tokens INTEGER, cache_1h_input_tokens INTEGER,
            usage_complete INTEGER NOT NULL DEFAULT 0, estimated_usd TEXT,
            rates_date TEXT)''')
        self.db.execute('CREATE INDEX IF NOT EXISTS calls_started ON calls(started)')
        self.db.commit()
        self._verify()

    def _now(self):
        value = self.clock()
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError('invalid usage clock')
        return value

    def _prune(self):
        self._verify()
        self.db.execute('DELETE FROM calls WHERE started < ?', (self._now() - self.retention_days * 86400,))
        self.db.commit()
        self._verify()

    def begin(self, model, stage):
        with self.lock:
            if not self.available:
                return None
            if not _model(model) or not isinstance(stage, str) or stage not in STAGES:
                self.tracking_incomplete = True
                self.reason = 'invalid_usage_metadata'
                return None
            try:
                self._prune()
                call_id = str(uuid.uuid4())
                self.db.execute('INSERT INTO calls (id,started,requested_model,stage,status) VALUES (?,?,?,?,?)',
                                (call_id, self._now(), model, stage, 'unknown'))
                self.db.commit()
                self._verify()
                return call_id
            except (OSError, ValueError, sqlite3.Error):
                self._fail()
                return None

    def finish(self, call_id, response, error_bool):
        with self.lock:
            if not self.available:
                return False
            try:
                try:
                    valid_id = isinstance(call_id, str) and str(uuid.UUID(call_id)) == call_id
                except (ValueError, AttributeError):
                    valid_id = False
                if not valid_id or type(error_bool) is not bool:
                    self.tracking_incomplete, self.reason = True, 'invalid_usage_metadata'
                    return False
                self._verify()
                # No partial update can win twice, including simultaneous finishers.
                self.db.execute('BEGIN IMMEDIATE')
                row = self.db.execute('SELECT ended FROM calls WHERE id=?', (call_id,)).fetchone()
                if row is None or row['ended'] is not None:
                    self.db.rollback()
                    if row is None:
                        self.tracking_incomplete, self.reason = True, 'usage_record_missing'
                        return False
                    return True
                model, counts, complete, standard = _usage(response)
                estimate = _estimate(self.pricing, model, counts, complete and standard and not error_bool)
                status = 'error' if error_bool else 'response' if isinstance(response, dict) else 'unknown'
                self.db.execute('''UPDATE calls SET ended=?, returned_model=?, status=?,
                    input_tokens=?, output_tokens=?, cache_creation_input_tokens=?, cache_read_input_tokens=?,
                    cache_5m_input_tokens=?, cache_1h_input_tokens=?, usage_complete=?, estimated_usd=?, rates_date=?
                    WHERE id=?''', (self._now(), model, status, *(counts[key] for key in COUNTERS),
                                   int(complete), estimate, self.pricing['verified_date'] if estimate is not None else None, call_id))
                self.db.commit()
                self._verify()
                return True
            except (OSError, ValueError, TypeError, sqlite3.Error):
                self._fail()
                return False

    def status(self):
        return {'available': self.available, 'tracking_incomplete': self.tracking_incomplete,
                'reason': self.reason}

    def summary(self, days=30):
        with self.lock:
            result = dict(self.status(), days=None, calls=0, inflight_calls=0, unknown_calls=0,
                          partial_usage_calls=0, priced_calls=0, unpriced_calls=0,
                          observed_tokens={key: 0 for key in COUNTERS}, estimated_usd=None,
                          estimate_is_partial=True, rates_date=self.pricing['verified_date'] if self.pricing else None,
                          estimate_rate_dates=[],
                          rates_source=PRICE_SOURCE,
                          qualification='Provider-reported token usage; USD amounts are dated estimates, not an invoice. Missing usage and unsettled calls are unknown, not zero. In-flight counts include attempts left unsettled by a crash. Cache creation totals include the separate five-minute and one-hour components; do not add those components twice. Totals cover only retained Officer calls; tracking failures may omit calls.')
            if not self.available:
                return result
            if type(days) is not int or not 1 <= days <= self.retention_days:
                result.update(tracking_incomplete=True, reason='invalid_usage_period')
                return result
            try:
                self._prune()
                rows = self.db.execute('SELECT * FROM calls WHERE started >= ?', (self._now() - days * 86400,)).fetchall()
                result['days'], result['calls'] = days, len(rows)
                total = Decimal(0)
                rate_dates = set()
                for row in rows:
                    if (not _model(row['requested_model']) or row['stage'] not in STAGES or
                        row['returned_model'] is not None and not _model(row['returned_model']) or
                        row['status'] not in ('unknown', 'error', 'response') or
                        row['usage_complete'] not in (0, 1) or
                        any(row[key] is not None and _count(row[key]) is None for key in COUNTERS)):
                        raise ValueError('invalid stored usage metadata')
                    result['inflight_calls'] += row['ended'] is None
                    result['unknown_calls'] += row['status'] in ('unknown', 'error')
                    result['partial_usage_calls'] += not row['usage_complete']
                    for key in COUNTERS:
                        if row[key] is not None:
                            result['observed_tokens'][key] += row[key]
                    if row['estimated_usd'] is not None:
                        if not isinstance(row['estimated_usd'], str) or len(row['estimated_usd']) > 64:
                            raise ValueError('invalid stored usage estimate')
                        amount = Decimal(row['estimated_usd'])
                        if not amount.is_finite() or not 0 <= amount <= 10**12 or date.fromisoformat(row['rates_date']).isoformat() != row['rates_date']:
                            raise ValueError('invalid stored usage estimate')
                        result['priced_calls'] += 1
                        total += amount
                        rate_dates.add(row['rates_date'])
                    else:
                        result['unpriced_calls'] += 1
                result['estimated_usd'] = str(total) if result['priced_calls'] else None
                result['estimate_rate_dates'] = sorted(rate_dates)
                result['estimate_is_partial'] = bool(result['unpriced_calls'] or self.tracking_incomplete)
                result['tracking_incomplete'] = bool(self.tracking_incomplete or result['inflight_calls'] or result['unknown_calls'] or result['partial_usage_calls'])
                self._verify()
                return result
            except (OSError, ValueError, TypeError, InvalidOperation, sqlite3.Error):
                self._fail()
                result.update(self.status())
                return result

    def close(self):
        with self.lock:
            if self.db is not None:
                try:
                    self.db.close()
                except sqlite3.Error:
                    self._fail()
                self.db = None
            self.available = False

    def delete(self):
        with self.lock:
            if not self.available:
                return False
            try:
                self._verify()
                # Clear records transactionally while retaining the DB identity.
                # Other WATCH connections remain valid and can keep recording.
                self.db.execute('BEGIN IMMEDIATE')
                self.db.execute('DELETE FROM calls')
                self.db.commit()
                self._verify()
                self.reason = 'usage_records_deleted'
                return True
            except (OSError, sqlite3.Error):
                self._fail()
                return False
