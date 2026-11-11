#!/usr/bin/env python3
"""Real demo-derived tool evidence and counterfactuals, with no external calls.

These tests establish fixture provenance, tool behavior, and rubric sensitivity.
They do NOT establish natural-language model routing or instruction following.
The JSON questions are also a semantic rubric for an opt-in real-model review.
"""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests' / 'fixtures' / 'officer'
sys.path.insert(0, str(ROOT / 'src'))
import server


def demo_fixture():
    """No checked-in duplicate seed: derive it from the existing browser demo."""
    result = subprocess.run(['node', str(FIXTURES / 'export_demo.cjs')], cwd=ROOT,
                            check=True, capture_output=True, text=True, timeout=5)
    return json.loads(result.stdout)


@unittest.skipUnless(shutil.which('node'), 'Node is required to execute the actual demo seed')
class OfficerEvidenceEvals(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.seed = demo_fixture()
        cls.golden = json.loads((FIXTURES / 'golden_questions.json').read_text(encoding='utf-8'))

    def setUp(self):
        self.saved = {key: copy.deepcopy(getattr(server, key)) for key in
                      ('AGENTS', 'LOG', 'PENDING', 'KNOWN_NAMES')}
        self.load(copy.deepcopy(self.seed))
        self.clock = patch.object(server.time, 'time', return_value=self.seed['now'])
        self.clock.start()
        self.history = patch.object(server, 'HISTORY', None)
        self.history.start()

    def tearDown(self):
        self.history.stop()
        self.clock.stop()
        for key, value in self.saved.items():
            target = getattr(server, key)
            target.clear()
            target.update(value) if isinstance(target, dict) else target.extend(value)

    def load(self, fixture):
        # The browser seed already contains projected WATCH events. This adapter
        # intentionally tests Officer tools, not native log parsing or UI state.
        server.AGENTS.clear(); server.LOG.clear(); server.PENDING.clear(); server.KNOWN_NAMES.clear()
        for row in fixture['records']:
            row = copy.deepcopy(row)
            if row['kind'] == 'agent':
                server.AGENTS[row['agent']] = row
            else:
                server.LOG.append(row)
                if row['kind'] == 'perm':
                    server.PENDING[row['pid']] = {'aid': row['agent'], 'allow': None}

    def tool(self, name, inputs=None):
        actions = []
        result = server.officer_tool(name, inputs or {}, actions)
        return result, actions

    def test_real_demo_export_is_deterministic_and_has_no_scripted_answers(self):
        self.assertEqual(self.seed, demo_fixture())
        names = [row['name'] for row in self.seed['records'] if row['kind'] == 'agent']
        self.assertEqual(names, ['Ada', 'Bram', 'Cleo', 'Dov', 'Esme', 'Codex cloud', 'Fen'])
        self.assertEqual(len(self.seed['records']), 49)
        self.assertEqual(max(row.get('seq', 0) for row in self.seed['records']), 42)
        self.assertFalse(any(row.get('kind') == 'officer_answer' for row in self.seed['records']))

    def test_live_runner_rejects_key_shaped_model_before_call_or_output(self):
        secret = 'sk-' + 'ant-' + 'x' * 25
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/eval_officer.py'),
                                 '--live', '--model', secret, '--case', 'shift-default',
                                 '--output', '/not-used-for-rejected-model.json'],
                                cwd=ROOT, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertIn('Invalid model ID', result.stderr)
        self.assertNotIn(secret, result.stdout + result.stderr)

    def test_every_golden_question_executes_real_evidence_tools(self):
        counts = {}
        for case in self.golden['cases']:
            counts[case['skill']] = counts.get(case['skill'], 0) + 1
            self.assertTrue(case['required_evidence']); self.assertTrue(case['forbidden_claims'])
            for name, inputs in case['tools']:
                with self.subTest(question=case['id'], tool=name):
                    out, actions = self.tool(name, inputs)
                    self.assertIsInstance(out, dict)
                    self.assertEqual(actions, [], 'evidence gathering must not cause screen actions')
                    if name == 'session_day_summary':
                        self.assertIn('unavailable', out['error'].lower())
                    else:
                        self.assertNotIn('error', out)
                        self.assertTrue(out.get('steps') or out.get('hits') or out.get('agents'))
        self.assertEqual(counts, {name: 3 for name in
                         ('shift-report', 'stuck-agent', 'conflict-check', 'handover', 'watch-rule')})

    def test_shift_and_waiting_facts_match_the_snapshot(self):
        out, _ = self.tool('events_since', {'minutes': 60, 'limit': 60})
        text = json.dumps(out)
        self.assertIn('12 checks passing', text)
        self.assertNotIn('Thirteen passing', text)
        self.assertIn('only new ones', text)
        agents, _ = self.tool('list_agents')
        by_name = {a['name']: a for a in agents['agents']}
        self.assertTrue(by_name['Cleo']['waiting_for_owner_permission'])
        self.assertFalse(by_name['Esme']['waiting_for_owner_permission'])
        self.assertEqual(by_name['Dov']['parent']['how'], 'recorded')
        bram, _ = self.tool('agent_timeline', {'agent': 'Bram', 'limit': 40})
        self.assertTrue(any(s['what'] == 'its turn ended' for s in bram['steps']))
        self.assertTrue(any('read ' in s['what'] for s in bram['steps']))
        self.assertFalse(any('edited ' in s['what'] for s in bram['steps']))

    def test_synthetic_silence_counterfactual_changes_evidence_not_into_idle(self):
        before, _ = self.tool('events_since', {'minutes': 30})
        self.assertTrue(before['steps'])
        server.LOG.clear()  # Synthetic mutation: remove observations, not agent state.
        after, _ = self.tool('events_since', {'minutes': 30})
        self.assertEqual(after['steps'], [])
        self.assertIn('silence, not idleness', after['note'])
        agents, _ = self.tool('list_agents')
        self.assertTrue(all(a['last_recorded'] == 'nothing in what the room keeps' for a in agents['agents']))

    def test_synthetic_overlap_preserves_read_vs_edit_and_citations(self):
        path = next(e['path'] for e in server.LOG if e.get('id') == 'a-seed' and e['kind'] == 'edit')
        # Synthetic counterfactual: make Bram read the very same file as Ada.
        read = next(e for e in server.LOG if e.get('id') == 'b1' and e['kind'] == 'read')
        read['path'] = path
        out, _ = self.tool('search_events', {'text': path, 'limit': 30})
        bram = [s for s in out['hits'] if s['agent'] == 'Bram']
        ada = [s for s in out['hits'] if s['agent'] == 'Ada']
        self.assertTrue(bram and ada)
        self.assertTrue(all('read ' in s['what'] and 'edited ' not in s['what'] for s in bram))
        self.assertTrue(any('edited ' in s['what'] for s in ada))
        self.assertEqual(bram[0]['n'], read['seq'])
        self.assertEqual(bram[0]['id'], read['agent'])
        # Change only that observation: the evidence must change with it.
        read['kind'] = 'edit'
        changed, _ = self.tool('search_events', {'text': path, 'limit': 30})
        self.assertTrue(any(s['agent'] == 'Bram' and 'edited ' in s['what'] for s in changed['hits']))

    def test_synthetic_qualifiers_and_parent_relation_cannot_disappear(self):
        run = next(e for e in server.LOG if e.get('id') == 'g2' and e['kind'] == 'run')
        run['guess'] = True  # Synthetic uncertainty added to the real demo observation.
        cloud, _ = self.tool('agent_timeline', {'agent': 'Codex cloud', 'limit': 40})
        what = next(s['what'] for s in cloud['steps'] if 'INVENTORY.md' in s['what'])
        self.assertTrue(what.startswith('probably '))
        self.assertIn('outcome not recorded', what)
        self.assertIn('process list, not a log', cloud['source'])
        server.AGENTS['c:demo-dov'].update(soft=True, via='synthetic timing inference')
        agents, _ = self.tool('list_agents')
        self.assertEqual(next(a for a in agents['agents'] if a['name'] == 'Dov')['parent']['how'], 'inferred')
        run.pop('guess')
        changed, _ = self.tool('agent_timeline', {'agent': 'Codex cloud', 'limit': 40})
        self.assertFalse(next(s['what'] for s in changed['steps'] if 'INVENTORY.md' in s['what']).startswith('probably '))

    def test_unknown_return_and_no_result_remain_distinct_from_success(self):
        cloud, _ = self.tool('agent_timeline', {'agent': 'Codex cloud', 'limit': 40})
        words = [s['what'] for s in cloud['steps']]
        self.assertTrue(any('outcome not recorded' in w for w in words))
        self.assertTrue(any('sleep 120' in w and 'no result yet' in w for w in words))
        result = next(e for e in server.LOG if e.get('id') == 'g2' and e['kind'] == 'result')
        result['unknown'] = False  # Synthetic mutation supplies a known return.
        changed, _ = self.tool('agent_timeline', {'agent': 'Codex cloud', 'limit': 40})
        self.assertNotIn('outcome not recorded', next(s['what'] for s in changed['steps'] if 'INVENTORY.md' in s['what']))

    def test_rule_and_handover_drafts_do_not_save_approve_or_send(self):
        spelling = next(e['cmd'] for e in server.LOG if e.get('id') == 'a0' and e['kind'] == 'run')
        result, actions = self.tool('draft_watch_rule', {'words': spelling, 'show_as': 'check'})
        self.assertTrue(result['queued_for_screen'])
        self.assertIn('nothing is kept until the owner presses it', result['note'])
        self.assertEqual(actions, [{'do': 'watch_draft', 'words': spelling, 'label': 'check'}])
        draft, actions = self.tool('draft_to_clipboard', {'text': 'Fictional handover draft'})
        self.assertTrue(draft['queued_for_screen'])
        self.assertIn('do not say it is done', draft['note'])
        self.assertEqual(actions, [{'do': 'draft', 'text': 'Fictional handover draft'}])
        self.assertIsNone(server.PENDING['demo-c3']['allow'])


if __name__ == '__main__':
    unittest.main()
