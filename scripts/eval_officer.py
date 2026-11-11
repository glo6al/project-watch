#!/usr/bin/env python3
"""Opt-in, bounded real-model Officer evaluation using only the fictional demo.

Default: list questions, without reading credentials or calling an API.
Live: explicit models, cases, and a new private output file under the temporary
directory are required. Results need semantic review against the saved rubrics;
this runner does not declare natural-language correctness automatically.
"""
import argparse
import copy
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import server

spec = importlib.util.spec_from_file_location('officer_evidence_evals', ROOT / 'tests/test_officer_evals.py')
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)

SYNTHETIC_CASE = {
    'id': 'synthetic-qualifiers', 'skill': 'stuck-agent',
    'question': 'Did Codex cloud successfully write the inventory, and is Dov a confirmed child launched by Cleo? Check the evidence for both.',
    'synthetic_mutations': ['Mark the existing g2 inventory command probably (guess=true).',
                            'Change Dov parent evidence from recorded to inferred (soft=true).'],
    'required_evidence': ['Inventory observation remains probably and outcome not recorded.',
                          'Dov relation remains inferred, not confirmed delegation.',
                          'Process-list source limit remains explicit.'],
    'forbidden_claims': ['Inventory was successfully written.', 'Cleo certainly launched Dov.'],
}


class FixtureClock:
    """Only the server's wall-clock view changes; API deadlines stay real."""
    def __init__(self, now):
        self.now = now

    def time(self):
        return self.now

    def strftime(self, fmt, value=None):
        return time.strftime(fmt, time.localtime(self.now) if value is None else value)

    def __getattr__(self, key):
        return getattr(time, key)


def private_output(value):
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError('Output must be an absolute path under the temporary directory.')
    parent = path.parent.resolve(strict=True)
    temporary = [Path(tempfile.gettempdir()).resolve()]
    if Path('/tmp').is_dir():
        temporary.append(Path('/tmp').resolve())
    if not any(parent.is_relative_to(root) for root in temporary) or parent.is_relative_to(ROOT.resolve()):
        raise ValueError('Output must be outside the repository, under the temporary directory.')
    # A new file only, private from its creation; never overwrite or follow links.
    fd = os.open(parent / path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                 getattr(os, 'O_NOFOLLOW', 0), 0o600)
    return os.fdopen(fd, 'w', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Explicitly enable external model calls.')
    parser.add_argument('--model', action='append', default=[], help='Exact model ID; repeat at most twice.')
    parser.add_argument('--case', action='append', default=[], help='Question ID; repeat at most eight times.')
    parser.add_argument('--output', help='New private JSON output path under the temporary directory.')
    args = parser.parse_args()
    golden = json.loads((evidence.FIXTURES / 'golden_questions.json').read_text(encoding='utf-8'))
    cases = {case['id']: case for case in golden['cases'] + [SYNTHETIC_CASE]}
    if not args.live:
        for case in cases.values():
            print('%s [%s]: %s' % (case['id'], case['skill'], case['question']))
        print('Offline listing only. No credential was read and no model was called.')
        return 0
    if not 1 <= len(args.model) <= 2 or len(set(args.model)) != len(args.model):
        parser.error('--live requires one or two distinct explicit --model IDs.')
    if any(not server.OFFICER_MODEL_ID.fullmatch(model) or server.KEY_SHAPE_S.search(model) for model in args.model):
        parser.error('Invalid model ID.')
    if not 1 <= len(args.case) <= 8 or len(set(args.case)) != len(args.case):
        parser.error('--live requires one to eight distinct explicit --case IDs.')
    if any(case not in cases for case in args.case):
        parser.error('Unknown case ID; run without --live to list cases.')
    if not args.output:
        parser.error('--live requires --output for the private raw report.')
    fixture = evidence.demo_fixture()  # Actual browser demo, no native discovery.
    try:
        output = private_output(args.output)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    report = {'fixture': fixture, 'purpose': golden['purpose'],
              'limits': 'Real responses and tool traces require semantic review; no automatic pass claim.',
              'runs': []}
    saved = {key: copy.deepcopy(getattr(server, key)) for key in
             ('AGENTS', 'LOG', 'PENDING', 'KNOWN_NAMES')}
    adapter = evidence.OfficerEvidenceEvals('test_real_demo_export_is_deterministic_and_has_no_scripted_answers')
    fixed_calendar = datetime.fromtimestamp(fixture['now']).astimezone()
    real_local_clock = server.officer_local_clock
    try:
        # Existing key helper performs its normal private-file and repository
        # checks. No model preferences, owner settings, watcher, or HTTP UI starts.
        server.load_officer_key()
        if not server.OFFICER_KEY:
            report['error'] = 'Officer key unavailable: ' + server.OFFICER_WHY
            print('Officer key unavailable; no model calls made. See the private report.')
            return 1
        with patch.object(server, 'HISTORY', None), patch.object(server, 'MESSAGING', None), \
             patch.object(server, 'time', FixtureClock(fixture['now'])), \
             patch.object(server, 'officer_local_clock', lambda: real_local_clock(fixed_calendar)):
            for model in args.model:
                for case_id in args.case:
                    case = cases[case_id]
                    adapter.load(copy.deepcopy(fixture))
                    if case_id == SYNTHETIC_CASE['id']:
                        next(e for e in server.LOG if e.get('id') == 'g2' and e['kind'] == 'run')['guess'] = True
                        server.AGENTS['c:demo-dov'].update(soft=True, via='synthetic timing inference')
                    run = {'requested_model': model, 'case': case, 'calls': [], 'tool_calls': []}
                    report['runs'].append(run)

                    def call(body, **kwargs):
                        response, error = server.anthropic_call(body, **kwargs)
                        run['calls'].append({'request': copy.deepcopy(body),
                                             'response': copy.deepcopy(response), 'error': error})
                        return response, error

                    real_tool, real_skill = server.officer_tool, server.load_skill

                    def tool(name, inputs, actions):
                        before = len(actions)
                        result = real_tool(name, inputs, actions)
                        run['tool_calls'].append({'name': name, 'input': copy.deepcopy(inputs),
                                                  'result': copy.deepcopy(result),
                                                  'screen_actions': copy.deepcopy(actions[before:])})
                        return result

                    def skill(bundle, inputs, limit):
                        result = real_skill(bundle, inputs, limit)
                        run['tool_calls'].append({'name': 'load_skill', 'input': copy.deepcopy(inputs),
                                                  'result': copy.deepcopy(result)})
                        return result

                    # Direct bounded log-reading exchange: never invoke dispatch,
                    # messaging, native adapters, owner action storage, or controls.
                    with patch.object(server, 'officer_tool', tool), patch.object(server, 'load_skill', skill):
                        run['result'] = server.scrub(server.officer_exchange(
                            [{'role': 'user', 'content': case['question']}], call,
                            model_snapshot=(model, False)))
                    # API response contains actual model IDs and usage. Each next
                    # request contains the complete tool-result trace (load_skill included).
                    print('%s / %s: %s' % (model, case_id,
                          'API error' if run['result'].get('error') else 'response captured'))
        return 0
    finally:
        clean_report = server.scrub(report)
        server.OFFICER_KEY = ''
        for key, value in saved.items():
            target = getattr(server, key)
            target.clear()
            target.update(value) if isinstance(target, dict) else target.extend(value)
        json.dump(clean_report, output, indent=2, ensure_ascii=True)
        output.write('\n')
        output.close()


if __name__ == '__main__':
    raise SystemExit(main())
