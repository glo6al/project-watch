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


def test_platform():
    """Runs on every system (CI: Ubuntu, macOS, Windows): the key, previews with a link in the way or in place of the
    granted folder, what Windows grants (pages only, and only from Python 3.12), and the port held exclusively."""
    import socket
    saved = (server.HOME, server.PRIVATE_ROOTS, dict(server.ALLOWED_FILES), dict(server.ALLOWED_DIRS), list(server.LOG), server.BASE)
    tmp = tempfile.TemporaryDirectory()
    home = os.path.realpath(tmp.name)
    links = []                                   # made below, removed before the folder is: a junction is removed as a folder, never followed

    def mklink(target, link):
        if server.WIN:
            import _winapi
            _winapi.CreateJunction(target, link)
        else:
            os.symlink(target, link)
        links.append(link)

    def unlink_all():
        while links:
            (os.rmdir if server.WIN else os.unlink)(links.pop())

    class Fake:
        def send(self, code, body, *a):
            self.got = (code, body)

    def get(path):
        f = Fake()
        server.H.preview(f, path)
        return f.got

    previews, folders = server.WIN_IDS, not server.WIN          # Windows: pages only, and only with whole file ids
    server.HOME = home
    server.PRIVATE_ROOTS = tuple(os.path.join(home, p) + os.sep for p in ('.live-room', 'AppData'))
    try:
        k = server.load_key()
        check('key made once', len(k) >= 24 and server.load_key() == k)
        with open(os.path.join(home, '.live-room', 'header'), encoding='utf-8') as f:
            check('header written', f.read().strip().endswith(k))
        proj = os.path.join(home, 'work', 'site')
        os.makedirs(os.path.join(proj, 'css'))
        page = os.path.join(proj, 'index.html')
        with open(page, 'w', encoding='utf-8') as f:
            f.write('<link rel="stylesheet" href="css/a.css">ü')
        asset = os.path.join(proj, 'css', 'a.css')
        with open(asset, 'wb') as f:
            f.write(b'body{}\r\n\x1a after a Ctrl-Z')
        private = os.path.join(home, '.live-room')
        other = os.path.join(home, 'other')
        os.makedirs(other)
        for d in (private, other):
            with open(os.path.join(d, 'x.css'), 'w', encoding='utf-8') as f:
                f.write('secret')
        server.ALLOWED_FILES.clear(); server.ALLOWED_DIRS.clear()
        server.allow_preview('c:00000000', False, 0, 'id1', page)
        if not previews:
            check('nothing granted on Windows before Python 3.12', not server.ALLOWED_FILES and not server.ALLOWED_DIRS and get(page)[0] == 404)
        else:
            check('page granted', page in server.ALLOWED_FILES)
            check('folder granted' if folders else 'no folder granted on Windows', (proj in server.ALLOWED_DIRS) == folders)
            check('page served', get(page)[0] == 200)
            if folders:
                check('asset served byte for byte', get(asset) == (200, b'body{}\r\n\x1a after a Ctrl-Z'))
            else:
                check('asset beside the page refused on Windows', get(asset)[0] == 404)
            check('outside the grant refused', get(os.path.join(private, 'x.css'))[0] == 404)
            twin = os.path.join(proj, 'css', 'twin.css')
            os.link(os.path.join(private, 'key'), twin)
            check('a hard link to the key is refused', get(twin)[0] == 404)
            kpage = os.path.join(proj, 'k.html')
            os.link(os.path.join(private, 'key'), kpage)
            server.allow_preview('c:00000000', False, 0, 'id2', kpage)
            check('a hard-linked page is not granted', kpage not in server.ALLOWED_FILES and get(kpage)[0] == 404)
            if server.WIN:
                check('a device name is refused', get(os.path.join(proj, 'css', 'CON.css'))[0] == 404)
                with open(page + ':s.html', 'w', encoding='utf-8') as f:     # an alternate data stream of the page
                    f.write('<p>stream')
                server.allow_preview('c:00000000', False, 0, 'id3', page + ':s.html')
                check('an alternate data stream is not granted', page + ':s.html' not in server.ALLOWED_FILES and get(page + ':s.html')[0] == 404)
            for name, target in (('in', private), ('out', other)):
                mklink(target, os.path.join(proj, 'css', name))
                check('a link inside the grant is refused (%s)' % name, get(os.path.join(proj, 'css', name, 'x.css'))[0] == 404)
            unlink_all()
            # the granted folder moved aside and a link put in its place: the link is seen before anything follows it
            os.rename(proj, proj + '-aside')
            mklink(proj + '-aside', proj)
            check('an asset behind a link in place of the granted folder is refused', get(asset)[0] == 404)
            # the page itself: on Windows the name walk from the drive root sees the link; on macOS the open follows a
            # link above the file (O_NOFOLLOW guards only the last name) and the inode check finds the very same file
            check('a page behind a link in place of its folder: refused on Windows, the same file on macOS', get(page)[0] == (404 if server.WIN else 200))
            unlink_all()
            os.rename(proj + '-aside', proj)
            check('the page back in place is served again', get(page)[0] == 200 and (get(asset)[0] == 200) == folders)
            # a page granted, then swapped for another file under the same name: not the same file, not served
            with open(page + '.new', 'w', encoding='utf-8') as f:
                f.write('<p>other')
            os.replace(page + '.new', page)
            check('a page swapped under its name is refused', get(page)[0] == 404)
    finally:
        unlink_all()
        server.HOME, server.PRIVATE_ROOTS = saved[0], saved[1]
        server.ALLOWED_FILES.clear(); server.ALLOWED_FILES.update(saved[2])
        server.ALLOWED_DIRS.clear(); server.ALLOWED_DIRS.update(saved[3])
        server.LOG[:] = saved[4]; server.BASE = saved[5]
        tmp.cleanup()

    s1 = server.Server(('127.0.0.1', 0), server.H)
    s2 = socket.socket()
    try:
        s2.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s2.bind(('127.0.0.1', s1.server_address[1]))
            taken = False
        except OSError:
            taken = True
        check('nothing else can bind the port', taken)
        check('a Mac path is still a path', server.WIN or server.PATH_RX.findall('see file:/Users/me/a/b/c.py') == ['/Users/me/a/b/c.py'])
        check('a Mac cd is still a cd, and a drive path is not one there',
              server.WIN or (bool(server.CD_RX.match('cd /Users/me/a')) and not server.CD_RX.match('cd C:/Users/me/a')))
    finally:
        s2.close()
        s1.server_close()


def test_cloud_exec():
    """A Codex cloud exec server seen in the process list: recognised by the text of its executable and subcommand,
    never by words in a command line; one step per command with the command line and folder as the list and lsof
    state them; helpers ignored by their executable; a pid seen again with another start text is another process; a
    hostile command line is shown unjudged rather than lost, an over-long one as partly judged; a burst examined
    over samples; an unreadable list marks the agent stale and a readable one clears it; the agent gone when the
    server is. Everything is injected, so this runs on every system and touches no process or folder."""
    cx = server.CloudExec()
    check('a server is its executable and subcommand, not words in a command',
          cx.is_root('/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex exec-server --remote https://x --environment-id e')
          and not cx.is_root('/bin/zsh -lc echo /tmp/codex exec-server --remote fake') and not cx.is_root('codex exec-server forward'))
    check('a helper is its executable, not a word', bool(cx.HELPER.match('/A/cua_node/bin/node ./server.mjs')) and bool(cx.HELPER.match('/A/cua_node/bin/node_repl'))
          and not cx.HELPER.match('/bin/zsh -lc echo server.mjs') and not cx.HELPER.match('/bin/zsh -lc rm -rf /tmp/cua_node'))
    check('etime: days, hours, minutes', [server.etime_secs(x) for x in ('02-03:04:05', '03:04:05', '04:05')] == [183845, 11045, 245])
    bad = 0
    for x in ('x', '1:2:3:4', 'a-1:2'):
        try:
            server.etime_secs(x)
        except ValueError:
            bad += 1
    check('etime: anything else is refused', bad == 3)
    ev = []
    orig_emit, orig_risk = server.emit, server.risk
    server.emit = lambda agent, kind, old, ts=None, **kw: ev.append((agent, kind, dict(kw)))
    ROOT = '/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex exec-server --remote https://example.invalid/api --environment-id env_x'
    T0, T1 = 1000.0, 1010.0
    proj = os.path.join(os.sep, 'tmp', 'proj')
    rows = [[(100, 1, T0, ROOT), (101, 100, T0, '/Applications/ChatGPT.app/Contents/Resources/cua_node/bin/node ./server.mjs'), (102, 100, T0, '/bin/zsh -lc git push origin main')]]
    cwds = {102: proj}
    cx = server.CloudExec(rows=lambda: list(rows[0]), cwd=lambda pid: cwds.get(pid, ''))
    kinds = lambda: [k for a, k, _ in ev]
    aid = None
    try:
        cx.poll(2000.0)
        aid = ev[0][0]
        check('one agent, from the exec server', aid.startswith('e:') and server.AID_RX.match(aid) and 'agent' in kinds())
        runs = [kw for a, k, kw in ev if k == 'run']
        check('one step: the command, not the helper', len(runs) == 1 and runs[0]['cmd'] == 'git push origin main' and runs[0]['seen'] and runs[0]['judged'] == 'full')
        check('the command is judged like any other', runs[0]['alert'] == 'push')
        check('the step carries its own folder', runs[0]['cwd'] == proj)
        check('the agent is described from the list', server.AGENTS[aid].get('runtime') == 'Codex cloud' and server.AGENTS[aid].get('cwd') == proj and 'process list' in server.AGENTS[aid].get('observed', ''))
        rows[0] = [(pid, ppid, st + 1.5, args) for pid, ppid, st, args in rows[0]]      # the start, now minus a rounded elapsed time, moves a little between samples
        cx.poll(2000.5)
        check('nothing twice: a start that moved by rounding is the same process', len([1 for a, k, _ in ev if k == 'run']) == 1)
        rows[0] = rows[0][:2]
        cx.poll(2002.0)
        res = [kw for a, k, kw in ev if k == 'result']
        check('the command ended with no outcome, and nothing claims what it changed', len(res) == 1 and res[0]['unknown'] and res[0]['seen'] and res[0]['text'] == '' and 'patch' not in kinds())
        # pid 102 comes back with another start text: another command, whose folder lsof cannot give; the classifier chokes on it
        server.risk = lambda cmd: (_ for _ in ()).throw(RecursionError('deep'))
        cwds.pop(102)
        rows[0] = rows[0] + [(102, 100, T1, '/bin/zsh -lc ' + 'echo $(' * 300)]
        cx.poll(2006.0)
        runs = [kw for a, k, kw in ev if k == 'run']
        check('a reused pid with a start beyond the tolerance is another command', len(runs) == 2 and runs[1]['cmd'].startswith('echo $('))
        check('a command the classifier cannot parse is shown unjudged, not lost', runs[1]['judged'] == 'none' and runs[1]['alert'] == '')
        check('an unreadable folder is empty on the step and leaves the last known one on the agent', runs[1]['cwd'] == '' and server.AGENTS[aid].get('cwd') == proj)
        server.risk = orig_risk
        rows[0] = rows[0][:2] + [(103, 100, T1, '/bin/zsh -lc ' + 'echo x; ' * 600 + 'git push')]
        cx.poll(2008.0)
        runs = [kw for a, k, kw in ev if k == 'run']
        check('an over-long command is judged on its start only, and says so', runs[-1]['judged'] == 'partial')
        rows[0] = rows[0][:2] + [(200 + i, 100, T1 + i, '/bin/zsh -lc echo %d' % i) for i in reversed(range(6))]
        cx.poll(2011.0)
        seen_echo = lambda: sorted(int(kw['cmd'][5:]) for a, k, kw in ev if k == 'run' and kw['cmd'].startswith('echo ') and kw['cmd'][5:].isdigit())
        check('four new commands per sample, the oldest first', seen_echo() == [0, 1, 2, 3])
        cx.poll(2012.0)
        check('the rest the next sample', seen_echo() == [0, 1, 2, 3, 4, 5])
        # a churn of newer short-lived commands never starves one that stays
        for n in range(3):
            rows[0] = rows[0][:2] + [(400 + 10 * n + i, 100, T1 + 100 + 10 * n, '/bin/zsh -lc echo churn') for i in range(5)] + [(300, 100, T1, '/bin/zsh -lc echo stays')]
            cx.poll(2013.0 + n)
            if n == 0:
                check('the command that stays is examined before a newer churn listed ahead of it', any(kw['cmd'] == 'echo stays' for a, k, kw in ev if k == 'run'))
        check('three samples of churn never hid it, and each sample took four', len([1 for a, k, kw in ev if k == 'run' and kw['cmd'] == 'echo churn']) == 3 + 4 + 4)
        # a folder lookup that blows up leaves nothing half-done: the step is still shown, its folder unreadable
        cx.cwd = lambda pid: (_ for _ in ()).throw(UnicodeDecodeError('utf-8', b'', 0, 1, 'bad'))
        rows[0] = rows[0][:2] + [(500, 100, T1 + 200, '/bin/zsh -lc echo boom')]
        try:
            cx.poll(2014.0)
        except Exception:
            pass
        cx.cwd = lambda pid: ''
        cx.poll(2015.0)
        check('a command whose folder lookup failed is still shown, folder unreadable', any(kw['cmd'] == 'echo boom' and kw['cwd'] == '' for a, k, kw in ev if k == 'run'))
        good = cx.rows
        cx.rows = lambda: (_ for _ in ()).throw(OSError('no ps'))
        for t in (2013.0, 2014.0, 2015.0):
            cx.poll(t)
        check('three unreadable samples mark the agent stale', server.AGENTS[aid].get('stale') is True)
        cx.rows = good
        cx.poll(2016.0)
        check('a readable sample clears it, and the record is resent', 'stale' not in server.AGENTS[aid] and ev[-1][1] == 'agent' and not ev[-1][2].get('stale'))
        rows[0] = rows[0][:2] + [(600, 100, T1 + 300, '/bin/zsh -lc sleep 999'), (601, 100, T1 + 300, '/bin/zsh -lc sleep 1')]
        cx.poll(2017.0)
        rows[0] = [(600, 1, T1 + 300, '/bin/zsh -lc sleep 999')]            # the server is gone; one command is gone with it, one lives on, reparented
        before = len(ev)
        cx.poll(2018.0)
        tail = [(k, kw.get('id')) for a, k, kw in ev[before:]]
        check('the agent goes with its server: the command gone with it ended, the one still running is not said to have ended',
              aid not in server.AGENTS and tail[-1][0] == 'forget' and ('result', 'p601-%d' % int(T1 + 300)) in tail and not any(k == 'result' and i == 'p600-%d' % int(T1 + 300) for k, i in tail))
        check('no process list on Windows', (server.ps_rows() == []) if server.WIN else True)
    finally:
        server.emit, server.risk = orig_emit, orig_risk
        if aid:
            server.AGENTS.pop(aid, None)


def test_active_log():
    """A log enters the room when it was written within the window by its own clock, not merely touched; the clock
    is the last record's own timestamp, however long that record is, never a timestamp nested inside it."""
    import time as _t
    d = tempfile.TemporaryDirectory()
    try:
        now = _t.time()
        iso = lambda t: _t.strftime('%Y-%m-%dT%H:%M:%S', _t.gmtime(t)) + '.000Z'
        old_iso = '2026-09-21T15:42:00.000Z'
        def log(name, *records, mtime=None):
            path = os.path.join(d.name, name)
            with open(path, 'w') as f:
                f.write(''.join(json.dumps(r) + '\n' for r in records))
            if mtime is not None:
                os.utime(path, (mtime, mtime))
            return path
        stale = log('stale.jsonl', {'type': 'assistant', 'timestamp': old_iso})            # touched now, written weeks ago by its own clock
        check('a touched log with weeks-old records is not followed', not server.active(stale, now))
        fresh = log('fresh.jsonl', {'type': 'assistant', 'timestamp': old_iso}, {'type': 'user', 'timestamp': iso(now - 30)})
        check('a log with a recent last record is followed', server.active(fresh, now))
        check('the last timestamp is read, not the first', abs(server.last_record_ts(fresh) - (now - 30)) < 2)
        old = log('old.jsonl', {'type': 'user', 'timestamp': iso(now - 30)}, mtime=now - server.WINDOW * 2)
        check('a log not changed within the window is not followed, whatever it says', not server.active(old, now))
        big = log('big.jsonl', {'type': 'assistant', 'timestamp': iso(now - 40), 'message': {'content': 'x' * 70000}})
        check('a last record far longer than a chunk is still read', server.active(big, now) and abs(server.last_record_ts(big) - (now - 40)) < 2)
        nested = log('nested.jsonl', {'type': 'assistant', 'timestamp': iso(now - 20), 'message': {'content': [{'type': 'tool_use', 'input': {'timestamp': old_iso, 'note': '"timestamp":"2026-01-01T00:00:00Z"'}}]}})
        check('a timestamp nested in a record is not its clock', server.active(nested, now))
        nested_old = log('nested_old.jsonl', {'type': 'assistant', 'timestamp': old_iso, 'message': {'content': [{'type': 'tool_use', 'input': {'timestamp': iso(now - 5)}}]}})
        check('a recent timestamp nested in an old record does not admit it', not server.active(nested_old, now))
        tailless = log('tailless.jsonl', {'type': 'assistant', 'timestamp': iso(now - 50)}, {'type': 'summary', 'summary': 'no time here'})
        check('a timestamp-less record at the end is skipped back to the last timed one', server.active(tailless, now))
        untimed = log('untimed.jsonl', {'role': 'user', 'message': {'content': []}})
        check('a log that never states a time is not admitted by its file alone when timed', not server.active(untimed, now))
        check('a Cursor-style log is judged by the file alone', server.active(untimed, now, timed=False))
        deep = log('deep.jsonl', {'type': 'assistant', 'timestamp': iso(now - 10), 'message': 'y' * 3000})
        check('no more than the limit is read, and a timed record beyond it is inconclusive', server.last_record_ts(deep, limit=1000) is None and server.last_record_ts(deep, limit=5000) is not None)
        check('an inconclusive tail does not admit a freshly changed log', not server.active(deep, now, limit=1000) and server.active(deep, now))
        # the tail's cut falls exactly on the newline before a complete recent record: that record must still count
        rec = json.dumps({'type': 'user', 'timestamp': iso(now - 15), 'pad': 'z' * 940})
        edge = os.path.join(d.name, 'edge.jsonl')
        with open(edge, 'wb') as f:                             # bytes, so the cut lands where the test says on every system (text mode would write \r\n on Windows)
            f.write(b'{}\n' + rec.encode() + b'\n')
        lim = len(rec) + 2                                      # the read starts at the newline that precedes the record (record, its newline, and that one)
        check('a record whose preceding newline sits at the cut is read', server.last_record_ts(edge, limit=lim) is not None and server.active(edge, now, limit=lim))
        check('a record the cut lands inside is not trusted', server.last_record_ts(edge, limit=lim - 1) is None)
        if not server.WIN:
            os.chmod(deep, 0)
            try:
                check('a tail that cannot be read is inconclusive, and the log is not admitted', server.last_record_ts(deep) is None and not server.active(deep, now))
            finally:
                os.chmod(deep, 0o600)
    finally:
        d.cleanup()


if __name__ == '__main__':
    test_import_is_quiet()
    test_shell()
    test_user_file_replaces_a_key()
    test_plain_words()
    test_cursor_transcript()
    test_terminal_text()
    test_platform()
    test_cloud_exec()
    test_active_log()
    print('all ok')
