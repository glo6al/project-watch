"""Answer-preservation regressions with fictional sources and scripted models only."""
import copy
import unittest
from unittest.mock import patch

import test_officer_capabilities as fixtures
from test_officer_capabilities import message, tool
import server
from officer_capabilities import authenticate_receipt, record_fingerprint, sign_receipt
from officer_evidence import EvidenceService, EvidenceSession
from deck_log import DeckLog


class AnswerAssessmentTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.OfficerCapabilityTests(); self.f.setUp(); self.addCleanup(self.f.tearDown)

    def two_source_answer(self, erase_during=False):
        f = self.f
        f.add('REMOVED_ALPHA: owner private decision.', chat='x:demo-ada', name='Ada')
        f.add('SURVIVING_BETA: report still available.', chat='x:demo-bram', name='Bram')
        self.assertTrue(f.history.flush(10))
        def model(body):
            if len(f.calls) == 1:
                return message([tool('evidence_query', f.query())])
            result = f.supplied(body)[0]
            ref = next(row['ref'] for row in result['evidence'] if row['chat_id'] == 'x:demo-bram')
            if erase_during:
                f.history.delete(chat_id='x:demo-ada')
            # Deliberately misattribute Alpha's information to Beta's citation.
            # Fresh dependency-safe rendering must not preserve this prose.
            return message([tool('finish_answer', dict(text='REMOVED_ALPHA model prose.', references=[ref], assertions=[]))])
        return f.execute(model)

    def test_erasure_during_model_preserves_beta_without_alpha_or_paid_retry(self):
        result = self.two_source_answer(erase_during=True)
        self.assertEqual(result['verification'], 'filtered')
        self.assertIn('SURVIVING_BETA', result['text'])
        self.assertNotIn('REMOVED_ALPHA', str(result))
        self.assertEqual(len(self.f.calls), 2)

    def test_erasure_before_second_paid_call_preserves_fresh_pending_survivors(self):
        original = EvidenceSession.validate_pending
        erased = []
        def erase_before_supply(evidence, results):
            if results and not erased:
                erased.append(True)
                self.f.history.delete(chat_id='x:demo-ada')
            return original(evidence, results)
        with patch.object(EvidenceSession, 'validate_pending', erase_before_supply):
            result = self.two_source_answer()
        self.assertEqual(len(self.f.calls), 1)
        self.assertEqual(result['verification'], 'filtered')
        self.assertIn('SURVIVING_BETA', result['text'])
        self.assertNotIn('REMOVED_ALPHA', str(result))

    def test_late_erasure_of_uncited_input_does_not_trust_declared_citation(self):
        result = self.two_source_answer()
        self.f.history.delete(chat_id='x:demo-ada')
        assessment = server.officer_publication(result['receipt'], result['text'])
        self.assertEqual(assessment['status'], 'filtered')
        self.assertFalse(assessment['valid'])  # Old clients must not accept their unchanged original.
        self.assertTrue(assessment['publication'])
        self.assertIn('SURVIVING_BETA', assessment['answer']['text'])
        self.assertNotIn('REMOVED_ALPHA', str(assessment))
        self.assertEqual(len(self.f.calls), 2)

    def test_archive_answer_survives_unrelated_live_ring_trim(self):
        result = self.f.answered()
        server.BASE += 1000
        assessment = server.officer_publication(result['receipt'], result['text'])
        # Derived current live totals could become stale; archive-only counts do not.
        self.assertEqual(assessment['status'], 'supported')
        self.assertEqual(assessment['answer']['text'], result['text'])
        self.assertTrue(assessment['valid'])

    def test_first_archive_input_read_after_erasure_is_supported_and_replayable(self):
        f = self.f
        f.add('Earlier removed material.', chat='x:demo-ada', name='Ada')
        f.add('Fresh surviving report.', chat='x:demo-bram', name='Bram')
        f.history.flush(10)
        def model(body):
            if len(f.calls) == 1:
                f.history.delete(chat_id='x:demo-ada')
                return message([tool('evidence_query', f.query())])
            return message([f.finish(f.supplied(body)[0], include_quantity=False)])
        result = f.execute(model)
        self.assertEqual(result['verification'], 'supported')
        self.assertEqual(result['warnings'], [])
        self.assertEqual(result['receipt']['payload']['generation']['erasure'], 1)
        self.assertTrue(server.officer_receipt_valid(result['receipt'], result['text']))
        turns = [dict(role='user', content='Earlier question'), dict(role='assistant', content=result['text'], receipt=result['receipt'])]
        replay, _ = server.officer_replay(turns, server.officer_evidence_session())
        self.assertEqual(len(replay), 2)
        self.assertEqual(replay[1]['content'], result['text'])

    def test_changed_fact_keeps_original_observation_and_original_proof(self):
        f = self.f
        view = dict(boot=server.BOOT, events=[], subjects=[], facts=[dict(subject='x:demo-ada', field='model', value='fictional-small')])
        service = EvidenceService(f.history, lambda: copy.deepcopy(view))
        session = service.start()
        fact = session.facts[0]; ref = fact['ref']
        text = 'The observed model was fictional-small.'
        receipt = sign_receipt(text, dict(refs=[ref], fingerprints={ref: record_fingerprint(fact)}, archive_used=False), session.generation(), server.BOOT)
        view['facts'][0]['value'] = 'fictional-large'
        assessment = server.officer_receipt_assessment(receipt, text, service.start())
        self.assertEqual(assessment['status'], 'stale')
        self.assertEqual(assessment['answer']['text'], text)
        self.assertEqual(assessment['answer']['receipt'], receipt)
        self.assertTrue(assessment['warnings'])

    def test_temporary_archive_failure_is_not_deletion(self):
        result = self.f.answered(); state = self.f.history.evidence_state()
        unavailable = dict(available=False, known_store=state['store'], committed_erasure=state['erasure'])
        with patch.object(self.f.history, 'evidence_state', return_value=unavailable):
            assessment = server.officer_publication(result['receipt'], result['text'])
        self.assertEqual(assessment['status'], 'unavailable')
        self.assertFalse(assessment['removed'])
        self.assertNotIn('deleted', str(assessment))

    def test_surviving_source_can_be_read_after_other_input_is_deleted(self):
        result = self.two_source_answer(); self.f.start_http()
        self.f.history.delete(chat_id='x:demo-ada')
        ref = result['cites'][0]['ref']
        status, body = self.f.post('/officer/evidence', dict(text=result['text'], receipt=result['receipt'], ref=ref))
        self.assertEqual(status, 200)
        self.assertIn('SURVIVING_BETA', body['event']['text'])
        self.assertNotIn('REMOVED_ALPHA', str(body))

    def test_forged_receipt_never_authorizes_sources_or_replay(self):
        result = self.f.answered(); receipt = copy.deepcopy(result['receipt'])
        receipt['payload']['evidence']['archive_used'] = False
        self.assertFalse(authenticate_receipt(receipt, result['text'], server.BOOT))
        assessment = server.officer_publication(receipt, result['text'])
        self.assertEqual(assessment['status'], 'invalid')
        self.assertNotIn('answer', assessment)

    def test_different_store_absence_is_unavailable_not_deletion(self):
        result = self.f.answered()
        with self.subTest('different-store'):
            # An independent fictional archive intentionally lacks the old ID.
            other = DeckLog(self.f.home + '/other-fictional-store')
            try:
                other.configure(enabled=True)
                session = EvidenceService(other, server.officer_evidence_view).start()
                assessment = server.officer_receipt_assessment(result['receipt'], result['text'], session)
            finally:
                other.close()
        self.assertEqual(assessment['status'], 'unavailable')
        self.assertFalse(assessment['removed'])
        self.assertNotIn('answer', assessment)

    def test_known_erasure_dominates_missing_or_malformed_receipt_proof(self):
        result = self.two_source_answer(); self.f.history.delete(chat_id='x:demo-ada'); self.f.start_http()
        for malformed_fallback in (False, True):
            metadata = copy.deepcopy(result['receipt']['payload']['evidence'])
            metadata['fingerprints'] = {}
            if malformed_fallback:
                metadata['fallback_refs'] = 'not-a-reference-list'
            receipt = sign_receipt(result['text'], metadata, result['receipt']['payload']['generation'], server.BOOT)
            packet = dict(result, receipt=receipt)
            assessment = server.officer_publication(receipt, result['text'])
            self.assertEqual(assessment['status'], 'filtered')
            self.assertNotIn('REMOVED_ALPHA', str(assessment))
            with patch.object(server, 'officer_ask', return_value=packet):
                status, body = self.f.post('/ask', dict(turns=[dict(role='user', content='Review activity')], context=dict(scope='room')))
            self.assertEqual(status, 200)
            self.assertEqual(body['verification'], 'filtered')
            self.assertNotIn('REMOVED_ALPHA', str(body))
            self.assertIn('SURVIVING_BETA', body['text'])

    def test_fresh_excerpts_preserve_speaker_time_and_process_qualifications(self):
        base = dict(provider='codex', chat_id='x:demo-ada', chat_name='Ada', project_id='', project_name='',
                    source_ts=self.f.now - 60, time_basis='source', certainty='recorded')
        events = [dict(base, seq=0, kind='message', role='user', text='Owner requested a review.'),
                  dict(base, seq=1, kind='message', role='assistant', text='Agent reported a review.'),
                  dict(base, seq=2, kind='operation', role='', phase='started', outcome='unknown', operation='run',
                       observation_source='process_list; completion is not established', source_qualification='Observed process only.')]
        session = EvidenceService(self.f.history, lambda: dict(boot=server.BOOT, cutoff_seq=2, events=events)).start()
        result = server.officer_surviving_answer(session, ['live:' + server.BOOT + ':' + str(i) for i in range(3)])
        self.assertIn('owner message', result['text']); self.assertIn('agent report', result['text'])
        self.assertIn('unknown', result['text']); self.assertIn('completion is not established', result['text'])
        self.assertIn('; source)', result['text'])
        self.assertNotIn('time not recorded', result['text'])

    def test_count_only_replay_retains_independent_archive_erasure_dependency(self):
        f = self.f; f.seed(); session = server.officer_evidence_session()
        text = 'The earlier retained population contained one record.'
        receipt = sign_receipt(text, dict(refs=[], fingerprints={}, archive_used=True,
                                          fallback_refs=['deck:' + f.history.search()['events'][0]['id']]), session.generation(), server.BOOT)
        original_replay = server.officer_replay
        def erase_after_replay(turns, evidence):
            out = original_replay(turns, evidence)
            self.assertIn(text, str(out))
            f.history.delete(all=True)
            return out
        turns = [dict(role='user', content='Earlier question'), dict(role='assistant', content=text, receipt=receipt), dict(role='user', content='Follow up')]
        with patch.object(server, 'officer_replay', side_effect=erase_after_replay):
            result = f.execute(lambda body: self.fail('Deleted replay input must not reach a paid call'), turns=turns)
        self.assertEqual(len(f.calls), 0)
        self.assertEqual(result['verification'], 'filtered')
        self.assertNotIn(text, result['text'])


if __name__ == '__main__':
    unittest.main()
