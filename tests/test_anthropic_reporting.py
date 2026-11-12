"""Synthetic provider reports only; never reads a credential or contacts a service."""
import datetime as dt
import io
import http.client
import json
import os
from pathlib import Path
import stat
import ssl
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import anthropic_reporting as ar

NOW = dt.datetime(2026, 11, 12, 16, 35, tzinfo=ar.UTC)
KEY = 'sk' + '-ant-' + 'admin-' + 'fictional' * 4
CANARY = 'PRIVATE_CANARY_ACCOUNT_ACTOR_WORKSPACE'


def payload(kind, model=None, amount='41280.000000'):
    row = {'starting_at': '2026-11-12T00:00:00Z', 'ending_at': '2026-11-12T16:00:00Z',
           'group': {'model': model, 'workspace_id': None}, 'private_extra': CANARY}
    if kind == 'usage':
        row['usage'] = {'uncached_input_tokens': 10, 'cache_creation': {
            'ephemeral_1h_input_tokens': 2, 'ephemeral_5m_input_tokens': 3, 'secret': CANARY},
            'cache_read_input_tokens': 4, 'output_tokens': 5, 'total_tokens': 24,
            'requests': 1, 'server_tool_use': {'web_search_requests': 0, 'actor': CANARY}, 'raw': CANARY}
    else:
        row['cost'] = {'amount': amount, 'list_amount': CANARY, 'currency': 'USD', 'requests': 1, 'account': CANARY}
    return {'data': [row], 'has_more': False, 'next_page': None,
            'data_refreshed_at': '2026-11-12T16:00:00Z', 'secret': CANARY, 'api_key': KEY}


class Reporting(unittest.TestCase):
    def setUp(self):
        self.old_tz = os.environ.get('TZ')
        os.environ['TZ'] = 'UTC'
        if hasattr(time, 'tzset'):
            time.tzset()
        self.patches = [patch.object(ar, '_now', return_value=NOW), patch.object(ar, '_load_key', return_value=KEY)]
        for item in self.patches:
            item.start()
        self.calls = []

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        if self.old_tz is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = self.old_tz
        if hasattr(time, 'tzset'):
            time.tzset()

    def fake(self, path, key, end):
        self.calls.append((path, key, end))
        return payload('usage' if '/usage?' in path else 'cost')

    def run_report(self, inp=None, fake=None):
        with patch.object(ar, '_request', side_effect=fake or self.fake):
            return ar.report(inp or {})

    def test_exact_actual_counts_and_cents_projection(self):
        result = self.run_report()
        self.assertEqual(result['cost']['totals']['amount_usd'], '412.80000000')
        self.assertEqual(result['usage']['totals']['total_tokens'], 24)
        self.assertEqual(result['scope'], 'organization')
        text = json.dumps(result)
        for forbidden in (KEY, CANARY, 'workspace_id', 'api_key', 'list_amount', 'private_extra'):
            self.assertNotIn(forbidden, text)
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all(key == KEY for _, key, _ in self.calls))
        self.assertIn('16%3A00%3A00Z', self.calls[0][0])

    def test_independent_cost_failure_is_unknown_not_zero(self):
        def fake(path, key, end):
            if '/cost?' in path:
                raise ar.ReportError('authentication_failed')
            return payload('usage')
        result = self.run_report(fake=fake)
        self.assertTrue(result['usage']['complete'])
        self.assertFalse(result['cost']['available'])
        self.assertIsNone(result['cost']['totals'])
        self.assertEqual(result['cost']['qualifications'], ['authentication_failed'])

    def test_empty_unknown_versus_refreshed_zero(self):
        def fake(path, key, end):
            return {'data': [], 'has_more': False, 'next_page': None, 'data_refreshed_at': None}
        self.assertIsNone(self.run_report(fake=fake)['usage']['totals'])
        def ready(path, key, end):
            value = fake(path, key, end)
            value['data_refreshed_at'] = '2026-11-12T16:00:00Z'
            return value
        result = self.run_report(fake=ready)
        self.assertEqual(result['usage']['totals']['total_tokens'], 0)
        self.assertEqual(result['cost']['totals']['amount_usd'], '0.00000000')

    def test_bad_counts_currency_and_money_never_coerce(self):
        mutations = [lambda p: p['data'][0]['usage'].__setitem__('output_tokens', True),
                     lambda p: p['data'][0]['usage'].__setitem__('total_tokens', 25),
                     lambda p: p['data'][0]['cost'].__setitem__('currency', CANARY),
                     lambda p: p['data'][0]['cost'].__setitem__('amount', '-1.000000'),
                     lambda p: p['data'][0]['cost'].__setitem__('amount', 41280)]
        for change in mutations:
            def fake(path, key, end):
                kind = 'usage' if '/usage?' in path else 'cost'
                value = payload(kind)
                try:
                    change(value)
                except KeyError:
                    pass
                return value
            result = self.run_report(fake=fake)
            target = 'usage' if change in mutations[:2] else 'cost'
            self.assertFalse(result[target]['complete'])
            self.assertIsNone(result[target]['totals'])

    def test_model_is_only_public_dimension_and_unknown_is_fixed_bucket(self):
        for name, expected in [('claude-sonnet-5-5', 'claude-sonnet-5-5'), (CANARY, 'other'), (KEY, 'other'), (None, None)]:
            def fake(path, key, end):
                return payload('usage' if '/usage?' in path else 'cost', name)
            result = self.run_report({'by_model': True}, fake)
            self.assertEqual(result['usage']['by_model'][0]['model'], expected)
            self.assertNotIn(CANARY, json.dumps(result))
            self.assertNotIn(KEY, json.dumps(result))
        def bad(path, key, end):
            value = payload('usage' if '/usage?' in path else 'cost')
            value['data'][0]['group']['workspace_id'] = CANARY
            return value
        self.assertFalse(self.run_report(fake=bad)['usage']['complete'])

    def test_pagination_cursor_is_only_encoded_parameter_and_failure_discards_totals(self):
        paths = []
        def fake(path, key, end):
            paths.append(path)
            kind = 'usage' if '/usage?' in path else 'cost'
            value = payload(kind)
            if 'page=' not in path:
                value.update(has_more=True, next_page='cursor?x=https://example.invalid')
            else:
                value['data'] = []
            return value
        result = self.run_report(fake=fake)
        self.assertTrue(result['usage']['complete'])
        self.assertEqual(len(paths), 4)
        self.assertIn('page=cursor%3Fx%3Dhttps%3A%2F%2Fexample.invalid', paths[1])
        def repeating(path, key, end):
            value = payload('usage' if '/usage?' in path else 'cost')
            value.update(has_more=True, next_page='same')
            return value
        result = self.run_report(fake=repeating)
        self.assertFalse(result['usage']['complete'])
        self.assertIsNone(result['usage']['totals'])

    def test_refresh_change_and_duplicate_rows_rejected(self):
        for mode in ('refresh', 'duplicate'):
            def fake(path, key, end):
                value = payload('usage' if '/usage?' in path else 'cost')
                if 'page=' not in path:
                    value.update(has_more=True, next_page='second')
                elif mode == 'refresh':
                    value['data_refreshed_at'] = '2026-11-12T15:00:00Z'
                return value
            self.assertFalse(self.run_report(fake=fake)['usage']['complete'])

    def test_large_decimal_remains_exact_through_addition(self):
        value = ar._money('999999999999999999999999.123456')
        total = {}
        ar._add(total, {'amount_usd': value})
        ar._add(total, {'amount_usd': value})
        self.assertEqual(ar._render(total, 'cost')['amount_usd'], '19999999999999999999999.98246912')

    def test_partial_interval_cannot_claim_full_retrieval(self):
        def fake(path, key, end):
            value = payload('usage' if '/usage?' in path else 'cost')
            value['data'][0]['starting_at'] = '2026-11-12T01:00:00Z'
            return value
        result = self.run_report(fake=fake)
        self.assertFalse(result['usage']['complete'])
        self.assertIsNone(result['usage']['totals'])

    def test_refresh_is_provisional_and_preserved_on_pagination_failure(self):
        def lagged(path, key, end):
            value = payload('usage' if '/usage?' in path else 'cost')
            value['data_refreshed_at'] = '2026-11-12T12:00:00Z'
            return value
        result = self.run_report(fake=lagged)
        self.assertTrue(result['usage']['coverage']['retrieval_complete'])
        self.assertFalse(result['usage']['coverage']['requested_period_complete'])
        self.assertNotIn('through', result['usage']['coverage'])
        self.assertEqual(result['usage']['totals']['total_tokens'], 24)
        def partial(path, key, end):
            if 'page=' in path:
                raise ar.ReportError('provider_unavailable')
            value = lagged(path, key, end)
            value.update(has_more=True, next_page='second')
            return value
        result = self.run_report(fake=partial)
        self.assertEqual(result['usage']['data_refreshed_at'], '2026-11-12T12:00:00Z')
        self.assertIsNone(result['usage']['totals'])

    @unittest.skipUnless(hasattr(time, 'tzset'), 'requires local timezone switching')
    def test_fall_dst_31_calendar_days_split_into_two_bounded_chunks(self):
        os.environ['TZ'] = 'America/New_York'
        time.tzset()
        paths = []
        def fake(path, key, end):
            paths.append(path)
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
            value = payload('usage' if '/usage?' in path else 'cost')
            value['data'][0]['starting_at'] = query['starting_at'][0]
            value['data'][0]['ending_at'] = query['ending_at'][0]
            self.assertLessEqual((ar._stamp(query['ending_at'][0])-ar._stamp(query['starting_at'][0])).total_seconds(), 31*86400)
            return value
        result = self.run_report({'period': 'custom', 'start_date': '2026-10-02', 'end_date': '2026-11-01'}, fake)
        self.assertTrue(result['usage']['complete'])
        self.assertEqual(len(paths), 4)
        self.assertEqual(result['usage']['totals']['total_tokens'], 48)

    def test_duplicate_instant_spellings_within_and_across_pages(self):
        for across in (False, True):
            def fake(path, key, end):
                value = payload('usage' if '/usage?' in path else 'cost')
                duplicate = dict(value['data'][0])
                duplicate['starting_at'] = duplicate['starting_at'].replace('Z', '+00:00')
                duplicate['ending_at'] = duplicate['ending_at'].replace('Z', '+00:00')
                if across:
                    if 'page=' in path:
                        value['data'] = [duplicate]
                    else:
                        value.update(has_more=True, next_page='second')
                else:
                    value['data'].append(duplicate)
                return value
            result = self.run_report(fake=fake)
            self.assertFalse(result['usage']['complete'])
            self.assertIsNone(result['usage']['totals'])
            self.assertFalse(result['cost']['complete'])

    def test_strict_request_and_private_key_loaded_once(self):
        for inp in ({'url': CANARY}, {'by_model': 1}, {'period': 'custom', 'start_date': '2026-10-01', 'end_date': '2026-11-01'},
                    {'period': 'custom', 'start_date': '2026-11-13', 'end_date': '2026-11-13'},
                    {'period': 'today', 'start_date': '2026-11-12'}, {'period': 'balance'}):
            with patch.object(ar, '_load_key') as loader:
                self.assertEqual(ar.report(inp)['error'], 'invalid_request')
                loader.assert_not_called()
        with patch.object(ar, '_load_key', return_value=KEY) as loader:
            self.run_report()
            loader.assert_called_once_with()

    def test_deadline_before_credential_or_network(self):
        with patch.object(ar, '_load_key') as loader, patch.object(ar, '_request') as request:
            self.assertEqual(ar.report({}, end=time.monotonic()-1)['error'], 'deadline')
            loader.assert_not_called()
            request.assert_not_called()

    @unittest.skipUnless(hasattr(time, 'tzset'), 'requires local timezone switching')
    def test_calendar_periods_dst_and_utc_chunking(self):
        os.environ['TZ'] = 'America/New_York'
        time.tzset()
        for day, hours in [('2026-03-08', 23), ('2026-11-01', 25)]:
            _, start, end = ar._period({'period': 'custom', 'start_date': day, 'end_date': day}, NOW)
            self.assertEqual((end-start).total_seconds(), hours*3600)
        period, start, end = ar._period({'period': 'last_month'}, NOW)
        self.assertEqual(period['start_date'], '2026-10-01')
        self.assertEqual(period['end_date'], '2026-10-31')
        self.assertEqual((end-start).total_seconds(), 31*86400)
        _, start, end = ar._period({'period': 'this_week'}, NOW)
        self.assertEqual(start.astimezone().date().isoformat(), '2026-11-09')
        _, start, end = ar._period({'period': 'last_24_hours'}, NOW)
        self.assertEqual((end-start).total_seconds(), 86400)
        # A 31-calendar-day range crossing fall DST exceeds a 31-day UTC request.
        period, start, end = ar._period({'period': 'custom', 'start_date': '2026-10-13', 'end_date': '2026-11-12'}, NOW)
        self.assertLessEqual((end-start).total_seconds(), 32*86400)

    def test_request_limit_is_independent_and_bounded(self):
        calls = []
        def fake(path, key, end):
            calls.append(path)
            value = payload('usage' if '/usage?' in path else 'cost', 'claude-sonnet-5-5')
            # Unique canonical interval prevents duplicate rejection while forcing pages.
            n = len(calls)
            value['data'][0]['group']['model'] = 'untrusted-model-' + str(n)
            value.update(has_more=True, next_page='page' + str(n))
            return value
        result = self.run_report({'by_model': True}, fake)
        self.assertEqual(len(calls), 12)
        self.assertFalse(result['usage']['complete'])
        self.assertFalse(result['cost']['complete'])
        self.assertIsNone(result['usage']['totals'])


@unittest.skipUnless(os.name == 'posix' and hasattr(os, 'O_NOFOLLOW'), 'requires POSIX private-file protections')
class Credentials(unittest.TestCase):
    def test_fixed_private_regular_file_and_rotation(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(ar.Path, 'home', return_value=Path(tmp)):
            parent = Path(tmp) / '.live-room'
            parent.mkdir(mode=0o700)
            keyfile = parent / 'anthropic-admin-key'
            keyfile.write_text(KEY + '\n')
            keyfile.chmod(0o600)
            self.assertEqual(ar._load_key(), KEY)
            keyfile.write_text(KEY + 'new')
            self.assertEqual(ar._load_key(), KEY + 'new')
            for mode in (0o644, 0o700):
                keyfile.chmod(mode)
                with self.assertRaises(ar.ReportError):
                    ar._load_key()
            keyfile.chmod(0o600)
            parent.chmod(0o755)
            with self.assertRaises(ar.ReportError):
                ar._load_key()
            parent.chmod(0o700)
            keyfile.unlink()
            keyfile.symlink_to(Path(tmp) / 'missing')
            with self.assertRaises(ar.ReportError):
                ar._load_key()

    def test_hardlinks_non_ascii_and_oversize(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(ar.Path, 'home', return_value=Path(tmp)):
            parent = Path(tmp) / '.live-room'
            parent.mkdir(mode=0o700)
            keyfile = parent / 'anthropic-admin-key'
            keyfile.write_text(KEY)
            keyfile.chmod(0o600)
            os.link(keyfile, parent / 'other')
            with self.assertRaises(ar.ReportError):
                ar._load_key()
            (parent / 'other').unlink()
            for raw in (b'x' * 513, b'\xff', b'header\r\ninjection'):
                keyfile.write_bytes(raw)
                with self.assertRaises(ar.ReportError):
                    ar._load_key()


class CredentialPlatform(unittest.TestCase):
    def test_non_posix_loader_fails_closed_without_opening_a_file(self):
        with patch.object(ar.os, 'name', 'nt'), patch.object(ar.os, 'open') as opening:
            with self.assertRaises(ar.ReportError) as raised:
                ar._load_key()
            self.assertEqual(raised.exception.reason, 'credential_unavailable')
            opening.assert_not_called()


class Transport(unittest.TestCase):
    def test_connection_disables_tls_key_logging_without_weakening_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = str(Path(tmp) / 'synthetic-tls-debug.log')
            with patch.dict(os.environ, {'SSLKEYLOGFILE': target}):
                preconfigured = ssl.create_default_context()
                if hasattr(ssl.SSLContext, 'keylog_filename'):
                    self.assertEqual(preconfigured.keylog_filename, target)
                else:
                    # Older TLS builds have no engine key logger; also clear a preconfigured attribute.
                    preconfigured.keylog_filename = target
                with patch.object(ar.ssl, 'create_default_context', return_value=preconfigured):
                    conn = ar._connection(.1)
                self.assertIsNone(conn._context.keylog_filename)
                self.assertEqual(conn._context.verify_mode, ssl.CERT_REQUIRED)
                self.assertTrue(conn._context.check_hostname)
                self.assertGreater(conn._context.cert_store_stats()['x509_ca'], 0)
                self.assertEqual(os.environ['SSLKEYLOGFILE'], target)
                conn.close()

    def test_connection_disables_inherited_http_debug_logging(self):
        with patch.object(http.client.HTTPConnection, 'debuglevel', 1):
            conn = ar._connection(.1)
            self.assertEqual(conn.debuglevel, 0)
            conn.close()

    def test_fixed_get_headers_and_response_projection_boundary(self):
        class Response:
            status = 200
            def getheader(self, name):
                return None
            def read(self, count):
                out, self.body = self.body[:count], self.body[count:]
                return out
            body = b'{"data":[]}'
        class Conn:
            sock = None
            def request(self, *args, **kwargs):
                self.args, self.kwargs = args, kwargs
            def getresponse(self):
                return Response()
            def close(self):
                pass
        conn = Conn()
        with patch.object(ar, '_connection', return_value=conn):
            self.assertEqual(ar._request(ar.ROUTES['usage'], KEY, time.monotonic()+1), {'data': []})
        self.assertEqual(conn.args, ('GET', ar.ROUTES['usage']))
        self.assertEqual(conn.kwargs['headers']['x-api-key'], KEY)
        self.assertEqual(conn.kwargs['headers']['anthropic-version'], '2026-10-21')

    def test_deadline_returns_and_abandoned_workers_remain_capped(self):
        release = threading.Event()
        class Conn:
            sock = None
            def request(self, *args, **kwargs):
                release.wait(2)
            def getresponse(self):
                raise OSError(CANARY)
            def close(self):
                pass
        with patch.object(ar, 'SLOTS', threading.BoundedSemaphore(2)), patch.object(ar, '_connection', side_effect=lambda _: Conn()):
            try:
                for _ in range(2):
                    start = time.monotonic()
                    with self.assertRaises(ar.ReportError) as raised:
                        ar._request(ar.ROUTES['usage'], KEY, start+.03)
                    self.assertEqual(raised.exception.reason, 'deadline')
                    self.assertLess(time.monotonic()-start, .3)
                with self.assertRaises(ar.ReportError) as raised:
                    ar._request(ar.ROUTES['usage'], KEY, time.monotonic()+.03)
                self.assertEqual(raised.exception.reason, 'busy')
            finally:
                release.set()
                time.sleep(.03)

    def test_header_and_body_slow_drip_deadlines_shutdown_socket(self):
        for phase in ('headers', 'body'):
            stopped = threading.Event()
            completed = threading.Event()
            class Sock:
                def shutdown(self, how):
                    stopped.set()
            class Response:
                status = 200
                def getheader(self, name):
                    return None
                def read(self, count):
                    stopped.wait(1)
                    return b''
            class Conn:
                sock = Sock()
                def request(self, *args, **kwargs):
                    pass
                def getresponse(self):
                    if phase == 'headers':
                        stopped.wait(1)
                    return Response()
                def close(self):
                    completed.set()
            with patch.object(ar, '_connection', return_value=Conn()):
                start = time.monotonic()
                with self.assertRaises(ar.ReportError) as raised:
                    ar._request(ar.ROUTES['usage'], KEY, start+.03)
                self.assertEqual(raised.exception.reason, 'deadline')
                self.assertTrue(stopped.is_set())
                self.assertTrue(completed.wait(.3))
                self.assertLess(time.monotonic()-start, .4)

    def test_redirect_large_body_and_http_error_have_no_raw_diagnostics(self):
        for mode in ('redirect', 'length', 'body'):
            class Response:
                status = 302 if mode == 'redirect' else 200
                def getheader(self, name):
                    return str(ar.MAX_BODY+1) if mode == 'length' and name == 'Content-Length' else None
                def read(self, count):
                    return b'x'*count
            class Conn:
                sock = None
                calls = 0
                def request(self, *args, **kwargs):
                    self.calls += 1
                def getresponse(self):
                    return Response()
                def close(self):
                    pass
            conn = Conn()
            with patch.object(ar, '_connection', return_value=conn):
                with self.assertRaises(ar.ReportError) as raised:
                    ar._request(ar.ROUTES['usage'], KEY, time.monotonic()+1)
            self.assertIn(raised.exception.reason, ('provider_unavailable', 'response_too_large'))
            self.assertEqual(conn.calls, 1)

    def test_header_limits_and_raw_errors_never_escape(self):
        reader = ar._HeaderReader(io.BytesIO(b'x'*8193+b'\n'))
        with self.assertRaises(ar.ReportError):
            reader.readline()
        class Conn:
            sock = None
            def request(self, *args, **kwargs):
                raise OSError(CANARY + KEY)
            def close(self):
                pass
        with patch.object(ar, '_connection', return_value=Conn()):
            with self.assertRaises(ar.ReportError) as raised:
                ar._request(ar.ROUTES['usage'], KEY, time.monotonic()+1)
        self.assertEqual(str(raised.exception), 'transport_unavailable')


if __name__ == '__main__':
    unittest.main()
