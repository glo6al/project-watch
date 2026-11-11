#!/usr/bin/env python3
"""Request admission and typed evidence checks with fictional in-memory data."""
from concurrent.futures import ThreadPoolExecutor
import copy
from decimal import Decimal
import inspect
import json
from pathlib import Path
import sys
import threading
import textwrap
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import officer_request as request

MODEL = 'claude-haiku-5-5'
SCOPE = {'sources': ['live', 'retained'], 'chat_ids': ['c:demo-ada'], 'project_ids': []}
INTERVAL = {'since': 100, 'until': 200, 'time_basis': 'source'}
SNAPSHOT = 'a' * 32
REF = 'deck:' + 'b' * 64
AGG = 'agg:' + SNAPSHOT + ':1'


def body(output=1200):
    return {'model': MODEL, 'max_tokens': output, 'system': 'Fictional instructions',
            'messages': [{'role': 'user', 'content': 'Fictional question'}]}


def response(inputs=10, outputs=10):
    return {'model': MODEL, 'usage': {'input_tokens': inputs, 'output_tokens': outputs,
            'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0,
            'cache_creation': {'ephemeral_5m_input_tokens': 0, 'ephemeral_1h_input_tokens': 0},
            'service_tier': 'standard', 'inference_geo': 'global', 'speed': 'standard'}}


def bound(value, tokens=100):
    return request.InputBound(tokens, 'conservative', request.request_digest(value))


def quantity():
    return {'ref': AGG, 'metric': 'event_count', 'value': 3, 'unit': 'events',
            'scope': copy.deepcopy(SCOPE), 'interval': copy.deepcopy(INTERVAL),
            'snapshot': SNAPSHOT, 'complete': True}


def registry(supplied=True):
    item = request.AssertionRegistry()
    item.register({'evidence': [{'ref': REF, 'text': 'PRIVATE-SOURCE-PROSE'}],
                   'facts': [], 'quantities': [quantity()]})
    if supplied:
        item.mark_supplied()
    return item


class RequestBudgetTests(unittest.TestCase):
    def setUp(self):
        self.now = 10
        self.pricing = json.loads((Path(__file__).resolve().parents[1] / 'officer/PRICING.json').read_text())
        self.billing = {'service_tier': 'standard', 'inference_geo': 'global', 'speed': 'standard', 'extras': False}

    def budget(self, **kwargs):
        return request.RequestBudget(request.BudgetPolicy(**kwargs), clock=lambda: self.now, pricing=self.pricing)

    def test_default_policy_is_explicit_nonmonetary_and_admits_before_calls(self):
        budget = self.budget()
        seen = []
        def transport(value, **kwargs):
            self.assertEqual(budget.receipt()['calls'], 1)
            seen.append(value)
            return response(), ''
        out = budget.call(transport, body(), 'dispatch')
        self.assertEqual(len(seen), 1)
        self.assertEqual(out, (response(), ''))
        self.assertEqual(budget.receipt()['mode'], 'bounded')
        self.assertIsNone(budget.receipt()['estimated_usd'])
        self.assertEqual(budget.policy.max_seconds, 120)
        self.assertEqual(budget.policy.max_calls, 7)

    def test_output_reservation_reconciles_only_valid_actual_usage(self):
        budget = self.budget(max_output_tokens=1200)
        reservation = budget.reserve_call(body(), 'dispatch')
        with self.assertRaises(request.BudgetExceeded):
            budget.reserve_call(body(), 'answer')
        self.assertFalse(budget.settle(reservation, None, True))
        self.assertEqual(budget.receipt()['output_tokens'], 1200)
        self.assertEqual(budget.receipt()['unsettled_calls'], 1)
        self.assertTrue(budget.settle(reservation, response(), False))
        self.assertTrue(budget.settle(reservation, response(outputs=900), False))
        self.assertEqual(budget.receipt()['output_tokens'], 10)
        budget.reserve_call(body(1000), 'answer')

    def test_calls_time_input_evidence_and_query_limits(self):
        budget = self.budget(max_calls=1)
        budget.reserve_call(body(), 'dispatch')
        with self.assertRaisesRegex(request.BudgetExceeded, 'calls'):
            budget.reserve_call(body(), 'answer')
        tiny = self.budget(max_input_bytes=1)
        with self.assertRaisesRegex(request.BudgetExceeded, 'input'):
            tiny.reserve_call(body(), 'dispatch')
        self.assertEqual(tiny.receipt()['calls'], 0)
        time_budget = self.budget()
        self.now += 120
        with self.assertRaisesRegex(request.BudgetExceeded, 'time'):
            time_budget.reserve_call(body(), 'dispatch')
        local = self.budget(max_queries=1, max_query_rows=50000)
        local.claim_query(50000)
        with self.assertRaisesRegex(request.BudgetExceeded, 'query'):
            local.claim_query(1)
        local.supply_evidence(512, 65536)
        with self.assertRaisesRegex(request.BudgetExceeded, 'evidence'):
            local.supply_evidence(1, 1)

    def test_strict_mode_unknowns_block_without_invoking_transport(self):
        budget = self.budget(mode='strict_usd', usd_limit='0.01')
        called = []
        for input_bound, billing in [(None, self.billing), (bound(body()), None)]:
            with self.assertRaises(request.BudgetExceeded):
                budget.call(lambda b: called.append(b), body(), 'dispatch', input_bound=input_bound, billing=billing)
        self.assertEqual(called, [])
        budget.pricing = None
        with self.assertRaises(request.BudgetExceeded):
            budget.reserve_call(body(), 'dispatch', input_bound=bound(body()), billing=self.billing)
        self.assertEqual(budget.receipt()['calls'], 0)
        with self.assertRaises(ValueError):
            request.BudgetPolicy(mode='strict_usd')
        with self.assertRaises(ValueError):
            request.BudgetPolicy(usd_limit='1')

    def test_strict_reservation_is_conservative_and_unsettled_not_free(self):
        budget = self.budget(mode='strict_usd', usd_limit='0.001')
        first = budget.reserve_call(body(), 'dispatch', input_bound=bound(body()), billing=self.billing)
        self.assertEqual(Decimal(first.estimated_usd), Decimal('0.00062'))
        with self.assertRaisesRegex(request.BudgetExceeded, 'dollars'):
            budget.reserve_call(body(), 'answer', input_bound=bound(body()), billing=self.billing)
        self.assertFalse(budget.settle(first, None, True))
        with self.assertRaisesRegex(request.BudgetExceeded, 'dollars'):
            budget.reserve_call(body(), 'answer', input_bound=bound(body()), billing=self.billing)
        self.assertTrue(budget.settle(first, response(), False))
        budget.reserve_call(body(), 'answer', input_bound=bound(body()), billing=self.billing)

    def test_input_bound_binds_full_request_and_threshold_includes_all_input(self):
        budget = self.budget(mode='strict_usd', usd_limit='1')
        original = body()
        counted = bound(original)
        changed = copy.deepcopy(original)
        changed['tools'] = [{'name': 'extra_tool_definition'}]
        with self.assertRaisesRegex(request.BudgetExceeded, 'input_bound_invalid'):
            budget.reserve_call(changed, 'dispatch', input_bound=counted, billing=self.billing)
        with self.assertRaises(request.BudgetExceeded):
            budget.reserve_call(original, 'dispatch', input_bound=bound(original, 100001), billing=self.billing)

    def test_actual_usage_exceeding_bound_prevents_further_calls(self):
        budget = self.budget(max_input_tokens=100)
        reservation = budget.reserve_call(body(), 'dispatch', input_bound=bound(body(), 20))
        self.assertTrue(budget.settle(reservation, response(inputs=21), False))
        self.assertTrue(budget.receipt()['exhausted'])
        with self.assertRaises(request.BudgetExceeded):
            budget.reserve_call(body(), 'answer', input_bound=bound(body()))

    def test_admission_race_never_overbooks_last_call_slot(self):
        budget = self.budget(max_calls=1)
        barrier = threading.Barrier(8)
        def reserve(_):
            barrier.wait()
            try:
                return budget.reserve_call(body(), 'dispatch')
            except request.BudgetExceeded:
                return None
        with ThreadPoolExecutor(max_workers=8) as pool:
            values = list(pool.map(reserve, range(8)))
        self.assertEqual(sum(value is not None for value in values), 1)
        self.assertEqual(budget.receipt()['calls'], 1)

    def test_transport_exception_keeps_reservation_without_retry(self):
        budget = self.budget()
        count = []
        def transport(value, **kwargs):
            count.append(1)
            raise OSError('fictional transport interruption')
        with self.assertRaises(OSError):
            budget.call(transport, body(), 'dispatch')
        self.assertEqual(len(count), 1)
        self.assertEqual(budget.receipt()['unsettled_calls'], 1)
        self.assertEqual(budget.receipt()['output_tokens'], 1200)

    def test_reserved_body_isolated_from_mutable_caller_data(self):
        budget = self.budget()
        original = body()
        def transport(value, **kwargs):
            original['max_tokens'] = 999999
            self.assertEqual(value['max_tokens'], 1200)
            return response(), ''
        budget.call(transport, original, 'dispatch')
        self.assertEqual(budget.receipt()['input_bytes'], len(request._json(body()).encode()))

    def test_total_deadline_is_passed_to_existing_transport(self):
        budget = self.budget()
        ends = []
        def transport(value, end):
            ends.append(end)
            return response(), ''
        budget.call(transport, body(), 'dispatch', end=99999)
        self.assertEqual(ends, [130])
        self.assertEqual(budget.remaining(), 120)


class AssertionTests(unittest.TestCase):
    def test_registration_rejects_bool_quantities(self):
        with self.assertRaises(request.EvidenceRejected):
            request.AssertionRegistry().register({'quantities': [dict(quantity(), value=True)]})

    def test_pending_references_are_not_supplied_evidence(self):
        item = registry(False)
        with self.assertRaises(request.EvidenceRejected):
            item.validate_qualitative_refs([REF])
        with self.assertRaises(request.EvidenceRejected):
            item.validate_assertion(quantity())
        item.mark_supplied()
        self.assertEqual(item.validate_qualitative_refs([REF, REF]), [REF])
        with self.assertRaises(request.EvidenceRejected):
            item.validate_qualitative_refs(['deck:' + 'c' * 64])

    def test_short_aliases_require_this_registry_registration_and_supply(self):
        first, second = request.AssertionRegistry(), request.AssertionRegistry()
        first.register({'evidence': [{'ref': 'ev:1'}], 'facts': [{'ref': 'ev:9999'}]})
        with self.assertRaises(request.EvidenceRejected):
            first.validate_qualitative_refs(['ev:1'])
        first.mark_supplied()
        self.assertEqual(first.validate_qualitative_refs(['ev:1', 'ev:9999']), ['ev:1', 'ev:9999'])
        for alias in ('ev:0', 'ev:01', 'ev:-1', 'ev:10000', 'ev:1x', 'ev: 1', 'ev:1\n'):
            with self.subTest(alias=alias), self.assertRaises(request.EvidenceRejected):
                request.AssertionRegistry().register({'evidence': [{'ref': alias}]})
        with self.assertRaises(request.EvidenceRejected):
            first.validate_qualitative_refs(['ev:2'])
        with self.assertRaises(request.EvidenceRejected):
            second.validate_qualitative_refs(['ev:1'])
        with self.assertRaises(request.EvidenceRejected):
            first.register({'quantities': [dict(quantity(), ref='ev:1')]})

    def test_every_typed_field_is_compared_including_bool_vs_number(self):
        item = registry()
        alternatives = {'ref': 'agg:' + SNAPSHOT + ':2', 'metric': 'deployment_count',
                        'value': 4, 'unit': 'deployments', 'scope': dict(SCOPE, chat_ids=[]),
                        'interval': dict(INTERVAL, until=201), 'snapshot': 'c' * 32, 'complete': False}
        for key, value in alternatives.items():
            changed = dict(quantity(), **{key: value})
            with self.subTest(key=key), self.assertRaises(request.EvidenceRejected):
                item.validate_assertion(changed)
        with self.assertRaises(request.EvidenceRejected):
            item.validate_assertion(dict(quantity(), value=True))
        with self.assertRaises(request.EvidenceRejected):
            item.validate_assertion(dict(quantity(), value=3.0))

    def test_registered_quantity_rendering_and_placeholder_contract(self):
        item = registry()
        answer = {'text': 'Recorded: {{quantity:' + AGG + '}}. Ada reported progress.',
                  'references': [REF], 'assertions': [quantity()]}
        out = item.validate_answer(answer)
        self.assertIn('3 events', out['text'])
        self.assertIn('complete matching recorded population', out['text'])
        self.assertNotIn('{{quantity:', out['text'])
        self.assertEqual(set(out['references']), {REF, AGG})
        for changed in [dict(answer, assertions=[]), dict(answer, text='Freehand summary'),
                        dict(answer, references=['deck:' + 'c' * 64]),
                        dict(answer, text='{{quantity:missing}}')]:
            with self.assertRaises(request.EvidenceRejected):
                item.validate_answer(changed)

    def test_unknown_quantity_is_not_rendered_as_zero(self):
        item = request.AssertionRegistry()
        value = dict(quantity(), value=None, complete=False)
        item.register({'quantities': [value]}); item.mark_supplied()
        rendered = item.render_assertion(value)
        self.assertTrue(rendered.startswith('Unknown events'))
        self.assertIn('incomplete matching recorded population; total unknown', rendered)
        with self.assertRaises(request.EvidenceRejected):
            item.register({'quantities': [dict(value, complete=True)]})

    def test_rendered_fragments_distinguish_sources_subjects_period_and_filters(self):
        first = quantity()
        second = dict(quantity(), ref='agg:' + SNAPSHOT + ':2',
                      scope={'sources': ['retained'], 'chat_ids': ['x:demo-bram'], 'project_ids': ['c' * 64],
                             'filters': {'outcome': ['failure'], 'operation': ['run']}},
                      interval={'since': 300, 'until': 400, 'time_basis': 'observed'})
        item = request.AssertionRegistry(); item.register({'quantities': [first, second]}); item.mark_supplied()
        a, b = item.render_assertion(first), item.render_assertion(second)
        self.assertNotEqual(a, b)
        for expected in ('live room', 'c:demo-ada', 'source time', request.datetime.fromtimestamp(100).astimezone().isoformat(sep=' ', timespec='seconds')):
            self.assertIn(expected, a)
        for expected in ('retained records', 'x:demo-bram', 'c' * 64, 'observation time', 'outcome: failure', 'operation: run'):
            self.assertIn(expected, b)
        changed = copy.deepcopy(second)
        changed['scope']['filters']['outcome'] = ['success']
        with self.assertRaises(request.EvidenceRejected):
            item.validate_assertion(changed)
        self.assertEqual(set(item.receipt(SCOPE, SNAPSHOT)['scope']), {'sources', 'chat_ids', 'project_ids'})

    def test_text_match_binding_uses_digest_not_raw_query(self):
        value = dict(quantity(), scope=dict(SCOPE, match={'mode': 'terms', 'digest': 'd' * 64}))
        item = request.AssertionRegistry(); item.register({'quantities': [value]}); item.mark_supplied()
        self.assertIn('token text filter ' + 'd' * 12, item.render_assertion(value))
        changed = copy.deepcopy(value); changed['scope']['match']['digest'] = 'e' * 64
        with self.assertRaises(request.EvidenceRejected):
            item.validate_assertion(changed)
        leaked = copy.deepcopy(value); leaked['scope']['match']['text'] = 'PRIVATE QUERY'
        with self.assertRaises(request.EvidenceRejected):
            item.register({'quantities': [leaked]})

    def test_reference_reuse_and_prose_free_receipt(self):
        item = registry()
        with self.assertRaises(request.EvidenceRejected):
            item.register({'quantities': [dict(quantity(), value=99)]})
        receipt = item.receipt(SCOPE, SNAPSHOT)
        self.assertNotIn('PRIVATE-SOURCE-PROSE', json.dumps(receipt))
        self.assertEqual(set(receipt), {'version', 'scope', 'snapshot', 'references', 'quantity_references'})
        with self.assertRaises(request.EvidenceRejected):
            item.receipt(dict(SCOPE, question='private question'), SNAPSHOT)

    def test_erasure_or_unavailable_source_invalidates_whole_exchange(self):
        generation = {'store': 'a' * 64, 'erasure': 0, 'live_boot': 'fictional-boot'}
        guard = request.EvidenceValidity(generation)
        self.assertTrue(guard.check(generation, {'valid': True}))
        with self.assertRaises(request.ExchangeInvalidated):
            guard.check(dict(generation, erasure=1), {'valid': True})
        with self.assertRaises(request.ExchangeInvalidated):
            guard.check(generation, {'valid': True})
        another = request.EvidenceValidity(generation)
        with self.assertRaises(request.ExchangeInvalidated):
            another.check(generation, {'valid': False, 'reason': 'expired'})

    def test_deletion_during_blocked_call_prevents_publication(self):
        generation = {'store': 'a' * 64, 'erasure': 0, 'live_boot': 'fictional-boot'}
        guard = request.EvidenceValidity(generation)
        blocked, release = threading.Event(), threading.Event()
        published = []
        def answer():
            guard.check(generation, {'valid': True})
            blocked.set()
            self.assertTrue(release.wait(2))
            try:
                guard.check(generation, {'valid': True})
                published.append('Must not publish deleted evidence')
            except request.ExchangeInvalidated:
                pass
        thread = threading.Thread(target=answer)
        thread.start()
        try:
            self.assertTrue(blocked.wait(2))
            generation['erasure'] += 1
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(published, [])


class GuardMutationTests(unittest.TestCase):
    def test_concrete_guards_are_causally_required(self):
        cases = [
            (request, '_quantity', "type(number) not in (int, float)", "not isinstance(number, (int, float))",
             AssertionTests, 'test_registration_rejects_bool_quantities'),
            (request.RequestBudget, '_totals', "sum(v['output_tokens'] for v in values)",
             "sum(v['output_tokens'] for v in values if v['settled'])",
             RequestBudgetTests, 'test_output_reservation_reconciles_only_valid_actual_usage'),
            (request.AssertionRegistry, 'validate_assertion', '_json(value) != _json(expected)', 'False',
             AssertionTests, 'test_every_typed_field_is_compared_including_bool_vs_number'),
            (request.EvidenceValidity, 'check', '_json(_structured(current_generation, 2048)) != _json(self.generation)', 'False',
             AssertionTests, 'test_erasure_or_unavailable_source_invalidates_whole_exchange'),
            (request.AssertionRegistry, 'render_assertion', "scope.get('filters', {}).items()", '{}.items()',
             AssertionTests, 'test_rendered_fragments_distinguish_sources_subjects_period_and_filters'),
        ]
        for owner, name, before, after, case, method in cases:
            original = getattr(owner, name)
            source = textwrap.dedent(inspect.getsource(original))
            self.assertIn(before, source)
            try:
                namespace = dict(request.__dict__)
                exec(compile(source.replace(before, after), '<request guard mutant>', 'exec'), namespace)
                setattr(owner, name, namespace[name])
                result = unittest.TestResult()
                case(method).run(result)
                self.assertFalse(result.wasSuccessful(), 'mutation must fail its regression: ' + name)
            finally:
                setattr(owner, name, original)


if __name__ == '__main__':
    unittest.main()
