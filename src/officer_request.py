"""Bounded Officer request execution and mechanical evidence checks.

No network, persistence, action authority, or import-time I/O. A valid reference
or typed quantity does not establish that surrounding prose follows from it.
The server owns authentication, evidence acquisition, deletion locks and signed
conversation receipts; these helpers cannot make publication atomic themselves.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re
import threading
import time
import uuid

from officer_usage import KEY_SHAPE, _model, _usage


class BudgetExceeded(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class EvidenceRejected(ValueError):
    pass


class ExchangeInvalidated(EvidenceRejected):
    pass


def _integer(value, maximum=10**12):
    return type(value) is int and 0 <= value <= maximum


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def request_digest(body):
    return hashlib.sha256(_json(body).encode()).hexdigest()


@dataclass(frozen=True)
class BudgetPolicy:
    mode: str = 'bounded'
    max_calls: int = 7
    max_seconds: int = 120
    max_input_bytes: int = 524288
    max_output_tokens: int = 12288
    max_evidence_bytes: int = 65536
    max_evidence_rows: int = 512
    max_queries: int = 24
    max_query_rows: int = 1000000
    max_input_tokens: int | None = None
    usd_limit: str | None = None

    def __post_init__(self):
        for field in ('max_calls', 'max_seconds', 'max_input_bytes', 'max_output_tokens',
                      'max_evidence_bytes', 'max_evidence_rows', 'max_queries', 'max_query_rows'):
            if not _integer(getattr(self, field)) or getattr(self, field) == 0:
                raise ValueError('invalid_request_policy')
        if self.max_input_tokens is not None and (not _integer(self.max_input_tokens) or self.max_input_tokens == 0):
            raise ValueError('invalid_request_policy')
        if self.mode not in ('bounded', 'strict_usd'):
            raise ValueError('invalid_request_policy')
        if self.mode == 'bounded' and self.usd_limit is not None:
            raise ValueError('invalid_request_policy')
        if self.mode == 'strict_usd':
            try:
                amount = Decimal(self.usd_limit) if isinstance(self.usd_limit, str) else None
                if amount is None or not amount.is_finite() or amount <= 0:
                    raise ValueError('invalid_request_policy')
            except InvalidOperation:
                raise ValueError('invalid_request_policy') from None


@dataclass(frozen=True)
class InputBound:
    tokens: int
    basis: str
    request_digest: str


@dataclass(frozen=True)
class Reservation:
    id: str
    model: str
    stage: str
    input_bytes: int
    input_tokens: int | None
    output_tokens: int
    estimated_usd: str | None
    body_digest: str


class RequestBudget:
    def __init__(self, policy=None, *, clock=time.monotonic, pricing=None):
        self.policy = policy or BudgetPolicy()
        self.clock, self.started, self.pricing = clock, clock(), pricing
        if type(self.started) not in (int, float) or not math.isfinite(self.started) or self.started < 0:
            raise ValueError('invalid_request_clock')
        self.lock = threading.RLock()
        self.attempts = {}
        self.queries = self.query_rows = self.evidence_rows = self.evidence_bytes = 0
        self.exhausted = False
        self.last_reason = ''

    def _deny(self, reason):
        self.last_reason = reason
        raise BudgetExceeded(reason)

    def _time(self):
        now = self.clock()
        if not isinstance(now, (int, float)) or isinstance(now, bool) or not math.isfinite(now) or now < self.started:
            self._deny('request_clock_invalid')
        if now - self.started >= self.policy.max_seconds:
            self._deny('request_time_exhausted')

    def _totals(self):
        values = list(self.attempts.values())
        return {'calls': len(values),
                'input_bytes': sum(v['reservation'].input_bytes for v in values),
                'input_tokens': sum(v['input_tokens'] or 0 for v in values),
                'input_tokens_unknown': any(v['input_tokens'] is None for v in values),
                'output_tokens': sum(v['output_tokens'] for v in values),
                'estimated_usd': sum((v['usd'] for v in values if v['usd'] is not None), Decimal(0)),
                'cost_unknown': any(v['usd'] is None for v in values),
                'unsettled_calls': sum(not v['settled'] for v in values)}

    def _quote(self, model, bound, output, billing):
        expected = {'service_tier': 'standard', 'inference_geo': 'global', 'speed': 'standard', 'extras': False}
        if billing != expected or type(billing.get('extras')) is not bool:
            self._deny('billing_basis_unknown')
        try:
            p = self.pricing
            if (not isinstance(p, dict) or p.get('currency') != 'USD' or p.get('per_tokens') != 1000000 or
                date.fromisoformat(p['verified_date']).isoformat() != p['verified_date'] or
                p.get('source') != 'https://platform.claude.com/docs/en/about-claude/pricing'):
                self._deny('pricing_unknown')
            rates = p['models'][model]
            prices = [Decimal(rates[key]) for key in ('input', 'cache_5m', 'cache_1h', 'cache_read')]
            output_price = Decimal(rates['output'])
            if any(not number.is_finite() or number < 0 for number in prices + [output_price]):
                self._deny('pricing_unknown')
            threshold = rates['max_total_input_tokens']
            if threshold is not None and (not _integer(threshold) or bound > threshold):
                self._deny('pricing_unknown')
            # Every input token is reserved at the most expensive applicable
            # input/cache rate. No cache discount is assumed before the response.
            return (Decimal(bound) * max(prices) + Decimal(output) * output_price) / Decimal(1000000)
        except (KeyError, TypeError, ValueError, InvalidOperation):
            self._deny('pricing_unknown')

    def reserve_call(self, body, stage, *, input_bound=None, billing=None):
        with self.lock:
            self._time()
            if self.exhausted:
                self._deny('request_budget_exhausted')
            if not isinstance(body, dict) or not _model(body.get('model')) or stage not in ('dispatch', 'answer', 'plan', 'verify'):
                self._deny('invalid_call_metadata')
            output = body.get('max_tokens')
            if not _integer(output) or output == 0:
                self._deny('invalid_output_bound')
            try:
                size = len(_json(body).encode())
            except (TypeError, ValueError, OverflowError):
                self._deny('invalid_call_metadata')
            tokens = None
            if input_bound is not None:
                if (not isinstance(input_bound, InputBound) or not _integer(input_bound.tokens) or
                    input_bound.basis not in ('exact', 'conservative') or
                    input_bound.request_digest != request_digest(body)):
                    self._deny('input_bound_invalid')
                tokens = input_bound.tokens
            total = self._totals()
            for used, added, maximum, reason in (
                (total['calls'], 1, self.policy.max_calls, 'request_calls_exhausted'),
                (total['input_bytes'], size, self.policy.max_input_bytes, 'request_input_exhausted'),
                (total['output_tokens'], output, self.policy.max_output_tokens, 'request_output_exhausted')):
                if used + added > maximum:
                    self._deny(reason)
            if self.policy.max_input_tokens is not None:
                if tokens is None or total['input_tokens_unknown']:
                    self._deny('input_bound_unknown')
                if total['input_tokens'] + tokens > self.policy.max_input_tokens:
                    self._deny('request_input_tokens_exhausted')
            usd = None
            if self.policy.mode == 'strict_usd':
                if tokens is None:
                    self._deny('input_bound_unknown')
                usd = self._quote(body['model'], tokens, output, billing)
                if total['cost_unknown'] or total['estimated_usd'] + usd > Decimal(self.policy.usd_limit):
                    self._deny('request_dollars_exhausted')
            reservation = Reservation(str(uuid.uuid4()), body['model'], stage, size, tokens, output,
                                      str(usd) if usd is not None else None, request_digest(body))
            self.attempts[reservation.id] = {'reservation': reservation, 'input_tokens': tokens,
                                             'output_tokens': output, 'usd': usd, 'settled': False}
            return reservation

    def settle(self, reservation, response, error=False):
        with self.lock:
            entry = self.attempts.get(getattr(reservation, 'id', None))
            if entry is None or entry['reservation'] != reservation:
                raise ValueError('unknown_reservation')
            if entry['settled']:
                return True
            model, counts, complete, standard = _usage(response)
            if error or not complete or model != reservation.model:
                return False  # Unknown attempts keep their entire reservation.
            actual_input = counts['input_tokens'] + counts['cache_creation_input_tokens'] + counts['cache_read_input_tokens']
            actual_output = counts['output_tokens']
            if self.policy.mode == 'strict_usd':
                from officer_usage import _estimate
                amount = _estimate(self.pricing, model, counts, standard)
                if amount is None:
                    return False
                actual_usd = Decimal(amount)
            else:
                actual_usd = None
            entry.update(input_tokens=actual_input, output_tokens=actual_output, usd=actual_usd, settled=True)
            if (actual_output > reservation.output_tokens or
                reservation.input_tokens is not None and actual_input > reservation.input_tokens or
                reservation.estimated_usd is not None and actual_usd > Decimal(reservation.estimated_usd)):
                self.exhausted, self.last_reason = True, 'measured_usage_exceeded_reservation'
            return True

    def call(self, call, body, stage, *, input_bound=None, billing=None, **kwargs):
        frozen_body = json.loads(_json(body))
        reservation = self.reserve_call(frozen_body, stage, input_bound=input_bound, billing=billing)
        # Exactly one invocation; exceptions and unknown responses retain the
        # reservation. The caller's established delivery/recovery path owns errors.
        kwargs['end'] = min(kwargs.get('end', self.started + self.policy.max_seconds), self.started + self.policy.max_seconds)
        response, error = call(frozen_body, **kwargs)
        self.settle(reservation, response, bool(error))
        self._time()
        return response, error

    def claim_query(self, work_rows):
        with self.lock:
            self._time()
            if not _integer(work_rows):
                self._deny('invalid_query_bound')
            if self.queries + 1 > self.policy.max_queries or self.query_rows + work_rows > self.policy.max_query_rows:
                self._deny('request_query_exhausted')
            self.queries += 1
            self.query_rows += work_rows

    def remaining(self):
        with self.lock:
            return max(0, self.started + self.policy.max_seconds - self.clock())

    def supply_evidence(self, rows, bytes):
        with self.lock:
            self._time()
            if not _integer(rows) or not _integer(bytes):
                self._deny('invalid_evidence_bound')
            if self.evidence_rows + rows > self.policy.max_evidence_rows or self.evidence_bytes + bytes > self.policy.max_evidence_bytes:
                self._deny('request_evidence_exhausted')
            self.evidence_rows += rows
            self.evidence_bytes += bytes

    def receipt(self):
        with self.lock:
            total = self._totals()
            total['estimated_usd'] = str(total['estimated_usd']) if self.policy.mode == 'strict_usd' else None
            return dict(total, mode=self.policy.mode, queries=self.queries, query_rows=self.query_rows,
                        evidence_rows=self.evidence_rows, evidence_bytes=self.evidence_bytes,
                        exhausted=self.exhausted, reason=self.last_reason,
                        qualification='Reservations are conservative bounds, not invoices. Unsettled attempts retain their reservations.')


REF = re.compile(r'(?:deck:[0-9a-f]{64}|live:[A-Za-z0-9_-]{1,80}:[0-9]{1,16}|agg:[0-9a-f]{16,64}:[0-9]{1,8}|fact:[A-Za-z0-9:_-]{1,160}|ev:[1-9][0-9]{0,3})\Z')
SNAPSHOT = re.compile(r'[0-9a-f]{16,64}\Z')
QUANTITY_KEYS = frozenset(('ref', 'metric', 'value', 'unit', 'scope', 'interval', 'snapshot', 'complete'))
PLACEHOLDER = re.compile(r'\{\{quantity:([^{}]+)\}\}')


def _structured(value, limit=8192):
    try:
        encoded = _json(value)
    except (TypeError, ValueError, OverflowError):
        raise EvidenceRejected('invalid_evidence_metadata') from None
    if len(encoded) > limit:
        raise EvidenceRejected('evidence_metadata_too_large')
    return json.loads(encoded)


def _quantity(value):
    if not isinstance(value, dict) or set(value) != QUANTITY_KEYS:
        raise EvidenceRejected('invalid_quantity')
    if not _valid_ref(value['ref']) or not value['ref'].startswith('agg:'):
        raise EvidenceRejected('invalid_quantity_reference')
    if (not isinstance(value['metric'], str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,79}', value['metric']) or
        not isinstance(value['unit'], str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,79}', value['unit']) or
        not isinstance(value['snapshot'], str) or not SNAPSHOT.fullmatch(value['snapshot']) or
        type(value['complete']) is not bool or not isinstance(value['scope'], dict) or not isinstance(value['interval'], dict)):
        raise EvidenceRejected('invalid_quantity')
    number = value['value']
    if number is not None and (type(number) not in (int, float) or not math.isfinite(number) or abs(number) > 10**15):
        raise EvidenceRejected('invalid_quantity_value')
    if number is None and value['complete']:
        raise EvidenceRejected('unknown_quantity_cannot_be_complete')
    normalized = _structured(value)
    if normalize_scope(value['scope']) != value['scope'] or normalize_interval(value['interval']) != value['interval']:
        raise EvidenceRejected('quantity_scope_not_normalized')
    return normalized


def _valid_ref(value):
    return isinstance(value, str) and REF.fullmatch(value) and not KEY_SHAPE.search(value)


def normalize_scope(scope):
    if not isinstance(scope, dict) or not {'sources', 'chat_ids', 'project_ids'} <= set(scope) or set(scope) - {'sources', 'chat_ids', 'project_ids', 'filters', 'match'}:
        raise EvidenceRejected('invalid_evidence_scope')
    out = {}
    for key in ('sources', 'chat_ids', 'project_ids'):
        values = scope[key]
        if not isinstance(values, list) or len(values) > 512 or any(not isinstance(v, str) or len(v) > 200 or KEY_SHAPE.search(v) for v in values):
            raise EvidenceRejected('invalid_evidence_scope')
        if key == 'sources':
            if any(v not in ('live', 'retained') for v in values):
                raise EvidenceRejected('invalid_evidence_scope')
        elif key == 'chat_ids':
            if any(not re.fullmatch(r'[xcke]:[A-Za-z0-9_.:-]{1,196}', v) for v in values):
                raise EvidenceRejected('invalid_evidence_scope')
        elif any(not re.fullmatch(r'[0-9a-f]{64}', v) for v in values):
            raise EvidenceRejected('invalid_evidence_scope')
        out[key] = sorted(set(values))
    if 'filters' in scope:
        fields = {
            'provider': {'claude', 'codex', 'cursor'},
            'kind': {'message', 'user', 'say', 'operation', 'read', 'write', 'edit', 'patch', 'run',
                     'search', 'web', 'tool', 'result', 'turn_start', 'turn_end', 'compact', 'queued',
                     'dequeued', 'waiting', 'ask', 'task', 'task_upd', 'spawn', 'assign', 'see'},
            'role': {'', 'user', 'assistant'},
            'operation': {'', 'read', 'write', 'edit', 'patch', 'run', 'search', 'web', 'tool',
                          'spawn', 'task', 'delete', 'check', 'compact', 'see'},
            'phase': {'', 'started', 'result', 'observation'},
            'outcome': {'success', 'failure', 'unknown', 'pending'},
            'certainty': {'recorded', 'inferred', 'probably'},
            'file_ref': None,
        }
        filters = scope['filters']
        if not isinstance(filters, dict) or set(filters) - set(fields):
            raise EvidenceRejected('invalid_evidence_filters')
        clean = {}
        for key, values in filters.items():
            if not isinstance(values, list) or not values or len(values) > 50 or any(not isinstance(v, str) or len(v) > 300 or KEY_SHAPE.search(v) for v in values):
                raise EvidenceRejected('invalid_evidence_filters')
            if fields[key] is not None and any(v not in fields[key] for v in values):
                raise EvidenceRejected('invalid_evidence_filters')
            if key == 'file_ref' and any(not v or v.startswith(('/', '~')) or ':' in v or '\\' in v or any(part in ('', '.', '..') for part in v.split('/')) or any(ord(c) < 32 for c in v) for v in values):
                raise EvidenceRejected('invalid_evidence_filters')
            clean[key] = sorted(set(values))
        out['filters'] = clean
    if 'match' in scope:
        match = scope['match']
        if (not isinstance(match, dict) or set(match) != {'mode', 'digest'} or
            match['mode'] not in ('literal', 'terms') or not isinstance(match['digest'], str) or
            not re.fullmatch(r'[0-9a-f]{64}', match['digest'])):
            raise EvidenceRejected('invalid_evidence_match')
        out['match'] = dict(match)
    return out


def normalize_interval(interval):
    if not isinstance(interval, dict) or set(interval) != {'since', 'until', 'time_basis'} or interval['time_basis'] not in ('source', 'observed'):
        raise EvidenceRejected('invalid_evidence_interval')
    for key in ('since', 'until'):
        value = interval[key]
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
            raise EvidenceRejected('invalid_evidence_interval')
    if interval['since'] is not None and interval['until'] is not None and interval['since'] >= interval['until']:
        raise EvidenceRejected('invalid_evidence_interval')
    return _structured(interval)


class AssertionRegistry:
    """Tracks supply and exact typed equality, never semantic entailment of prose."""
    def __init__(self):
        self.pending_refs, self.supplied_refs = set(), set()
        self.pending_quantities, self.quantities = {}, {}

    def register(self, envelope):
        if not isinstance(envelope, dict):
            raise EvidenceRejected('invalid_evidence_envelope')
        refs, quantities = set(), {}
        if any(not isinstance(envelope.get(key, []), list) for key in ('evidence', 'facts', 'quantities')):
            raise EvidenceRejected('invalid_evidence_envelope')
        for row in envelope.get('evidence', []) + envelope.get('facts', []):
            ref = row.get('ref') if isinstance(row, dict) else None
            if ref is not None:
                if not _valid_ref(ref):
                    raise EvidenceRejected('invalid_evidence_reference')
                refs.add(ref)
        for item in envelope.get('quantities', []):
            value = _quantity(item)
            ref = value['ref']
            prior = self.quantities.get(ref) or self.pending_quantities.get(ref) or quantities.get(ref)
            if prior is not None and _json(prior) != _json(value):
                raise EvidenceRejected('quantity_reference_reused')
            quantities[ref] = value
            refs.add(ref)
        if len(self.supplied_refs | self.pending_refs | refs) > 1024 or len(self.quantities | self.pending_quantities | quantities) > 256:
            raise EvidenceRejected('evidence_registry_full')
        self.pending_refs.update(refs)
        self.pending_quantities.update(quantities)

    def mark_supplied(self):
        self.supplied_refs.update(self.pending_refs)
        self.quantities.update(self.pending_quantities)
        self.pending_refs.clear()
        self.pending_quantities.clear()

    def validate_qualitative_refs(self, refs):
        if not isinstance(refs, list) or len(refs) > 128 or any(not isinstance(ref, str) or ref not in self.supplied_refs for ref in refs):
            raise EvidenceRejected('reference_not_supplied')
        return list(dict.fromkeys(refs))

    def validate_assertion(self, assertion):
        value = _quantity(assertion)
        expected = self.quantities.get(value['ref'])
        if expected is None or _json(value) != _json(expected):
            raise EvidenceRejected('quantity_assertion_mismatch')
        return expected.copy()

    def render_assertion(self, assertion):
        value = self.validate_assertion(assertion)
        number = 'Unknown' if value['value'] is None else str(value['value'])
        # Labels are generated from registered metadata, never model-authored
        # scope labels. Stable IDs distinguish subjects when no trusted name map
        # is available; source time and observation time remain separate.
        scope, interval = value['scope'], value['interval']
        sources = ' and '.join({'live': 'live room', 'retained': 'retained records'}[source] for source in scope['sources'])
        chats = 'chats ' + ', '.join(scope['chat_ids']) if scope['chat_ids'] else 'all recorded chats'
        projects = 'projects ' + ', '.join(scope['project_ids']) if scope['project_ids'] else 'all recorded projects'
        def when(stamp):
            try:
                return datetime.fromtimestamp(stamp, timezone.utc).astimezone().isoformat(sep=' ', timespec='seconds')
            except (ValueError, OverflowError, OSError):
                raise EvidenceRejected('unrenderable_evidence_interval') from None
        start, end = interval['since'], interval['until']
        period = ('from ' + when(start) if start is not None else 'from the earliest retained time')
        period += (' until (excluding) ' + when(end) if end is not None else ' through the snapshot cutoff')
        basis = 'source time' if interval['time_basis'] == 'source' else 'observation time'
        filters = '; '.join(key.replace('_', ' ') + ': ' + ', '.join(v or '(not recorded)' for v in values)
                            for key, values in sorted(scope.get('filters', {}).items()))
        if scope.get('match'):
            selected = ('literal' if scope['match']['mode'] == 'literal' else 'token') + ' text filter ' + scope['match']['digest'][:12]
            filters = filters + '; ' + selected if filters else selected
        status = 'complete matching recorded population' if value['complete'] else 'incomplete matching recorded population; total unknown'
        return '%s %s (%s; %s; %s; %s; %s, %s%s; %s)' % (
            number, value['unit'].replace('_', ' '), value['metric'].replace('_', ' '), sources,
            chats, projects, period, basis, '; ' + filters if filters else '', status)

    def validate_answer(self, answer):
        if not isinstance(answer, dict) or set(answer) != {'text', 'references', 'assertions'}:
            raise EvidenceRejected('invalid_typed_answer')
        if not isinstance(answer['text'], str) or len(answer['text']) > 16000 or not isinstance(answer['assertions'], list) or len(answer['assertions']) > 128:
            raise EvidenceRejected('invalid_typed_answer')
        refs = self.validate_qualitative_refs(answer['references'])
        assertions = {}
        for item in answer['assertions']:
            value = self.validate_assertion(item)
            if value['ref'] in assertions:
                raise EvidenceRejected('duplicate_quantity_assertion')
            assertions[value['ref']] = value
        used = PLACEHOLDER.findall(answer['text'])
        if set(used) != set(assertions) or any(ref not in self.supplied_refs for ref in used):
            raise EvidenceRejected('quantity_placeholder_mismatch')
        text = PLACEHOLDER.sub(lambda m: self.render_assertion(assertions[m.group(1)]), answer['text'])
        if '{{quantity:' in text:
            raise EvidenceRejected('invalid_quantity_placeholder')
        return {'text': text, 'references': list(dict.fromkeys(refs + used)), 'assertions': list(assertions.values())}

    def receipt(self, scope, snapshot):
        if not isinstance(snapshot, str) or not SNAPSHOT.fullmatch(snapshot):
            raise EvidenceRejected('invalid_snapshot')
        # The server signs this metadata. It must not accept an unsigned browser
        # receipt as proof of owner authorship or surviving evidence.
        return {'version': 1, 'snapshot': snapshot, 'scope': normalize_scope(scope),
                'references': sorted(self.supplied_refs), 'quantity_references': sorted(self.quantities)}


class EvidenceValidity:
    def __init__(self, generation):
        self.generation = _structured(generation, 2048)
        self.invalidated = False

    def check(self, current_generation, validation):
        if (self.invalidated or _json(_structured(current_generation, 2048)) != _json(self.generation) or
            not isinstance(validation, dict) or validation.get('valid') is not True):
            self.invalidated = True
            raise ExchangeInvalidated('evidence_changed_or_unavailable')
        return True
