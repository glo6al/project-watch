"""Bounded, request-local read capabilities over already captured WATCH evidence.

No original-log, filesystem, native-provider or model access is performed here.
Snapshots keep cutoffs and metadata, never a second durable copy of source prose.
"""
import collections
from datetime import datetime, timedelta
import copy
import hashlib
import json
import math
import re
import secrets
import sqlite3
import time

from deck_log import CHAT, HEX, KINDS, OPERATIONS, SECRET, _prose, _relative, _text

SOURCES = ('live', 'retained')
# Process-list observations are live-only; Deck Log capture identities stay unchanged.
LIVE_CHAT = re.compile(r'[xckea]:[A-Za-z0-9_.:-]{1,196}\Z')
FACT_FIELDS = ('model', 'context', 'branch', 'parent', 'last_activity', 'owner_request')
FILTERS = {
    'provider': {'codex', 'claude', 'cursor', 'codex_cloud', 'antigravity'}, 'kind': KINDS,
    'role': {'', 'user', 'assistant'}, 'operation': OPERATIONS,
    'phase': {'', 'started', 'result', 'observation'},
    'outcome': {'success', 'failure', 'pending', 'unknown'},
    'certainty': {'recorded', 'probably', 'inferred'}, 'file_ref': None,
}
GROUPS = ('chat', 'project', 'day', 'operation', 'outcome', 'certainty')
OPERATION_KINDS = (OPERATIONS - {''}) | {'operation', 'result'}
EMPTY_INTERVAL = dict(since=None, until=None, time_basis='source')


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def _keys(value, allowed):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError('Invalid evidence request fields.')


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError('Invalid evidence limit.')
    return value


def _scope(value=None):
    value = {} if value is None else value
    _keys(value, ('sources', 'chat_ids', 'project_ids'))
    result = {}
    for key, default in [('sources', list(SOURCES)), ('chat_ids', []), ('project_ids', [])]:
        items = value.get(key, default)
        if not isinstance(items, list) or len(items) > 100 or any(not isinstance(x, str) for x in items):
            raise ValueError('Invalid evidence scope.')
        if key == 'sources' and (not items or any(x not in SOURCES for x in items)):
            raise ValueError('Invalid evidence source.')
        if key == 'chat_ids' and any(not LIVE_CHAT.fullmatch(x) for x in items):
            raise ValueError('Invalid chat identity.')
        if key == 'project_ids' and any(not HEX.fullmatch(x) for x in items):
            raise ValueError('Invalid project identity.')
        result[key] = sorted(set(items))
    return result


def _interval(value=None, now=None):
    value = {} if value is None else value
    _keys(value, (*EMPTY_INTERVAL, 'day', 'start_day', 'end_day', 'minutes'))
    basis = value.get('time_basis', 'source')
    if basis not in ('source', 'observed'):
        raise ValueError('Invalid evidence time basis.')
    numeric = any(k in value for k in ('since', 'until'))
    calendar = any(k in value for k in ('day', 'start_day', 'end_day'))
    recent = 'minutes' in value
    if sum((numeric, calendar, recent)) > 1:
        raise ValueError('Do not mix calendar, minute and numeric time bounds.')
    result = dict(since=value.get('since'), until=value.get('until'), time_basis=basis)
    if calendar:
        if 'day' in value:
            if set(value) - {'day', 'time_basis'}:
                raise ValueError('Choose one calendar interval form.')
            first = last = value['day']
        else:
            if 'start_day' not in value or 'end_day' not in value:
                raise ValueError('Calendar ranges need both start_day and end_day.')
            first, last = value['start_day'], value['end_day']
        dates=[]
        for text in (first,last):
            if not isinstance(text,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',text):
                raise ValueError('Invalid local calendar date.')
            dates.append(datetime.strptime(text,'%Y-%m-%d').date())
        if dates[0]>dates[1]:
            raise ValueError('Invalid local calendar range.')
        try:
            following=dates[1]+timedelta(days=1)
            # Local midnights, not 86,400-second arithmetic: DST days can be 23/25 hours.
            result['since']=time.mktime((dates[0].year,dates[0].month,dates[0].day,0,0,0,0,0,-1))
            result['until']=time.mktime((following.year,following.month,following.day,0,0,0,0,0,-1))
        except (OverflowError,OSError):
            raise ValueError('Unsupported local calendar date.') from None
    elif recent:
        minutes=_integer(value['minutes'],1,525600)
        result['until']=time.time() if now is None else now
        result['since']=result['until']-minutes*60
    for key in ('since', 'until'):
        x = result[key]
        if x is not None and (type(x) not in (float, int) or not math.isfinite(x)):
            raise ValueError('Invalid evidence interval.')
    if result['since'] is not None and result['until'] is not None and result['since'] >= result['until']:
        raise ValueError('Invalid evidence interval.')
    return result


def _spec(inp, now=None):
    scope, interval = _scope(inp.get('scope')), _interval(inp.get('interval'), now)
    filters = inp.get('filters', {})
    _keys(filters, FILTERS)
    clean = {}
    for key, values in filters.items():
        if not isinstance(values, list) or not 1 <= len(values) <= 100 or any(not isinstance(x, str) for x in values):
            raise ValueError('Invalid evidence filter.')
        if FILTERS[key] is not None and not set(values) <= FILTERS[key]:
            raise ValueError('Unsupported evidence filter.')
        if key == 'file_ref' and any(not _relative(x) or _relative(x) != x for x in values):
            raise ValueError('Invalid relative file label.')
        clean[key] = sorted(set(values))
    match = inp.get('match', {})
    _keys(match, ('text', 'mode'))
    text, mode = match.get('text', ''), match.get('mode', 'literal')
    if not isinstance(text, str) or len(text) > 256 or SECRET.search(text) or mode not in ('literal', 'terms'):
        raise ValueError('Invalid evidence text match.')
    return dict(scope=scope, interval=interval, filters=clean, match=dict(text=text, mode=mode))


def _stamp(row, basis):
    value = row.get(basis + '_ts')
    return row.get('observed_ts') if value is None and basis == 'source' else value


def _fingerprint(row):
    keys = ('ref', 'source', 'id', 'chat_id', 'subject', 'field', 'value', 'chat_name', 'provider', 'project_id', 'project_name', 'source_ts', 'kind', 'role', 'text', 'operation', 'operation_id', 'phase', 'outcome', 'file_ref', 'certainty', 'qualification', 'freshness')
    value={k:row[k] for k in keys if k in row}
    for key in ('time_basis','observation_source','source_qualification','redacted'):
        if key in row:value[key]=row[key]
    if not row.get('field') and 'observed_ts' in row:value['observed_ts']=row['observed_ts']
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _matches(row, spec):
    scope, interval = spec['scope'], spec['interval']
    for field, key in [('chat_id', 'chat_ids'), ('project_id', 'project_ids')]:
        if scope[key] and row.get(field) not in scope[key]:
            return False
    stamp = _stamp(row, interval['time_basis'])
    if interval['since'] is not None and (stamp is None or stamp < interval['since']):
        return False
    if interval['until'] is not None and (stamp is None or stamp >= interval['until']):
        return False
    if any(row.get(key, '') not in values for key, values in spec['filters'].items()):
        return False
    text = spec['match']['text'].casefold()
    blob = ' '.join(str(row.get(k, '')) for k in ('text', 'kind', 'operation', 'outcome', 'file_ref', 'chat_name', 'project_name', 'certainty')).casefold()
    if not text.strip():
        return True
    if spec['match']['mode'] == 'literal':
        return text in blob
    terms = re.findall(r'\w+', text)
    words = set(re.findall(r'\w+', blob))
    return bool(terms) and all(t in words for t in terms)


def _excerpt(text, match, cap=480):
    if len(text) <= cap:
        return text
    needle = match['text']
    if match['mode'] == 'terms':
        needle = next(iter(re.findall(r'\w+', needle)), '')
    at = text.casefold().find(needle.casefold()) if needle else 0
    start = max(0, at - cap // 3) if at >= 0 else 0
    return ('…' if start else '') + text[start:start + cap] + ('…' if start + cap < len(text) else '')


class EvidenceService:
    def __init__(self, history=None, live_view=None, *, clock=time.monotonic, max_work=250000, max_bytes=24000):
        self.history, self.live_view, self.clock = history, live_view or (lambda: {}) , clock
        self.max_work, self.max_bytes = max_work, max_bytes

    def start(self):
        return EvidenceSession(self)


class EvidenceSession:
    def __init__(self, service):
        self.service = service
        self.snapshot = secrets.token_hex(16)
        self.started = service.clock()
        self.observed_at = time.time()
        self.fence = self._archive_state()
        self._last_archive_generation = self.fence.get('erasure')
        self._erasure_highwater = self.fence.get('erasure')
        live = self._live()
        self.boot = str(live.get('boot', ''))[:100]
        self.live_floor = live.get('floor_seq', min((e.get('seq',0) for e in live.get('events',[]) if isinstance(e,dict) and type(e.get('seq')) is int), default=0))
        self.live_cutoff = live.get('cutoff_seq', live.get('seq', 0))
        if type(self.live_cutoff) is not int:
            self.live_cutoff = 0
        self.subjects = self._subjects(live.get('subjects', []))
        self.projects = self._projects(live.get('projects', []))
        self.facts = self._facts(live.get('facts', []))
        self.pending = {}
        self.receipt_fingerprinter = None
        self.supplied = {}
        self.cursors = {}
        self.quantities = {}
        self.populations = {}
        self.frontiers = {}
        self._frontier_supplied_count = 0
        self.queries = []
        self.work = 0
        self.failure = ''
        self._erasure_seen = False
        self._last_coverage = []

    def _live(self):
        try:
            value = self.service.live_view()
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def _archive_state(self):
        if self.service.history is None:
            return dict(available=False, store=None, fence=0, erasure=None, coverage=[], queued=None)
        try:
            return self.service.history.evidence_state()
        except (OSError, ValueError, TypeError, sqlite3.Error):
            return dict(available=False, store=None, fence=0, erasure=None, coverage=[], queued=None,
                        known_store=getattr(self.service.history,'_evidence_store',None),committed_erasure=getattr(self.service.history,'_committed_erasure',None))

    @staticmethod
    def _subjects(items):
        result = []
        for item in items[:1000] if isinstance(items, list) else []:
            if not isinstance(item, dict) or not LIVE_CHAT.fullmatch(str(item.get('chat_id', item.get('id', '')))):
                continue
            chat = item.get('chat_id', item.get('id'))
            result.append(dict(chat_id=chat, chat_name=_text(item.get('chat_name', item.get('name', ''))),
                               provider=item.get('provider') if item.get('provider') in FILTERS['provider'] else {'x': 'codex', 'c': 'claude', 'k': 'cursor', 'e': 'codex_cloud', 'a': 'antigravity'}.get(chat[0], ''),
                               project_id=item.get('project_id') if HEX.fullmatch(str(item.get('project_id', ''))) else '',
                               project_name=_text(item.get('project_name', '')), live=True, retained=False))
        return result

    @staticmethod
    def _projects(items):
        return [dict(project_id=x['project_id'], project_name=_text(x.get('project_name', '')))
                for x in (items[:1000] if isinstance(items, list) else [])
                if isinstance(x, dict) and HEX.fullmatch(str(x.get('project_id', '')))]

    def _facts(self, items):
        facts = []
        for item in items[:1000] if isinstance(items, list) else []:
            if not isinstance(item, dict) or item.get('field') not in FACT_FIELDS:
                continue
            subject = item.get('subject', item.get('chat_id'))
            if not isinstance(subject, str) or not LIVE_CHAT.fullmatch(subject):
                continue
            value = item.get('value')
            encoded = _json(value)
            if len(encoded) > 1200 or SECRET.search(encoded) or re.search(r'"(?:/|[A-Za-z]:[\\/])', encoded):
                continue
            fact = dict(ref='fact:' + self.boot + ':' + subject + ':' + item['field'], subject=subject,
                        field=item['field'], value=copy.deepcopy(value), source='live', observation_source=_text(item.get('source', 'WATCH observation')),
                        source_ts=item.get('source_ts'), observed_ts=item.get('observed_ts'),
                        qualification=_text(item.get('qualification', 'Recorded WATCH state'), 240),
                        freshness=item.get('freshness') if item.get('freshness') in ('fresh', 'stale', 'unknown', 'observed_now', 'last_recorded') else 'unknown')
            for field in ('source_ts', 'observed_ts'):
                if type(fact[field]) not in (float, int) or not math.isfinite(fact[field]):
                    fact[field] = None
            fact['fingerprint'] = _fingerprint(fact)
            facts.append(fact)
        return facts

    @staticmethod
    def _identity(state, boot):
        return dict(store=state.get('store') or state.get('known_store'),
                    erasure=state.get('erasure') if state.get('erasure') is not None else state.get('committed_erasure'),
                    live_boot=boot)

    def generation(self):
        return self._identity(self.fence,self.boot)

    def current_generation(self):
        return self._identity(self._archive_state(),self.boot)

    def original_observation(self, ref):
        # Typed facts are already bounded snapshot metadata. No event prose is copied.
        fact=next((item for item in self.facts if item['ref']==ref),None)
        if fact is not None:return copy.deepcopy(fact)
        item=self.supplied.get(ref) or self.pending.get(ref)
        return dict(ref=ref,**copy.deepcopy(item)) if item is not None else None

    def original_fingerprints(self):
        return {ref:item.get('fingerprint') for ref,item in self.supplied.items()}

    def assess_refs(self, refs, fingerprints=None, fingerprinter=None):
        """Classify exact source state; routine changes never masquerade as deletion."""
        if not isinstance(refs,list) or len(refs)>3200 or any(not isinstance(ref,str) for ref in refs):
            return dict(status='unavailable',refs={},warnings=['invalid_references'],erasure_changed=False,identity=self.current_generation())
        refs=list(dict.fromkeys(refs))
        hashes=fingerprints if fingerprints is not None else {ref:(self.supplied.get(ref) or self.pending.get(ref) or {}).get('fingerprint') for ref in refs}
        fingerprint=fingerprinter or _fingerprint
        state=self._archive_state();identity=self._identity(state,self.boot);original=self.generation()
        changed=(original['store'] is not None and identity['store']==original['store'] and original['erasure'] is not None
                 and identity['erasure'] is not None and identity['erasure']>original['erasure'])
        if changed:self._erasure_seen=True
        if identity['store']==original['store'] and identity['erasure'] is not None:
            self._erasure_highwater=max(self._erasure_highwater or 0,identity['erasure'])
        assessment={};warnings=[];live_view=None
        retained=[ref for ref in refs if ref.startswith('deck:') and HEX.fullmatch(ref[5:])]
        for at in range(0,len(retained),200):
            chunk=retained[at:at+200]
            try:
                result=self.service.history.evidence_current([ref[5:] for ref in chunk],original['store']) if self.service.history is not None else dict(available=False,records={},missing=[],reason='retained_unavailable')
            except (OSError,ValueError,TypeError,sqlite3.Error):
                result=dict(available=False,records={},missing=[],reason='retained_unavailable')
            seen_identity=result.get('identity',{})
            if seen_identity.get('store')==original['store'] and seen_identity.get('erasure') is not None:
                identity=dict(seen_identity,live_boot=self.boot)
                self._erasure_highwater=max(self._erasure_highwater or 0,seen_identity['erasure'])
                if original['erasure'] is not None and seen_identity['erasure']>original['erasure']:self._erasure_seen=True
            for ref in chunk:
                if not result.get('available'):
                    assessment[ref]=dict(status='unavailable',reason=result.get('reason') or 'retained_unavailable');continue
                record=result.get('records',{}).get(ref[5:])
                if record is None:
                    assessment[ref]=dict(status='removed',reason='retained_record_absent');continue
                record=dict(record,source='retained',ref=ref)
                status='stale' if hashes.get(ref) is not None and fingerprint(record)!=hashes[ref] else 'supported'
                assessment[ref]=dict(status=status,record=record,reason='source_changed' if status=='stale' else '')
        live_refs=[ref for ref in refs if ref.startswith(('live:','fact:'))]
        if live_refs:
            view=live_view=self._live()
            same_boot=str(view.get('boot',''))[:100]==self.boot
            current=self._pending_records(live_refs,view) if same_boot else {}
            for ref in live_refs:
                record=current.get(ref)
                if record is None:
                    assessment[ref]=dict(status='stale',reason='live_boot_changed' if not same_boot else 'live_observation_unavailable');continue
                status='stale' if hashes.get(ref) is not None and fingerprint(record)!=hashes[ref] else 'supported'
                assessment[ref]=dict(status=status,record=record,reason='observation_changed' if status=='stale' else '')
        for ref in refs:
            if ref.startswith('agg:'):
                quantity=self.quantities.get(ref);meta=self.supplied.get(ref) or self.pending.get(ref) or {}
                stale=self._archive_tainted([meta])
                if meta.get('source')=='live' and meta.get('requires_source',True):
                    if live_view is None:live_view=self._live()
                    view=live_view
                    floor=view.get('floor_seq',min((e.get('seq',0) for e in view.get('events',[]) if isinstance(e,dict) and type(e.get('seq')) is int),default=0))
                    stale=bool(stale or str(view.get('boot',''))[:100]!=self.boot or floor>self.live_floor)
                if quantity is None:assessment[ref]=dict(status='unavailable',reason='quantity_not_available')
                else:assessment[ref]=dict(status='stale' if stale else 'supported',reason='population_changed' if stale else '',record=copy.deepcopy(quantity))
            elif ref not in assessment:
                assessment[ref]=dict(status='unavailable',reason='unknown_reference')
        for item in assessment.values():
            if item['status']!='supported':warnings.append(item['reason'])
        if self._archive_tainted(list(self.supplied.values())+[self.pending.get(ref,{}) for ref in refs]):
            warnings.append('archive_erasure_changed')
        status=next((name for name in ('removed','unavailable','stale') if any(item['status']==name for item in assessment.values())),'supported')
        return dict(status=status,refs=assessment,warnings=list(dict.fromkeys(warnings)),erasure_changed=self._erasure_seen,identity=identity)

    def _used_archive(self, metadata):
        return any(item.get('requires_source',True) and (item.get('source')=='retained' or item.get('source')=='catalog' and 'retained' in item.get('sources',[])) for item in metadata)

    def _archive_tainted(self, metadata):
        return any(self._used_archive([item]) and item.get('archive_generation',self.generation()['erasure']) is not None
                   and self._erasure_highwater is not None and self._erasure_highwater>item.get('archive_generation',self.generation()['erasure'])
                   for item in metadata)

    def work_bound(self, name=None, inp=None):
        """Conservative row-work admission bound; SQL also has its own deadline."""
        retained = self.service.history.limits['events'] if self.service.history is not None else 0
        inp = inp if isinstance(inp, dict) else {}
        if name == 'room_snapshot':
            # Catalog metadata scan plus a bounded current-state projection.
            return retained + 61000
        if name == 'evidence_expand':
            refs=inp.get('refs',[])
            if not isinstance(refs,list): return 3*(retained+60000)
            return min(self.service.max_work, sum(3*retained if str(ref).startswith('deck:') else 180000 for ref in refs[:10]))
        sources=inp.get('scope',{}).get('sources',list(SOURCES)) if isinstance(inp.get('scope',{}),dict) else list(SOURCES)
        return 4*((retained if 'retained' in sources else 0)+(60000 if 'live' in sources else 0))

    def _base(self, scope=None, interval=None):
        return dict(snapshot=self.snapshot, scope=scope or _scope(), interval=interval or _interval(),
                    facts=[], evidence=[], quantities=[], population=dict(counts={}, units={}, complete=False),
                    selection=dict(method='none', selected=0, supplied=0, omitted_groups=0, omitted_detail=0, work_rows=0),
                    continuation=dict(cursor=None, status='exhausted'),
                    coverage=dict(sources=[], time_filter='Live records without source time use their retained record timestamp; native timestamp basis is unknown.', capture='Partial captured observations only; not all real activity.',
                                  retention='Deletion and expiry override snapshots.', retrieval='not_evaluated',
                                  supply='not_evaluated', interpretation='not_assessed',
                                  source_cutoffs=dict(live=dict(boot=self.boot, seq=self.live_cutoff),
                                                      retained=dict(store=self.fence.get('store'), ingest=self.fence.get('fence'))),
                                  ingestion_lag=dict(queued_at_snapshot=self.fence.get('queued'), atomic_sources=False)))

    def _check(self):
        if self.service.clock() - self.started > 300:
            self.failure = 'snapshot_expired'
        if self.failure:
            raise ValueError(self.failure)

    def _token(self, identity, position):
        if len(self.cursors) >= 128:
            raise ValueError('continuation_limit')
        token = secrets.token_hex(24)
        self.cursors[token] = (identity, position)
        return token

    def _position(self, token, identity):
        if token is None:
            return None
        if not isinstance(token, str) or token not in self.cursors or self.cursors[token][0] != identity:
            raise ValueError('invalid_or_changed_cursor')
        return self.cursors[token][1]

    def _archive_rows(self, spec, metadata=False, count_work=True):
        if not self.fence.get('available'):
            return [], False, 'retained_unavailable', 0
        query = {k: spec[k] for k in ('scope', 'interval', 'filters')}
        result = self.service.history.evidence_rows(self.fence, query, max_work=max(0,self.service.max_work-self.work) if count_work else 200, metadata_only=metadata)
        if result.get('complete') and result.get('erasure') is not None:self._last_archive_generation=result['erasure']
        if count_work:self.work += result.get('work', 0)
        return result.get('rows', []), result.get('complete', False), result.get('reason', ''), result.get('work', 0)

    def _live_rows(self, spec, count_work=True, view=None):
        view = self._live() if view is None else view
        if str(view.get('boot', ''))[:100] != self.boot:
            return [], False, 'live_boot_changed', 0
        raw = view.get('events', [])
        if not isinstance(raw, list):
            return [], False, 'live_unavailable', 0
        if count_work and self.work + len(raw) > self.service.max_work:
            return [], False, 'query_work_exhausted', 0
        if len(raw) > 60000:
            return [], False, 'live_population_exceeds_limit', 0
        if count_work: self.work += len(raw)
        result = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            seq = item.get('seq')
            if type(seq) is not int or seq > self.live_cutoff:
                continue
            chat = item.get('chat_id', item.get('agent', ''))
            if not isinstance(chat, str) or not LIVE_CHAT.fullmatch(chat):
                continue
            row = {key: item.get(key, '') for key in ('provider', 'chat_name', 'project_id', 'project_name', 'kind', 'role', 'operation', 'operation_id', 'phase', 'outcome', 'certainty')}
            row.update(id=str(seq), ref='live:' + self.boot + ':' + str(seq), source='live', chat_id=chat,
                       source_ts=item.get('source_ts', item.get('ts')), observed_ts=item.get('observed_ts'),
                       time_basis=item.get('time_basis','timestamp_basis_unknown'),
                       timestamp_qualification='live_record_timestamp_basis_unknown',
                       observation_source=_text(item.get('observation_source','process_observation' if chat.startswith('e:') else '')),
                       source_qualification=_text(item.get('source_qualification','Process-list observation only; agent prose and completion are not established.' if chat.startswith('e:') else ''),240),
                       text=str(item.get('text', ''))[:60000], file_ref=_relative(item.get('file_ref', '')))
            if SECRET.search(row['text']):
                row['text'] = '[content omitted]'
            if row['kind'] not in KINDS or row['certainty'] not in FILTERS['certainty']:
                continue
            if not HEX.fullmatch(str(row['project_id'])):
                row['project_id'] = ''
            row['chat_name'], row['project_name'] = _text(row['chat_name']), _text(row['project_name'])
            for field in ('source_ts', 'observed_ts'):
                if type(row[field]) not in (int, float) or not math.isfinite(row[field]):
                    row[field] = None
            if _matches(row, spec):
                result.append(row)
        return result, True, '', len(raw)

    def _read(self, spec):
        rows, sources, work = [], [], 0
        for source in spec['scope']['sources']:
            if source == 'retained':
                found, complete, reason, used = self._archive_rows(spec)
                for row in found:
                    row.update(source='retained', ref='deck:' + row['id'])
                found = [r for r in found if _matches(r, spec)]
            else:
                found, complete, reason, used = self._live_rows(spec)
            rows.extend(found)
            sources.append(dict(source=source, available=reason not in ('retained_unavailable', 'live_unavailable'), complete=complete, reason=reason,
                                matched=len(found) if complete else None))
            work += used
        self._last_coverage = sources
        return rows, sources, work

    @staticmethod
    def _order(row, basis='source'):
        return (_stamp(row,basis) or 0, row['source'], row['id'])

    def _receipt_fingerprint(self, row):
        if not callable(self.receipt_fingerprinter):return None
        value=self.receipt_fingerprinter(row)
        if not isinstance(value,str) or not HEX.fullmatch(value):raise ValueError('reference_fingerprint_unavailable')
        return value

    def _record(self, row, match=None, full=False):
        # Identity is carried once by ref; empty operation fields and internal
        # fingerprints consume model supply without adding evidence.
        keys = ('ref', 'source', 'chat_id', 'chat_name', 'provider', 'project_id', 'project_name',
                'source_ts', 'observed_ts', 'time_basis', 'kind', 'role', 'operation',
                'operation_id', 'phase', 'outcome', 'file_ref', 'certainty', 'redacted', 'observation_source', 'source_qualification')
        value = {key: row[key] for key in keys if key in row and (row[key] not in ('',None,False) or key in ('source_ts','observed_ts'))}
        if row.get('kind') in ('message','say','user','ask','assign'):
            for key in ('operation','operation_id','phase','outcome'):
                value.pop(key,None)
            value['attribution']='owner_message' if row.get('role')=='user' else 'agent_report'
        if row.get('timestamp_qualification'): value['timestamp_qualification'] = row['timestamp_qualification']
        value['text'] = _excerpt(str(row.get('text', '')), match or dict(text='', mode='literal'), 2048 if full else 480)
        value['text_complete'] = value['text'] == str(row.get('text', ''))
        self.pending[row['ref']]=dict(source=row.get('source'),chat_id=row.get('chat_id'),project_id=row.get('project_id'),
                                    source_ts=row.get('source_ts'),observed_ts=row.get('observed_ts'),archive_generation=self._last_archive_generation,snapshot=self.snapshot,fingerprint=_fingerprint(row),receipt_fingerprint=self._receipt_fingerprint(row))
        return value

    def _quantity(self, result, metric, value, unit, complete, source, scope=None, interval=None):
        ref = 'agg:' + self.snapshot + ':' + str(len(self.quantities))
        item = dict(ref=ref, metric=metric, value=value if complete else None, unit=unit,
                    scope=dict(scope or result['scope'], **({'sources':[source]} if source is not None else {})), interval=interval or result['interval'], snapshot=self.snapshot, complete=complete)
        self.quantities[ref] = item
        self.pending[ref] = dict(source=source or 'catalog', sources=item['scope']['sources'], requires_source=bool(complete), archive_generation=self._last_archive_generation, snapshot=self.snapshot, fingerprint=hashlib.sha256(_json(item).encode()).hexdigest())
        result['quantities'].append(item)
        return ref

    def _fit(self, result, candidate_count=None, identity=None, order='asc', limit=20):
        records = result['evidence']
        original = len(records)
        while len(_json(result).encode()) > self.service.max_bytes - 512:
            if records:
                records.pop()
            elif result['facts']:
                result['facts'].pop()
            elif result.get('groups'):
                result['groups'].pop()
                result['selection']['omitted_groups'] += 1
            elif result.get('subjects'):
                result['subjects'].pop()
            elif result.get('projects'):
                result['projects'].pop()
            else:
                raise ValueError('evidence_metadata_exceeds_limit')
        result['selection']['supplied'] = len(records) + len(result['facts'])
        result['selection']['omitted_detail'] += original - len(records)
        if identity and candidate_count is not None and candidate_count > len(records) and records:
            # Cursor anchors the last actually supplied observation, never an omitted row.
            result['continuation'] = dict(cursor=self._token(identity, records[-1]['ref']), status='more')
        for item in records + result['facts']:
            self.pending[item['ref']] = dict(self.pending.get(item['ref'],{}),source=item.get('source'),chat_id=item.get('chat_id',item.get('subject')),snapshot=self.snapshot,fingerprint=item.get('fingerprint') or self.pending.get(item['ref'],{}).get('fingerprint'))
            if item.get('field') in FACT_FIELDS:self.pending[item['ref']]['receipt_fingerprint']=self._receipt_fingerprint(item)
        result['coverage']['supply'] = 'selected_detail_omitted' if result['selection']['omitted_detail'] else 'selected_records_supplied'
        return result

    def tool(self, name, inp):
        try:
            self._check()
            if not isinstance(inp, dict):
                raise ValueError('Invalid evidence request.')
            fn = {'room_snapshot': self.room_snapshot, 'evidence_query': self.query, 'evidence_expand': self.expand}.get(name)
            if fn is None:
                raise ValueError('Unknown evidence capability.')
            return fn(inp)
        except (ValueError, TypeError, OSError, sqlite3.Error) as error:
            result = self._base()
            # Exception details may contain caller data. Return only registered fixed codes.
            code = str(error)
            safe = {'snapshot_expired', 'evidence_removed', 'invalid_or_changed_cursor', 'continuation_limit', 'evidence_metadata_exceeds_limit'}
            result['error'] = code if code in safe else 'Invalid or unavailable evidence request.'
            result['coverage']['retrieval'] = 'unavailable'
            result['continuation']['status'] = 'invalidated' if self.failure else 'unavailable'
            return result

    def room_snapshot(self, inp=None):
        inp = {} if inp is None else inp
        _keys(inp, ('scope', 'fields', 'limit', 'cursor'))
        self._check()
        scope, limit = _scope(inp.get('scope')), _integer(inp.get('limit', 20), 1, 50)
        fields = inp.get('fields', [])
        if not isinstance(fields, list) or not set(fields) <= set(FACT_FIELDS):
            raise ValueError('Invalid current-fact field.')
        spec = dict(scope=scope, interval=_interval(), filters={}, match=dict(text='', mode='literal'))
        rows, complete, reason, work = self._archive_rows(spec, True) if 'retained' in scope['sources'] else ([], True, '', 0)
        subjects = {}
        for row in rows:
            key = (row['chat_id'], row['project_id'])
            item = subjects.setdefault(key, dict(chat_id=row['chat_id'], provider=row['provider'], chat_name=row['chat_name'],
                                                project_id=row['project_id'], project_name=row['project_name'], live=False, retained=True, aliases=[]))
            if row['chat_name'] and row['chat_name'] not in item['aliases']:
                item['aliases'].append(row['chat_name'])
        for row in self.subjects if 'live' in scope['sources'] else []:
            if scope['chat_ids'] and row['chat_id'] not in scope['chat_ids'] or scope['project_ids'] and row['project_id'] not in scope['project_ids']:
                continue
            key = (row['chat_id'], row['project_id'])
            if key in subjects:
                subjects[key].update(live=True, chat_name=row['chat_name'])
            else:
                subjects[key] = dict(row, aliases=[row['chat_name']] if row['chat_name'] else [])
        ordered = sorted(subjects.values(), key=lambda x: (x['chat_id'], x['project_id']))
        identity = 'catalog:' + _json(dict(scope=scope, fields=fields, limit=limit))
        position = self._position(inp.get('cursor'), identity)
        if position is not None:
            ordered = [x for x in ordered if (x['chat_id'], x['project_id']) > tuple(position)]
        result = self._base(scope)
        result['subjects'] = ordered[:limit]
        result['projects'] = sorted({x['project_id']: dict(project_id=x['project_id'], project_name=x['project_name']) for x in result['subjects'] if x['project_id']}.values(), key=lambda x: x['project_id'])
        selected_ids = {x['chat_id'] for x in result['subjects'] if x.get('live') and any(live['chat_id']==x['chat_id'] and live['project_id']==x['project_id'] for live in self.subjects)} if 'live' in scope['sources'] else set()
        result['facts'] = [dict(f) for f in self.facts if f['field'] in fields and f['subject'] in selected_ids]
        result['population'] = dict(counts=dict(subjects=len(subjects) if complete else None), units=dict(subjects='chat_project_identities'), complete=complete)
        self._quantity(result,'subjects',len(subjects),'chat_project_identities',complete,None)
        if 'live' in scope['sources']:
            live_chats=len({x['chat_id'] for x in subjects.values() if x.get('live')})
            self._quantity(result,'live_chats',live_chats,'current_live_chats',True,'live')
        result['selection'].update(method='stable_identity_catalog', selected=len(result['subjects']), omitted_detail=max(0, len(ordered)-limit), work_rows=work)
        result['coverage']['sources'] = [dict(source='retained', available=self.fence.get('available'), complete=complete, reason=reason)]
        result['coverage']['retrieval'] = 'complete_retained_catalog' if complete else 'partial_catalog'
        self._fit(result)
        if len(ordered) > len(result['subjects']) and result['subjects']:
            last = result['subjects'][-1]
            result['continuation'] = dict(cursor=self._token(identity, [last['chat_id'], last['project_id']]), status='more')
        return result

    @staticmethod
    def _view_identity(row):
        return (row.get('source',''),row.get('chat_id',''),row.get('project_id',''))

    def _frontier_key(self, spec):
        normalized=copy.deepcopy(spec)
        for field in ('since','until'):
            if normalized['interval'][field] is not None: normalized['interval'][field]=float(normalized['interval'][field])
        match=normalized['match']
        text=match['text'].casefold()
        normalized['match']=(dict(mode='terms',terms=sorted(set(re.findall(r'\w+',text))))
                             if match['mode']=='terms' else dict(mode='literal',text=text if text.strip() else ''))
        return 'frontier:'+hashlib.sha256(_json(normalized).encode()).hexdigest()

    def _frontier(self, result, key, matching, complete, spec):
        state=self.frontiers.get(key)
        if state is None and len(self.frontiers)<64:
            state=dict(scope=copy.deepcopy(spec['scope']),interval=dict(spec['interval']),
                       supplied=set(),tracking_complete=True)
            self.frontiers[key]=state
        previous=state['supplied'] if state is not None else set()
        current={self._view_identity(row) for row in result['evidence']}
        known=previous|current
        absent=matching-known
        population_complete=bool(complete and state is not None and state['tracking_complete'])
        digest=hashlib.sha256(_json(sorted(matching)).encode()).hexdigest()
        if state is not None:
            state.update(matching_views=len(matching) if complete else None,population_complete=population_complete,
                         population_digest=digest if complete else None)
        result['coverage']['frontier']=dict(
            query_ref=key,matching_views=len(matching) if complete else None,
            previously_supplied_views=len(previous),in_this_result_views=len(current),
            unrepresented_views=len(absent) if population_complete else None,
            unrepresented_ids=[dict(source=s,chat_id=c,project_id=p) for s,c,p in sorted(absent)[:8]],
            population_complete=population_complete,
            qualification='Inventory/supply frontier only; represented subjects do not establish semantic completeness.')
        if not population_complete:
            result['coverage']['frontier']['reason']='population_or_supply_tracking_incomplete'

    def query(self, inp):
        _keys(inp, ('scope', 'interval', 'match', 'filters', 'view', 'group_by', 'order', 'limit', 'cursor'))
        self._check()
        spec = _spec(inp, self.observed_at)
        view, group, order = inp.get('view', 'records'), inp.get('group_by', 'chat'), inp.get('order', 'asc')
        limit = _integer(inp.get('limit', 20), 1, 50)
        if view not in ('records', 'aggregates', 'overview') or group not in GROUPS or order not in ('asc', 'desc'):
            raise ValueError('Unsupported evidence view.')
        identity = 'query:' + _json(dict(spec=spec, view=view, group=group, order=order, limit=limit))
        position = self._position(inp.get('cursor'), identity)
        rows, sources, work = self._read(spec)
        if self.failure:
            raise ValueError(self.failure)
        result = self._base(spec['scope'], spec['interval'])
        result['coverage'].update(sources=sources, retrieval='complete_retained_match' if all(x['complete'] for x in sources) else 'partial_population')
        result['selection']['work_rows'] = work
        counts = {}
        metric_completeness = {}
        metric_qualifications = {}
        quantity_scope = dict(spec['scope'])
        if spec['filters']: quantity_scope['filters'] = copy.deepcopy(spec['filters'])
        if spec['match']['text'].strip():
            quantity_scope['match'] = dict(mode=spec['match']['mode'],digest=hashlib.sha256(_json(spec['match']).encode()).hexdigest())
        for source in spec['scope']['sources']:
            selected = [r for r in rows if r['source'] == source]
            complete = next(x['complete'] for x in sources if x['source'] == source)
            operation_rows=[r for r in selected if r.get('kind') in OPERATION_KINDS
                            or r.get('operation') or r.get('phase') in ('started','result')]
            missing_operation_identity=any(not isinstance(r.get('operation_id'),str) or not r['operation_id'].strip() for r in operation_rows)
            metrics = dict(events=len(selected), operations=len({(r['chat_id'], r['operation_id']) for r in operation_rows if isinstance(r.get('operation_id'),str) and r['operation_id'].strip()}),
                           operation_results=sum(r.get('phase') == 'result' for r in selected), chats=len({r['chat_id'] for r in selected}))
            if view != 'aggregates': metrics={k:metrics[k] for k in ('events','chats')}
            metric_completeness[source]={metric:bool(complete and (metric!='operations' or not missing_operation_identity)) for metric in metrics}
            if missing_operation_identity and view=='aggregates':
                metric_qualifications[source]={'operations':'Some matching operation observations lack stable identities; a distinct-operation count is unknown.'}
            counts[source] = {metric:value if metric_completeness[source][metric] else None for metric,value in metrics.items()}
            for metric, value in metrics.items():
                self._quantity(result, metric, value, {'events': 'recorded_events', 'operations': 'distinct_observed_operations', 'operation_results': 'explicit_result_observations', 'chats': 'recorded_chats'}[metric], metric_completeness[source][metric], source, quantity_scope)
        units=dict(events='recorded_events',chats='recorded_chats')
        if view=='aggregates': units.update(operations='distinct_observed_operations',operation_results='explicit_result_observations')
        result['population'] = dict(counts=counts, units=units, complete=all(s['complete'] for s in sources), deduplication='Sources counted separately; no inferred deduplication.')
        if view=='aggregates':
            result['population']['metric_completeness']=metric_completeness
            if metric_qualifications: result['population']['metric_qualifications']=metric_qualifications
        population_ref='population:'+self.snapshot+':'+hashlib.sha256(identity.encode()).hexdigest()[:16]
        result['population']['ref']=population_ref
        prior=self.populations.get(identity)
        if position is not None and prior and prior['counts']==counts and prior['complete']==result['population']['complete']:
            refs=prior['quantity_refs']
            # Do not rely on a previous page that was never admitted to the model.
            if all(ref in self.supplied for ref in refs):
                result['quantities']=[]
                result['population']=dict(ref=population_ref,unchanged=True,quantity_refs=refs,complete=prior['complete'])
        else:
            self.populations[identity]=dict(counts=copy.deepcopy(counts),complete=result['population']['complete'],quantity_refs=[q['ref'] for q in result['quantities']])
        ordered = sorted(rows, key=lambda r: self._order(r, spec['interval']['time_basis']), reverse=order == 'desc')
        groups = collections.defaultdict(list)
        for row in ordered:
            if group == 'day':
                stamp = _stamp(row,spec['interval']['time_basis'])
                key = time.strftime('%Y-%m-%d', time.localtime(stamp)) if stamp else 'unknown_time'
            else:
                key = str(row.get({'chat': 'chat_id', 'project': 'project_id'}.get(group, group), ''))
            groups[(row['source'], key)].append(row)
        group_rows=[]
        group_quantity_limit=min(16,max(0,(self.service.max_bytes-6000)//700))
        registered_groups=0
        for (source,key),items in sorted(groups.items()) if view=='aggregates' else []:
            item=dict(source=source,key=key,events=len(items))
            group_scope=copy.deepcopy(quantity_scope)
            group_interval=dict(spec['interval'])
            expressible=True
            if group in ('chat','project'):
                if not key: expressible=False
                else: group_scope['chat_ids' if group=='chat' else 'project_ids']=[key]
            elif group=='day':
                if key=='unknown_time': expressible=False
                else:
                    parsed=time.strptime(key,'%Y-%m-%d')
                    start=time.mktime((parsed.tm_year,parsed.tm_mon,parsed.tm_mday,0,0,0,0,0,-1))
                    following=time.localtime(start+36*3600)
                    end=time.mktime((following.tm_year,following.tm_mon,following.tm_mday,0,0,0,0,0,-1))
                    group_interval['since']=max(start,group_interval['since']) if group_interval['since'] is not None else start
                    group_interval['until']=min(end,group_interval['until']) if group_interval['until'] is not None else end
            else:
                if key not in FILTERS[group]: expressible=False
                else: group_scope.setdefault('filters',{})[group]=[key]
            if expressible and registered_groups<group_quantity_limit:
                complete=next(s['complete'] for s in sources if s['source']==source)
                item['quantity_ref']=self._quantity(result,'events',len(items),'recorded_events',complete,source,group_scope,group_interval)
                registered_groups+=1
            group_rows.append(item)
        if view=='aggregates': result['groups']=group_rows[:50]
        result['selection']['omitted_group_quantities']=max(0,len(groups)-registered_groups) if view=='aggregates' else 0
        result['selection']['omitted_groups'] = max(0, len(groups)-50) if view=='aggregates' else 0
        if view == 'records':
            if position is not None:
                at = next((i for i, r in enumerate(ordered) if r['ref'] == position), None)
                if at is None:
                    raise ValueError('invalid_or_changed_cursor')
                ordered = ordered[at+1:]
            chosen = ordered[:limit]
            method = 'chronological_matching_records'
        elif view == 'overview':
            # Separate prose from structural observations, then interleave subjects.
            # A thousand operation rows cannot crowd a handful of substantive messages.
            strata = collections.defaultdict(list)
            for row in ordered:
                stamp = _stamp(row,spec['interval']['time_basis']) or 0
                channel = ('prose' if row.get('kind') in ('message','say','user','ask','assign')
                           else 'results' if row.get('phase') == 'result'
                           else 'changes' if row.get('operation') in ('write','edit','patch','delete')
                           else 'operations')
                strata[(row['source'], row['chat_id'], int(stamp//86400), channel)].append(row)
            candidates = []
            seen = set()
            for index in ('middle', -1, 0):
                for channel in ('prose','results','changes','operations'):
                    for key in sorted(k for k in strata if k[3] == channel):
                        items = strata[key]
                        row = items[len(items)//2 if index == 'middle' else index]
                        if row['ref'] not in seen:
                            candidates.append(row); seen.add(row['ref'])
            if position is not None:
                at = next((i for i,r in enumerate(candidates) if r['ref'] == position), None)
                if at is None: raise ValueError('invalid_or_changed_cursor')
                candidates = candidates[at+1:]
            ordered, chosen = candidates, candidates[:limit]
            method = 'chat_time_kind_stratified; prose/results prioritized; middle_latest_first; semantic importance is not guaranteed'
            result['selection']['detail_strata'] = len(strata)
        else:
            if position is not None:
                raise ValueError('Aggregates do not accept record continuation.')
            chosen, ordered, method = [], [], 'full_matching_population_counts'
        result['evidence'] = [self._record(row, spec['match']) for row in chosen]
        frontier_key=self._frontier_key(spec)
        matching_views={self._view_identity(row) for row in rows}
        population_complete=all(source['complete'] for source in sources)
        result['selection'].update(method=method, selected=len(chosen), omitted_detail=max(0, len(rows)-len(chosen)))
        self.queries.append(dict(scope=spec['scope'], interval=spec['interval'], view=view))
        if len(self.queries) > 64: raise ValueError('Request query limit reached.')
        # Frontier size depends on what fitting removes. Fit without minting a
        # cursor, recompute from actually returned rows, then mint one final cursor.
        while True:
            self._frontier(result,frontier_key,matching_views,population_complete,spec)
            result=self._fit(result)
            self._frontier(result,frontier_key,matching_views,population_complete,spec)
            if len(_json(result).encode())<=self.service.max_bytes-512:
                break
        if view!='aggregates' and len(ordered)>len(result['evidence']) and result['evidence']:
            result['continuation']=dict(cursor=self._token(identity,result['evidence'][-1]['ref']),status='more')
        if view == 'overview':
            represented={(r['source'],r['chat_id'],int((_stamp(r,spec['interval']['time_basis']) or 0)//86400)) for r in result['evidence']}
            result['selection']['represented_groups']=len(represented)
            result['selection']['omitted_groups']=max(0,len({k[:3] for k in strata})-len(represented))
        return result

    def resolve(self, ref):
        self._check()
        if not isinstance(ref, str):
            return None
        if ref.startswith('deck:') and HEX.fullmatch(ref[5:]) and self.fence.get('available'):
            spec = dict(scope=_scope(dict(sources=['retained'])), interval=_interval(), filters={'id': [ref[5:]]})
            rows, _, _, _ = self._archive_rows(spec,count_work=False)
            if rows:
                return dict(rows[0], source='retained', ref=ref)
        elif ref.startswith('live:' + self.boot + ':'):
            spec = dict(scope=_scope(dict(sources=['live'])), interval=_interval(), filters={}, match=dict(text='',mode='literal'))
            rows, _, _, _ = self._live_rows(spec, count_work=False)
            return next((r for r in rows if r['ref'] == ref), None)
        elif ref.startswith('fact:' + self.boot + ':'):
            current = self._facts(self._live().get('facts', []))
            return next((dict(f) for f in current if f['ref'] == ref), None)
        return None

    def expand(self, inp):
        _keys(inp, ('refs','mode','before','after'))
        self._check()
        refs, mode = inp.get('refs'), inp.get('mode','context')
        if not isinstance(refs,list) or not 1<=len(refs)<=10 or any(not isinstance(x,str) or len(x)>240 for x in refs) or mode not in ('context','operation'):
            raise ValueError('Invalid evidence expansion.')
        before, after = _integer(inp.get('before',2),0,10), _integer(inp.get('after',2),0,10)
        result = self._base()
        result['relationships'], result['unavailable_refs'] = [], []
        chosen = {}
        for ref in refs:
            anchor = self.resolve(ref)
            if anchor is None or anchor.get('source') not in SOURCES:
                result['unavailable_refs'].append(ref); continue
            scope = _scope(dict(sources=[anchor['source']], chat_ids=[anchor['chat_id']]))
            spec = dict(scope=scope, interval=_interval(), filters={}, match=dict(text='',mode='literal'))
            if mode == 'operation':
                operation = anchor.get('operation_id')
                if not operation:
                    result['relationships'].append(dict(ref=ref,status='operation_identity_unavailable'));continue
                spec['filters']['operation_id'] = [operation]
            rows, sources, work = self._read(spec)
            result['coverage']['sources'].extend(sources)
            result['selection']['work_rows'] += work
            rows = sorted(rows,key=self._order)
            if mode == 'context':
                at=next((i for i,r in enumerate(rows) if r['ref']==ref),None)
                connected=rows[max(0,at-before):at+after+1] if at is not None else []
            else:
                connected=rows if len(rows)<=50 else rows[:24]+[anchor]+rows[-25:]
                connected=list({r['ref']:r for r in connected}.values())
            for row in connected: chosen[row['ref']]=row
            result['relationships'].append(dict(ref=ref,mode=mode,refs=[r['ref'] for r in connected],
                result_observed=any(r.get('phase')=='result' for r in connected),
                complete=all(s['complete'] for s in sources) and (mode!='operation' or len(rows)<=50)))
        if self.failure: raise ValueError(self.failure)
        result['evidence']=[self._record(r,full=True) for r in chosen.values()]
        result['selection'].update(method='retained_context' if mode=='context' else 'explicit_operation_identity',selected=len(chosen))
        result['coverage']['retrieval']='connected_evidence_only; omitted original content cannot be recovered'
        return self._fit(result)

    def _pending_records(self, refs, live=None):
        """Batch live validation so a page does not re-project the ring per row."""
        wanted=set(refs);resolved={}
        if any(ref.startswith(('live:','fact:')) for ref in wanted):
            live=self._live() if live is None else live
            if str(live.get('boot',''))[:100]==self.boot:
                if any(ref.startswith('live:') for ref in wanted):
                    spec=dict(scope=_scope(dict(sources=['live'])),interval=_interval(),filters={},match=dict(text='',mode='literal'))
                    rows,complete,_,_=self._live_rows(spec,count_work=False,view=live)
                    if complete:resolved.update({row['ref']:row for row in rows if row['ref'] in wanted})
                if any(ref.startswith('fact:') for ref in wanted):
                    resolved.update({row['ref']:row for row in self._facts(live.get('facts',[])) if row['ref'] in wanted})
        for ref in wanted:
            if ref.startswith('deck:'):
                row=self.resolve(ref)
                if row is not None:resolved[ref]=row
        return resolved

    def prepare_pending_refs(self, refs):
        """Stage legacy reference fingerprints without promoting any supplied state."""
        if not isinstance(refs,list) or len(refs)>200 or any(not isinstance(ref,str) for ref in refs):
            return dict(valid=False,reason='invalid_pending_references')
        try:
            resolved=self._pending_records([ref for ref in refs if ref not in self.pending])
            for ref in dict.fromkeys(refs):
                if ref in self.pending:continue
                row=resolved.get(ref)
                if row is None:return dict(valid=False,reason='pending_source_unavailable')
                self.pending[ref]=dict(source=row.get('source','fact'),chat_id=row.get('chat_id',row.get('subject')),
                                       fingerprint=_fingerprint(row),snapshot=self.snapshot,archive_generation=self.generation()['erasure'],requires_source=True,receipt_fingerprint=self._receipt_fingerprint(row))
        except (ValueError,TypeError,OSError,sqlite3.Error):
            return dict(valid=False,reason='pending_source_unavailable')
        return dict(valid=True,reason='')

    def validate_pending(self, results):
        """Pure supply check: qualify movement, but never transmit removed source content."""
        if not isinstance(results,list) or len(results)>64:return dict(valid=False,reason='invalid_pending_results')
        items=[]
        for result in results:
            if not isinstance(result,dict) or result.get('snapshot',self.snapshot)!=self.snapshot:return dict(valid=False,reason='invalid_pending_results')
            for field in ('evidence','facts','quantities'):
                values=result.get(field,[])
                if not isinstance(values,list):return dict(valid=False,reason='invalid_pending_results')
                items.extend(values)
        if len(items)>3200 or any(not isinstance(item,dict) or not isinstance(item.get('ref'),str) for item in items):
            return dict(valid=False,reason='invalid_pending_references')
        for item in items:
            ref=item['ref']
            if ref.startswith('agg:') and (ref not in self.quantities or hashlib.sha256(_json(item).encode()).hexdigest()!=self.pending.get(ref,{}).get('fingerprint')):
                return dict(valid=False,reason='pending_quantity_changed')
        assessment=self.assess_refs([item['ref'] for item in items])
        removed=any(value['status']=='removed' for value in assessment['refs'].values())
        tainted=self._archive_tainted(list(self.supplied.values())+[self.pending.get(item['ref'],{}) for item in items])
        needs_fallback=bool(removed or tainted)
        return dict(valid=not needs_fallback,reason='archive_input_removed' if needs_fallback else '',needs_fallback=needs_fallback,assessment=assessment,warnings=assessment['warnings'])

    def track_refs(self, refs):
        if not isinstance(refs,list) or len(refs)>200:
            return False
        missing=[]
        for ref in dict.fromkeys(refs):
            if ref in self.supplied: continue
            if ref in self.pending:
                self.supplied[ref]=dict(self.pending[ref]);continue
            row=self.resolve(ref)
            if row is None:
                missing.append(ref);continue
            self.supplied[ref]=dict(source=row.get('source','fact'),chat_id=row.get('chat_id',row.get('subject')),
                                    fingerprint=_fingerprint(row),snapshot=self.snapshot,receipt_fingerprint=self._receipt_fingerprint(row))
        if missing:
            self.failure='supplied_evidence_unavailable'
        return not missing

    def mark_supplied(self, result):
        if not isinstance(result,dict): return
        refs=[item['ref'] for item in result.get('evidence',[])+result.get('facts',[])+result.get('quantities',[])
              if isinstance(item,dict) and isinstance(item.get('ref'),str)]
        self.track_refs(refs)
        frontier=result.get('coverage',{}).get('frontier',{})
        key=frontier.get('query_ref') if isinstance(frontier,dict) else None
        state=self.frontiers.get(key)
        if state is not None and result.get('snapshot')==self.snapshot:
            for row in result.get('evidence',[]):
                if not isinstance(row,dict) or row.get('ref') not in self.supplied: continue
                view=self._view_identity(row)
                if view not in state['supplied']:
                    if self._frontier_supplied_count>=50000:
                        state['tracking_complete']=False
                        state['population_complete']=False
                        break
                    state['supplied'].add(view)
                    self._frontier_supplied_count+=1

    def validate(self):
        assessment=self.assess_refs(list(self.supplied))
        removed=any(item['status']=='removed' for item in assessment['refs'].values())
        needs_fallback=bool(removed or self._archive_tainted(self.supplied.values()))
        return dict(valid=not needs_fallback,reason='archive_input_removed' if needs_fallback else '',needs_fallback=needs_fallback,
                    assessment=assessment,warnings=assessment['warnings'])

    def archive_input_generation(self, refs=None, include_pending=False):
        metadata=list(self.supplied.values()) if refs is None else [self.supplied.get(ref) or self.pending.get(ref) or {} for ref in refs]
        if include_pending:metadata+=list(self.pending.values())
        return min((item.get('archive_generation',self.generation()['erasure']) for item in metadata
                    if self._used_archive([item]) and item.get('archive_generation',self.generation()['erasure']) is not None),default=None)

    def receipt(self):
        return dict(snapshot=self.snapshot,generation=self.generation(),observed_at=self.observed_at,refs=sorted(self.supplied),
                    archive_input_generation=self.archive_input_generation(),archive_used=self._used_archive(self.supplied.values()),
                    reference_fingerprints={ref:meta.get('fingerprint') for ref,meta in self.supplied.items()},
                    live_cutoff=self.live_cutoff,live_floor=self.live_floor,archive_fence=self.fence.get('fence'),
                    queries=copy.deepcopy(self.queries),work_rows=self.work,
                    frontiers=[dict(query_ref=key,scope=copy.deepcopy(state['scope']),interval=dict(state['interval']),
                                    matching_views=state.get('matching_views'),supplied_views=len(state['supplied']),
                                    unrepresented_views=max(0,state['matching_views']-len(state['supplied'])) if state.get('population_complete') and state.get('matching_views') is not None else None,
                                    population_complete=state.get('population_complete',False),population_digest=state.get('population_digest'),
                                    supplied_digest=hashlib.sha256(_json(sorted(state['supplied'])).encode()).hexdigest())
                               for key,state in self.frontiers.items()],
                    qualification='References describe supplied captured evidence, not complete real-world activity.')
