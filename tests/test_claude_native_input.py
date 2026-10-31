"""Offline callback/schema/protocol tests. All identities and payloads are fictional."""
import copy
import io
import json
from pathlib import Path
import sys
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import claude_native_input as n

FIXTURES = Path(__file__).parent / 'fixtures' / 'claude_native_input'


def payload(name='question'):
    return json.loads((FIXTURES / (name + '.json')).read_text(encoding='utf-8'))


def decision():
    return {'kind': 'claude_question_answers', 'answers': [
        {'question_index': 0, 'option_indices': [1]},
        {'question_index': 1, 'option_indices': [0, 1, 2], 'other': 'Other\\line\n雪'}]}


class Clock:
    def __init__(self):
        self.now = 10.0

    def __call__(self):
        return self.now


class NativeInputTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.inv = n.HookInvocation.create(payload(), str(uuid.uuid4()), 70, self.clock)

    def test_standard_return_preserves_complete_original_input(self):
        output = n.parse_json(self.inv.native_output(decision()))['hookSpecificOutput']
        self.assertEqual(output['hookEventName'], 'PreToolUse')
        self.assertEqual(output['permissionDecision'], 'allow')
        updated = output['updatedInput']
        original = self.inv.payload['tool_input']
        self.assertEqual({k: v for k, v in updated.items() if k != 'answers'}, original)
        self.assertEqual(updated['answers']['Choose the theme?'], 'Forest')
        self.assertEqual(updated['answers']['Choose the labels?'], '"One, two", "A \\"quote\\"", 雪, Other\\line\n雪')

    def test_multiselect_native_canonical_quoting(self):
        values = ['a, b', '"quoted"', 'a\\b', 'line\nnext', '雪']
        self.assertEqual(n.encode_multiselect(values), '"a, b", "\\"quoted\\"", a\\b, line\nnext, 雪')

    def test_free_text_not_label_enum_or_trimmed(self):
        d = decision()
        d['answers'][0] = {'question_index': 0, 'other': '  owner text\n雪  '}
        out = n.parse_json(self.inv.native_output(d))
        self.assertEqual(out['hookSpecificOutput']['updatedInput']['answers']['Choose the theme?'], '  owner text\n雪  ')

    def test_snapshot_and_binding_immutable(self):
        p = payload()
        inv = n.HookInvocation.create(p, str(uuid.uuid4()), 70, self.clock)
        before = inv.payload_digest
        p['tool_input']['questions'][0]['question'] = 'changed'
        exposed = inv.payload
        exposed['tool_input']['questions'].clear()
        binding = inv.binding
        binding['session_id'] = 'changed'
        self.assertEqual(inv.payload_digest, before)
        self.assertEqual(len(inv.payload['tool_input']['questions']), 2)
        self.assertEqual(inv.binding['session_id'], payload()['session_id'])

    def test_skip_explicit_and_all_skipped_requires_explicit_action(self):
        d = decision()
        d['answers'][1] = {'question_index': 1, 'skip': True}
        out = n.parse_json(self.inv.native_output(d))
        self.assertEqual(out['hookSpecificOutput']['updatedInput']['answers'], {'Choose the theme?': 'Forest'})
        d['answers'][0] = {'question_index': 0, 'skip': True}
        with self.assertRaises(n.InputError):
            self.inv.native_output(d)
        d['skip_all'] = True
        self.assertEqual(n.parse_json(self.inv.native_output(d))['hookSpecificOutput']['updatedInput']['answers'], {})

    def test_invalid_owner_answers_rejected(self):
        bad = []
        for change in [lambda d: d.update(kind='permission'),
                       lambda d: d.update(allow=True),
                       lambda d: d['answers'][0].update(option_indices=[True]),
                       lambda d: d['answers'][0].update(option_indices=[8]),
                       lambda d: d['answers'][0].update(option_indices=[0, 1]),
                       lambda d: d['answers'][0].update(other='also'),
                       lambda d: d['answers'][0].update(question_index=1),
                       lambda d: d['answers'][1].update(option_indices=[1, 1]),
                       lambda d: d['answers'][0].update(other='x' * 33000),
                       lambda d: d['answers'].pop(),
                       lambda d: d['answers'][0].update(skip=False)]:
            d = decision()
            change(d)
            bad.append(d)
        for d in bad:
            with self.subTest(case=bad.index(d)), self.assertRaises(n.InputError):
                self.inv.native_output(d)

    def test_unsupported_native_schema_falls_out(self):
        for mode in ('kind', 'answers', 'duplicate', 'empty', 'wrong_event', 'wrong_tool', 'missing_tool', 'extended', 'bad_session'):
            p = payload()
            if mode == 'kind': p['tool_input']['questions'][0]['kind'] = 'text'
            if mode == 'answers': p['tool_input']['answers'] = {}
            if mode == 'duplicate': p['tool_input']['questions'][1]['question'] = p['tool_input']['questions'][0]['question']
            if mode == 'empty': p['tool_input']['questions'][0]['options'] = []
            if mode == 'wrong_event': p['hook_event_name'] = 'PermissionRequest'
            if mode == 'wrong_tool': p['tool_name'] = 'Bash'
            if mode == 'missing_tool': del p['tool_use_id']
            if mode == 'extended': p['tool_input']['followUp'] = True
            if mode == 'bad_session': p['session_id'] = 'guessed'
            with self.subTest(mode=mode), self.assertRaises(n.InputError):
                n.HookInvocation.create(p, str(uuid.uuid4()), 70, self.clock)

    def test_invalid_json_duplicates_nan_encoding_and_size(self):
        for raw in (b'{"x":1,"x":2}', b'{"x":NaN}', b'\xff', b'{}' * n.MAX_INPUT, b''):
            with self.assertRaises(n.InputError): n.parse_json(raw)

    def test_form_separate_typed_false_zero_empty_and_optional(self):
        inv = n.HookInvocation.create(payload('form'), str(uuid.uuid4()), 70, self.clock)
        self.assertNotIn('tool_use_id', inv.binding)
        d = {'kind': 'mcp_form_accept', 'content': {'name': '', 'enabled': False, 'count': 0}}
        out = n.parse_json(inv.native_output(d))['hookSpecificOutput']
        self.assertEqual(out, {'hookEventName': 'Elicitation', 'action': 'accept', 'content': d['content']})
        for action in ('decline', 'cancel'):
            out = n.parse_json(inv.native_output({'kind': 'mcp_form_' + action}))
            self.assertEqual(out['hookSpecificOutput'], {'hookEventName': 'Elicitation', 'action': action})
        with self.assertRaises(n.InputError): inv.native_output(decision())
        with self.assertRaises(n.InputError): self.inv.native_output(d)

    def test_form_strict_validation(self):
        inv = n.HookInvocation.create(payload('form'), str(uuid.uuid4()), 70, self.clock)
        for values in ({'name': '', 'enabled': False}, {'name': '', 'enabled': 0, 'count': 0},
                       {'name': '', 'enabled': False, 'count': True}, {'name': '', 'enabled': False, 'count': 6},
                       {'name': '', 'enabled': False, 'count': 0, 'extra': ''},
                       {'name': '', 'enabled': False, 'count': 0, 'color': 'Blue display label'},
                       {'name': 'x' * 41, 'enabled': False, 'count': 0}):
            with self.assertRaises(n.InputError): inv.native_output({'kind': 'mcp_form_accept', 'content': values})

    def test_forms_unsupported_never_weakened(self):
        for prop in ({'type': 'object'}, {'type': 'array', 'items': {'type': 'string'}},
                     {'type': 'string', 'format': 'email'}, {'type': 'string', 'isSecret': True},
                     {'type': 'string', 'oneOf': [{'const': 'a', 'title': 'A'}]},
                     {'type': 'number', 'minimum': float('inf')}, {'type': 'boolean', 'default': 'yes'}):
            p = payload('form')
            p['requested_schema']['properties']['name'] = prop
            with self.assertRaises(n.InputError): n.HookInvocation.create(p, str(uuid.uuid4()), 70, self.clock)
        p = payload('form'); p['mode'] = 'url'
        with self.assertRaises(n.InputError): n.HookInvocation.create(p, str(uuid.uuid4()), 70, self.clock)

    def test_no_decision_is_empty(self):
        self.assertEqual(self.inv.native_output({'kind': 'return_to_native'}), b'')

    def test_callback_requires_separate_owner_authorization(self):
        cb = n.OriginalCallback(self.inv, self.clock)
        with self.assertRaises(n.InputError): cb.reserve_owner_decision(decision(), str(uuid.uuid4()))
        self.assertEqual(cb.state, 'pending')

    def test_callback_first_decision_idempotent_but_output_not_replayed(self):
        cb = n.OriginalCallback(self.inv, self.clock)
        sid = str(uuid.uuid4())
        self.assertEqual(cb.reserve_owner_decision(decision(), sid, owner_authorized=True), 'reserved')
        self.assertEqual(cb.reserve_owner_decision(decision(), sid, owner_authorized=True), 'reserved')
        self.assertEqual(cb.take_output(), self.inv.native_output(decision()))
        self.assertEqual(cb.reserve_owner_decision(decision(), sid, owner_authorized=True), 'output_taken')
        with self.assertRaises(n.InputError): cb.take_output()
        changed = decision(); changed['answers'][0]['option_indices'] = [0]
        with self.assertRaises(n.InputError): cb.reserve_owner_decision(changed, sid, owner_authorized=True)

    def test_callback_cancellation_expiry_and_disconnect_win(self):
        for outcome in ('cancelled', 'expired', 'disconnected'):
            cb = n.OriginalCallback(self.inv, self.clock)
            cb.reserve_owner_decision(decision(), str(uuid.uuid4()), owner_authorized=True)
            if outcome == 'expired': self.clock.now = 71
            else: cb.close(outcome)
            with self.assertRaises(n.InputError): cb.take_output()
            self.assertEqual(cb.state, outcome)
            self.clock.now = 10

    def test_callback_external_cancel_signal_and_no_decision(self):
        cancelled = [False]
        cb = n.OriginalCallback(self.inv, self.clock, lambda: cancelled[0])
        cancelled[0] = True
        with self.assertRaises(n.InputError): cb.reserve_owner_decision(decision(), str(uuid.uuid4()), owner_authorized=True)
        cb = n.OriginalCallback(self.inv, self.clock)
        cb.reserve_owner_decision({'kind': 'return_to_native'}, str(uuid.uuid4()), owner_authorized=True)
        self.assertEqual(cb.take_output(), b'')
        self.assertEqual(cb.state, 'relinquished_to_native')

    def test_extreme_numeric_deadline_refused_without_overflow(self):
        with self.assertRaises(n.InputError):
            n.HookInvocation.create(payload(), str(uuid.uuid4()), 10 ** 1000, self.clock)

    def test_repr_has_no_question_or_answer(self):
        self.assertNotIn('Choose', repr(self.inv))
        self.assertNotIn('Ocean', repr(self.inv))


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.cred = n.HookCredential(str(uuid.uuid4()), b'k' * 32)
        self.port = 12345
        self.service = n.HookProtocol(self.cred, self.port, clock=self.clock)
        self.inv = n.HookInvocation.create(payload(), str(uuid.uuid4()), 70, self.clock)
        self.token = object()
        self.req = n.challenge_request(self.cred, 'a' * 64, self.inv.invocation_id)
        self.proof = self.service.issue_challenge(self.req, self.token)
        self.raw, self.sig = n.invocation_request(self.cred, self.proof, self.req, self.inv, self.port)

    def verify(self, raw, sig, **kwargs):
        return n.verify_response(self.cred, self.proof, self.req, self.inv, self.port, 200, raw, sig, clock=self.clock, **kwargs)

    def response(self, inv=None):
        return self.service.response(inv or self.inv, self.req['client_nonce'], decision())

    def resign(self, body):
        raw = n.json_bytes(body)
        sig = n.mac(self.cred, 'watch-native-input-response-v1', self.port, self.proof['run_id'], self.req['client_nonce'], 200, n.digest(raw))
        return raw, sig

    def test_mutual_proof_and_exact_return(self):
        n.verify_challenge(self.cred, self.req, self.proof, self.port)
        got = self.service.accept_invocation(self.raw, self.sig, self.token)
        self.assertEqual(got.binding, self.inv.binding)
        self.assertEqual(self.verify(*self.response(got)), self.inv.native_output(decision()))

    def test_challenge_wrong_key_port_nonce_or_signature(self):
        wrong = n.HookCredential(self.cred.credential_id, b'z' * 32)
        for cred, req, port in ((wrong, self.req, self.port), (self.cred, self.req, self.port + 1),
                                (self.cred, dict(self.req, client_nonce='b' * 64), self.port)):
            with self.assertRaises(n.InputError): n.verify_challenge(cred, req, self.proof, port)
        bad = dict(self.proof, proof='0' * 64)
        with self.assertRaises(n.InputError): n.verify_challenge(self.cred, self.req, bad, self.port)

    def test_challenge_is_connection_bound_one_use(self):
        with self.assertRaises(n.InputError): self.service.accept_invocation(self.raw, self.sig, object())
        self.service.accept_invocation(self.raw, self.sig, self.token)
        with self.assertRaises(n.InputError): self.service.accept_invocation(self.raw, self.sig, self.token)
        with self.assertRaises(n.InputError): self.service.issue_challenge(self.req, object())

    def test_request_auth_run_nonce_method_and_payload_binding(self):
        for key, value in (('run_id', str(uuid.uuid4())), ('client_nonce', 'c' * 64), ('server_nonce', 'd' * 64)):
            service = n.HookProtocol(self.cred, self.port, clock=self.clock)
            token = object(); proof = service.issue_challenge(self.req, token)
            raw, _ = n.invocation_request(self.cred, proof, self.req, self.inv, self.port)
            data = n.parse_json(raw); data[key] = value; raw = n.json_bytes(data)
            sig = n.mac(self.cred, 'watch-native-input-request-v1', self.port, 'POST', n.INVOCATION_PATH, n.digest(raw))
            with self.assertRaises(n.InputError): service.accept_invocation(raw, sig, token)
        for sig in ('0' * 64, n.mac(self.cred, 'watch-bridge-request-v2', n.digest(self.raw)),
                    n.mac(self.cred, 'watch-native-input-request-v1', self.port, 'GET', n.INVOCATION_PATH, n.digest(self.raw))):
            with self.assertRaises(n.InputError): self.service.accept_invocation(self.raw, sig, self.token)

    def test_client_nonce_reuse_rejected_even_for_another_invocation(self):
        req = dict(self.req, invocation_id=str(uuid.uuid4()))
        with self.assertRaises(n.InputError):
            self.service.issue_challenge(req, object())

    def test_expired_challenge_rejected(self):
        self.clock.now = 21
        with self.assertRaises(n.InputError): self.service.accept_invocation(self.raw, self.sig, self.token)

    def test_hook_payload_cannot_submit_owner_decision(self):
        data = n.parse_json(self.raw); data['decision'] = decision(); raw = n.json_bytes(data)
        sig = n.mac(self.cred, 'watch-native-input-request-v1', self.port, 'POST', n.INVOCATION_PATH, n.digest(raw))
        with self.assertRaises(n.InputError): self.service.accept_invocation(raw, sig, self.token)

    def test_signed_wrong_callback_schema_epoch_and_invocation_rejected(self):
        body = n.parse_json(self.response()[0])
        for key in ('invocation_id', 'binding_digest', 'payload_digest', 'run_id'):
            changed = dict(body, **{key: str(uuid.uuid4()) if key.endswith('_id') else '0' * 64})
            with self.subTest(key=key), self.assertRaises(n.InputError): self.verify(*self.resign(changed))

    def test_response_signature_and_request_nonce_rejected(self):
        raw, sig = self.response()
        with self.assertRaises(n.InputError): self.verify(raw, '0' * 64)
        wrong = n.mac(self.cred, 'watch-native-input-response-v1', self.port, self.proof['run_id'], 'f' * 64, 200, n.digest(raw))
        with self.assertRaises(n.InputError): self.verify(raw, wrong)

    def test_two_identical_callback_payloads_cannot_swap_answers(self):
        other = n.HookInvocation.create(payload(), str(uuid.uuid4()), 70, self.clock)
        with self.assertRaises(n.InputError): self.verify(*self.response(other))
        a = n.HookInvocation.create(payload('form'), str(uuid.uuid4()), 70, self.clock)
        b = n.HookInvocation.create(payload('form'), str(uuid.uuid4()), 70, self.clock)
        self.assertNotEqual(a.binding_digest, b.binding_digest)

    def test_verified_response_still_rejects_bad_decision(self):
        body = n.parse_json(self.response()[0]); body['decision']['answers'][0]['option_indices'] = [999]
        with self.assertRaises(n.InputError): self.verify(*self.resign(body))

    def test_response_deadline_cancel_and_no_decision(self):
        raw, sig = self.response()
        with self.assertRaises(n.InputError): self.verify(raw, sig, cancelled=lambda: True)
        self.clock.now = 71
        with self.assertRaises(n.InputError): self.verify(raw, sig)
        self.clock.now = 10
        self.assertEqual(self.verify(*self.service.response(self.inv, self.req['client_nonce'])), b'')

    def test_response_cannot_change_original_questions(self):
        p = self.inv.payload; p['tool_input']['questions'][0]['options'][0]['label'] = 'Changed'
        other = n.HookInvocation.create(p, self.inv.invocation_id, 70, self.clock)
        with self.assertRaises(n.InputError): self.verify(*self.response(other))

    def test_signing_domains_do_not_publish_raw_secret(self):
        self.assertNotIn('kkkk', repr(self.cred))
        self.assertNotIn('Ocean', self.sig)
        self.assertEqual(len(self.sig), 64)


if __name__ == '__main__':
    unittest.main()
