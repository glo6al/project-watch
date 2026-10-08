#!/usr/bin/env python3
"""Parsers, watch.json, and one Cursor transcript. Run: python3 tests/test_parsers.py"""
import json, os, sys, tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import server


def check(name, cond):
    if not cond:
        raise SystemExit('FAIL ' + name)
    print('ok', name)


def test_import_is_quiet():
    check('import does not load a key', server.KEY == '')


def test_shell():
    check('push', server.risk('git push origin main') == 'push')
    check('echo is not a push', server.risk('echo git push origin main') == '')
    check('push after echo', server.risk('echo hi && git push origin main') == 'push')
    check('sh -c is followed', server.risk('sh -c "git push origin main"') == 'push')
    check('delete', server.risk('rm -rf /tmp/x') == 'delete')
    check('deploy', server.risk('npx vercel deploy --prod') == 'deploy')
    check('npm test is a check', server.is_check('npm test'))
    check('git push is not a check', not server.is_check('git push origin main'))
    calls = server.launch_calls('claude -p "build the pricing table for the catalogue"')
    check('launch sees claude', calls and calls[0][0] == 'claude' and not calls[0][2])


def test_user_file_replaces_a_key():
    d = tempfile.mkdtemp()
    path = os.path.join(d, 'watch.json')
    with open(path, 'w') as f:
        json.dump({
            'risks': [
                {'label': 'nope', 'pattern': '(['},
                {'label': 'push', 'pattern': r'^git\s+push\b'},
            ],
            'needsYou': {'action': r'\bplease\b', 'decide': '(['},
        }, f)
    old = server.USER_WATCH
    server.USER_WATCH = path
    try:
        server.apply_watch(True)
        check('bad pattern skipped', server.risk('git push') == 'push')
        check('user list replaces shipped risks', server.risk('npx vercel deploy --prod') == '')
        check('needsYou action kept', server.NEEDS.get('action') == r'\bplease\b')
        check('bad decide dropped', 'decide' not in server.NEEDS)
    finally:
        server.USER_WATCH = old
        server.apply_watch(False)
    check('shipped deploy restored', server.risk('npx vercel deploy --prod') == 'deploy')


def test_plain_words():
    import re as _re
    d = tempfile.mkdtemp()
    old = server.USER_WATCH
    server.USER_WATCH = os.path.join(d, 'watch.json')
    try:
        view = server.change_watch('add', 'check', 'make test', None)
        check('row shows the words', view and any(r.get('text') == 'make test' for r in view['rules']))
        check('make test is a check', server.is_check('make test -j2'))
        server.change_watch('add', 'decide', 'want me to commit', None)
        check('decide hears the words', _re.search(server.NEEDS['decide'], 'Do you want me to commit this?', _re.I))
        server.change_watch('off', 'deploy', '', False)
        check('built-in deploy is off', server.risk('npx vercel deploy --prod') == '')
        check('built-in push stays', server.risk('git push origin') == 'push')
        server.change_watch('drop', 'check', 'make test', None)
        check('removed phrase is gone', not server.is_check('make test -j2'))
        server.change_watch('off', 'deploy', '', True)
        check('built-in deploy is back', server.risk('npx vercel deploy --prod') == 'deploy')
        check('a pattern is refused as words only', server.phrase_pattern('git push', 'push').startswith('^git\\s+push'))
    finally:
        server.USER_WATCH = old
        server.apply_watch(False)


def test_cursor_transcript():
    d = tempfile.mkdtemp()
    path = os.path.join(d, 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl')
    rows = [
        {'role': 'user', 'message': {'content': [{'type': 'text', 'text':
            '<timestamp>Wednesday, Oct 7, 2026, 3:00 PM (UTC-4)</timestamp>\n<user_query>\nfix the door\n</user_query>'}]}},
        {'role': 'assistant', 'message': {'content': [
            {'type': 'text', 'text': 'Looking.'},
            {'type': 'tool_use', 'name': 'Shell', 'input': {'command': 'git push origin main'}},
        ]}},
        {'type': 'turn_ended', 'status': 'success'},
    ]
    with open(path, 'w') as f:
        for row in rows:
            f.write(json.dumps(row) + '\n')
    before = len(server.LOG)
    tail = server.CursorTail(path)
    tail.poll()
    ev = [e for e in server.LOG[before:] if e['agent'] == tail.aid]
    kinds = [e['kind'] for e in ev]
    check('cursor id', tail.aid == 'k:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee')
    check('user text', any(e['kind'] == 'user' and e['text'] == 'fix the door' for e in ev))
    check('push alert', any(e['kind'] == 'run' and e.get('alert') == 'push' for e in ev))
    check('result is not done', any(e['kind'] == 'result' and e.get('unknown') for e in ev))
    check('turn ended', 'turn_end' in kinds)
    check('goal', server.AGENTS[tail.aid].get('goal') == 'fix the door')


def test_terminal_text():
    import urllib.request
    import watch_tty as w
    check('escapes stripped', '\x1b' not in w.clean('a\x1b]0;pwned\x07b\x9b2J'))
    check('clipped', len(w.clean('x' * 200, 10)) == 10)
    check('lone surrogate stripped', '\ud800' not in w.clean('hi \ud800'))
    check('bidi and zero-width stripped', w.clean('a\u202eb\u200bc\ufeff').replace(' ', '') == 'abc')
    check('no proxy for the key', not any(isinstance(h, urllib.request.ProxyHandler) and h.proxies for h in w.NO_PROXY.handlers))
    check('agent id checked', not w.AID_RX.match('c:12345678\n') and w.AID_RX.match('c:1234abcd-0000'))

    def agent(*events):
        a = {'turn': '', 'wait': '', 'perms': set(), 'calls': set(), 'asks': set()}
        for e in events:
            w.track(a, e)
        return w.state(a)
    perm = {'kind': 'perm', 'pid': 'p1'}
    check('a result elsewhere leaves a permission open', agent(perm, {'kind': 'result', 'id': 'other'}).startswith('WAITING'))
    check('its own end closes it', not agent(perm, {'kind': 'perm_end', 'pid': 'p1'}).startswith('WAITING'))
    q = {'kind': 'waiting', 'sticky': True, 'qid': 'q1'}
    check('a Codex question outlives its next step', agent(q, {'kind': 'run'}).startswith('WAITING'))
    check('withdrawn when the log says so', agent(q, {'kind': 'unask', 'qid': 'q1'}) == '')
    check('a notification clears when the log moves on', agent({'kind': 'waiting', 'why': 'idle'}, {'kind': 'run'}) == 'working')
    check('a question waits for its own answer', agent({'kind': 'ask', 'id': 'a1'}, {'kind': 'result', 'id': 'x'}).startswith('WAITING'))

if __name__ == '__main__':
    test_import_is_quiet()
    test_shell()
    test_user_file_replaces_a_key()
    test_plain_words()
    test_cursor_transcript()
    test_terminal_text()
    print('all ok')
