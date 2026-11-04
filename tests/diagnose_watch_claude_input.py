"""Temporary metadata-only observation of the unchanged loopback helper tests."""
from collections import deque
import importlib.util
import io
import json
import math
from pathlib import Path
import sys
import threading
import time
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
TEST = ROOT / 'tests' / 'test_watch_claude_input.py'
METHODS = (
    'test_expired_and_parent_cancelled_before_any_output',
    'test_connected_blocking_mode_keeps_finite_connect_and_wakes_cancelled_reads',
    'test_callback_errors_abort_held_response_without_output_or_guard_leak',
    'test_valid_held_response_remains_available_until_the_original_deadline',
)
SITES = ((205, 209, 210, 211, 217), (233, 236, 236, 237, 247),
         (262, 267, 267, 268, 278), (323, None, None, 327, 334))


def number(value):
    if type(value) not in (int, float):
        return {'type': 'none' if value is None else 'other'}
    result = {'type': type(value).__name__, 'repr': repr(value)}
    if type(value) is float:
        result['hex'] = value.hex()
    return result


def frame_rows(frame):
    rows = []
    while frame is not None and len(rows) < 20:
        rows.append({'file': Path(frame.f_code.co_filename).name,
                     'function': frame.f_code.co_name, 'line': frame.f_lineno})
        frame = frame.f_back
    return rows


def emit(value):
    print(json.dumps(value, separators=(',', ':'), allow_nan=False), file=sys.__stdout__, flush=True)


def nested(code, name):
    return next(item for item in code.co_consts if isinstance(item, types.CodeType) and item.co_name == name)


class Collector:
    def __init__(self, module):
        self.module = module
        self.tests = {getattr(module.HelperTests, name).__code__: (index, sites)
                      for index, (name, sites) in enumerate(zip(METHODS, SITES))}
        self.exchange = module.helper.exchange.__code__
        self.run_hook = module.helper.run_hook.__code__
        self.watch = nested(self.exchange, 'watch')
        self.now = nested(self.run_hook, 'now')
        self.cases, self.threads, self.current = [], {}, None
        self.pending = deque(maxlen=16)
        self.collector_errors = []

    def event(self, case, phase, **extra):
        case['events'].append(dict(phase=phase, us=int((time.perf_counter() - case['start']) * 1e6), **extra))

    def begin(self, frame, family):
        variant = frame.f_locals.get('reason', frame.f_locals.get('callback', 'positive'))
        if variant not in ('deadline', 'cancel', 'parent', 'clock', 'positive'):
            raise RuntimeError('Unexpected diagnostic case')
        case = {'family': family, 'variant': variant, 'start': time.perf_counter(),
                'events': deque(maxlen=40), 'clocks': [], 'guard': None, 'guard_exited': False,
                'polls': 0, 'first_poll_us': None, 'last_poll_us': None,
                'shutdown_error': False, 'close_error': False, 'closing': False}
        self.cases.append(case)
        self.current = case

    @staticmethod
    def mode(sock):
        if sock is None:
            return {'type': 'missing'}
        try:
            return number(sock.gettimeout())
        except OSError:
            return {'type': 'unavailable'}

    def socket_state(self, sock):
        state = {'timeout': self.mode(sock)}
        for name, kind in (('_io_refs', int), ('_closed', bool)):
            value = getattr(sock, name, None)
            if type(value) is kind:
                state[name] = value
        return state

    def stacks(self, worker, case, fixture):
        frames = sys._current_frames()
        result, owned_socket = {}, {}
        try:
            for role, ident in (('client', worker.ident), ('guard', case['guard'])):
                result[role] = frame_rows(frames[ident]) if ident in frames else []
            frame = frames.get(worker.ident)
            while frame is not None:
                if frame.f_code == self.exchange:
                    owned_socket = self.socket_state(frame.f_locals.get('pinned'))
                    break
                frame = frame.f_back
            result['server'] = []
            for top in frames.values():
                frame = top
                while frame is not None:
                    if frame.f_code.co_filename == str(TEST) and frame.f_code.co_name in ('do_POST', 'handle'):
                        owner = frame.f_locals.get('self')
                        if getattr(owner, 'server', None) is fixture.server:
                            result['server'] = frame_rows(top)
                            break
                    frame = frame.f_back
                if result['server']:
                    break
                frame = None
        finally:
            frames.clear()
            frame = top = None
        return result, owned_socket

    def flush(self):
        while self.pending:
            print(self.pending.popleft(), file=sys.__stdout__, flush=True)

    def snapshot(self, frame, post=False):
        case = self.current
        marker = 'post_snapshot' if post else 'pre_snapshot'
        if case.get(marker):
            return
        case[marker] = True
        frames = sys._current_frames()
        guard_alive = case['guard'] in frames
        frames.clear()
        worker, fixture = frame.f_locals['t'], frame.f_locals['f']
        alive = worker.is_alive()
        row = {'kind': 'post_cleanup' if post else 'pre_assert', 'family': case['family'],
               'variant': case['variant'], 'client_alive': alive,
               'guard_state': 'alive' if guard_alive else 'exited' if case['guard_exited'] else 'missing',
               'server_started': fixture.started.is_set(), 'server_released': fixture.release.is_set(),
               'request_count': len(fixture.seen), 'elapsed_ms': (time.perf_counter() - case['start']) * 1000,
               'polls': case['polls'], 'first_poll_us': case['first_poll_us'], 'last_poll_us': case['last_poll_us']}
        for key in ('trigger', 'join'):
            if key in case:
                row['since_' + key + '_ms'] = (time.perf_counter() - case[key]) * 1000
        if post:
            row['cleanup_tail'] = list(case['events'])[-8:]
        if not post:
            row['events'] = list(case['events'])
            row['timing'] = {key: case[key] for key in ('requested', 'deadline', 'constructor_clock', 'constructor_timeout', 'initial_mode', 'connected_mode') if key in case}
            row['clock_returns'] = case['clocks']
            failure = alive and case['family'] != 3
            if case['family'] == 3:
                timeout = frame.f_locals['connections'][0].timeout
                row['positive_timeout'] = number(timeout)
                row['greater_than_70'], row['at_most_75'] = 70 < timeout, timeout <= 75
                failure = not (row['greater_than_70'] and row['at_most_75'])
            if failure:
                row['stacks'], row['owned_socket'] = self.stacks(worker, case, fixture)
                row['guard_state'] = 'alive' if row['stacks']['guard'] else 'exited' if case['guard_exited'] else 'missing'
        # Serialize the actual pre-assert state now; defer output until method
        # cleanup so stdout backpressure cannot consume the fixture's hold time.
        self.pending.append(json.dumps(row, separators=(',', ':'), allow_nan=False))

    def trace(self, frame, event, arg):
        try:
            return self.observe(frame, event, arg)
        except Exception as error:
            if len(self.collector_errors) < 4:
                self.collector_errors.append(type(error).__name__)
            return None

    def observe(self, frame, event, arg):
        code = frame.f_code
        if code in self.tests:
            if event == 'return':
                self.flush()
            if event == 'line':
                family, (begin, trigger, join, assertion, cleanup) = self.tests[code]
                if frame.f_lineno == begin:
                    self.begin(frame, family)
                case = self.current
                if case is not None:
                    if frame.f_lineno == trigger:
                        case['trigger'] = time.perf_counter()
                    if frame.f_lineno == join:
                        case['join'] = time.perf_counter()
                    if frame.f_lineno == assertion:
                        self.snapshot(frame)
                    if frame.f_lineno == cleanup:
                        self.snapshot(frame, post=True)
            return self.trace
        if code == self.run_hook and event == 'call':
            self.threads[threading.get_ident()] = self.current
            if self.current is not None:
                self.current['requested'] = number(frame.f_locals['timeout'])
            return None
        if code not in (self.exchange, self.watch, self.now):
            return None
        ident = threading.get_ident()
        if code == self.watch and event == 'call':
            self.threads[ident] = self.current
            self.current['guard'] = ident
        case = self.threads.get(ident)
        if case is None:
            raise RuntimeError('Missing diagnostic owner')
        if code == self.now and event == 'return':
            if type(arg) in (int, float):
                parent = frame.f_back
                point = {'function': parent.f_code.co_name, 'line': parent.f_lineno,
                         'role': 'guard' if ident == case['guard'] else 'client', 'value': number(arg)}
                if len(case['clocks']) < 8:
                    case['clocks'].append(point)
                if parent.f_code == self.exchange and parent.f_lineno == 57:
                    case['constructor_clock'] = number(arg)
                parent = None
            return self.trace
        if code == self.exchange:
            if event == 'call':
                case['deadline'] = number(frame.f_locals['invocation'].deadline)
            if event == 'line':
                phases = {65: 'connect_enter', 66: 'connect_return', 72: 'blocking_handoff_return',
                          96: 'challenge_send_enter', 97: 'challenge_send_return', 98: 'challenge_headers_return',
                          106: 'invocation_send_enter', 107: 'invocation_send_return', 108: 'invocation_headers_return',
                          109: 'invocation_body_return', 115: 'cleanup_enter', 117: 'connection_close_enter',
                          121: 'guard_join_return'}
                if frame.f_lineno in phases:
                    ordinal = {96: 1, 97: 1, 106: 2, 107: 2}.get(frame.f_lineno)
                    extra = {'request_ordinal': ordinal} if ordinal is not None else {}
                    if frame.f_lineno in (96, 106):
                        extra['same_socket'] = frame.f_locals.get('pinned') is frame.f_locals['conn'].sock
                    self.event(case, phases[frame.f_lineno], **extra)
                conn = frame.f_locals.get('conn')
                if frame.f_lineno == 117:
                    case['connection_close_error'] = False
                if frame.f_lineno == 120:
                    if not case.get('connection_close_error'):
                        self.event(case, 'connection_close_return')
                    case['joining'] = True
                    self.event(case, 'guard_join_enter')
                if frame.f_lineno == 121:
                    case['joining'] = False
                if frame.f_lineno == 65:
                    case['constructor_timeout'] = number(conn.timeout)
                if frame.f_lineno == 66:
                    case['initial_mode'] = self.mode(conn.sock)
                if frame.f_lineno == 72:
                    case['connected_mode'] = self.mode(frame.f_locals['pinned'])
            if event == 'exception' and frame.f_lineno in (97, 107, 108, 117, 120):
                error = arg[1]
                if frame.f_lineno == 117:
                    case['connection_close_error'] = True
                if frame.f_lineno == 120:
                    case['joining'] = False
                self.event(case, 'exchange_operation_error', line=frame.f_lineno, error_class=type(error).__name__,
                           errno=error.errno if type(getattr(error, 'errno', None)) is int else None,
                           winerror=error.winerror if type(getattr(error, 'winerror', None)) is int else None)
                error = None
            if event == 'return':
                if case.get('joining'):
                    self.event(case, 'guard_join_return')
                    case['joining'] = False
                self.event(case, 'exchange_exit')
        if code == self.watch:
            if event == 'line':
                line = frame.f_lineno
                if line == 77:
                    case['polls'] += 1
                    stamp = int((time.perf_counter() - case['start']) * 1e6)
                    case['first_poll_us'] = case['first_poll_us'] if case['first_poll_us'] is not None else stamp
                    case['last_poll_us'] = stamp
                if line in (78, 81, 85):
                    self.event(case, {78: 'abort_predicate', 81: 'abort_exception', 85: 'shutdown_enter'}[line])
                if line == 89:
                    if not case['shutdown_error']:
                        self.event(case, 'shutdown_return')
                    case['closing'] = True
                    self.event(case, 'socket_close_enter')
            if event == 'exception' and frame.f_lineno in (85, 89):
                error = arg[1]
                case['shutdown_error' if frame.f_lineno == 85 else 'close_error'] = True
                self.event(case, 'shutdown_error' if frame.f_lineno == 85 else 'socket_close_error',
                           error_class=type(error).__name__,
                           errno=error.errno if type(getattr(error, 'errno', None)) is int else None,
                           winerror=error.winerror if type(getattr(error, 'winerror', None)) is int else None)
                error = None
            if event == 'return':
                if case['closing'] and not case['close_error']:
                    self.event(case, 'socket_close_return')
                case['guard_exited'] = True
                self.event(case, 'guard_return')
        return self.trace


class MetadataResult(unittest.TextTestResult):
    def _exc_info_to_string(self, err, test):
        rows, tb = [], err[2]
        while tb is not None and len(rows) < 20:
            rows.append({'file': Path(tb.tb_frame.f_code.co_filename).name,
                         'function': tb.tb_frame.f_code.co_name, 'line': tb.tb_lineno})
            tb = tb.tb_next
        tb = None
        return json.dumps({'error_class': err[0].__name__, 'frames': rows})


class MetadataStderr(io.TextIOBase):
    def __init__(self):
        self.writes = 0
    def write(self, text):
        self.writes += 1
        return len(text)
    def flush(self):
        pass


def main():
    spec = importlib.util.spec_from_file_location('original_loopback_fixture', TEST)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    collector = Collector(module)
    clock = time.get_clock_info('monotonic')
    emit({'kind': 'runtime', 'python': list(sys.version_info[:3]),
          'monotonic': {key: getattr(clock, key) for key in ('implementation', 'resolution', 'monotonic', 'adjustable')},
          'ulp_75': number(math.ulp(75.0))})
    previous_trace, previous_stderr = sys.gettrace(), sys.stderr
    previous_thread_trace = threading.gettrace() if hasattr(threading, 'gettrace') else getattr(threading, '_trace_hook', None)
    sink = MetadataStderr()
    try:
        sys.stderr = sink
        threading.settrace(collector.trace)
        sys.settrace(collector.trace)
        suite = unittest.TestSuite(module.HelperTests(name) for name in METHODS)
        result = unittest.TextTestRunner(stream=sys.__stdout__, verbosity=2, resultclass=MetadataResult).run(suite)
    finally:
        sys.settrace(previous_trace)
        threading.settrace(previous_thread_trace)
        sys.stderr = previous_stderr
        collector.threads.clear()
        collector.flush()
    emit({'kind': 'result', 'tests': result.testsRun, 'failures': len(result.failures),
          'errors': len(result.errors), 'stderr_writes_withheld': sink.writes,
          'collector_errors': collector.collector_errors})
    return 0 if result.wasSuccessful() and not collector.collector_errors else 1


if __name__ == '__main__':
    raise SystemExit(main())
