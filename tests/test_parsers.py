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


if __name__ == '__main__':
    test_import_is_quiet()
    test_shell()
    test_user_file_replaces_a_key()
    test_plain_words()
    test_cursor_transcript()
    test_terminal_text()
    test_platform()
    print('all ok')
