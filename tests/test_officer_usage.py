#!/usr/bin/env python3
"""Private, bounded Officer usage accounting using fictional API metadata."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
import inspect
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import officer_usage as usage


def response(model='claude-haiku-5-5', **changes):
    values = {'input_tokens': 1000, 'output_tokens': 200,
              'cache_creation_input_tokens': 300, 'cache_read_input_tokens': 300,
              'cache_creation': {'ephemeral_5m_input_tokens': 100, 'ephemeral_1h_input_tokens': 200},
              'service_tier': 'standard', 'inference_geo': 'global', 'speed': 'standard'}
    values.update(changes)
    return {'model': model, 'usage': values,
            'content': [{'type': 'text', 'text': 'PRIVATE-REPLY-MUST-NOT-PERSIST'}],
            'error': 'PRIVATE-PROVIDER-ERROR-MUST-NOT-PERSIST'}


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(os.path.realpath(self.tmp.name)) / 'private-state'
        self.now = 1800000000
        self.ledgers = []

    def tearDown(self):
        for ledger in self.ledgers:
            ledger.close()
        self.tmp.cleanup()

    def ledger(self, **kwargs):
        item = usage.UsageLedger(self.root, clock=lambda: self.now, **kwargs)
        self.ledgers.append(item)
        return item

    def record(self, ledger, value=None, error=False, stage='answer'):
        call = ledger.begin('claude-haiku-5-5', stage)
        self.assertIsNotNone(call)
        self.assertTrue(ledger.finish(call, response() if value is None else value, error))
        return call

    def test_actual_cache_components_and_dated_estimate(self):
        ledger = self.ledger()
        self.assertTrue(ledger.available)
        self.record(ledger)
        total = ledger.summary()
        self.assertEqual(total['observed_tokens'], dict(zip(usage.COUNTERS, (1000, 200, 300, 300, 100, 200))))
        self.assertEqual(Decimal(total['estimated_usd']), Decimal('0.0002555'))
        self.assertEqual(total['estimate_rate_dates'], ['2026-11-11'])
        self.assertEqual(total['rates_source'], usage.PRICE_SOURCE)
        self.assertFalse(total['tracking_incomplete'])
        self.assertFalse(total['estimate_is_partial'])
        self.assertIn('not an invoice', total['qualification'])

    def test_begin_is_durable_unknown_and_finish_is_idempotent(self):
        ledger = self.ledger()
        call = ledger.begin('claude-haiku-5-5', 'dispatch')
        second = self.ledger()
        before = second.summary()
        self.assertEqual(before['calls'], 1)
        self.assertEqual(before['inflight_calls'], 1)
        self.assertTrue(before['tracking_incomplete'])
        self.assertIsNone(before['estimated_usd'])
        self.assertTrue(ledger.finish(call, response(), False))
        self.assertTrue(ledger.finish(call, response(input_tokens=9999999), False))
        self.assertEqual(second.summary()['observed_tokens']['input_tokens'], 1000)

    def test_unknown_failed_and_missing_usage_are_never_zero_cost(self):
        ledger = self.ledger()
        first = ledger.begin('claude-haiku-5-5', 'answer')
        self.assertTrue(ledger.finish(first, None, True))
        second = ledger.begin('claude-haiku-5-5', 'answer')
        self.assertTrue(ledger.finish(second, {'model': 'claude-haiku-5-5'}, False))
        total = ledger.summary()
        self.assertEqual(total['calls'], 2)
        self.assertEqual(total['unknown_calls'], 1)
        self.assertEqual(total['partial_usage_calls'], 2)
        self.assertEqual(total['unpriced_calls'], 2)
        self.assertIsNone(total['estimated_usd'])
        self.assertTrue(total['tracking_incomplete'])

    def test_missing_invalid_or_inconsistent_counters_remain_partial(self):
        for invalid in (None, True, -1, 1.5, '5', 10**13):
            ledger = self.ledger()
            self.record(ledger, response(input_tokens=invalid))
        ledger = self.ledgers[-1]
        self.record(ledger, response(cache_creation_input_tokens=301))
        self.record(ledger, response(cache_creation={}))
        total = ledger.summary()
        self.assertEqual(total['partial_usage_calls'], 8)
        self.assertIsNone(total['estimated_usd'])
        self.assertTrue(total['tracking_incomplete'])

    def test_price_unknown_for_nonstandard_or_extra_billing_dimensions(self):
        ledger = self.ledger()
        values = [response(service_tier='priority'), response(inference_geo='us'),
                  response(speed='fast'), response(service_tier=None),
                  response(server_tool_use={'web_search_requests': 1}),
                  response(server_tool_use={'unknown_billing': 0}),
                  response(future_billing=10)]
        for value in values:
            self.record(ledger, value)
        total = ledger.summary()
        self.assertEqual(total['partial_usage_calls'], 0)
        self.assertEqual(total['priced_calls'], 0)
        self.assertIsNone(total['estimated_usd'])
        self.assertTrue(total['estimate_is_partial'])

    def test_haiku_threshold_includes_cache_creation_and_reads(self):
        ledger = self.ledger()
        self.record(ledger, response(input_tokens=99400))  # Total input exactly 100,000.
        self.assertIsNotNone(ledger.summary()['estimated_usd'])
        self.record(ledger, response(input_tokens=99401))
        total = ledger.summary()
        self.assertEqual(total['priced_calls'], 1)
        self.assertEqual(total['unpriced_calls'], 1)
        self.assertTrue(total['estimate_is_partial'])

    def test_returned_model_drives_estimate_and_unknown_models_are_unpriced(self):
        ledger = self.ledger()
        self.record(ledger, response('claude-sonnet-5-5'))
        self.assertEqual(Decimal(ledger.summary()['estimated_usd']), Decimal('0.00508'))
        self.record(ledger, response('claude-fictional-future'))
        self.record(ledger, response('/private/not-a-model'))
        self.assertEqual(ledger.summary()['unpriced_calls'], 2)
        self.assertIsNone(ledger.db.execute('SELECT returned_model FROM calls ORDER BY rowid DESC LIMIT 1').fetchone()[0])

    def test_only_metadata_is_stored_and_bad_inputs_never_throw(self):
        ledger = self.ledger()
        for model, stage in [('/private/path', 'answer'), ('sk-' + 'ant-fictional-secret-marker', 'answer'),
                             ('claude-haiku-5-5', ['answer']), ('claude-haiku-5-5', 'PRIVATE-PROMPT')]:
            self.assertIsNone(ledger.begin(model, stage))
        self.record(ledger)
        columns = {row[1] for row in ledger.db.execute('PRAGMA table_info(calls)')}
        self.assertFalse(columns & {'prompt', 'content', 'response', 'error', 'question', 'api_key'})
        raw = Path(ledger.path).read_bytes()
        for marker in (b'PRIVATE-REPLY', b'PRIVATE-PROVIDER', b'PRIVATE-PROMPT', b'sk-ant-fictional'):
            self.assertNotIn(marker, raw)
        self.assertTrue(ledger.summary()['tracking_incomplete'])

    def test_embedded_key_shape_cannot_be_stored_as_a_model(self):
        ledger = self.ledger()
        shape = 'sk-' + 'ant-' + 'x' * 25  # Fictional shape assembled only in temporary state.
        model = 'claude-' + shape
        self.assertIsNone(ledger.begin(model, 'answer'))
        self.record(ledger, response(model))
        self.assertIsNone(ledger.db.execute('SELECT returned_model FROM calls').fetchone()[0])
        self.assertNotIn(shape.encode(), Path(ledger.path).read_bytes())

    def test_thread_concurrency_records_each_call_once(self):
        ledger = self.ledger()
        def record(index):
            call = ledger.begin('claude-haiku-5-5', 'dispatch' if index % 2 else 'answer')
            self.assertIsNotNone(call)
            self.assertTrue(ledger.finish(call, response(), False))
            self.assertTrue(ledger.finish(call, response(), False))
        with ThreadPoolExecutor(max_workers=8) as workers:
            list(workers.map(record, range(40)))
        total = ledger.summary()
        self.assertEqual(total['calls'], 40)
        self.assertEqual(total['observed_tokens']['input_tokens'], 40000)
        self.assertEqual(total['inflight_calls'], 0)

    def test_retention_prunes_old_calls_and_reuses_bounded_storage(self):
        ledger = self.ledger()
        self.record(ledger)
        self.now += 91 * 86400
        self.record(ledger)
        total = ledger.summary(90)
        self.assertEqual(total['calls'], 1)
        self.assertEqual(ledger.db.execute('SELECT count(*) FROM calls').fetchone()[0], 1)
        self.assertEqual(ledger.summary(91)['reason'], 'invalid_usage_period')

    def test_disk_and_journal_cap_and_full_disk_failure_are_explicit(self):
        ledger = self.ledger(max_bytes=131072)
        self.assertTrue(ledger.available)
        page_size = ledger.db.execute('PRAGMA page_size').fetchone()[0]
        maximum = ledger.db.execute('PRAGMA max_page_count').fetchone()[0] * page_size
        self.assertLessEqual(maximum * 2 + 65536, ledger.max_bytes)
        for _ in range(500):
            if ledger.begin('claude-haiku-5-5', 'answer') is None:
                break
        self.assertFalse(ledger.available)
        self.assertTrue(ledger.summary()['tracking_incomplete'])
        self.assertEqual(ledger.status()['reason'], 'usage_storage_unavailable')
        total_size = sum(p.stat().st_size for p in self.root.iterdir())
        self.assertLessEqual(total_size, ledger.max_bytes)

    @unittest.skipIf(os.name == 'nt', 'POSIX permission and symlink fixtures')
    def test_private_directory_file_and_sidecars_checked_before_open(self):
        self.root.mkdir(mode=0o755)
        bad = self.ledger()
        self.assertFalse(bad.available)
        self.assertFalse(Path(bad.path).exists())
        self.root.chmod(0o700)
        good = self.ledger(); good.close()
        target = self.root / 'innocent'
        target.write_text('do not touch'); target.chmod(0o600)
        journal = Path(good.path + '-journal')
        journal.symlink_to(target)
        blocked = self.ledger()
        self.assertFalse(blocked.available)
        self.assertEqual(target.read_text(), 'do not touch')
        journal.unlink()
        Path(good.path).chmod(0o644)
        self.assertFalse(self.ledger().available)

    @unittest.skipIf(os.name == 'nt', 'POSIX inode and hard-link fixtures')
    def test_replacement_hardlinks_and_oversized_sidecars_fail_closed(self):
        ledger = self.ledger()
        alias = self.root / 'alias'
        os.link(ledger.path, alias)
        self.assertIsNone(ledger.begin('claude-haiku-5-5', 'answer'))
        self.assertTrue(ledger.tracking_incomplete)
        alias.unlink()
        another = self.ledger()
        sidecar = Path(another.path + '-journal')
        sidecar.touch(mode=0o600)
        with sidecar.open('r+b') as out:
            out.truncate(usage.MAX_BYTES)
        self.assertIsNone(another.begin('claude-haiku-5-5', 'answer'))
        self.assertFalse(another.available)

    def test_write_failure_preserves_unknown_attempt_and_never_raises(self):
        ledger = self.ledger()
        call = ledger.begin('claude-haiku-5-5', 'answer')
        with patch.object(ledger, '_verify', side_effect=OSError('PRIVATE-ERROR')):
            self.assertFalse(ledger.finish(call, response(), False))
        self.assertNotIn('PRIVATE-ERROR', json.dumps(ledger.summary()))
        reopened = self.ledger()
        self.assertEqual(reopened.summary()['inflight_calls'], 1)
        self.assertIsNone(reopened.summary()['estimated_usd'])

    def test_missing_or_malformed_pricing_does_not_lose_actual_usage(self):
        with patch.object(usage, 'PRICING_PATH', self.root / 'absent.json'):
            ledger = self.ledger()
        self.record(ledger)
        total = ledger.summary()
        self.assertEqual(total['observed_tokens']['input_tokens'], 1000)
        self.assertIsNone(total['estimated_usd'])
        self.assertIsNone(total['rates_date'])

    def test_malformed_rate_snapshot_fails_closed(self):
        self.root.mkdir(mode=0o700)
        pricing = self.root / 'pricing.json'
        value = json.loads(usage.PRICING_PATH.read_text())
        value['models']['claude-haiku-5-5']['input'] = 'NaN'
        pricing.write_text(json.dumps(value))
        with patch.object(usage, 'PRICING_PATH', pricing):
            ledger = self.ledger()
        self.record(ledger)
        self.assertIsNone(ledger.summary()['estimated_usd'])
        self.assertEqual(ledger.summary()['observed_tokens']['input_tokens'], 1000)

    def test_delete_securely_clears_records_and_preserves_other_connections(self):
        ledger = self.ledger()
        self.record(ledger)
        pending = ledger.begin('claude-haiku-5-5', 'answer')
        second = self.ledger()
        identity = Path(ledger.path).stat().st_ino
        other = self.root / 'unrelated-settings.json'
        other.write_text('{}')
        self.assertTrue(second.delete())
        self.assertTrue(Path(ledger.path).exists())
        self.assertEqual(Path(ledger.path).stat().st_ino, identity)
        self.assertEqual(ledger.summary()['calls'], 0)
        self.assertFalse(ledger.finish(pending, response(), False))
        self.assertEqual(ledger.status()['reason'], 'usage_record_missing')
        self.assertTrue(ledger.available)
        self.record(ledger)
        self.assertEqual(second.summary()['calls'], 1)
        self.assertTrue(other.exists())
        self.assertEqual(second.status()['reason'], 'usage_records_deleted')


class UsageGuardMutationTests(unittest.TestCase):
    def test_guards_have_causal_regressions(self):
        cases = [
            (usage, '_count', 'type(value) is int', 'isinstance(value, int)',
             'test_missing_invalid_or_inconsistent_counters_remain_partial'),
            (usage, '_estimate', "counts['input_tokens'] + counts['cache_creation_input_tokens'] + counts['cache_read_input_tokens']",
             "counts['input_tokens']", 'test_haiku_threshold_includes_cache_creation_and_reads'),
        ]
        for owner, name, before, after, test in cases:
            original = getattr(owner, name)
            source = inspect.getsource(original)
            self.assertIn(before, source)
            try:
                exec(compile(source.replace(before, after, 1), '<isolated usage guard mutant>', 'exec'), usage.__dict__)
                result = unittest.TestResult()
                UsageTests(test).run(result)
                self.assertFalse(result.wasSuccessful(), 'mutation must be detected: ' + name)
            finally:
                setattr(owner, name, original)


if __name__ == '__main__':
    unittest.main()
