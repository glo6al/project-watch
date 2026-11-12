#!/usr/bin/env python3
"""Real Officer/evidence integration with fictional retained data and fake API.

These checks prove admission, evidence supply, typed quantities, replay and
publication boundaries. Scripted model responses do not prove model judgment.
No external model, native provider, or owner archive is accessed.
"""
import copy
import contextlib
import http.client
import importlib.util
import inspect
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import server
import officer_capabilities
import officer_request
from deck_log import DeckLog
from officer_request import BudgetPolicy, RequestBudget
from officer_messaging import MessagingStore

MODEL = 'claude-haiku-5-5'
SCOPE = {'sources': ['retained'], 'chat_ids': [], 'project_ids': []}


def message(content=None, stop='tool_use'):
    return {'model': MODEL, 'content': content or [], 'stop_reason': stop,
            'usage': {'input_tokens': 50, 'output_tokens': 10,
                      'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0,
                      'cache_creation': {'ephemeral_5m_input_tokens': 0, 'ephemeral_1h_input_tokens': 0},
                      'service_tier': 'standard', 'inference_geo': 'global', 'speed': 'standard'}}, ''


def tool(name, inputs, identity='tool-1'):
    return {'type': 'tool_use', 'id': identity, 'name': name, 'input': inputs}


class IntentLedger:
    def __init__(self):
        self.decisions, self.claims = {}, []

    def claim_ask(self, identity, text):
        self.claims.append(identity)
        return self.decisions.get(identity)

    def decide_ask(self, identity, value):
        self.decisions[identity] = value

    def owner_action(self, *args, **kwargs):
        raise AssertionError('Fictional read tests must never send or control')


class OfficerCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = time.time()
        self.home = os.path.realpath(self.tmp.name)
        self.saved = {name: getattr(server, name) for name in
                      ('HISTORY', 'MESSAGING', 'OFFICER_USAGE', 'KEY', 'OFFICER_KEY', 'BASE', 'PORT')}
        self.old_agents, self.old_log = copy.deepcopy(server.AGENTS), list(server.LOG)
        self.old_names, self.old_asks = dict(server.KNOWN_NAMES), list(server.OFFICER_ASKS)
        server.AGENTS.clear(); server.LOG.clear(); server.KNOWN_NAMES.clear(); server.OFFICER_ASKS.clear()
        server.BASE = 0
        server.HISTORY = self.history = DeckLog(Path(self.home) / 'fictional-archive', clock=lambda: self.now)
        self.epoch = self.history.configure(enabled=True)['capture_epoch']
        self.history.activate(self.epoch)
        server.MESSAGING = self.intent = IntentLedger()
        server.OFFICER_USAGE = None
        server.KEY, server.OFFICER_KEY = 'fictional-owner-key', 'fictional-provider-key'
        self.model_patch = patch.object(server, 'officer_model_snapshot', return_value=(MODEL, False))
        self.routing_patch = patch.object(server, 'officer_routing_snapshot',
                                          return_value=types.SimpleNamespace(automatic=False, qa=(MODEL, False), summary=(MODEL, False)))
        self.model_patch.start(); self.routing_patch.start()
        self.env_patch = patch.dict(os.environ)
        self.env_patch.start(); os.environ.pop('OFFICER_REQUEST_USD', None)
        self.calls, self.number = [], 0
        self.http = self.http_thread = None

    def tearDown(self):
        if self.http:
            self.http.shutdown(); self.http_thread.join(2); self.http.server_close()
        self.history.close()
        self.env_patch.stop(); self.routing_patch.stop(); self.model_patch.stop()
        for name, value in self.saved.items():
            setattr(server, name, value)
        server.AGENTS.clear(); server.AGENTS.update(self.old_agents)
        server.LOG[:] = self.old_log
        server.KNOWN_NAMES.clear(); server.KNOWN_NAMES.update(self.old_names)
        server.OFFICER_ASKS[:] = self.old_asks
        self.tmp.cleanup()

    def add(self, text, *, chat='x:demo-ada', name='Ada', stamp=None):
        self.number += 1
        record = dict(key=self.history.opaque_identity('record', 'capability-test', self.number),
                      stream=self.history.opaque_identity('stream', 'fictional'), provider='codex', chat_id=chat,
                      project_id=self.history.opaque_identity('project', 'Orchard'),
                      source_ts=self.now - 3600 + self.number if stamp is None else stamp, time_basis='source')
        row = dict(kind='message', role='assistant', text=text, chat_name=name, project_name='Orchard', source={'slot': '0'})
        out = self.history.offer_record(record, [row], capture_epoch=self.epoch)
        self.assertEqual(out['status'], 'queued')
        if self.number % 100 == 0:
            self.assertTrue(self.history.flush(10))

    def seed(self, dense=False):
        if dense:
            for index in range(1200):
                self.add('Middle milestone: the rounding decision was reversed.' if index == 600 else 'Busy pricing detail ' + str(index))
            for index in range(3):
                self.add('Quiet critical decision: preserve old session rows.' if index == 1 else 'Quiet migration detail ' + str(index),
                         chat='x:demo-bram', name='Bram', stamp=self.now - 3000 + index)
        else:
            self.add('Fictional saved progress: pricing review needs a rounding correction.')
        self.assertTrue(self.history.flush(10))

    def query(self, **changes):
        value = {'scope': copy.deepcopy(SCOPE), 'view': 'overview', 'limit': 20}
        value.update(changes)
        return value

    def supplied(self, body):
        return [json.loads(block['content']) for block in body['messages'][-1]['content'] if block.get('type') == 'tool_result']

    def finish(self, result, *, text=None, include_quantity=True):
        assertions = [next(q for q in result['quantities'] if q['metric'] == 'events')] if include_quantity else []
        refs = [row['ref'] for row in result.get('evidence', [])]
        rendered = 'Saved activity: {{quantity:' + assertions[0]['ref'] + '}}.' if assertions else 'The retained evidence does not establish completed goals.'
        return tool('finish_answer', {'text': text or rendered, 'references': refs, 'assertions': assertions}, 'finish')

    def execute(self, model, *, ask=False, turns=None, budget=None, model_snapshot=None):
        def call(body, **kwargs):
            self.calls.append(copy.deepcopy(body))
            return model(body)
        turns = turns or [{'role': 'user', 'content': 'Summarise the recorded work across the retained room.'}]
        if ask:
            return server.officer_ask(turns, call=call, request_id=str(uuid.uuid4()), context={'scope': 'room'})
        return server.officer_exchange(turns, call, context={'scope': 'room'}, model_snapshot=model_snapshot or (MODEL, False), budget=budget, broad=True)

    def answered(self):
        self.seed()
        def model(body):
            if len(self.calls) == 1:
                return message([tool('evidence_query', self.query())])
            return message([self.finish(self.supplied(body)[0])])
        result = self.execute(model)
        self.assertTrue(result.get('evidence_checked'), result)
        return result

    def start_http(self):
        self.http = server.Server(('127.0.0.1', 0), server.H)
        server.PORT = self.http.server_address[1]
        self.http_thread = threading.Thread(target=lambda: self.http.serve_forever(poll_interval=0.01), daemon=True)
        self.http_thread.start()

    def post(self, path, data, authenticated=True):
        connection = http.client.HTTPConnection('127.0.0.1', server.PORT, timeout=10)
        headers = {'Content-Type': 'application/json'}
        if authenticated:
            headers['X-Live-Room-Key'] = server.KEY
        connection.request('POST', path, json.dumps(data), headers)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, json.loads(raw) if response.getheader('Content-Type', '').startswith('application/json') else raw.decode()

    def test_dense_overview_keeps_full_counts_quiet_and_middle_evidence(self):
        self.seed(dense=True)
        seen = []
        def model(body):
            if len(self.calls) == 1:
                return message([tool('evidence_query', self.query())])
            result = self.supplied(body)[0]; seen.append(result)
            self.assertEqual(result['population']['counts']['retained']['events'], 1203)
            texts = [row['text'] for row in result['evidence']]
            self.assertTrue(any('Quiet critical decision' in text for text in texts))
            self.assertTrue(any('Middle milestone' in text for text in texts))
            self.assertLess(len(texts), 1203)
            return message([self.finish(result)])
        result = self.execute(model)
        self.assertTrue(result.get('evidence_checked'), result)
        self.assertIn('1203 recorded events', result['text'])
        self.assertTrue(server.officer_receipt_valid(result['receipt'], result['text']))
        self.assertEqual(len(self.calls), 2)

    def test_json_final_uses_supplied_quantities_schema_and_preserves_manual_effort(self):
        self.seed()
        def model(body):
            self.assertEqual(body['output_config']['effort'], 'low')
            if len(self.calls) == 1:
                self.assertNotIn('format', body['output_config'])
                return message([tool('evidence_query', self.query())])
            self.assertEqual(body['output_config']['format'], {'type': 'json_schema', 'schema': officer_capabilities.ANSWER_SCHEMA})
            self.assertNotIn('tool_choice', body)
            self.assertTrue(any(item['name'] == 'evidence_expand' for item in body['tools']))
            for block in body['messages'][-1]['content']:
                if block.get('type') == 'tool_result':
                    parsed = json.loads(block['content'])
                    self.assertEqual(block['content'], json.dumps(parsed, separators=(',', ':'), ensure_ascii=True))
                    self.assertEqual(len(block['content'].encode()), len(officer_request._json(parsed).encode()))
            final = self.finish(self.supplied(body)[0])['input']
            return message([{'type': 'text', 'text': json.dumps(final)}], 'end_turn')
        result = self.execute(model, model_snapshot=(MODEL, True))
        self.assertTrue(result.get('evidence_checked'), result)
        self.assertIn('1 recorded events', result['text'])
        self.assertTrue(server.officer_receipt_valid(result['receipt'], result['text']))
        self.assertEqual(len(self.calls), 2)

    def test_invalid_json_final_never_publishes_unchecked_claim(self):
        self.seed()
        for case in ('count', 'duplicate', 'nonfinite', 'malformed', 'plaintext'):
            self.calls.clear()
            def model(body):
                if len(self.calls) == 1:
                    return message([tool('evidence_query', self.query())])
                final = self.finish(self.supplied(body)[0])['input']
                final['text'] = 'UNCHECKED JSON CLAIM {{quantity:' + final['assertions'][0]['ref'] + '}}'
                if case == 'count':
                    final['assertions'][0]['value'] += 1
                    raw = json.dumps(final)
                elif case == 'duplicate':
                    raw = '{"text":"UNCHECKED JSON CLAIM","text":"UNCHECKED JSON CLAIM AGAIN","references":[],"assertions":[]}'
                elif case == 'nonfinite':
                    final['assertions'][0]['value'] = float('nan')
                    raw = json.dumps(final)
                elif case == 'malformed':
                    raw = '{"text":"UNCHECKED JSON CLAIM"'
                else:
                    raw = 'UNCHECKED JSON CLAIM in bare plaintext'
                return message([{'type': 'text', 'text': raw}], 'end_turn')
            with self.subTest(case=case):
                result = self.execute(model)
                self.assertTrue(result.get('partial') or result.get('error'), result)
                self.assertNotIn('UNCHECKED JSON CLAIM', json.dumps(result))
                self.assertNotIn('receipt', result)
                self.assertEqual(len(self.calls), 2)

    def test_json_final_cannot_claim_a_reference_not_supplied_in_prior_call(self):
        self.seed(); self.add('Unread second saved observation.'); self.assertTrue(self.history.flush(5))
        unread = next(event for event in self.history.search()['events'] if 'Unread second' in event['text'])
        def model(body):
            if len(self.calls) == 1:
                return message([tool('evidence_query', self.query(match={'text': 'pricing', 'mode': 'literal'}))])
            result = self.supplied(body)[0]
            self.assertFalse(any(row['ref'] == 'deck:' + unread['id'] for row in result['evidence']))
            final = {'text': 'UNREAD JSON CLAIM MUST NOT PUBLISH', 'references': ['deck:' + unread['id']], 'assertions': []}
            return message([{'type': 'text', 'text': json.dumps(final)}], 'end_turn')
        result = self.execute(model)
        self.assertTrue(result.get('partial'), result)
        self.assertNotIn('UNREAD JSON CLAIM', json.dumps(result))

    def test_actual_transport_bytes_match_budget_canonical_size_with_unicode_schema(self):
        payload = {'model': MODEL, 'max_tokens': 1200, 'system': 'Fictional café instructions',
                   'messages': [{'role': 'user', 'content': 'A fictional ⚓ question'}],
                   'tools': officer_capabilities.EVIDENCE_TOOLS,
                   'output_config': {'effort': 'low', 'format': {'type': 'json_schema', 'schema': officer_capabilities.ANSWER_SCHEMA}}}
        canonical = officer_request._json(payload).encode()
        budget = RequestBudget(BudgetPolicy(max_input_bytes=len(canonical)))
        sent = []
        class Response:
            status = 200
            def __init__(self): self.done = False
            def read(self, size):
                if self.done: return b''
                self.done = True
                return json.dumps(message([], 'end_turn')[0]).encode()
            def close(self): pass
        class Connection:
            sock = None
            def request(self, method, path, body, headers): sent.append((method, path, body))
            def getresponse(self): return Response()
            def close(self): pass
        with patch.object(server, 'CONNECT', return_value=Connection()):
            _, error = budget.call(server.anthropic_call, payload, 'answer')
        self.assertFalse(error)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][:2], ('POST', '/v1/messages'))
        self.assertEqual(sent[0][2], canonical)
        self.assertEqual(budget.receipt()['input_bytes'], len(sent[0][2]))
        self.assertIn(b'caf\\u00e9', sent[0][2])
        self.assertIn(b'\\u2693', sent[0][2])

    def test_same_batch_read_and_finish_is_rejected_even_with_prior_valid_refs(self):
        self.seed()
        def model(body):
            if len(self.calls) == 1:
                return message([tool('evidence_query', self.query())])
            result = self.supplied(body)[0]
            return message([tool('evidence_query', self.query(view='aggregates'), 'read-again'), self.finish(result)])
        result = self.execute(model)
        self.assertIn('same batch', result['error'])
        self.assertNotIn('receipt', result)

    def test_aliases_stay_stable_across_pages_expand_and_publish_canonical_refs(self):
        self.seed(); self.add('A middle contextual observation.'); self.add('A later contextual observation.')
        self.assertTrue(self.history.flush(5))
        seen = {}
        def model(body):
            if len(self.calls) == 1:
                return message([tool('evidence_query', self.query(view='records', limit=1))])
            result = self.supplied(body)[0]
            if len(self.calls) == 2:
                seen['first'], seen['first_ref'] = result, result['evidence'][0]['ref']
                self.assertRegex(seen['first_ref'], '^ev:[1-9][0-9]{0,3}$')
                return message([tool('evidence_query', self.query(view='records', limit=1, cursor=result['continuation']['cursor']))])
            if len(self.calls) == 3:
                seen['second_ref'] = result['evidence'][0]['ref']
                self.assertNotEqual(seen['first_ref'], seen['second_ref'])
                return message([tool('evidence_query', self.query(view='records', limit=1))])
            if len(self.calls) == 4:
                self.assertEqual(result['evidence'][0]['ref'], seen['first_ref'])
                return message([tool('evidence_expand', {'refs': [seen['first_ref']], 'mode': 'context', 'before': 0, 'after': 1})])
            self.assertEqual(len(self.calls), 5)
            refs = [row['ref'] for row in result['evidence']]
            self.assertEqual(set(refs), {seen['first_ref'], seen['second_ref']})
            self.assertTrue(any('middle contextual' in row['text'] for row in result['evidence']))
            final = self.finish(seen['first'])
            final['input']['references'] = refs
            return message([final])
        result = self.execute(model)
        self.assertTrue(result.get('evidence_checked'), result)
        self.assertTrue(all(cite['ref'].startswith('deck:') for cite in result['cites']))
        self.assertNotIn('"ev:', json.dumps(result['receipt']))
        self.assertTrue(server.officer_receipt_valid(result['receipt'], result['text']))

    def test_aliases_from_another_request_or_unknown_alias_cannot_publish(self):
        self.answered()  # The earlier exchange allocated ev:1 locally.
        self.calls.clear()
        unknown = {'text': 'UNSUPPLIED ALIAS CLAIM', 'references': ['ev:1'], 'assertions': []}
        result = self.execute(lambda body: message([tool('finish_answer', unknown)]))
        self.assertIn('actually supplied', result['error'])
        self.assertNotIn('UNSUPPLIED ALIAS CLAIM', json.dumps(result))
        self.calls.clear()
        def model(body):
            if len(self.calls) == 1:
                return message([tool('evidence_query', self.query())])
            return message([tool('finish_answer', dict(unknown, references=['ev:9999']))])
        result = self.execute(model)
        self.assertIn('actually supplied', result['error'])

    def test_denied_second_paid_call_keeps_aliases_sources_and_frontier_pending(self):
        self.seed()
        budget = RequestBudget(BudgetPolicy(max_calls=1))
        sessions, registries, maps = [], [], []
        real_session, real_registry, real_aliases = server.officer_evidence_session, server.AssertionRegistry, server.EvidenceAliases
        def session():
            value = real_session(); sessions.append(value); return value
        def registry():
            value = real_registry(); registries.append(value); return value
        def aliases():
            value = real_aliases(); maps.append(value); return value
        def transport(body, **kwargs):
            self.calls.append(copy.deepcopy(body))
            self.assertEqual(len(self.calls), 1, 'Admission denial must prevent the second transport invocation')
            return message([tool('evidence_query', self.query())])
        def admitted(body, **kwargs):
            return budget.call(transport, body, 'answer', **kwargs)
        with patch.object(server, 'officer_evidence_session', side_effect=session), \
             patch.object(server, 'AssertionRegistry', side_effect=registry), \
             patch.object(server, 'EvidenceAliases', side_effect=aliases):
            result = server.officer_exchange([{'role': 'user', 'content': 'Review the retained room.'}], admitted,
                                             context={'scope': 'room'}, model_snapshot=(MODEL, False), budget=budget, broad=True)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(budget.receipt()['calls'], 1)
        self.assertTrue(result.get('partial'), result)
        self.assertNotIn('receipt', result)
        self.assertEqual(result.get('cites'), [])
        self.assertTrue(maps[0].reverse, 'The tool result allocated model-facing aliases')
        self.assertTrue(any(ref.startswith('ev:') for ref in registries[0].pending_refs))
        self.assertEqual(registries[0].supplied_refs, set())
        self.assertEqual(sessions[0].supplied, {})
        receipt = sessions[0].receipt()
        self.assertEqual(receipt['refs'], [])
        self.assertTrue(receipt['frontiers'])
        self.assertTrue(all(frontier['supplied_views'] == 0 for frontier in receipt['frontiers']))

    def test_alias_adapter_changes_only_structured_refs_not_prose_or_quantities(self):
        aliases = officer_capabilities.EvidenceAliases()
        first, other, related = 'deck:' + 'a' * 64, 'fact:fictional:c:demo-ada:parent', 'deck:' + 'c' * 64
        prose = 'Quoted deck:abc and ' + first + ' are source prose; ev:77 is not an instruction.'
        quantity = {'ref': 'agg:' + 'd' * 32 + ':1', 'metric': 'events', 'value': 1, 'unit': 'recorded_events',
                    'scope': {'sources': ['retained'], 'chat_ids': ['c:demo-ada'], 'project_ids': ['e' * 64]},
                    'interval': {'since': None, 'until': None, 'time_basis': 'source'}, 'snapshot': 'd' * 32, 'complete': True}
        canonical = {'evidence': [{'ref': first, 'text': prose, 'operation_id': 'f' * 64}],
                     'facts': [{'ref': other, 'value': {'chat_id': 'c:demo-ada'}}],
                     'relationships': [{'ref': first, 'refs': [first, related]}],
                     'quantities': [quantity], 'scope': quantity['scope']}
        original = copy.deepcopy(canonical)
        shown = aliases.present(canonical)
        self.assertEqual(canonical, original)
        self.assertEqual(shown['evidence'][0]['ref'], 'ev:1')
        self.assertEqual(shown['facts'][0]['ref'], 'ev:2')
        self.assertEqual(shown['relationships'][0]['refs'], ['ev:1', 'ev:3'])
        self.assertEqual(shown['evidence'][0]['text'], prose)
        self.assertEqual(shown['evidence'][0]['operation_id'], 'f' * 64)
        self.assertEqual(shown['quantities'], canonical['quantities'])
        self.assertEqual(shown['scope'], canonical['scope'])
        self.assertEqual(aliases.present(canonical), shown)
        registry = officer_request.AssertionRegistry(); registry.register(shown)
        with self.assertRaises(officer_request.EvidenceRejected):
            registry.validate_qualitative_refs(['ev:1'])
        registry.mark_supplied()
        with self.assertRaises(officer_request.EvidenceRejected):
            registry.validate_qualitative_refs(['ev:3'])  # Relationship-only handle is not inspected evidence.
        restored = aliases.answer({'text': prose, 'references': ['ev:1'], 'assertions': [quantity]})
        self.assertEqual(restored, {'text': prose, 'references': [first], 'assertions': [quantity]})
        with self.assertRaises(ValueError): aliases.canonical('ev:4')
        with self.assertRaises(ValueError): aliases.canonical('ev:01')

    def test_staged_or_unread_references_cannot_be_claimed(self):
        self.seed()
        never_supplied = 'deck:' + self.history.search()['events'][0]['id']
        forged = {'text': 'Unsupplied source claim', 'references': [never_supplied], 'assertions': []}
        result = self.execute(lambda body: message([tool('finish_answer', forged)]))
        self.assertIn('actually supplied', result['error'])
        self.assertNotIn('Unsupplied source claim', json.dumps(result))

    def test_delete_while_model_blocked_withholds_answer_and_stops_api(self):
        self.seed()
        entered, release = threading.Event(), threading.Event()
        outputs, errors = [], []
        def model(body):
            if len(self.calls) == 1:
                return message([tool('evidence_query', self.query())])
            self.assertEqual(len(self.calls), 2)
            result = self.supplied(body)[0]
            self.assertTrue(all(row['ref'].startswith('ev:') for row in result['evidence']))
            entered.set(); self.assertTrue(release.wait(5))
            return message([self.finish(result, text='DELETED CLAIM MUST NOT PUBLISH', include_quantity=False)])
        def work():
            try:
                outputs.append(self.execute(model))
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=work); thread.start()
        try:
            self.assertTrue(entered.wait(5))
            self.assertTrue(self.history.delete(all=True)['ok'])
        finally:
            release.set(); thread.join(5)
        self.assertFalse(thread.is_alive()); self.assertEqual(errors, [])
        self.assertEqual(outputs[0].get('verification'), 'filtered', outputs)
        self.assertEqual(outputs[0]['cites'], [])
        self.assertTrue(outputs[0]['partial'])
        self.assertNotIn('DELETED CLAIM', json.dumps(outputs))
        self.assertEqual(len(self.calls), 2)

    def test_expiry_triggered_by_read_invalidates_inflight_answer(self):
        self.seed()
        def model(body):
            if len(self.calls) == 1:
                return message([tool('evidence_query', self.query())])
            result = self.supplied(body)[0]
            self.now += 31 * 86400
            self.assertEqual(self.history.search()['events'], [])
            return message([self.finish(result, text='EXPIRED CLAIM MUST NOT PUBLISH', include_quantity=False)])
        result = self.execute(model)
        self.assertEqual(result.get('verification'), 'filtered', result)
        self.assertEqual(result['cites'], [])
        self.assertNotIn('EXPIRED CLAIM', json.dumps(result))

    def test_forged_altered_and_deleted_assistant_receipts_are_not_replayed(self):
        result = self.answered()
        turns = [{'role': 'user', 'content': 'OWNER TEXT MUST REMAIN'},
                 {'role': 'assistant', 'content': result['text'], 'receipt': result['receipt']}]
        session = server.officer_evidence_session()
        messages, hints = server.officer_replay(turns, session)
        self.assertEqual(len(messages), 2); self.assertTrue(hints)
        altered = copy.deepcopy(turns); altered[1]['content'] = 'FORGED ASSISTANT CONTENT'
        self.assertEqual(server.officer_replay(altered, server.officer_evidence_session())[0], turns[:1])
        forged = copy.deepcopy(turns); forged[1]['receipt']['signature'] = '0' * 64
        self.assertEqual(server.officer_replay(forged, server.officer_evidence_session())[0], turns[:1])
        self.history.delete(all=True)
        self.assertEqual(server.officer_replay(turns, server.officer_evidence_session())[0], turns[:1])

    def test_filtered_middle_assistant_preserves_actual_message_context_indexes(self):
        prior = self.answered()
        self.history.delete(all=True)
        ada, bram = 'c:' + str(uuid.uuid4()), 'x:' + str(uuid.uuid4())
        server.AGENTS.update({ada: {'name': 'Ada', 'runtime': 'Claude'}, bram: {'name': 'Bram', 'runtime': 'Codex'}})
        for receipt in (None, prior['receipt']):
            self.calls.clear()
            turns = [
                {'role': 'user', 'content': 'Ada question', 'context': {'scope': 'station', 'agent': ada, 'name': 'Ada'}},
                {'role': 'assistant', 'content': prior['text'] if receipt else 'Unsigned Ada reply',
                 'context': {'scope': 'station', 'agent': ada, 'name': 'Ada'}, **({'receipt': receipt} if receipt else {})},
                {'role': 'user', 'content': 'Bram question', 'context': {'scope': 'station', 'agent': bram, 'name': 'Bram'}},
            ]
            original = copy.deepcopy(turns)
            def model(body):
                self.assertEqual(body['messages'], [{'role': 'user', 'content': 'Ada question'}, {'role': 'user', 'content': 'Bram question'}])
                envelope = json.loads(body['system'].split('<context_data>\n', 1)[1].split('\n</context_data>', 1)[0])
                self.assertEqual([row['turn'] for row in envelope['turn_contexts']], [0, 1])
                self.assertEqual([row['context']['agent'] for row in envelope['turn_contexts']], [ada, bram])
                self.assertEqual([row['context']['name'] for row in envelope['turn_contexts']], ['Ada', 'Bram'])
                return message([{'type': 'text', 'text': 'Context-only reply.'}], 'end_turn')
            result = self.execute(model, turns=turns)
            self.assertNotIn('error', result)
            self.assertEqual(turns, original, 'Replay filtering must not mutate caller turns or contexts')
            self.assertEqual(len(self.calls), 1)

    def test_http_validation_filters_deleted_answer_and_requires_auth(self):
        result = self.answered(); self.start_http()
        payload = {'receipt': result['receipt'], 'text': result['text']}
        self.assertEqual(self.post('/officer/validate', payload, False)[0], 403)
        status, permit = self.post('/officer/validate', payload)
        self.assertEqual(status, 200); self.assertTrue(permit['valid'])
        self.assertEqual(permit['boundary'], 'server_commit_before_network_delivery')
        self.history.delete(all=True)
        status, permit = self.post('/officer/validate', payload)
        self.assertEqual(status, 200); self.assertFalse(permit['valid'])
        self.assertTrue(permit['publication'])
        self.assertEqual(permit['status'], 'filtered')
        self.assertNotEqual(permit['answer']['text'], result['text'])
        self.assertEqual(permit['answer']['cites'], [])
        safe = permit['answer']
        status, unchanged = self.post('/officer/validate', {'receipt': safe['receipt'], 'text': safe['text']})
        self.assertEqual(status, 200); self.assertTrue(unchanged['valid'])
        self.assertEqual(unchanged['status'], 'filtered')
        self.assertEqual(unchanged['answer']['text'], safe['text'])

    def test_source_erasure_before_source_publication_permit_returns_409(self):
        result = self.answered(); self.start_http()
        original = server.officer_evidence_session
        gates = []
        def erase_before_permit():
            gates.append(1)
            self.assertTrue(self.history.delete(all=True)['ok'])
            return original()
        payload = {'receipt': result['receipt'], 'text': result['text'], 'ref': result['cites'][0]['ref']}
        with patch.object(server, 'officer_evidence_session', side_effect=erase_before_permit):
            status, body = self.post('/officer/evidence', payload)
        self.assertEqual(gates, [1])
        self.assertEqual(status, 409)
        self.assertEqual(body['status'], 'removed')
        self.assertTrue(body['removed'])
        self.assertNotIn('Fictional saved progress', json.dumps(body))

    def test_erasure_committed_before_publication_gate_filters_original(self):
        result = self.answered()
        entered, permits, errors = threading.Event(), [], []
        def publish():
            entered.set()
            try:
                permits.append(server.officer_publication(result['receipt'], result['text']))
            except BaseException as error:
                errors.append(error)
        with self.history.lock:
            thread = threading.Thread(target=publish); thread.start()
            self.assertTrue(entered.wait(2))
            # Publication waits on this same gate while erasure commits first.
            self.assertTrue(self.history.delete(all=True)['ok'])
        thread.join(3)
        self.assertFalse(thread.is_alive()); self.assertEqual(errors, [])
        self.assertFalse(permits[0]['valid']); self.assertTrue(permits[0]['publication'])
        self.assertEqual(permits[0]['status'], 'filtered')
        self.assertNotEqual(permits[0]['answer']['text'], result['text'])
        self.assertEqual(permits[0]['answer']['cites'], [])

    def test_publication_gate_before_erasure_is_already_published_boundary(self):
        result = self.answered()
        gate_entered, release_gate, delete_started, deleted = [threading.Event() for _ in range(4)]
        permits, errors = [], []
        original = server.officer_receipt_assessment
        def validate(*args, **kwargs):
            gate_entered.set()
            self.assertTrue(release_gate.wait(3))
            return original(*args, **kwargs)
        def publish():
            try:
                permits.append(server.officer_publication(result['receipt'], result['text']))
            except BaseException as error:
                errors.append(error)
        def erase():
            delete_started.set()
            try:
                self.history.delete(all=True)
                deleted.set()
            except BaseException as error:
                errors.append(error)
        with patch.object(server, 'officer_receipt_assessment', side_effect=validate):
            publisher = threading.Thread(target=publish); publisher.start()
            try:
                self.assertTrue(gate_entered.wait(2))
                eraser = threading.Thread(target=erase); eraser.start()
                self.assertTrue(delete_started.wait(2))
                self.assertFalse(deleted.is_set(), 'Erasure must wait while publication owns the gate')
            finally:
                release_gate.set(); publisher.join(3)
                if 'eraser' in locals(): eraser.join(3)
        self.assertEqual(errors, []); self.assertTrue(deleted.is_set())
        self.assertTrue(permits[0]['valid']); self.assertTrue(permits[0]['publication'])
        self.assertEqual(permits[0]['boundary'], 'server_commit_before_network_delivery')
        self.assertFalse(server.officer_receipt_valid(result['receipt'], result['text']))
        # The earlier permit remains an already-published server outcome; it is
        # not a promise that physical network/DOM delivery preceded erasure.
        self.assertTrue(permits[0]['valid'])

    def test_delayed_http_response_holds_no_archive_gate_during_network_wait(self):
        result = self.answered(); self.start_http()
        waiting, release, erased = threading.Event(), threading.Event(), threading.Event()
        delivered, errors = [], []
        original_send = server.H.send
        def delayed_send(handler, *args, **kwargs):
            if handler.path == '/officer/validate':
                waiting.set()
                self.assertTrue(release.wait(5))
            return original_send(handler, *args, **kwargs)
        def fetch():
            try:
                delivered.append(self.post('/officer/validate', {'receipt': result['receipt'], 'text': result['text']}))
            except BaseException as error:
                errors.append(error)
        def erase():
            try:
                self.history.delete(all=True); erased.set()
            except BaseException as error:
                errors.append(error)
        with patch.object(server.H, 'send', delayed_send):
            reader = threading.Thread(target=fetch); reader.start()
            try:
                self.assertTrue(waiting.wait(3))
                eraser = threading.Thread(target=erase); eraser.start()
                self.assertTrue(erased.wait(3), 'Archive gate must be released before delayed network delivery')
                self.assertEqual(delivered, [])
            finally:
                release.set(); reader.join(5)
                if 'eraser' in locals(): eraser.join(5)
        self.assertEqual(errors, [])
        status, permit = delivered[0]
        self.assertEqual(status, 200); self.assertTrue(permit['valid'])
        self.assertEqual(permit['boundary'], 'server_commit_before_network_delivery')
        fresh = self.post('/officer/validate', {'receipt': result['receipt'], 'text': result['text']})[1]
        self.assertFalse(fresh['valid']); self.assertEqual(fresh['status'], 'filtered')
        self.assertTrue(fresh['publication'])
        self.assertNotEqual(fresh['answer']['text'], result['text'])
        self.assertEqual(fresh['answer']['cites'], [])

    def test_legacy_plain_answer_has_signed_lineage_and_deleted_arrival_is_filtered(self):
        self.seed()
        def model(body):
            if len(self.calls) == 1:
                return message([tool('search_history', {'query': 'pricing'})])
            return message([{'type': 'text', 'text': 'Legacy source-backed response.'}], 'end_turn')
        result = self.execute(model)
        self.assertIn('receipt', result)
        self.assertTrue(server.officer_receipt_valid(result['receipt'], result['text']))
        self.history.delete(all=True)
        permit = server.officer_publication(result['receipt'], result['text'])
        self.assertFalse(permit['valid']); self.assertEqual(permit['status'], 'filtered')
        self.assertTrue(permit['publication'])
        self.assertNotIn('Legacy source-backed response.', permit['answer']['text'])
        self.start_http()
        fresh = self.post('/officer/validate', {'receipt': result['receipt'], 'text': result['text']})[1]
        self.assertFalse(fresh['valid']); self.assertEqual(fresh['status'], 'filtered')
        self.assertTrue(fresh['publication'])
        self.assertNotEqual(fresh['answer']['text'], result['text'])
        self.assertEqual(fresh['answer']['cites'], [])

    def test_legacy_read_denied_by_tiny_evidence_budget_supplies_no_saved_text(self):
        self.seed()
        budget = RequestBudget(BudgetPolicy(max_evidence_bytes=1))
        def model(body):
            if len(self.calls) == 1:
                return message([tool('search_history', {'query': 'pricing'})])
            supplied = self.supplied(body)[0]
            self.assertIn('allowance', supplied.get('error', '').lower())
            self.assertNotIn('Fictional saved progress', json.dumps(body))
            self.assertNotIn('events', supplied)
            return message([{'type': 'text', 'text': 'The evidence allowance prevented this review.'}], 'end_turn')
        result = self.execute(model, budget=budget)
        self.assertEqual(result.get('cites'), [])
        self.assertEqual(budget.receipt()['queries'], 1)
        self.assertEqual(budget.receipt()['evidence_rows'], 0)
        self.assertEqual(budget.receipt()['evidence_bytes'], 0)

    def test_http_publication_revalidates_after_model_return(self):
        result = self.answered(); self.start_http()
        def ask(*args, **kwargs):
            self.history.delete(all=True)
            return copy.deepcopy(result)
        with patch.object(server, 'officer_ask', side_effect=ask):
            status, value = self.post('/ask', {'turns': [{'role': 'user', 'content': 'Review the saved activity.'}], 'context': {'scope': 'room'}})
        self.assertEqual(status, 200)
        self.assertEqual(value.get('verification'), 'filtered', value)
        self.assertNotEqual(value['text'], result['text'])
        self.assertEqual(value['cites'], [])
        self.assertFalse(value['publication']['valid'])
        self.assertTrue(value['publication']['publication'])

    def test_strict_budget_blocks_intent_before_any_api_or_action(self):
        os.environ['OFFICER_REQUEST_USD'] = '0.01'
        result = self.execute(lambda body: self.fail('Denied intent must not call provider'), ask=True)
        self.assertIn('No model call', result['error'])
        self.assertIn('agent action', result['error'])
        self.assertEqual(self.calls, [])
        self.assertEqual(result['budget']['calls'], 0)

    def test_normal_budget_includes_dispatch_and_answer_rounds(self):
        self.seed()
        def model(body):
            if body['tools'] == server.DISPATCH_TOOLS:
                return message([], 'end_turn')
            if len(self.calls) == 2:
                return message([tool('evidence_query', self.query())])
            return message([self.finish(self.supplied(body)[0])])
        result = self.execute(model, ask=True)
        self.assertTrue(result.get('evidence_checked'), result)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(result['budget']['calls'], 3)
        self.assertEqual(result['budget']['queries'], 1)

    def test_catalog_query_expand_finish_fits_everyday_request_budget(self):
        self.seed()
        selected = {}
        def model(body):
            if body['tools'] == server.DISPATCH_TOOLS:
                return message([], 'end_turn')
            if len(self.calls) == 2:
                return message([tool('room_snapshot', {'scope': SCOPE, 'limit': 10})])
            result = self.supplied(body)[0]
            if len(self.calls) == 3:
                ids = [subject['chat_id'] for subject in result['subjects']]
                return message([tool('evidence_query', self.query(scope=dict(SCOPE, chat_ids=ids), view='records', limit=1))])
            if len(self.calls) == 4:
                selected['query'] = result
                return message([tool('evidence_expand', {'refs': [result['evidence'][0]['ref']], 'mode': 'context', 'before': 1, 'after': 1})])
            self.assertEqual(len(self.calls), 5)
            final = self.finish(selected['query'])
            final['input']['references'] = [row['ref'] for row in result['evidence']]
            return message([final])
        result = self.execute(model, ask=True)
        self.assertTrue(result.get('evidence_checked'), result)
        self.assertEqual(result['budget']['calls'], 5)
        self.assertEqual(result['budget']['queries'], 3)
        self.assertLessEqual(result['budget']['query_rows'], 3000000)

    def test_unchanged_owner_request_fact_remains_valid_as_observation_time_advances(self):
        aid = 'c:demo-ada'
        server.AGENTS[aid] = {'name': 'Ada', 'runtime': 'Claude', 'cwd': '/fictional/Orchard'}
        server.PENDING['fictional-request'] = {'aid': aid, 'allow': None}
        ticks = [self.now]
        def advancing():
            ticks[0] += 1
            return ticks[0]
        def model(body):
            if len(self.calls) == 1:
                return message([tool('room_snapshot', {'scope': {'sources': ['live']}, 'fields': ['owner_request']})])
            result = self.supplied(body)[0]
            fact = next(fact for fact in result['facts'] if fact['field'] == 'owner_request')
            self.assertIsNone(fact['source_ts']); self.assertTrue(fact['value'])
            return message([tool('finish_answer', {'text': 'A permission request is held for Ada.', 'references': [fact['ref']], 'assertions': []})])
        with patch.object(officer_capabilities.time, 'time', side_effect=advancing):
            result = self.execute(model)
            self.assertTrue(result.get('evidence_checked'), result)
            self.assertTrue(server.officer_receipt_valid(result['receipt'], result['text']))

    def test_live_projection_omits_raw_payloads_and_uses_stable_keyed_project_ids(self):
        aid = 'c:demo-ada'
        cwd = os.path.join(self.home, 'fictional', 'private', 'Orchard')
        agents = {aid: {'name': 'Ada', 'runtime': 'Claude', 'cwd': cwd, 'model': MODEL}}
        shape = 'sk-' + 'ant-' + 'x' * 25
        log = [dict(seq=1, agent=aid, kind='run', ts=self.now, id='op', cmd='PRIVATE RAW COMMAND'),
               dict(seq=2, agent=aid, kind='edit', ts=self.now, id='edit', path=os.path.join(cwd, 'src', 'example.py'), before='PRIVATE BEFORE CODE', after='PRIVATE AFTER CODE'),
               dict(seq=3, agent=aid, kind='say', ts=self.now, text='Credential ' + shape)]
        first = officer_capabilities.live_view('fictional-boot', agents, log, {}, now=self.now)
        second = officer_capabilities.live_view('fictional-boot', agents, log, {}, now=self.now + 1)
        encoded = json.dumps(first)
        for omitted in (cwd, 'PRIVATE RAW COMMAND', 'PRIVATE BEFORE CODE', 'PRIVATE AFTER CODE', shape):
            self.assertNotIn(json.dumps(omitted)[1:-1], encoded)
        identity = first['subjects'][0]['project_id']
        self.assertRegex(identity, '^[0-9a-f]{64}$')
        self.assertEqual(identity, second['subjects'][0]['project_id'])
        with patch.object(officer_capabilities, 'RECEIPT_KEY', b'fictional alternative key'):
            changed = officer_capabilities.live_view('fictional-boot', agents, log, {}, now=self.now)
        self.assertNotEqual(identity, changed['subjects'][0]['project_id'])
        self.assertEqual(first['events'][1]['file_ref'], 'src/example.py')

    def test_live_operation_identity_hides_poisoned_ids_and_preserves_scoped_joins(self):
        ada, bram = 'c:demo-ada', 'x:demo-bram'
        agents = {ada: {'name': 'Ada', 'runtime': 'Claude', 'cwd': '/fictional/Orchard'},
                  bram: {'name': 'Bram', 'runtime': 'Codex', 'cwd': '/fictional/Orchard'}}
        raw_path = '/fictional/private/native-operation-identity'
        raw_key = 'sk-' + 'ant-' + 'x' * 25
        log = []
        for raw in (raw_path, raw_key):
            for aid in (ada, bram):
                for kind in ('run', 'result'):
                    log.append(dict(seq=len(log) + 1, agent=aid, kind=kind, ts=self.now, id=raw))
        first = officer_capabilities.live_view('fictional-boot', agents, log, {}, now=self.now)
        second = officer_capabilities.live_view('fictional-boot', agents, log, {}, now=self.now + 1)
        encoded = json.dumps(first)
        self.assertNotIn(raw_path, encoded)
        self.assertNotIn(raw_key, encoded)
        identities = [row['operation_id'] for row in first['events']]
        self.assertEqual(identities, [row['operation_id'] for row in second['events']])
        for identity in identities:
            self.assertRegex(identity, '^[0-9a-f]{64}$')
        for offset in (0, 4):
            self.assertEqual(identities[offset], identities[offset + 1], 'Same-chat start and result must join')
            self.assertEqual(identities[offset + 2], identities[offset + 3])
            self.assertNotEqual(identities[offset], identities[offset + 2], 'Different chats must not join on a shared raw ID')
        self.assertNotEqual(identities[0], identities[4])

    def capability_runner(self):
        spec = importlib.util.spec_from_file_location('capability_eval_runner_under_test',
                                                      Path(__file__).resolve().parents[1] / 'scripts/eval_officer_capabilities.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        return module

    def test_capability_eval_default_listing_reads_no_key_or_fixture(self):
        runner = self.capability_runner()
        with patch.object(runner.fixture_tools.OfficerCapabilityTests, 'setUp', side_effect=AssertionError('No default fixture writes')), \
             patch.object(server, 'load_officer_key', side_effect=AssertionError('No default key read')), \
             patch.object(server, 'anthropic_call', side_effect=AssertionError('No default API call')), \
             contextlib.redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(runner.main([]), 0)
        self.assertIn('Offline listing only', printed.getvalue())

    def test_capability_eval_rejects_unbounded_or_hidden_warmup_args_before_setup(self):
        runner = self.capability_runner()
        base = ['--live', '--model', MODEL, '--output', str(Path(self.home) / 'not-created.json')]
        with patch.object(runner.fixture_tools.OfficerCapabilityTests, 'setUp', side_effect=AssertionError('Arguments must be checked first')), \
             patch.object(server, 'load_officer_key', side_effect=AssertionError('Invalid arguments must not read key')), \
             contextlib.redirect_stderr(io.StringIO()):
            for extra in (['--case', 'quiet_followup'],
                          ['--case', 'count_today', '--model', MODEL],
                          ['--case', 'broad_summary', '--case', 'count_today', '--case', 'quiet_followup']):
                with self.assertRaises(SystemExit) as error:
                    runner.main(base + extra)
                self.assertEqual(error.exception.code, 2)
        self.assertFalse((Path(self.home) / 'not-created.json').exists())

    def test_capability_eval_preserves_strict_policy_and_builds_exact_fictional_corpus(self):
        runner = self.capability_runner()
        output = Path(self.home) / 'private-evaluation.json'
        os.environ['OFFICER_REQUEST_USD'] = '0.01'
        key_reads = []
        def fictional_key():
            key_reads.append(1)
            self.assertEqual(os.environ.get('OFFICER_REQUEST_USD'), '0.01')
            self.assertEqual(server.HISTORY.db.execute('SELECT count(*) FROM events').fetchone()[0], 1315)
            self.assertEqual(server.HISTORY.db.execute('SELECT count(DISTINCT chat_id) FROM events').fetchone()[0], 39)
            maximum = server.HISTORY.db.execute('SELECT MAX(source_ts) FROM events').fetchone()[0]
            start = time.mktime(time.strptime(time.strftime('%Y-%m-%d', time.localtime(maximum)), '%Y-%m-%d'))
            self.assertEqual(server.HISTORY.db.execute('SELECT count(*) FROM events WHERE source_ts>=?', (start,)).fetchone()[0], 1314)
            server.OFFICER_KEY = 'fictional-provider-key'
        with patch.object(server, 'load_officer_key', side_effect=fictional_key), \
             patch.object(server, 'anthropic_call', side_effect=AssertionError('Strict policy must block every paid call')), \
             contextlib.redirect_stdout(io.StringIO()):
            code = runner.main(['--live', '--model', MODEL, '--case', 'count_today', '--output', str(output)])
        self.assertEqual(code, 0)
        self.assertEqual(key_reads, [1])
        self.assertEqual(os.environ.get('OFFICER_REQUEST_USD'), '0.01')
        report = json.loads(output.read_text())
        self.assertEqual(report['runs'][0]['calls'], [])
        self.assertEqual(report['runs'][0]['budget']['mode'], 'strict_usd')
        self.assertEqual(report['runs'][0]['budget']['calls'], 0)
        self.assertTrue(report['runs'][0]['result']['partial'])
        self.assertEqual(report['fixture']['retained_events_today'], 1314)
        if os.name != 'nt': self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_capability_eval_failed_broad_answer_skips_paid_followup_dependency(self):
        runner = self.capability_runner()
        output = Path(self.home) / 'dependency-evaluation.json'
        def fictional_key(): server.OFFICER_KEY = 'fictional-provider-key'
        with patch.object(runner, 'build_corpus', return_value={'observation_time': self.now}), \
             patch.object(server, 'load_officer_key', side_effect=fictional_key), \
             patch.object(server, 'officer_exchange', return_value={'error': 'Synthetic unchecked broad answer'}) as exchange, \
             patch.object(server, 'anthropic_call', side_effect=AssertionError('No API in runner control-flow test')), \
             contextlib.redirect_stdout(io.StringIO()):
            code = runner.main(['--live', '--model', MODEL, '--case', 'broad_summary', '--case', 'quiet_followup', '--output', str(output)])
        self.assertEqual(code, 0)
        self.assertEqual(exchange.call_count, 1)
        report = json.loads(output.read_text())
        self.assertEqual(report['runs'][1]['status'], 'not_verified_dependency')
        self.assertEqual(report['runs'][1]['calls'], [])

    def test_tracking_failure_notice_keeps_signed_answer_replayable(self):
        self.seed()
        server.OFFICER_USAGE = types.SimpleNamespace(begin=lambda *args: None, finish=lambda *args: False)
        def model(body):
            if body['tools'] == server.DISPATCH_TOOLS:
                return message([], 'end_turn')
            if len(self.calls) == 2:
                return message([tool('evidence_query', self.query())])
            return message([self.finish(self.supplied(body)[0])])
        result = self.execute(model, ask=True)
        self.assertIn('Cost tracking is incomplete', result.get('text', ''))
        self.assertTrue(server.officer_receipt_valid(result['receipt'], result['text']), result)

    @unittest.skipIf(os.name == 'nt', 'Production MessagingStore privacy boundary is POSIX only')
    def test_real_intent_claim_budget_denial_is_frozen_non_action(self):
        sessions = Path(self.home) / 'fictional-sessions'; sessions.mkdir()
        store = MessagingStore(Path(self.home) / 'fictional-message-state', str(sessions))
        server.MESSAGING = store
        identity = str(uuid.uuid4())
        turns = [{'role': 'user', 'content': 'Ask Ada for a progress report.'}]
        os.environ['OFFICER_REQUEST_USD'] = '0.01'
        def no_call(*args, **kwargs):
            self.fail('Budget denial must not call or replan the provider')
        try:
            first = server.officer_ask(turns, call=no_call, request_id=identity, context={'scope': 'room'})
            row = store.db.execute('SELECT decision FROM asks WHERE id=?', (identity,)).fetchone()
            self.assertIsNotNone(row)
            self.assertIsNotNone(row[0], 'Admission denial must not leave an uncertain interpretation claim')
            decision = json.loads(row[0])
            self.assertEqual(decision['kind'], 'clarify')
            self.assertIn('budget', decision['text'].lower())
            os.environ.pop('OFFICER_REQUEST_USD')
            second = server.officer_ask(turns, call=no_call, request_id=identity, context={'scope': 'room'})
            self.assertNotIn('already started', json.dumps(second))
            self.assertIn('budget', json.dumps(second).lower())
            self.assertEqual(store.snapshot()['requests'], [])
        finally:
            store.close()
            server.MESSAGING = self.intent

    def test_direct_exchange_strict_mode_cannot_bypass_admission(self):
        os.environ['OFFICER_REQUEST_USD'] = '0.01'
        result = self.execute(lambda body: self.fail('Direct exchange must not bypass strict admission'))
        self.assertEqual(self.calls, [])
        self.assertTrue(result.get('partial') or result.get('error'), result)

    def test_plain_truncated_or_exhausted_read_answers_withhold_unchecked_prose(self):
        self.seed()
        for stop in ('end_turn', 'max_tokens'):
            self.calls.clear()
            def model(body):
                if len(self.calls) == 1:
                    return message([tool('evidence_query', self.query())])
                return message([{'type': 'text', 'text': 'UNCHECKED CLAIM MUST NOT PUBLISH'}], stop)
            result = self.execute(model)
            self.assertTrue(result.get('partial'), result)
            self.assertNotIn('UNCHECKED CLAIM', json.dumps(result))
        self.calls.clear()
        with patch.object(server, 'OFFICER_ROUNDS', 2):
            result = self.execute(lambda body: message([tool('evidence_query', self.query())]))
        self.assertTrue(result.get('partial'), result)
        self.assertNotIn('receipt', result)

    def test_unsupported_goal_metric_is_not_invented_as_operation_count(self):
        self.seed()
        def model(body):
            if len(self.calls) == 1:
                return message([tool('evidence_query', dict(self.query(), metric='completed_goals'))])
            supplied = self.supplied(body)[0]
            self.assertTrue(supplied.get('error'), supplied)
            return message([tool('finish_answer', {'text': 'The retained evidence does not establish completed goals.',
                                                   'references': [], 'assertions': []})])
        result = self.execute(model)
        self.assertIn('does not establish completed goals', result.get('text', ''))


class CapabilityMutationTests(unittest.TestCase):
    def test_same_batch_and_replay_checks_have_causal_regressions(self):
        cases = [
            ('officer_exchange', 'if len(uses) != 1:', 'if False:',
             'test_same_batch_read_and_finish_is_rejected_even_with_prior_valid_refs'),
            ('officer_receipt_assessment', 'if not authenticate_receipt(receipt, text, BOOT):', 'if False:',
             'test_forged_altered_and_deleted_assistant_receipts_are_not_replayed'),
        ]
        for name, before, after, method in cases:
            original = getattr(server, name)
            source = inspect.getsource(original)
            self.assertIn(before, source)
            try:
                namespace = dict(server.__dict__)
                exec(compile(source.replace(before, after, 1), '<capability guard mutant>', 'exec'), namespace)
                setattr(server, name, namespace[name])
                result = unittest.TestResult()
                OfficerCapabilityTests(method).run(result)
                self.assertFalse(result.wasSuccessful(), 'Mutation must fail integration test: ' + name)
            finally:
                setattr(server, name, original)


if __name__ == '__main__':
    unittest.main()
