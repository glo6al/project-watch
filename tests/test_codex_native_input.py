"""Offline fictional native-input frames only; never contacts desktop or models."""
from copy import deepcopy
from dataclasses import replace
import importlib
import json
from pathlib import Path
import struct
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import codex_native_input as native

THREAD = '11111111-1111-4111-8111-111111111111'
OWNER = '22222222-2222-4222-8222-222222222222'
CLIENT = '33333333-3333-4333-8333-333333333333'
EPOCH = '44444444-4444-4444-8444-444444444444'
OTHER = '55555555-5555-4555-8555-555555555555'


def request(identity=7):
    return {'id': identity, 'method': native.QUESTION, 'params': {
        'threadId': THREAD, 'turnId': 'turn-fiction', 'itemId': 'item-fiction',
        'isBlocking': False, 'autoResolutionMs': None,
        'questions': [{'id': 'choice', 'header': 'Choose', 'question': 'Which color?',
                       'isOther': False, 'isSecret': False,
                       'options': [{'label': 'Blue', 'description': 'First'},
                                   {'label': 'Gold', 'description': 'Second'}]},
                      {'id': 'text', 'header': 'Text', 'question': 'What name?',
                       'isOther': True, 'options': None}]}}


def snapshot(requests=None, revision=3):
    return envelope({'type': 'snapshot', 'revision': revision, 'conversationState': {
        'id': THREAD, 'hostId': 'local', 'threadRuntimeStatus': 'active',
        'requests': [request()] if requests is None else requests,
        'turns': [], 'turnHistory': {'history': {'entitiesByKey': {
            'fiction': {'content': 'PRIVATE HISTORY NEVER RETAINED'}}}}}})


def envelope(change):
    return {'type': 'broadcast', 'sourceClientId': OWNER, 'version': 11,
            'method': 'thread-stream-state-changed', 'targetClientIds': [CLIENT],
            'params': {'hostId': 'local', 'conversationId': THREAD, 'change': change}}


def patches(operations, base=3, revision=4):
    return envelope({'type': 'patches', 'baseRevision': base, 'revision': revision,
                     'patches': operations, 'acceptedTextChanges': []})


def answer():
    return {'answers': {'choice': {'answers': ['Blue']}, 'text': {'answers': ['  Iris  ']}}}


def observer():
    return native.NativeInputObserver(native.Binding(THREAD, OWNER, CLIENT, EPOCH))


class ReducerTests(unittest.TestCase):
    def setUp(self):
        self.o = observer()
        self.o.consume(snapshot())

    def test_schema_preserved_private_immutable_and_history_discarded(self):
        d = self.o.descriptors[0]
        self.assertEqual(d.raw_request(), request())
        self.assertNotIn('Which color', repr(d))
        self.assertNotIn('turnHistory', self.o._projection)
        self.assertEqual(self.o._projection['threadRuntimeStatus'], 'active')
        changed = d.raw_request()
        changed['params']['questions'][0]['question'] = 'Changed'
        self.assertEqual(d.raw_request(), request())
        self.assertEqual(self.o.validate_response(d, answer()), answer())

    def test_private_metadata_repr_and_malformed_identity(self):
        sentinel = 'FICTIONAL_SECRET_METADATA'
        for field_name in ('turnId', 'itemId'):
            r = request(); r['params'][field_name] = {'private_payload': sentinel}
            with self.assertRaises(native.InvalidObservation) as raised:
                observer().consume(snapshot([r]))
            self.assertNotIn(sentinel, str(raised.exception))
        for field_name in ('id', 'method', 'turnId', 'itemId'):
            r = request()
            target = r if field_name in ('id', 'method') else r['params']
            target[field_name] = sentinel
            o = observer(); o.consume(snapshot([r])); d = o.descriptors[0]
            self.assertNotIn(sentinel, repr(d))
            self.assertIn(sentinel, d.raw_json)  # Preserved only in private descriptor.

    def test_numeric_string_bool_identity(self):
        o = observer()
        o.consume(snapshot([request(7), request('7')]))
        self.assertNotEqual(o.descriptors[0].native_id, o.descriptors[1].native_id)
        for bad in (True, False, 1.0, None, [], 2**60):
            with self.subTest(bad=bad), self.assertRaises(native.InvalidObservation):
                observer().consume(snapshot([request(bad)]))
        with self.assertRaises(native.InvalidObservation):
            observer().consume(snapshot([request(), request()]))

    def test_wrong_owner_thread_host_and_recipient(self):
        for path, value in [(('sourceClientId',), OTHER),
                            (('params', 'conversationId'), OTHER),
                            (('params', 'hostId'), 'remote')]:
            msg = patches([{'op': 'remove', 'path': ['requests', 0]}])
            obj = msg
            for k in path[:-1]: obj = obj[k]
            obj[path[-1]] = value
            self.assertFalse(self.o.consume(msg))
            self.assertEqual(len(self.o.descriptors), 1)
        msg = patches([])
        msg['targetClientIds'] = [OTHER]
        with self.assertRaises(native.InvalidObservation): self.o.consume(msg)
        self.assertFalse(self.o.available)

    def test_wrong_state_and_request_thread(self):
        for state in (True, False):
            msg = snapshot()
            obj = msg['params']['change']['conversationState']
            if state: obj['id'] = OTHER
            else: obj['requests'][0]['params']['threadId'] = OTHER
            with self.assertRaises(native.InvalidObservation): observer().consume(msg)

    def test_revision_gap_duplicate_and_type_revoke(self):
        for base, revision in [(2, 4), (3, 5), (3, 3), (True, 4), (3, True)]:
            o = observer(); o.consume(snapshot())
            with self.assertRaises(native.InvalidObservation):
                o.consume(patches([], base, revision))
            self.assertFalse(o.available)
            self.assertEqual(o.descriptors, ())

    def test_atomic_failure_and_resnapshot_does_not_revive_old_descriptor(self):
        d = self.o.descriptors[0]
        with self.assertRaises(native.InvalidObservation):
            self.o.consume(patches([{'op': 'remove', 'path': ['requests', 0]},
                                    {'op': 'move', 'path': ['requests']}]))
        self.assertEqual(self.o.descriptors, ())
        self.o.consume(snapshot())
        with self.assertRaises(native.InvalidObservation): self.o.validate_response(d, answer())

    def test_nested_patch_root_replace_and_discarded_history(self):
        original = self.o.descriptors[0]
        self.o.consume(patches([{'op': 'replace', 'path': ['requests', 0, 'params', 'itemId'],
                                'value': 'item-new'},
                               {'op': 'replace', 'path': ['turnHistory', 'arbitrary'],
                                'value': 'discard me'}]))
        self.assertEqual(self.o.descriptors[0].item_id, 'item-new')
        with self.assertRaises(native.InvalidObservation): self.o.validate_response(original, answer())
        state = snapshot([])['params']['change']['conversationState']
        self.o.consume(patches([{'op': 'replace', 'path': [], 'value': state}], 4, 5))
        self.assertEqual(self.o.descriptors, ())

    def test_add_remove_array_and_invalid_paths(self):
        self.o.consume(patches([{'op': 'add', 'path': ['requests', 1], 'value': request('7')},
                               {'op': 'remove', 'path': ['requests', 0]}]))
        self.assertEqual(self.o.descriptors[0].native_id, ('string', '7'))
        for path in ('/requests/0', ['requests', True], ['requests', -1],
                     ['requests', '0'], ['__proto__', 'requests']):
            o = observer(); o.consume(snapshot())
            with self.assertRaises(native.InvalidObservation):
                o.consume(patches([{'op': 'remove', 'path': path}]))

    def test_secret_unknown_schema_and_approval_not_answerable(self):
        for mutate, reason in [
            (lambda r: r['params']['questions'][0].update(isSecret=True), 'secret_native_only'),
            (lambda r: r['params']['questions'][0].update(multiselect=True), 'unsupported_schema'),
            (lambda r: r.update(method='item/commandExecution/requestApproval'), 'unsupported_method')]:
            r = request(); mutate(r)
            o = observer(); o.consume(snapshot([r])); d = o.descriptors[0]
            self.assertEqual(d.reason, reason)
            self.assertFalse(d.supported_schema)
            with self.assertRaises(native.InvalidObservation): o.validate_response(d, answer())

    def test_response_shape_options_cardinality_and_exact_binding(self):
        d = self.o.descriptors[0]
        for mutate in [lambda a: a['answers'].pop('text'),
                       lambda a: a['answers'].update(extra={'answers': ['x']}),
                       lambda a: a['answers']['choice'].update(answers=['Green']),
                       lambda a: a['answers']['choice'].update(answers=['Blue', 'Gold']),
                       lambda a: a['answers']['choice'].update(answers=[]),
                       lambda a: a['answers']['choice'].update(answers=['']),
                       lambda a: a['answers']['choice'].update(secret='x')]:
            a = answer(); mutate(a)
            with self.assertRaises(native.InvalidObservation): self.o.validate_response(d, a)
        for stale in [replace(d, revision=9), replace(d, schema_hash='wrong'),
                      replace(d, method='other'), replace(d, turn_id='other'),
                      replace(d, item_id='other'), replace(d, native_id=('string', '7')),
                      replace(d, binding=replace(d.binding, connection_epoch=OTHER))]:
            with self.assertRaises(native.InvalidObservation): self.o.validate_response(stale, answer())

    def test_owner_disconnect_or_ipc_reset_revokes_and_cannot_revive(self):
        for method, params in [('ipc-connection-reset', {}),
                               ('client-status-changed', {'clientId': OWNER, 'status': 'disconnected'})]:
            o = observer(); o.consume(snapshot())
            with self.assertRaises(native.InvalidObservation):
                o.consume({'type': 'broadcast', 'method': method, 'params': params})
            self.assertFalse(o.available)
            with self.assertRaises(native.InvalidObservation): o.consume(snapshot())

    def test_unconditional_no_dispatch_including_hidden_generation_reuse(self):
        old = self.o.descriptors[0]
        # Same native ID after backend restart can be indistinguishable on wire.
        self.o.invalidate(); self.o.consume(snapshot())
        for d in (old, self.o.descriptors[0], None):
            for i in range(2):
                self.assertEqual(self.o.dispatch_reserved(d, {'operation': 'same', 'response': answer()}),
                                 {'attempted': False, 'status': 'not_attempted',
                                  'reason': 'native_generation_unproven', 'consumed': False})
        self.o.consume(patches([{'op': 'remove', 'path': ['requests', 0]}]))
        self.assertFalse(self.o.dispatch_reserved(old, {'ack': {'ok': True}})['consumed'])
        self.o.close()
        with self.assertRaises(native.InvalidObservation): self.o.consume(snapshot())


class BoundsTests(unittest.TestCase):
    def test_large_snapshot_canonical_and_legacy_history_not_retained(self):
        msg = snapshot()
        state = msg['params']['change']['conversationState']
        state['turnHistory']['history']['entitiesByKey']['fiction']['content'] = 'x' * 6_700_000
        o = observer(); o.consume(native.decode_frame(json.dumps(msg).encode()))
        self.assertLess(len(json.dumps(o._projection)), 2000)
        state.pop('turnHistory'); state['turns'] = [{'content': 'legacy'}]
        observer().consume(native.decode_frame(json.dumps(msg).encode()))

    def test_encoded_depth_nodes_duplicates_invalid_json(self):
        bad = [b'[' * 65 + b']' * 65, b'{"a":1,"a":2}', b'{"a":NaN}', b'{} trailing']
        for raw in bad:
            with self.assertRaises(native.InvalidObservation): native.decode_frame(raw)
        with patch.object(native, 'MAX_NODES', 10):
            with self.assertRaises(native.InvalidObservation):
                native.decode_frame(b'{"a":[1,2,3,4,5,6,7,8,9,10]}')
        with patch.object(native, 'MAX_DEPTH', 4):
            with self.assertRaises(native.InvalidObservation):
                observer().consume(snapshot())
        # Delimiters escaped inside strings do not count as nesting.
        self.assertEqual(native.decode_frame(b'{"a":"[\\\"{]"}'), {'a': '["{]'})

    def test_projection_and_patch_limits(self):
        with patch.object(native, 'MAX_PROJECTION', 10):
            with self.assertRaises(native.InvalidObservation): observer().consume(snapshot())
        o = observer(); o.consume(snapshot())
        with patch.object(native, 'MAX_PATCHES', 0):
            with self.assertRaises(native.InvalidObservation):
                o.consume(patches([{'op': 'remove', 'path': ['requests', 0]}]))

    def test_initial_and_sliding_budgets(self):
        now = [0]
        b = native.FrameBudget(lambda: now[0])
        with self.assertRaises(native.InvalidObservation): b.charge(native.MAX_FRAME + 1)
        b = native.FrameBudget(lambda: now[0]); b.charge(native.MAX_FRAME)
        with self.assertRaises(native.InvalidObservation): b.charge(native.MAX_FRAME)
        b = native.FrameBudget(lambda: now[0]); now[0] = 11
        with self.assertRaises(native.InvalidObservation): b.charge(1)
        now[0] = 0; b = native.FrameBudget(lambda: now[0]); b.snapshot_received()
        for _ in range(200): b.charge(1)
        with self.assertRaises(native.InvalidObservation): b.charge(1)
        now[0] = 11; b.charge(1)  # Sliding, not a lifetime byte/frame limit.

    def test_announced_oversize_rejected_before_body_read(self):
        class Sock:
            def __init__(self): self.reads = []
            def settimeout(self, seconds): pass
            def recv(self, size):
                self.reads.append(size)
                if len(self.reads) > 1: self.fail = True; raise AssertionError('body read')
                return struct.pack('<I', native.MAX_FRAME + 1)
        transport = object.__new__(native._SocketTransport)
        transport.clock = lambda: 0; transport.sock = Sock()
        with self.assertRaises(native.InvalidObservation):
            transport.receive(native.FrameBudget(lambda: 0), 1)
        self.assertEqual(transport.sock.reads, [4])


class FakeTransport:
    def __init__(self, frames=None, failure=False):
        self.sent = []; self.closed = False; self.clock = 0
        self.frames = list(frames or [snapshot()]); self.failure = failure
    def send(self, message): self.sent.append(deepcopy(message))
    def receive(self, budget, deadline):
        last = self.sent[-1]
        if last.get('type') == 'request':
            method = last['method']
            reply = {'type': 'response', 'requestId': last['requestId'], 'method': method,
                     'resultType': 'success', 'handledByClientId': CLIENT if method == 'initialize' else OWNER,
                     'result': {'clientId': CLIENT} if method == 'initialize' else {'supportsUntrustedAppInput': True}}
        elif self.frames:
            reply = self.frames.pop(0)
        else:
            self.clock = 11
            if self.failure: raise OSError('fictional transport loss')
            reply = {'type': 'broadcast', 'sourceClientId': OTHER}
        budget.charge(len(json.dumps(reply)))
        return reply
    def close(self): self.closed = True


class LifecycleTests(unittest.TestCase):
    def test_import_does_not_connect(self):
        with patch('socket.socket', side_effect=AssertionError('network')):
            importlib.reload(native)

    def test_exact_subscription_and_cleanup_only(self):
        t = FakeTransport(); seen = []
        native._observe(t, THREAD, seen.append, lambda: t.clock, 10)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0].native_id, ('integer', 7))
        self.assertTrue(t.closed)
        self.assertEqual([m['method'] for m in t.sent], [
            'initialize', 'thread-owner-discovery', 'thread-stream-following-changed',
            'thread-stream-following-changed'])
        follows = t.sent[2:]
        self.assertEqual([m['params']['following'] for m in follows], [True, False])
        self.assertTrue(all(m['targetClientIds'] == [OWNER] for m in follows))
        self.assertTrue(all(m['params']['conversationId'] == THREAD for m in follows))

    def test_disconnect_and_callback_failure_always_unfollow_close_no_answers(self):
        for callback in (lambda _: None, lambda _: (_ for _ in ()).throw(ValueError('stop'))):
            t = FakeTransport(failure=True)
            with self.assertRaises((OSError, ValueError)):
                native._observe(t, THREAD, callback, lambda: t.clock, 10)
            self.assertTrue(t.closed)
            self.assertFalse(t.sent[-1]['params']['following'])
            self.assertFalse(any('submit' in m.get('method', '') for m in t.sent))

    def test_discovery_is_declined_never_takes_ownership(self):
        t = FakeTransport([{'type': 'client-discovery-request', 'requestId': OTHER}, snapshot()])
        native._observe(t, THREAD, lambda _: None, lambda: t.clock, 10)
        replies = [m for m in t.sent if m['type'] == 'client-discovery-response']
        self.assertEqual(replies[0]['response'], {'canHandle': False})


if __name__ == '__main__':
    unittest.main()
