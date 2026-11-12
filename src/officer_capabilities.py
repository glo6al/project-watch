"""Officer integration primitives. No I/O, authority or persistent state on import.

The adapter intentionally projects the same content classes as retained memory.
Receipts authenticate provenance for replay; they do not certify model meaning.
"""
import copy
import hashlib
import hmac
import json
import math
import os
import secrets
import time

from deck_log import _prose, _relative, _text


ANSWER_CHARS = 12000
RECEIPT_KEY = secrets.token_bytes(32)
READ_NAMES = frozenset(('room_snapshot', 'evidence_query', 'evidence_expand'))
SCOPE = {'type': 'object', 'properties': {
    'sources': {'type': 'array', 'items': {'type': 'string', 'enum': ['live', 'retained']}, 'maxItems': 2},
    'chat_ids': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 100},
    'project_ids': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 100}}, 'additionalProperties': False}
INTERVAL = {'type': 'object', 'properties': {
    'since': {'type': ['number', 'null']}, 'until': {'type': ['number', 'null']},
    'day': {'type': 'string', 'description': 'One local calendar day, YYYY-MM-DD. Python converts boundaries; do not calculate Unix seconds.'},
    'start_day': {'type': 'string', 'description': 'First included local calendar day, YYYY-MM-DD; use with end_day.'},
    'end_day': {'type': 'string', 'description': 'Last included local calendar day, YYYY-MM-DD; use with start_day.'},
    'minutes': {'type': 'integer', 'minimum': 1, 'maximum': 525600, 'description': 'Minutes before this request snapshot. Do not mix with calendar or numeric bounds.'},
    'time_basis': {'type': 'string', 'enum': ['source', 'observed']}}, 'additionalProperties': False}


def _tool(name, description, properties, required=()):
    return {'name': name, 'description': description, 'input_schema': {
        'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}}


class EvidenceAliases:
    """Exact exchange-local model handles; never persisted as source identities."""
    def __init__(self):
        self.forward, self.reverse = {}, {}

    def alias(self, ref):
        if not isinstance(ref, str) or not ref.startswith(('deck:', 'live:', 'fact:')):
            return ref
        if ref not in self.forward:
            if len(self.forward) >= 9999:
                raise ValueError('Evidence handle limit reached.')
            value = 'ev:' + str(len(self.forward) + 1)
            self.forward[ref], self.reverse[value] = value, ref
        return self.forward[ref]

    def canonical(self, ref):
        if isinstance(ref, str) and ref.startswith('ev:'):
            if ref not in self.reverse:
                raise ValueError('Unknown evidence handle.')
            return self.reverse[ref]
        return ref

    def present(self, value):
        out = copy.deepcopy(value)
        for row in out.get('evidence', []) + out.get('facts', []):
            if isinstance(row, dict) and 'ref' in row:
                row['ref'] = self.alias(row['ref'])
        for relation in out.get('relationships', []):
            if isinstance(relation, dict):
                if 'ref' in relation:
                    relation['ref'] = self.alias(relation['ref'])
                if isinstance(relation.get('refs'), list):
                    relation['refs'] = [self.alias(ref) for ref in relation['refs']]
        return out

    def answer(self, value):
        out = copy.deepcopy(value)
        out['references'] = [self.canonical(ref) for ref in value['references']]
        return out


EVIDENCE_TOOLS = [
    _tool('room_snapshot', 'Discover live AND retained chats/projects by stable IDs; inspect already-known qualified current facts. Names can be ambiguous. Read-only; grants no messaging authority.', {
        'scope': SCOPE, 'fields': {'type': 'array', 'items': {'type': 'string', 'enum': ['model', 'context', 'branch', 'parent', 'last_activity', 'owner_request']}, 'maxItems': 6},
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50}, 'cursor': {'type': 'string'}}),
    _tool('evidence_query', 'Query a snapshot of saved/live observations. Scope precedes text matching. Records give centered excerpts; aggregates count the full matching retained population; overview balances chat/time groups. Sources have separate counts, not a deduplicated total. Follow continuation/omissions. Unsupported semantic metrics are unknown.', {
        'scope': SCOPE, 'interval': INTERVAL,
        'match': {'type': 'object', 'properties': {'text': {'type': 'string', 'maxLength': 256}, 'mode': {'type': 'string', 'enum': ['literal', 'terms']}}, 'additionalProperties': False},
        'filters': {'type': 'object', 'properties': {key: {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 20} for key in ('provider', 'kind', 'role', 'operation', 'phase', 'outcome', 'certainty', 'file_ref')}, 'additionalProperties': False},
        'view': {'type': 'string', 'enum': ['records', 'aggregates', 'overview']},
        'group_by': {'type': 'string', 'enum': ['chat', 'project', 'day', 'operation', 'outcome', 'certainty']},
        'order': {'type': 'string', 'enum': ['asc', 'desc']}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50}, 'cursor': {'type': 'string'}}),
    _tool('evidence_expand', 'Expand retained context or an operation lifecycle from supplied references. Never opens original logs or command output. Preserve each observation certainty and distinguish operation result from goal completion.', {
        'refs': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 10},
        'mode': {'type': 'string', 'enum': ['context', 'operation']},
        'before': {'type': 'integer', 'minimum': 0, 'maximum': 10}, 'after': {'type': 'integer', 'minimum': 0, 'maximum': 10}}, ('refs',)),
    _tool('finish_answer', 'Publish a supported answer after inspecting evidence. Required after any shared evidence tool. References must have appeared in a tool result in an EARLIER model call, never a same-batch read. Use exact registered quantity assertions and {{quantity:REF}} placeholders for count/coverage fragments; never relabel event counts as completed goals.', {
        'text': {'type': 'string', 'maxLength': ANSWER_CHARS},
        'references': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 100},
        'assertions': {'type': 'array', 'maxItems': 50, 'items': {'type': 'object', 'properties': {
            'ref': {'type': 'string'}, 'metric': {'type': 'string'}, 'value': {'type': ['number', 'null']}, 'unit': {'type': 'string'},
            'scope': {'type': 'object'}, 'interval': {'type': 'object'}, 'snapshot': {'type': 'string'}, 'complete': {'type': 'boolean'}},
            'required': ['ref', 'metric', 'value', 'unit', 'scope', 'interval', 'snapshot', 'complete'], 'additionalProperties': False}}}, ('text', 'references', 'assertions')),
]

# Native structured output keeps finalization reliable on models which cannot
# force a tool call. No source names/IDs or owner text enter this cached schema.
# https://platform.claude.com/docs/en/build-with-claude/structured-outputs
ANSWER_SCHEMA = {
    'type': 'object', 'properties': {
        'text': {'type': 'string'},
        'references': {'type': 'array', 'items': {'type': 'string'}},
        'assertions': {'type': 'array', 'items': {
            'type': 'object', 'properties': {
                'ref': {'type': 'string'}, 'metric': {'type': 'string'},
                'value': {'type': ['number', 'null']}, 'unit': {'type': 'string'},
                'scope': {'type': 'object', 'properties': {
                    'sources': {'type': 'array', 'items': {'type': 'string'}},
                    'chat_ids': {'type': 'array', 'items': {'type': 'string'}},
                    'project_ids': {'type': 'array', 'items': {'type': 'string'}},
                    'filters': {'type': 'object', 'properties': {
                        k: {'type': 'array', 'items': {'type': 'string'}}
                        for k in ('provider', 'kind', 'role', 'operation', 'phase', 'outcome', 'certainty', 'file_ref')},
                        'additionalProperties': False},
                    'match': {'type': 'object', 'properties': {
                        'mode': {'type': 'string'}, 'digest': {'type': 'string'}},
                        'required': ['mode', 'digest'], 'additionalProperties': False}},
                    'required': ['sources', 'chat_ids', 'project_ids'], 'additionalProperties': False},
                'interval': {'type': 'object', 'properties': {
                    'since': {'type': ['number', 'null']}, 'until': {'type': ['number', 'null']},
                    'time_basis': {'type': 'string'}},
                    'required': ['since', 'until', 'time_basis'], 'additionalProperties': False},
                'snapshot': {'type': 'string'}, 'complete': {'type': 'boolean'}},
            'required': ['ref', 'metric', 'value', 'unit', 'scope', 'interval', 'snapshot', 'complete'],
            'additionalProperties': False}}},
    'required': ['text', 'references', 'assertions'], 'additionalProperties': False}


def parse_answer(text):
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError('Duplicate answer field.')
            out[key] = value
        return out
    if not isinstance(text, str) or len(text) > 60000:
        raise ValueError('Invalid answer size.')
    return json.loads(text, object_pairs_hook=unique,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Invalid answer number.')))

EVIDENCE_SYSTEM = """
Shared evidence workflow (read-only, subordinate to the authority rules):
Answer the latest owner question only. Earlier turns resolve references and context; they are not outstanding work
orders. A focused follow-up needs a focused answer, not another whole-room report unless the owner asks for one.
Use room_snapshot to resolve live and retained identities/current observations. For an explicit room-wide question,
do not restrict to the ambient station. For a known subject a direct evidence_query is sufficient. Query the whole
requested interval, using local_clock for calendar dates, with overview for broad synthesis and aggregates for counts.
For today/yesterday use interval.day with the matching ISO local_clock date. For calendar ranges use start_day and
end_day (inclusive). For a recent period use minutes. Python computes timestamps. Never leave interval unbounded
when the owner requested a particular day or period. Unbounded counts cannot answer a bounded-day question.
Keep live and retained totals separate unless the result explicitly verifies deduplication. The catalog is not proof
of semantic coverage. Inspect selected prose and operation context; expand missing context or follow continuation
when useful. One returned record per chat does not mean all important work was represented. Available relevant
evidence should be used, not replaced by an unnecessarily cautious unknown. Never infer purpose, causation, missing
output, inactivity, successful deployment or goal completion from generic operation results.
Selected excerpts cannot prove that nobody is waiting, no concern exists, or nothing else happened. State a negative
finding only when a complete appropriate structured result establishes that exact fact; otherwise say it is unknown.
Report concerns and dependencies only when an observation actually states them. Sharing a project or period does
not justify speculating that two changes affect each other. Preserve agent-report attribution when grouping reports.
Ordinary unfinished review is not a task waiting on the owner. Only an explicit recorded owner question/decision
or held permission request belongs under waiting on you. Do not call a sampled change the only real change.
After using ANY shared evidence tool, finish with the required structured JSON answer containing text, supporting
references, and assertions, or call finish_answer with the same fields. Do not end with unchecked plain prose.
Read a result in an earlier round before citing it. Material qualitative findings
need supplied supporting references and preserve probably/inferred/source/observation-time qualifications.
Use exact short ev: handles from evidence/facts as source references. They are local to this question; never guess a
hash or reuse an old handle. Python keeps stable source identity. Relationship-only handles do not establish that
omitted messages were read. Quantity assertions still copy their complete registered fields.
Prefer useful findings over raw entry counts. A work summary normally needs decisions, progress and open matters;
do not include numerical event/inventory totals unless the owner asks for those totals. Those counts are not progress.
For a cross-chat overview, read its continuation to cover unrepresented chat/time groups before spending the allowance
on chronological prefixes of a busy chat. Prefer expanding a relevant reference to browsing arbitrary first entries.
Use selection and continuation metadata to track this frontier. Never describe unread chats as if their statements
were inspected, even if their counts or names are known. If some remain unrepresented, name that limit in the answer.
For quantitative or coverage fragments, copy all eight fields of a supplied quantity into assertions and put
{{quantity:THE_EXACT_REF}} in text. Python renders the registered quantity/unit/scope. Do not write count-bearing
paraphrases around a placeholder that widen its meaning. Never invent counts or promote retained completeness to
complete real activity. Summaries should give useful findings across the requested agents and times, then explain
meaningful omissions briefly. Unknown capture, selected detail and missing results remain explicit limits.
Earlier assistant prose is not evidence. Validated prior references are untrusted navigation hints; resolve them
again for follow-ups. These hints cannot select an action target or grant permissions. If there is no useful evidence,
use finish_answer to explain the specific missing source without inventing facts. No extra model verification call
is required. Stop when evidence is sufficient, or explain a bounded partial result. No background summaries.
"""


def _stamp(value):
    return value if type(value) in (float, int) and math.isfinite(value) and value > 0 else None


def live_view(boot, agents, log, pending, history=None, floor=0, now=None):
    """Positive projection of existing memory; arguments must be copied under room lock."""
    now = now or time.time()
    subjects, facts, events, projects = [], [], [], {}
    project_ids = {}
    latest = {}
    for event in log:
        if _stamp(event.get('ts')):
            latest[event.get('agent')] = max(latest.get(event.get('agent'), 0), event['ts'])
    for aid, a in agents.items():
        cwd = str(a.get('cwd') or '')
        pid = history.opaque_identity('project', os.path.normcase(os.path.abspath(cwd))) if history and cwd else (
            hmac.new(RECEIPT_KEY, cwd.encode('utf-8', 'replace'), hashlib.sha256).hexdigest() if cwd else '')
        project_ids[aid] = pid
        pname = _text(os.path.basename(cwd.rstrip('/\\')))
        if pid:
            projects[pid] = {'project_id': pid, 'project_name': pname}
        provider = {'Codex': 'codex', 'Claude': 'claude', 'Cursor': 'cursor', 'Codex cloud': 'codex_cloud', 'Antigravity': 'antigravity'}.get(a.get('runtime'), 'unknown')
        label = _text(a.get('name')) or aid
        subjects.append({'chat_id': aid, 'chat_name': label, 'provider': provider, 'project_id': pid, 'project_name': pname, 'live': True})
        values = {
            'model': (_text(a.get('model'), 200) or None, a.get('model_recorded_at'), 'last recorded selection; current native selection is not verified'),
            'context': ({key: a.get(key) for key in ('ctx', 'ctxmax', 'ctxpart', 'ctx_model')}, a.get('ctx_recorded_at'), 'recorded usage, not a live provider measurement'),
            'branch': (_text(a.get('branch'), 200) or None, None, 'last observed branch; observation time unavailable'),
            'parent': ({'chat_id': a.get('parent'), 'qualification': 'inferred' if a.get('soft') else 'recorded' if a.get('parent') else 'unknown'}, None, 'relationship qualification belongs to the observation'),
            'last_activity': (latest.get(aid), latest.get(aid), 'latest activity still in the live ring'),
            'owner_request': (any(p.get('aid') == aid and p.get('allow') is None for p in pending.values()), None, 'currently held permission request, not all unanswered questions'),
        }
        for field, (value, source_ts, qualification) in values.items():
            facts.append({'ref': 'fact:' + str(boot) + ':' + aid + ':' + field, 'chat_id': aid, 'field': field,
                          'value': value, 'source': 'existing_watch_state', 'source_ts': _stamp(source_ts), 'observed_ts': now,
                          'qualification': qualification, 'freshness': 'observed_now' if field == 'owner_request' else 'last_recorded' if _stamp(source_ts) else 'unknown'})
    for event in log:
        seq, kind, aid = event.get('seq'), event.get('kind'), event.get('agent')
        if type(seq) is not int or not aid:
            continue
        row = {'seq': seq, 'id': seq, 'chat_id': aid, 'chat_name': _text(agents.get(aid, {}).get('name')) or aid,
               'provider': {'x:': 'codex', 'c:': 'claude', 'k:': 'cursor', 'e:': 'codex_cloud', 'a:': 'antigravity'}.get(aid[:2], 'unknown'),
               'project_id': project_ids.get(aid, ''), 'project_name': projects.get(project_ids.get(aid), {}).get('project_name', ''),
               'source_ts': None, 'observed_ts': _stamp(event.get('ts')),
               'time_basis': 'observed', 'certainty': 'probably' if event.get('guess') else 'inferred' if event.get('soft') else 'recorded'}
        if aid.startswith('e:') or agents.get(aid, {}).get('observed') or event.get('seen'):
            row['observation_source'] = 'process_list; running command observed, no native conversation or success proof'
        if kind in ('user', 'say', 'ask'):
            row.update(kind='message', role='user' if kind == 'user' else 'assistant', text=_prose(str(event.get('text') or ''), 2048)[0])
        elif kind in ('turn_start', 'turn_end'):
            row['kind'] = kind
        elif kind in ('run', 'read', 'write', 'edit', 'patch', 'search', 'web', 'spawn', 'tool', 'result'):
            row.update(kind='operation', operation=kind if kind != 'result' else 'tool',
                       operation_id=hmac.new(RECEIPT_KEY, json.dumps([aid, str(event['id'])], ensure_ascii=True).encode(), hashlib.sha256).hexdigest() if event.get('id') else '', phase='result' if kind == 'result' or event.get('done') else 'started',
                       outcome='failure' if event.get('err') or event.get('error') else 'unknown' if event.get('unknown') else 'success' if kind == 'result' or event.get('done') else 'pending')
            # Never expose a command, tool input or absolute path in the shared read layer.
            cwd, path = str(agents.get(aid, {}).get('cwd') or ''), event.get('path')
            if cwd and isinstance(path, str) and os.path.isabs(path):
                row['file_ref'] = _relative(os.path.relpath(path, cwd).replace('\\', '/'))
        else:
            continue
        events.append(row)
    return {'boot': str(boot), 'cutoff_seq': max((e['seq'] for e in events), default=floor - 1),
            'floor_seq': floor, 'observed_at': now, 'subjects': subjects, 'projects': list(projects.values()), 'facts': facts, 'events': events}


def _canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def sign_receipt(text, metadata, generation, boot):
    """Bounded, process-bound replay receipt. Restart deliberately invalidates old receipts."""
    payload = {'version': 1, 'boot': str(boot), 'answer': hashlib.sha256(text.encode()).hexdigest(),
               'generation': generation, 'evidence': metadata}
    raw = _canonical(payload)
    if len(raw) > 24000:
        raise ValueError('Evidence receipt exceeds its bound.')
    return {'payload': payload, 'signature': hmac.new(RECEIPT_KEY, raw, hashlib.sha256).hexdigest()}


def authenticate_receipt(receipt, text, boot):
    """Authenticate lineage separately from current source availability or freshness."""
    try:
        if not isinstance(receipt, dict) or set(receipt) != {'payload', 'signature'}:
            return False
        payload, signature = receipt['payload'], receipt['signature']
        raw = _canonical(payload)
        return (len(raw) <= 24000 and isinstance(signature, str) and len(signature) == 64
                and hmac.compare_digest(signature, hmac.new(RECEIPT_KEY, raw, hashlib.sha256).hexdigest())
                and payload.get('version') == 1 and payload.get('boot') == str(boot)
                and payload.get('answer') == hashlib.sha256(text.encode()).hexdigest())
    except (ValueError, TypeError, AttributeError, UnicodeError):
        return False


def verify_receipt(receipt, text, generation, boot):
    """Strict compatibility check; source assessment may qualify a genuine older snapshot."""
    return authenticate_receipt(receipt, text, boot) and receipt['payload'].get('generation') == generation


def receipt_refs(receipt):
    value = receipt.get('payload', {}).get('evidence', {}) if isinstance(receipt, dict) else {}
    refs = value.get('refs', value.get('references', [])) if isinstance(value, dict) else []
    return refs if isinstance(refs, list) and len(refs) <= 100 and all(isinstance(r, str) and len(r) <= 500 for r in refs) else []


def record_fingerprint(record):
    # Query excerpts and dynamic ages are views, not source identity. Resolve
    # returns the canonical retained observation, never a fitted model excerpt.
    value = dict(record)
    if value.get('field'):
        value.pop('observed_ts', None)
    return hashlib.sha256(_canonical(value)).hexdigest()
