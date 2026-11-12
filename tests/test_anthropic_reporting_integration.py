#!/usr/bin/env python3
"""Officer reporting integration using fictional provider data and scripted models.

No credentials, owner archives, native services, or external API/model calls are
used. Scripted responses prove delivery and isolation, not model judgment.
"""
import copy
import datetime as dt
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.parse
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import server
import anthropic_reporting as reporting
from deck_log import DeckLog
from officer_request import BudgetPolicy, RequestBudget
from officer_usage import UsageLedger

MODEL = 'claude-haiku-5-5'


def response(content, stop='end_turn'):
    return {'model': MODEL, 'content': content, 'stop_reason': stop,
            'usage': {'input_tokens': 50, 'output_tokens': 10,
                      'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0,
                      'cache_creation': {'ephemeral_5m_input_tokens': 0, 'ephemeral_1h_input_tokens': 0},
                      'service_tier': 'standard', 'inference_geo': 'global', 'speed': 'standard'}}, ''


def tool(name, inputs, identity='report'):
    return {'type': 'tool_use', 'id': identity, 'name': name, 'input': inputs}


class AnthropicReportingIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.history = DeckLog(self.root / 'fictional-archive')
        self.ledger = UsageLedger(self.root / 'fictional-accounting')
        self.calls = []
        self.patches = [patch.object(server, 'HISTORY', self.history),
                        patch.object(server, 'OFFICER_USAGE', self.ledger),
                        patch.object(server, 'MESSAGING', None)]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.ledger.close()
        self.history.close()
        self.tmp.cleanup()

    def execute(self, first, finish, *, budget=None):
        def model(body, **kwargs):
            self.calls.append(copy.deepcopy(body))
            if len(self.calls) == 1:
                return response(first, 'tool_use')
            supplied = [json.loads(block['content']) for block in body['messages'][-1]['content']
                        if block.get('type') == 'tool_result']
            return response([{'type': 'text', 'text': finish(supplied)}])
        return server.officer_exchange(
            [{'role': 'user', 'content': 'What does Anthropic report for yesterday?'}],
            model, context={'scope': 'room'}, model_snapshot=(MODEL, False), budget=budget)

    def test_reporting_tool_is_separate_read_capability_with_bounded_schema(self):
        definition = server.OFFICER_ANTHROPIC_USAGE_TOOL
        self.assertEqual(definition['name'], 'anthropic_spend')
        schema = definition['input_schema']
        self.assertFalse(schema['additionalProperties'])
        self.assertEqual(set(schema['properties']['period']['enum']),
                         {'today', 'yesterday', 'this_week', 'this_month', 'last_month',
                          'last_7_days', 'last_30_days', 'last_24_hours', 'custom'})
        self.assertEqual(schema['properties']['by_model']['type'], 'boolean')
        self.assertEqual(server.OFFICER_USAGE_TOOL['name'], 'officer_spend')
        self.assertNotIn('anthropic_spend', server.READ_NAMES)
        self.assertNotIn('anthropic_spend', [item['name'] for item in server.OFFICER_TOOLS])

    def test_model_and_catalog_tls_disable_keylogging_without_weakening_trust(self):
        context = server.ssl.create_default_context()
        roots = context.cert_store_stats()
        log = str(self.root / 'fictional-tls-secrets')
        context.keylog_filename = log
        with patch.dict(os.environ, {'SSLKEYLOGFILE': log}), \
             patch.object(http.client.HTTPConnection, 'debuglevel', 1), \
             patch.object(server.ssl, 'create_default_context', return_value=context):
            connection = server.api_connection(1)
            self.assertEqual(connection.debuglevel, 0)
            self.assertIsNone(connection._context.keylog_filename)
            self.assertTrue(connection._context.check_hostname)
            self.assertEqual(connection._context.verify_mode, server.ssl.CERT_REQUIRED)
            self.assertEqual(connection._context.cert_store_stats(), roots)
            self.assertEqual(os.environ['SSLKEYLOGFILE'], log)
            connection.close()

    def test_provider_totals_and_local_estimate_stay_distinct_in_actual_tool_loop(self):
        provider = {'source': 'anthropic_reported', 'scope': 'organization',
                    'cost': {'available': True, 'complete': True,
                             'totals': {'amount_usd': '37.25', 'currency': 'USD', 'requests': None}},
                    'usage': {'available': True, 'complete': True, 'totals': {'input_tokens': 1234}}}
        call = self.ledger.begin(MODEL, 'answer')
        self.assertTrue(self.ledger.finish(call, response([])[0], False))
        seen = []
        def finish(results):
            seen.extend(results)
            return ('Anthropic reports $' + results[0]['cost']['totals']['amount_usd'] +
                    ' for the organization. WATCH Officer has a separate local estimate of $' +
                    results[1]['estimated_usd'] + '.')
        budget = RequestBudget()
        with patch.object(server, 'anthropic_report', return_value=provider) as report:
            result = self.execute([tool('anthropic_spend', {'period': 'yesterday'}),
                                   tool('officer_spend', {'days': 1}, 'local')], finish, budget=budget)
        report.assert_called_once()
        self.assertEqual(report.call_args.args, ({'period': 'yesterday'},))
        self.assertIsInstance(report.call_args.kwargs['end'], float)
        self.assertEqual(len(self.calls), 2)
        names = [item['name'] for item in self.calls[0]['tools']]
        self.assertEqual(names.count('anthropic_spend'), 1)
        self.assertIn('officer_spend', names)
        self.assertIn('$37.25', result['text'])
        self.assertIn('separate local estimate', result['text'])
        self.assertNotEqual(seen[1]['estimated_usd'], '37.25')
        self.assertTrue(seen[1]['snapshot_before_final_reply'])
        self.assertEqual(result['actions'], [])
        self.assertEqual(result['cites'], [])
        self.assertEqual(budget.receipt()['queries'], 2)
        self.assertGreater(budget.receipt()['evidence_bytes'], 0)
        self.assertEqual(self.ledger.summary()['calls'], 1)

    def test_query_exhaustion_stops_second_provider_fetch(self):
        budget = RequestBudget(BudgetPolicy(max_queries=1))
        with patch.object(server, 'anthropic_report', return_value={'source': 'anthropic_reported'}) as report:
            result = self.execute([tool('anthropic_spend', {'period': 'yesterday'}),
                                   tool('anthropic_spend', {'period': 'today'}, 'second')],
                                  lambda rows: rows[1].get('error', 'Unexpected successful second report.'), budget=budget)
        self.assertEqual(report.call_count, 1)
        self.assertIn('allowance is exhausted', result['text'])
        self.assertEqual(budget.receipt()['reason'], 'request_query_exhausted')
        self.assertEqual(result['actions'], [])

    def test_evidence_byte_limit_never_supplies_oversized_provider_result(self):
        budget = RequestBudget(BudgetPolicy(max_evidence_bytes=32))
        marker = 'FICTIONAL-OVERSIZED-PROVIDER-RESULT'
        with patch.object(server, 'anthropic_report', return_value={'qualification': marker * 5}):
            result = self.execute([tool('anthropic_spend', {'period': 'yesterday'})],
                                  lambda rows: rows[0].get('error', 'Unexpected oversized report.'), budget=budget)
        self.assertIn('allowance is exhausted', result['text'])
        self.assertNotIn(marker, json.dumps(self.calls))
        self.assertNotIn(marker, json.dumps(result))
        self.assertEqual(budget.receipt()['evidence_bytes'], 0)

    def payload(self, path, *, marker='FICTIONAL-PRIVATE-PROVIDER-BODY', more=False):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        kind = 'usage' if '/usage?' in path else 'cost'
        value = ({'uncached_input_tokens': 100, 'cache_read_input_tokens': 20,
                  'cache_creation': {'ephemeral_1h_input_tokens': 3, 'ephemeral_5m_input_tokens': 7},
                  'output_tokens': 10, 'total_tokens': 140, 'requests': 4,
                  'server_tool_use': {'web_search_requests': 0}, 'raw_body': marker}
                 if kind == 'usage' else {'amount': '3725.000000', 'currency': 'USD', 'requests': None,
                                         'raw_body': marker})
        row = {'starting_at': query['starting_at'][0], 'ending_at': query['ending_at'][0],
               'group': {'model': marker if 'group_by[]' in query else None}, kind: value,
               'workspace_id': marker, 'api_key_id': marker}
        return {'data': [row], 'has_more': more,
                'next_page': 'FICTIONAL-PRIVATE-CURSOR' if more else None,
                'data_refreshed_at': '2026-11-11T20:00:00Z', 'raw_body': marker,
                'organization_id': marker}

    def test_real_projector_removes_hostile_provider_canaries_before_model_and_publication(self):
        key = 'sk-' + 'ant-admin-' + 'synthetic-test-only'
        marker = 'FICTIONAL-PRIVATE-PROVIDER-BODY'
        requests = []
        def transport(path, received_key, end):
            requests.append(path)
            self.assertEqual(received_key, key)
            self.assertGreater(end, 0)
            return self.payload(path, marker=marker)
        supplied = []
        def finish(rows):
            supplied.extend(rows)
            report = rows[0]
            return ('Anthropic reports $' + report['cost']['totals']['amount_usd'] +
                    ' for the organization, with ' + str(report['usage']['totals']['total_tokens']) +
                    ' tokens. Its data was refreshed at ' + report['cost']['data_refreshed_at'] + '.')
        with patch.object(reporting, '_load_key', return_value=key) as load, \
             patch.object(reporting, '_request', side_effect=transport), \
             patch.object(reporting, '_now', return_value=dt.datetime(2026, 11, 12, 12, tzinfo=dt.timezone.utc)):
            result = self.execute([tool('anthropic_spend', {'period': 'yesterday', 'by_model': True})], finish)
        self.assertEqual(load.call_count, 1)
        self.assertEqual(len(requests), 2)
        self.assertIn('$37.25000000', result['text'])
        self.assertIn('140 tokens', result['text'])
        self.assertEqual(supplied[0]['source'], 'anthropic_reported')
        self.assertEqual(supplied[0]['scope'], 'organization')
        self.assertEqual(supplied[0]['usage']['by_model'][0]['model'], 'other')
        self.assertEqual(supplied[0]['cost']['totals']['requests'], None)
        self.assertEqual(result['actions'], [])
        self.assertEqual(result['cites'], [])
        self.assertEqual(self.ledger.summary()['calls'], 0)
        self.assertEqual(self.history.search()['events'], [])
        public = json.dumps({'calls': self.calls, 'result': result,
                             'ledger': self.ledger.summary(), 'history': self.history.search()})
        for forbidden in (key, marker, 'FICTIONAL-PRIVATE-CURSOR', 'workspace_id', 'api_key_id', 'organization_id'):
            self.assertNotIn(forbidden, public)
        for path in self.root.rglob('*'):
            if path.is_file():
                raw = path.read_bytes()
                self.assertNotIn(key.encode(), raw)
                self.assertNotIn(marker.encode(), raw)

    def test_missing_and_expired_key_use_fixed_safe_messages(self):
        for reason in ('credential_unavailable', 'authentication_failed'):
            with self.subTest(reason=reason):
                self.calls.clear()
                key_patch = (patch.object(reporting, '_load_key', side_effect=reporting.ReportError(reason))
                             if reason == 'credential_unavailable' else
                             patch.object(reporting, '_load_key', return_value='fictional-admin-key'))
                with key_patch, patch.object(reporting, '_request', side_effect=reporting.ReportError(reason)) as transport:
                    result = self.execute([tool('anthropic_spend', {'period': 'yesterday'})],
                                          lambda rows: json.dumps(rows[0], sort_keys=True))
                parsed = json.loads(result['text'])
                self.assertFalse(parsed['available'])
                if reason == 'credential_unavailable':
                    self.assertEqual(parsed['error'], reason)
                    transport.assert_not_called()
                else:
                    self.assertEqual(parsed['usage']['qualifications'], [reason])
                    self.assertEqual(parsed['cost']['qualifications'], [reason])
                    self.assertIsNone(parsed['cost']['totals'])
                self.assertNotIn('fictional-admin-key', json.dumps(self.calls) + json.dumps(result))
                self.assertEqual(result['actions'], [])

    def test_incomplete_usage_pagination_preserves_unknown_and_independent_cost(self):
        requests = []
        def transport(path, key, end):
            requests.append(path)
            if '/usage?' in path and 'page=' in path:
                raise reporting.ReportError('pagination_incomplete')
            return self.payload(path, more='/usage?' in path)
        with patch.object(reporting, '_load_key', return_value='fictional-admin-key'), \
             patch.object(reporting, '_request', side_effect=transport):
            result = self.execute([tool('anthropic_spend', {'period': 'yesterday'})],
                                  lambda rows: json.dumps(rows[0], sort_keys=True))
        report = json.loads(result['text'])
        self.assertFalse(report['usage']['complete'])
        self.assertIsNone(report['usage']['totals'])
        self.assertEqual({key: report['usage']['coverage'][key] for key in ('has_more', 'pages', 'rows')},
                         {'has_more': True, 'pages': 1, 'rows': 1})
        self.assertIn('pagination_incomplete', report['usage']['qualifications'])
        self.assertTrue(report['cost']['complete'])
        self.assertEqual(report['cost']['totals']['amount_usd'], '37.25000000')
        self.assertEqual(report['cost']['data_refreshed_at'], '2026-11-11T20:00:00Z')
        self.assertNotIn('FICTIONAL-PRIVATE-CURSOR', json.dumps(self.calls) + json.dumps(result))
        self.assertEqual(len(requests), 3)

    def test_inclusive_custom_dates_and_provider_freshness_reach_model_unchanged(self):
        requests, supplied = [], []
        def transport(path, key, end):
            requests.append(urllib.parse.parse_qs(urllib.parse.urlsplit(path).query))
            return self.payload(path)
        def finish(rows):
            supplied.extend(rows)
            return 'The organization report covers the requested October dates, with delayed provider data.'
        inputs = {'period': 'custom', 'start_date': '2026-10-01', 'end_date': '2026-10-31'}
        with patch.object(reporting, '_load_key', return_value='fictional-admin-key'), \
             patch.object(reporting, '_request', side_effect=transport), \
             patch.object(reporting, '_now', return_value=dt.datetime(2026, 11, 12, 12, tzinfo=dt.timezone.utc)):
            result = self.execute([tool('anthropic_spend', inputs)], finish)
        period = supplied[0]['period']
        self.assertEqual(period['start_date'], inputs['start_date'])
        self.assertEqual(period['end_date'], inputs['end_date'])
        self.assertEqual(len(requests), 2)
        for query in requests:
            self.assertEqual(query['starting_at'], [period['effective_starting_at']])
            self.assertEqual(query['ending_at'], [period['effective_ending_at']])
            self.assertNotIn('group_by[]', query)
        for kind in ('usage', 'cost'):
            self.assertEqual(supplied[0][kind]['data_refreshed_at'], '2026-11-11T20:00:00Z')
        self.assertTrue(any('not_real_time' in item for item in supplied[0]['qualifications']))
        self.assertIn('requested October dates', result['text'])

    def test_http_ask_publishes_safe_report_and_missing_admin_key_keeps_officer_available(self):
        key = 'sk-' + 'ant-admin-' + 'synthetic-http-only'
        marker = 'FICTIONAL-PRIVATE-PROVIDER-BODY'
        real_ask = server.officer_ask
        model_calls, supplied = [], []
        def model(body, **kwargs):
            model_calls.append(copy.deepcopy(body))
            content = body['messages'][-1]['content']
            if isinstance(content, str):
                if 'ordinary room question' in content:
                    return response([{'type': 'text', 'text': 'The Officer can still answer ordinary room questions.'}])
                return response([tool('anthropic_spend', {'period': 'yesterday'})], 'tool_use')
            report = json.loads(content[0]['content'])
            supplied.append(report)
            text = ('Anthropic reports $' + report['cost']['totals']['amount_usd'] + ' for the organization.'
                    if report.get('available') else
                    'Anthropic reported spending is unavailable. Cost is unknown, not zero.')
            return response([{'type': 'text', 'text': text}])
        def ask(turns, **kwargs):
            return real_ask(turns, call=model, **kwargs)
        def transport(path, received_key, end):
            self.assertEqual(received_key, key)
            return self.payload(path, marker=marker)
        http_server = server.Server(('127.0.0.1', 0), server.H)
        port = http_server.server_address[1]
        thread = threading.Thread(target=lambda: http_server.serve_forever(poll_interval=0.01), daemon=True)
        def post(question, authenticated=True):
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
            try:
                headers = {'Content-Type': 'application/json'}
                if authenticated:
                    headers['X-Live-Room-Key'] = 'fictional-browser-key'
                connection.request('POST', '/ask', json.dumps({
                    'turns': [{'role': 'user', 'content': question}], 'context': {'scope': 'room'}}), headers)
                reply = connection.getresponse()
                raw = reply.read().decode()
                return reply.status, json.loads(raw) if reply.getheader('Content-Type', '').startswith('application/json') else raw
            finally:
                connection.close()
        try:
            with patch.object(server, 'PORT', port), patch.object(server, 'KEY', 'fictional-browser-key'), \
                 patch.object(server, 'OFFICER_KEY', 'fictional-answer-key'), \
                 patch.object(server, 'OFFICER_ASKS', []), \
                 patch.object(server, 'officer_routing_snapshot', return_value=server.RoutingSnapshot('manual', (MODEL, False), (MODEL, False))), \
                 patch.object(server, 'officer_ask', side_effect=ask), \
                 patch.object(reporting, '_load_key', return_value=key) as load, \
                 patch.object(reporting, '_request', side_effect=transport) as request:
                thread.start()
                status, forbidden = post('What does Anthropic report?', authenticated=False)
                self.assertEqual((status, forbidden), (403, 'forbidden'))
                self.assertEqual(model_calls, [])
                load.assert_not_called()
                request.assert_not_called()
                status, published = post('What does Anthropic report?')
                self.assertEqual(status, 200)
                self.assertIn('$37.25000000', published['text'])
                self.assertEqual(published['actions'], [])
                self.assertEqual(published['usage_tracking']['recorded_calls'], 2)
                self.assertEqual(len(model_calls), 2)
                self.assertEqual(request.call_count, 2)
                with patch.object(reporting, '_load_key', side_effect=reporting.ReportError('credential_unavailable')):
                    status, unavailable = post('What does Anthropic report without the optional credential?')
                    self.assertEqual(status, 200)
                    self.assertIn('Cost is unknown, not zero', unavailable['text'])
                    self.assertEqual(supplied[-1]['error'], 'credential_unavailable')
                    status, ordinary = post('Please answer an ordinary room question.')
                    self.assertEqual(status, 200)
                    self.assertEqual(ordinary['text'], 'The Officer can still answer ordinary room questions.')
                self.assertEqual(request.call_count, 2)
                public = json.dumps({'responses': [published, unavailable, ordinary], 'model_requests': model_calls})
                for forbidden in (key, marker, 'workspace_id', 'api_key_id', 'organization_id'):
                    self.assertNotIn(forbidden, public)
                self.assertEqual(self.ledger.summary()['calls'], 5)
                self.assertEqual(self.history.search()['events'], [])
        finally:
            if thread.is_alive():
                http_server.shutdown()
                thread.join(2)
            http_server.server_close()

    def test_custom_scope_validation_precedes_credentials_and_network(self):
        invalid = ({'period': 'custom', 'start_date': '2026-10-01', 'end_date': '2026-11-01'},
                   {'period': 'yesterday', 'start_date': '2026-11-01'},
                   {'period': 'custom', 'start_date': '2026-11-02', 'end_date': '2026-11-01'},
                   {'period': 'today', 'by_model': 1})
        with patch.object(reporting, '_load_key') as load, patch.object(reporting, '_request') as transport:
            for inputs in invalid:
                actions = []
                out = server.officer_tool('anthropic_spend', inputs, actions)
                self.assertFalse(out['available'])
                self.assertEqual(out['error'], 'invalid_request')
                self.assertEqual(actions, [])
        load.assert_not_called()
        transport.assert_not_called()


if __name__ == '__main__':
    unittest.main()
