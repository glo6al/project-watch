#!/usr/bin/env python3
"""Reproducible opt-in model evaluation over a temporary fictional archive.

Default: list cases only, without credentials, temporary stores, or API calls.
Live: one explicit model and at most two cases; the model chooses its own tools.
Reports contain fictional context only and require human semantic review.
"""
from datetime import datetime, timedelta
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch
import argparse
import copy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import server


def _module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fixture_tools = _module('officer_capability_fixture', ROOT / 'tests/test_officer_capabilities.py')
old_runner = _module('officer_demo_eval_helpers', ROOT / 'scripts/eval_officer.py')

CASES = {
    'broad_summary': {
        'question': "Summarise today's recorded work across the whole retained room. Include important changes from quiet chats as well as the busy chat, and explain any meaningful gaps.",
        'required': ['Use the fixture local day; exclude the yesterday-only deployment statement.',
                     'Retained inventory contains thirty-nine distinct chats; selected detail is not complete real capture.',
                     'Attribute Ada\'s middle-period reversal and need for renewed review to the saved statement.',
                     'Include Bram\'s rounding concern and Team36\'s withdrawn discard-old-rows plan and unresolved owner decision.',
                     'Counts remain attached to their exact unit, source, scope and period.'],
        'forbidden': ['Speculative dependencies or completion claims.',
                      'Assigning a concern to a group of agents whose supporting statements were not supplied.',
                      'Attaching a full-population count to only a selected subset.',
                      'Treating saved agent statements as independently verified work or all real activity.'],
    },
    'count_today': {
        'question': "How many saved events and recorded chats are there today across the whole retained room? Keep the requested local day and source counts clear.",
        'required': ['Exactly 1314 retained events and 39 retained chats for the fixture local day.',
                     'Typed assertions retain their exact source/unit/scope/interval/completeness.',
                     'The yesterday-only event does not enter today\'s count; all-time retained events would be 1315.',
                     'Retained completeness is distinct from capture completeness.'],
        'forbidden': ['All-time 1315 presented as today.', 'An event count relabeled as completed goals or successful deployments.',
                      'Live and retained counts added without verified deduplication.'],
    },
    'quiet_followup': {
        'question': "What about Team36's concern—what changed in its plan, and what is still waiting on the owner?",
        'requires': 'broad_summary',
        'required': ['Use the actual prior exchange as untrusted context and resolve supporting evidence again.',
                     'Team36 withdrew the discard-old-rows plan and says old session rows must be preserved.',
                     'Its revised migration plan still needs an owner decision; execution is not established.',
                     'Attribute these statements rather than inventing a current native waiting state.'],
        'forbidden': ['A migration was executed, approved or sent.', 'A prior assistant answer substitutes for saved evidence.',
                      'An unrelated chat\'s concern is assigned to Team36.'],
    },
}


def build_corpus(fixture):
    """1200 busy + 38×3 quiet today; one separate yesterday red herring."""
    today = time.strftime('%Y-%m-%d', time.localtime(fixture.now))
    start = time.mktime(time.strptime(today, '%Y-%m-%d'))
    elapsed = max(0.001, fixture.now - start)
    first = start + elapsed * 0.1
    span = elapsed * 0.8
    for index in range(1200):
        text = ('Ada states the goal is consistent seasonal pricing between catalogue and checkout.' if index == 0 else
                'Middle milestone: Ada reports the rounding fix was incomplete and the checkout change was reverted; renewed review is needed.' if index == 600 else
                'Ada reports the shared pricing change still needs review; shipping approval is not established.' if index == 1199 else
                'Ada reports another pricing inspection; this does not establish a completed goal.')
        fixture.add(text, chat='x:capability-01', name='Ada', stamp=first + span * index / 1199)
    names = ['Ada']
    for index in range(2, 40):
        name = 'Bram' if index == 2 else 'Team36' if index == 36 else 'Team%02d' % index
        names.append(name)
        texts = [name + ' reports reviewing a project change.',
                 name + ' reports the change still needs review; a completed goal is not established.',
                 name + ' reports the review remains open.']
        if name == 'Bram':
            texts[1] = 'Bram reports Ada\'s seasonal pricing change still has a one-cent discrepancy between table and checkout rounding; the shared line price needs review.'
        if name == 'Team36':
            texts = ['Team36 is reviewing the sessions migration plan.',
                     'Team36 reports the migration must preserve old session rows; its earlier discard-old-rows plan is withdrawn.',
                     'Team36 reports the revised migration plan still needs an owner decision; migration execution is not established.']
        for position, text in enumerate(texts):
            fixture.add(text, chat='x:capability-%02d' % index, name=name,
                        stamp=first + span * (position + 1) / 4)
    yesterday = (datetime.strptime(today, '%Y-%m-%d').date() - timedelta(days=1)).isoformat()
    old_stamp = time.mktime(time.strptime(yesterday, '%Y-%m-%d')) + 12 * 3600
    fixture.add('YESTERDAY ONLY: Ada reports an old deployment succeeded. This statement is outside today\'s recorded work.',
                chat='x:capability-01', name='Ada', stamp=old_stamp)
    if not fixture.history.flush(10):
        raise RuntimeError('Fictional corpus could not be retained')
    return {'today': today, 'yesterday': yesterday, 'observation_time': fixture.now,
            'retained_events_today': 1314, 'retained_events_all_time': 1315,
            'retained_chats_today': 39, 'chat_names': names, 'projects': ['Orchard'],
            'scope': 'Fictional retained statements only; no real native state or full capture guarantee.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--model', action='append', default=[], help='One exact model ID; no model preference is saved.')
    parser.add_argument('--case', action='append', default=[], help='One or two distinct case IDs, in execution order.')
    parser.add_argument('--output', help='New private report path under the temporary directory, outside the repository.')
    args = parser.parse_args(argv)
    if not args.live:
        for identity, case in CASES.items():
            print(identity + ': ' + case['question'] + (' [requires earlier broad_summary]' if case.get('requires') else ''))
        print('Offline listing only. No credential was read, archive created, or model called.')
        return 0
    if len(args.model) != 1 or not server.OFFICER_MODEL_ID.fullmatch(args.model[0]) or server.KEY_SHAPE_S.search(args.model[0]):
        parser.error('--live requires one valid explicit --model ID.')
    if not 1 <= len(args.case) <= 2 or len(set(args.case)) != len(args.case) or any(case not in CASES for case in args.case):
        parser.error('--live requires one or two distinct known --case IDs.')
    for index, identity in enumerate(args.case):
        if CASES[identity].get('requires') and CASES[identity]['requires'] not in args.case[:index]:
            parser.error('quiet_followup requires broad_summary earlier in the same invocation; no hidden warmup is made.')
    if not args.output:
        parser.error('--live requires a private --output path.')
    fixture = fixture_tools.OfficerCapabilityTests('test_dense_overview_keeps_full_counts_quiet_and_middle_evidence')
    strict_present, strict_value = 'OFFICER_REQUEST_USD' in os.environ, os.environ.get('OFFICER_REQUEST_USD')
    fixture.setUp()
    if strict_present:
        os.environ['OFFICER_REQUEST_USD'] = strict_value
    else:
        os.environ.pop('OFFICER_REQUEST_USD', None)
    output = None
    report = {'purpose': 'Reproducible real-model acceptance evidence, requiring human semantic review; no automatic GO.',
              'runs': [], 'policy': {'max_models': 1, 'max_cases': 2, 'max_calls_per_question': 7, 'max_seconds_per_question': 120,
                                     'strict_owner_policy_preserved': strict_present}}
    try:
        report['fixture'] = corpus = build_corpus(fixture)
        try:
            output = old_runner.private_output(args.output)
        except (OSError, ValueError):
            parser.error('Output must be a new private file under the temporary directory, outside the repository.')
        # No key, catalog, model setting, owner archive or native provider is read
        # during default listing or synthetic corpus setup.
        server.load_officer_key()
        if not server.OFFICER_KEY:
            report['error'] = 'The configured Officer key is unavailable. No model calls were made.'
            print(report['error'])
            return 1
        real_clock = server.officer_local_clock
        calendar = datetime.fromtimestamp(corpus['observation_time']).astimezone()
        prior_turns, prior_checked = [], False
        with patch.object(server, 'officer_local_clock', lambda: real_clock(calendar)):
            for identity in args.case:
                case = CASES[identity]
                if identity == 'quiet_followup' and not prior_checked:
                    report['runs'].append({'case': identity, 'question': case['question'], 'rubric': case,
                                           'requested_model': args.model[0], 'calls': [],
                                           'status': 'not_verified_dependency',
                                           'reason': 'The preceding broad answer lacked checked evidence and a receipt; no follow-up model call was made.'})
                    print(identity + ': not verified; preceding checked answer unavailable. No provider call made.')
                    continue
                turns = copy.deepcopy(prior_turns) if identity == 'quiet_followup' else []
                turns.append({'role': 'user', 'content': case['question'], 'context': {'scope': 'room'}})
                run = {'case': identity, 'question': case['question'], 'rubric': case,
                       'requested_model': args.model[0], 'calls': [], 'prior_checked': bool(prior_turns and prior_turns[-1].get('receipt'))}
                report['runs'].append(run)
                budget = server.officer_budget()  # Preserves strict policy; unknown bounds block, never downgrade.
                def transport(body, **kwargs):
                    response, error = server.anthropic_call(body, **kwargs)
                    run['calls'].append({'request': copy.deepcopy(body), 'response': copy.deepcopy(response), 'error': error,
                                         'tool_names': [block.get('name') for block in ((response or {}).get('content') or [])
                                                        if isinstance(block, dict) and block.get('type') == 'tool_use']})
                    return response, error
                def admitted(body, **kwargs):
                    return budget.call(transport, body, 'answer', **kwargs)
                run['result'] = server.officer_exchange(turns, admitted, context={'scope': 'room'},
                                                         model_snapshot=(args.model[0], False), budget=budget, broad=True)
                run['budget'] = budget.receipt()
                if identity == 'broad_summary':
                    prior_checked = run['result'].get('evidence_checked') is True and isinstance(run['result'].get('receipt'), dict)
                    prior_turns = copy.deepcopy(turns)
                    if run['result'].get('text'):
                        prior_turns.append({'role': 'assistant', 'content': run['result']['text'], 'context': {'scope': 'room'},
                                            **({'receipt': run['result']['receipt']} if run['result'].get('receipt') else {})})
                print(identity + ': captured ' + str(len(run['calls'])) + ' provider calls; human review required.')
        return 0
    finally:
        try:
            clean = server.scrub(report)
            server.OFFICER_KEY = ''
            if output is not None:
                json.dump(clean, output, indent=2, ensure_ascii=True)
                output.write('\n')
        finally:
            server.OFFICER_KEY = ''
            if output is not None:
                output.close()
            fixture.tearDown()


if __name__ == '__main__':
    raise SystemExit(main())
