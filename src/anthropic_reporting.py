"""Bounded, read-only organization analytics. No credentials or network activity at import."""
import datetime as dt
from decimal import Decimal, InvalidOperation, localcontext
import http.client
import json
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import threading
import time
import urllib.parse

UTC = dt.timezone.utc
ROUTES = {'usage': '/v1/organizations/analytics/usage', 'cost': '/v1/organizations/analytics/cost'}
MAX_BODY = 512 * 1024
MAX_REQUESTS = 12
SLOTS = threading.BoundedSemaphore(2)
MODEL = re.compile(r'claude-(?:(?:haiku|sonnet|opus)-[0-9]{1,2}(?:-[0-9]{1,2})?|[0-9]{1,2}(?:-[0-9]{1,2})?-(?:haiku|sonnet|opus))(?:-[0-9]{8})?\Z')
USAGE_FIELDS = ('uncached_input_tokens', 'cache_1h_input_tokens', 'cache_5m_input_tokens',
                'cache_read_input_tokens', 'output_tokens', 'total_tokens', 'requests', 'web_search_requests')
REASONS = {'credential_unavailable', 'invalid_request', 'deadline', 'busy', 'transport_unavailable',
           'authentication_failed', 'rate_limited', 'provider_unavailable', 'invalid_response',
           'response_too_large', 'pagination_incomplete', 'not_ready', 'request_limit'}


class ReportError(Exception):
    def __init__(self, reason):
        self.reason = reason if reason in REASONS else 'invalid_response'
        super().__init__(self.reason)


def _now():
    return dt.datetime.now(UTC)


def _load_key():
    """Open the fixed private credential once; never include filesystem diagnostics."""
    fd = parent_fd = None
    try:
        if os.name != 'posix' or not hasattr(os, 'O_NOFOLLOW'):
            raise ReportError('credential_unavailable')
        parent = Path.home() / '.live-room'
        parent_fd = os.open(str(parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        ps = os.fstat(parent_fd)
        if not stat.S_ISDIR(ps.st_mode) or ps.st_uid != os.getuid() or ps.st_mode & 0o077:
            raise ReportError('credential_unavailable')
        name = 'anthropic-admin-key'
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        after = os.fstat(fd)
        if ((before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or
                not stat.S_ISREG(after.st_mode) or after.st_uid != os.getuid() or
                after.st_nlink != 1 or after.st_mode & 0o177 or not 1 <= after.st_size <= 512):
            raise ReportError('credential_unavailable')
        raw = os.read(fd, 513)
        if len(raw) > 512:
            raise ReportError('credential_unavailable')
        key = raw.decode('ascii').strip()
        if not re.fullmatch(r'sk-ant-admin[0-9A-Za-z_-]{8,450}', key):
            raise ReportError('credential_unavailable')
        return key
    except (OSError, UnicodeError, ValueError):
        raise ReportError('credential_unavailable') from None
    finally:
        if fd is not None:
            os.close(fd)
        if parent_fd is not None:
            os.close(parent_fd)


class _HeaderReader:
    def __init__(self, fp):
        self.fp, self.used = fp, 0

    def readline(self, limit=-1):
        line = self.fp.readline(min(8193, limit) if limit >= 0 else 8193)
        self.used += len(line)
        if len(line) > 8192 or self.used > 32768:
            raise ReportError('response_too_large')
        return line

    def __getattr__(self, name):
        return getattr(self.fp, name)


class _Response(http.client.HTTPResponse):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fp = _HeaderReader(self.fp)


def _connection(timeout):
    context = ssl.create_default_context()
    context.keylog_filename = None  # SSLKEYLOGFILE must never capture admin-connection traffic secrets.
    conn = http.client.HTTPSConnection('api.anthropic.com', 443, timeout=timeout, context=context)
    conn.set_debuglevel(0)  # Never inherit process-wide HTTP debug output containing admin headers.
    conn.response_class = _Response
    return conn


def _request(path, key, end):
    """A fixed-host request with a wall deadline and a cap including abandoned workers."""
    if time.monotonic() >= end:
        raise ReportError('deadline')
    if not SLOTS.acquire(False):
        raise ReportError('busy')
    state, out = {}, {}

    def run():
        try:
            conn = state['conn'] = _connection(max(.001, end - time.monotonic()))
            conn.request('GET', path, headers={'x-api-key': key, 'anthropic-version': '2026-10-21',
                         'anthropic-beta': 'analytics-usage-cost-2026-08-24', 'Accept': 'application/json',
                         'Accept-Encoding': 'identity'})
            state['sock'] = conn.sock
            response = state['response'] = conn.getresponse()
            if response.status != 200:
                raise ReportError('authentication_failed' if response.status in (401, 403) else
                                  'rate_limited' if response.status == 429 else 'provider_unavailable')
            encoding = response.getheader('Content-Encoding')
            if encoding not in (None, 'identity'):
                raise ReportError('invalid_response')
            length = response.getheader('Content-Length')
            if length is not None and (not length.isdigit() or int(length) > MAX_BODY):
                raise ReportError('response_too_large')
            buf = bytearray()
            while True:
                if time.monotonic() >= end:
                    raise ReportError('deadline')
                chunk = response.read(16384)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > MAX_BODY:
                    raise ReportError('response_too_large')
            value = json.loads(buf.decode('utf-8'), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not isinstance(value, dict):
                raise ReportError('invalid_response')
            out['value'] = value
        except ReportError as exc:
            out['error'] = exc.reason
        except Exception:
            out['error'] = 'transport_unavailable'
        finally:
            try:
                state['conn'].close()
            except Exception:
                pass
            SLOTS.release()

    worker = threading.Thread(target=run, daemon=True)
    try:
        worker.start()
    except Exception:
        SLOTS.release()
        raise ReportError('transport_unavailable') from None
    worker.join(max(0, end - time.monotonic()))
    if worker.is_alive() or time.monotonic() >= end:
        # Shutdown unblocks TLS reads; never join or wait for close after the deadline.
        for sock in (state.get('sock'), getattr(state.get('conn'), 'sock', None)):
            try:
                if sock is not None:
                    sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
        raise ReportError('deadline')
    if 'value' not in out:
        raise ReportError(out.get('error', 'transport_unavailable'))
    return out['value']


def _stamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ReportError('invalid_response')
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
            raise ValueError()
        return parsed.astimezone(UTC)
    except ValueError:
        raise ReportError('invalid_response') from None


def _iso(value):
    return value.astimezone(UTC).isoformat().replace('+00:00', 'Z')


def _period(inp, now):
    if not isinstance(inp, dict) or set(inp) - {'period', 'start_date', 'end_date', 'by_model'}:
        raise ReportError('invalid_request')
    if type(inp.get('by_model', False)) is not bool:
        raise ReportError('invalid_request')
    period = inp.get('period', 'today')
    if period not in ('today', 'yesterday', 'this_week', 'last_7_days', 'last_24_hours',
                      'last_30_days', 'this_month', 'last_month', 'custom'):
        raise ReportError('invalid_request')
    today = now.astimezone().date()
    if period == 'custom':
        try:
            if not all(isinstance(inp.get(k), str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', inp[k])
                       for k in ('start_date', 'end_date')):
                raise ValueError()
            start, last = (dt.date.fromisoformat(inp[k]) for k in ('start_date', 'end_date'))
        except ValueError:
            raise ReportError('invalid_request') from None
    else:
        if 'start_date' in inp or 'end_date' in inp:
            raise ReportError('invalid_request')
        last = today - dt.timedelta(days=1) if period in ('yesterday', 'last_month') else today
        start = last
        if period == 'last_7_days':
            start = today - dt.timedelta(days=6)
        elif period == 'last_30_days':
            start = today - dt.timedelta(days=29)
        elif period == 'this_week':
            start = today - dt.timedelta(days=today.weekday())
        elif period == 'this_month':
            start = today.replace(day=1)
        elif period == 'last_month':
            last = today.replace(day=1) - dt.timedelta(days=1)
            start = last.replace(day=1)
    if last > today or start > last or (last - start).days >= 31:
        raise ReportError('invalid_request')
    beginning = dt.datetime.combine(start, dt.time()).astimezone(UTC)
    ending = min(dt.datetime.combine(last + dt.timedelta(days=1), dt.time()).astimezone(UTC), now)
    if period == 'last_24_hours':
        beginning, ending = now - dt.timedelta(hours=24), now
        start, last = beginning.astimezone().date(), today
    if beginning < max(dt.datetime(2026, 1, 1, tzinfo=UTC), now - dt.timedelta(days=365)):
        raise ReportError('invalid_request')
    effective_start = beginning.replace(minute=0, second=0, microsecond=0)
    effective_end = ending.replace(minute=0, second=0, microsecond=0)
    return {'period': period, 'start_date': start.isoformat(), 'end_date': last.isoformat(),
            'starting_at': _iso(beginning), 'ending_at': _iso(ending),
            'effective_starting_at': _iso(effective_start), 'effective_ending_at': _iso(effective_end)}, effective_start, effective_end


def _count(value):
    if type(value) is not int or not 0 <= value <= 10 ** 18:
        raise ReportError('invalid_response')
    return value


def _money(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,24}\.[0-9]{6}', value):
        raise ReportError('invalid_response')
    try:
        with localcontext() as ctx:
            ctx.prec = 64
            return Decimal(value) / Decimal(100)
    except InvalidOperation:
        raise ReportError('invalid_response') from None


def _row(row, kind, grouped, start, end):
    if not isinstance(row, dict) or (_stamp(row.get('starting_at')) != start or _stamp(row.get('ending_at')) != end):
        raise ReportError('invalid_response')
    group = row.get('group')
    if not isinstance(group, dict) or 'model' not in group or any(v is not None for k, v in group.items() if k != 'model'):
        raise ReportError('invalid_response')
    model = group['model']
    if model is not None and not isinstance(model, str) or not grouped and model is not None:
        raise ReportError('invalid_response')
    model = model if model is None or (len(model) < 100 and MODEL.fullmatch(model)) else 'other'
    value = row.get(kind)
    if not isinstance(value, dict):
        raise ReportError('invalid_response')
    if kind == 'cost':
        if value.get('currency') != 'USD':
            raise ReportError('invalid_response')
        return model, {'amount_usd': _money(value.get('amount')), 'requests': None if value.get('requests') is None else _count(value['requests'])}
    cache, tools = value.get('cache_creation'), value.get('server_tool_use')
    if not isinstance(cache, dict) or not isinstance(tools, dict):
        raise ReportError('invalid_response')
    totals = {k: _count(value.get(k)) for k in ('uncached_input_tokens', 'cache_read_input_tokens', 'output_tokens', 'total_tokens', 'requests')}
    totals.update(cache_1h_input_tokens=_count(cache.get('ephemeral_1h_input_tokens')),
                  cache_5m_input_tokens=_count(cache.get('ephemeral_5m_input_tokens')),
                  web_search_requests=_count(tools.get('web_search_requests')))
    if totals['total_tokens'] != sum(totals[k] for k in USAGE_FIELDS[:5]):
        raise ReportError('invalid_response')
    return model, totals


def _add(total, part):
    for key, value in part.items():
        with localcontext() as ctx:
            ctx.prec = 64
            total[key] = None if value is None or (key in total and total[key] is None) else total.get(key, 0) + value


def _render(total, kind):
    out = dict(total)
    if kind == 'cost':
        out['amount_usd'] = format(out['amount_usd'], '.8f')
        out['currency'] = 'USD'
    return out


def _endpoint(kind, start, end, grouped, key, deadline, budget):
    result = {'available': False, 'complete': False, 'totals': None, 'by_model': [],
              'data_refreshed_at': None, 'coverage': {'pages': 0, 'rows': 0, 'has_more': False, 'retrieval_complete': False,
                                                      'requested_period_complete': None}, 'qualifications': []}
    totals = {'amount_usd': Decimal(0), 'requests': 0} if kind == 'cost' else dict.fromkeys(USAGE_FIELDS, 0)
    models, refresh, seen_rows = {}, None, set()
    try:
        cursor_start = start
        while cursor_start < end:
            chunk_end = min(end, cursor_start + dt.timedelta(days=31))
            page, seen_pages = None, set()
            while True:
                if budget[0] >= MAX_REQUESTS or result['coverage']['pages'] >= 6:
                    raise ReportError('request_limit')
                query = [('starting_at', _iso(cursor_start)), ('ending_at', _iso(chunk_end)), ('limit', '1000')]
                if grouped:
                    query.append(('group_by[]', 'model'))
                if page is not None:
                    query.append(('page', page))
                if time.monotonic() >= deadline:
                    raise ReportError('deadline')
                budget[0] += 1
                data = _request(ROUTES[kind] + '?' + urllib.parse.urlencode(query), key, deadline)
                rows = data.get('data')
                more, next_page = data.get('has_more'), data.get('next_page')
                if not isinstance(rows, list) or len(rows) > 1000 or type(more) is not bool:
                    raise ReportError('invalid_response')
                if more and (not isinstance(next_page, str) or not 1 <= len(next_page) <= 1024 or
                             not next_page.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in next_page) or
                             next_page in seen_pages or key in next_page or 'sk-ant-' in next_page):
                    raise ReportError('pagination_incomplete')
                if not more and next_page is not None:
                    raise ReportError('invalid_response')
                refreshed = data.get('data_refreshed_at')
                if refreshed is None:
                    raise ReportError('not_ready' if not rows else 'invalid_response')
                refreshed = _iso(_stamp(refreshed))
                if refresh is not None and refresh != refreshed:
                    raise ReportError('pagination_incomplete')
                refresh = refreshed
                result['data_refreshed_at'] = refresh
                result['coverage']['requested_period_complete'] = False if _stamp(refresh) < end else None
                result['coverage']['pages'] += 1
                result['coverage']['has_more'] = more
                for row in rows:
                    model, part = _row(row, kind, grouped, cursor_start, chunk_end)
                    # The API has no time bucket in these fixed requests: one row per model per chunk.
                    identity = (cursor_start, chunk_end, row['group']['model'])
                    if identity in seen_rows:
                        raise ReportError('pagination_incomplete')
                    seen_rows.add(identity)
                    _add(totals, part)
                    if grouped:
                        _add(models.setdefault(model, {}), part)
                    result['coverage']['rows'] += 1
                if not more:
                    break
                seen_pages.add(next_page)
                page = next_page
            cursor_start = chunk_end
        # No completed UTC hour in the requested interval is not a known zero.
        if start == end:
            raise ReportError('not_ready')
        result.update(available=True, complete=True, totals=_render(totals, kind), data_refreshed_at=refresh)
        result['coverage']['retrieval_complete'] = True
        result['qualifications'].append('complete_means_retrieval_only; export_refresh_is_not_a_completeness_watermark')
        if result['coverage']['requested_period_complete'] is False:
            result['qualifications'].append('reported_so_far; requested_period_not_fully_refreshed')
        if grouped:
            result['by_model'] = [{'model': m, 'totals': _render(v, kind)} for m, v in sorted(models.items(), key=lambda x: x[0] or '')[:8]]
            if len(models) > 8:
                result['qualifications'].append('model_breakdown_limited_to_8; totals_cover_all_models')
    except ReportError as exc:
        result['qualifications'].append(exc.reason)
    except Exception:
        result['qualifications'].append('invalid_response')
    return result


def report(inp, end=None):
    """Return only safe aggregates. No admin identity or provider diagnostics escape."""
    now = _now()
    base = {'source': 'anthropic_reported', 'scope': 'organization', 'snapshot': _iso(now)}
    try:
        period, start, finish = _period(inp, now)
        deadline = min(time.monotonic() + 15, end) if end is not None else time.monotonic() + 15
        if deadline <= time.monotonic():
            raise ReportError('deadline')
        key = _load_key()
    except ReportError as exc:
        return dict(base, available=False, error=exc.reason)
    except Exception:
        return dict(base, available=False, error='credential_unavailable')
    base.update(period=period, qualifications=[
        'organization_aggregate_not_Officer_or_agent_attribution',
        'UTC_hour_bounds_rounded_down; current_partial_hour_excluded',
        'provider_refresh_typically_4_hours_may_lag_24_hours; not_real_time',
        'provider_values_may_be_corrected_until_7_days_after_month_end',
        'cost_after_discounts_before_credits; not_invoice_or_balance; excludes_priority_commitment_fees'])
    budget = [0]
    for kind in ('usage', 'cost'):
        base[kind] = _endpoint(kind, start, finish, inp.get('by_model', False), key, deadline, budget)
    base['available'] = base['usage']['available'] or base['cost']['available']
    return base
