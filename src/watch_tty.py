#!/usr/bin/env python3
"""The room in a terminal, from the running server's /events stream. Run the server first, then:
    python3 src/watch_tty.py              a board: one row per agent, redrawn in place
    python3 src/watch_tty.py --tail       one line per event as it arrives, greppable
    python3 src/watch_tty.py --once       one line, e.g. "2 waiting · 3 working", for a status bar
    python3 src/watch_tty.py --agent ID   one agent's log as it is written (ID as the server names it, e.g. c:1234abcd-…)
    python3 src/watch_tty.py --tab        iTerm2: the board in a new window, which then opens one tab per agent
    python3 src/watch_tty.py --split      iTerm2: the same, with each agent in a pane of the board's tab
A terminal bell rings when an agent starts waiting on you. In iTerm2 its tab (or its pane) also turns amber, and the
board's tab carries a "2 waiting" badge, until it no longer waits.
Read-only: it never answers a prompt. Standard library only."""
import base64, json, os, re, shlex, shutil, subprocess, sys, tempfile, time, unicodedata, urllib.error, urllib.request

PORT = int(os.environ.get('PORT', '8793'))
HERE = os.path.dirname(os.path.abspath(__file__))
TTY = sys.stdout.isatty()                           # bells and escape codes only for a person, never into a pipe
ITERM = TTY and os.environ.get('TERM_PROGRAM') == 'iTerm.app'
PROFILE = os.path.expanduser('~/Library/Application Support/iTerm2/DynamicProfiles/project-watch.json')
PROFILE_KEYS = {'Name', 'Guid', 'Badge Text', 'Background Color', 'Foreground Color', 'Bold Color', 'Cursor Color',
                'Selection Color', 'Selected Text Color', 'Use Tab Color', 'Tab Color', 'Badge Color', 'Close Sessions On End'}
UNSHOWN = {'Cc', 'Cf', 'Cs'}    # controls (escape sequences), format characters (bidi, zero-width), lone surrogates
MAX_VIEWS = 6               # ponytail: the server allows 12 streams in all; this leaves the board, browser windows and --once theirs
AID_RX = re.compile(r'[cxk]:[0-9a-f-]{8,64}\Z')    # the server's own agent ids; nothing else goes into a tab's command
ACTS = ('run', 'read', 'write', 'edit', 'tool', 'say', 'result', 'search', 'web', 'patch', 'spawn', 'ask', 'user', 'perm')
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))      # the key never goes to a proxy
TOUCHED = []                # what this process changed in its tab, so exiting undoes exactly that


def clean(v, n=80):
    """Log text is untrusted: nothing in it that a terminal acts on, or draws out of order, reaches the screen."""
    s = str(v or '')
    s = s if len(s) <= n else s[:n - 1] + '…'
    return ''.join(' ' if unicodedata.category(c) in UNSHOWN else c for c in s).strip()


def stream():
    with open(os.path.expanduser('~/.live-room/key')) as f:
        key = f.read().strip()
    r = NO_PROXY.open('http://127.0.0.1:%d/events?k=%s' % (PORT, key), timeout=30)
    for raw in r:
        if raw.startswith(b'data: '):
            yield json.loads(raw[6:])


def doing(e, n=80):
    k = e['kind']
    if k == 'run':
        return ('! %s: ' % clean(e['alert'], 12) if e.get('alert') else '$ ') + clean(e.get('cmd'), n)
    if k in ('read', 'write', 'edit'):
        return '%s %s' % (k, clean(e.get('path'), n))
    if k in ('say', 'user'):
        return ('» ' if k == 'user' else '') + clean(e.get('text'), n)
    if k == 'tool':
        return clean(e.get('name'), n)
    if k in ('waiting', 'perm', 'ask'):         # what happened; whether it still waits is the state's to say
        return {'perm': 'asked permission: ', 'ask': 'asked you: '}.get(k, 'needs you: ') + clean(e.get('text') or e.get('why') or e.get('tool'), n)
    if k == 'result' and e.get('error'):
        return '  failed: ' + clean(e.get('text'), n)
    if k == 'turn_end':
        return '— turn ended' + (': ' + clean(e.get('how'), n) if e.get('how') else '')
    return ''


def track(a, e):
    """The page's own rules for what an agent waits on (index.html, `case 'perm'` onwards), so both say the same:
    a permission request until its own end; a question until its own answer; a Codex question until the log
    withdraws it (after a message from you it is only probably answered, and no longer counted); a plain
    notification until the log moves on."""
    k = e['kind']
    if k == 'perm':
        a['perms'].add(e.get('pid'))
    elif k == 'perm_end':
        a['perms'].discard(e.get('pid'))
    elif k == 'ask':
        a['calls'].add(e.get('id'))
    elif k == 'result':
        a['calls'].discard(e.get('id'))
    elif k == 'waiting' and e.get('sticky'):
        a['asks'].add(e.get('qid') or e.get('seq'))
    elif k == 'unask':
        a['asks'].discard(e.get('qid'))
    elif k == 'user':
        a['asks'].clear()
    if k == 'waiting' and not e.get('sticky'):
        a['wait'] = clean(e.get('why') or 'needs you', 20)
    elif k in ACTS or k == 'turn_end':
        a['wait'] = ''
    if k in ('turn_start', 'user') or k in ACTS:
        a['turn'] = 'working'
    elif k == 'turn_end':
        a['turn'] = 'turn ended'


def state(a):
    if a['perms']:
        return 'WAITING: permission'
    if a['calls'] or a['asks']:
        return 'WAITING: question'
    if a['wait']:
        return 'WAITING: ' + a['wait']
    return a['turn']


def mark(code, what):
    TOUCHED.append(what)
    sys.stdout.write(code)
    sys.stdout.flush()


def amber(on):              # iTerm2's own tab-colour code; other terminals never receive it
    if on:
        return ''.join('\x1b]6;1;bg;%s;brightness;%d\a' % c for c in (('red', 245), ('green', 166), ('blue', 35)))
    return '\x1b]6;1;bg;*;default\a'                # back to the profile's own tab colour


def tint(on):               # one pane's background, for panes that share a tab (and so a tab colour)
    return '\x1b]1337;SetColors=bg=%s\a' % ('3a2a08' if on else '090d13')     # the second is the profile's own


def badge(text):
    return '\x1b]1337;SetBadgeFormat=%s\a' % base64.b64encode(text.encode()).decode()


def install_profile():
    """iTerm2 reads its DynamicProfiles folder by itself. The shipped profile is checked to hold only colours and
    names (a Command or Trigger there would run things), then published by rename, which replaces a symlink rather
    than writing through it."""
    with open(os.path.join(HERE, 'iterm-profile.json')) as f:
        want = f.read()
    extra = set().union(*(p.keys() for p in json.loads(want)['Profiles'])) - PROFILE_KEYS
    if extra:
        raise SystemExit('src/iterm-profile.json has settings this script does not install: %s' % ', '.join(sorted(extra)))
    try:
        with open(PROFILE) as f:
            if f.read() == want:
                return
    except OSError:
        pass
    os.makedirs(os.path.dirname(PROFILE), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(PROFILE), prefix='.project-watch-')
    with os.fdopen(fd, 'w') as f:
        f.write(want)
    os.replace(tmp, PROFILE)
    time.sleep(1.5)         # ponytail: iTerm2 notices a new profile file within about a second; a fixed wait, not a check


def iterm_open(args, beside='', pane=False):
    """Run this script with args in iTerm2: in a new window, or beside `beside` (an iTerm2 session id) in a new tab,
    or with `pane`, in a new pane of its tab. Panes tile: the largest pane other than the board is halved across its
    longer side (a cell is about twice as tall as it is wide), and the first one takes the lower half of the board.
    A tab too small to split again gets a new tab instead. Whatever had the focus in that window gets it back, so a
    new agent never takes it."""
    cmd = ' '.join(map(shlex.quote, ['/usr/bin/env', 'PORT=%d' % PORT, sys.executable, os.path.join(HERE, 'watch_tty.py')] + args))
    cmd = cmd.replace('\\', '\\\\').replace('"', '\\"')            # inside an AppleScript string
    with_ = 'with profile "W.A.T.C.H." command "%s"' % cmd
    script = '''on find(sid)
  tell application "iTerm2"
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          if unique id of s is sid then return {w, t, s}
        end repeat
      end repeat
    end repeat
  end tell
  return {}
end find
tell application "iTerm2"
  set b to my find("%(beside)s")
  if b is {} then
    create window %(with_)s
    return
  end if
  set w to item 1 of b
  set was to unique id of current session of current tab of w
  if %(pane)s then
    set best to item 3 of b
    set wide to true
    set most to 0
    repeat with s in sessions of item 2 of b
      if unique id of s is not "%(beside)s" and (columns of s) * (rows of s) > most then
        set most to (columns of s) * (rows of s)
        set best to s
        set wide to (columns of s) > 2 * (rows of s)
      end if
    end repeat
    try
      if wide and most > 0 then
        tell best to split vertically %(with_)s
      else
        tell best to split horizontally %(with_)s
      end if
    on error
      tell w to create tab %(with_)s
    end try
  else
    tell w to create tab %(with_)s
  end if
  delay 0.2
  set b to my find(was)
  if b is not {} then
    select item 2 of b
    select item 3 of b
  end if
end tell''' % dict(beside=re.sub(r'[^0-9A-Fa-f-]', '', beside), with_=with_, pane='true' if pane else 'false')
    return subprocess.run(['osascript', '-e', script], stdout=subprocess.DEVNULL).returncode


def current(agents, window):
    """Agents the server still lists, with a log entry inside its window: the rest are history, not the fleet."""
    return [a for a in agents.values() if a['known'] and a['ts'] > time.time() - window]


def draw(rows, note):       # on each server batch (the stream's 'health' line), so a burst draws once
    w = shutil.get_terminal_size().columns
    rows = sorted(rows, key=lambda a: (not state(a).startswith('WAIT'), -a['ts']))
    out = [('\x1b[H\x1b[2J' if TTY else '\n') + ' W.A.T.C.H.  %d agents  %s' % (len(rows), time.strftime('%H:%M:%S')), '']
    for a in rows:
        age = '%4dm' % ((time.time() - a['ts']) // 60) if a['ts'] else '    -'
        out.append(('%-20s %-16s %-19s %s %s' % (a['name'], a['where'], clean(state(a), 19) or '-', age, a['doing']))[:w])
    sys.stdout.write('\n'.join(out + ([note] if note else [])) + '\n')
    sys.stdout.flush()


def main():
    argv = sys.argv[1:]
    if '--tab' in argv or '--split' in argv:
        install_profile()
        raise SystemExit(iterm_open(['--panes' if '--split' in argv else '--tabs']))
    tail, once, panes, pane = '--tail' in argv, '--once' in argv, '--panes' in argv, '--pane' in argv
    tabs = panes or '--tabs' in argv
    one = argv[argv.index('--agent') + 1] if '--agent' in argv[:-1] else ''
    if '--agent' in argv and not AID_RX.match(one):
        raise SystemExit('--agent needs an id such as c:1234abcd-…; the board tab passes it for you')
    me = (os.environ.get('ITERM_SESSION_ID') or '').partition(':')[2]
    if tabs and not (ITERM and me):
        tabs = False        # outside iTerm2 (or inside tmux) there is no tab to open beside: show the board only
    if ITERM and not one:
        sys.stdout.write('\x1b]1;W.A.T.C.H.\a')
    agents, opened, skipped, shown, window, live = {}, set(), set(), None, 1200, False
    for e in stream():
        k, aid = e.get('kind'), e.get('agent')
        if k == 'gap':
            raise SystemExit('fell behind the server; run again')
        if k == 'hello':
            window = e.get('window') or window
        if k == 'health':
            live = True         # everything before the first one is the server replaying what it kept
            now = current(agents, window)
            n = sum(state(a).startswith('WAIT') for a in now)
            if once:
                working = sum(state(a) == 'working' for a in now)
                print(' · '.join('%d %s' % (c, w) for c, w in ((n, 'waiting'), (working, 'working')) if c) or 'none active')
                return
            if tabs:            # a view for each current agent, once; a closed one stays closed
                for x, a in sorted(agents.items(), key=lambda xa: -xa[1]['ts']):
                    if a in now and x not in opened and AID_RX.match(x):
                        if len(opened) >= MAX_VIEWS:
                            skipped.add(x)
                            continue
                        opened.add(x)
                        iterm_open(['--agent', x] + (['--pane'] if panes else []), me, panes)
            if one:
                n = int(bool(agents.get(one) and state(agents[one]).startswith('WAIT')))
            elif not tail:
                left = len(skipped - opened)
                draw(now, '\n %d more agents have no view of their own: the server takes 12 windows at a time' % left if left else '')
            if ITERM and n != shown and (n or shown is not None):
                if one:
                    mark(tint(n) if pane else amber(n), 'tint' if pane else 'amber')
                else:
                    mark(amber(n) + badge('%d waiting' % n if n else ''), 'amber')
            shown = n
        if not aid or (one and aid != one):
            continue
        if k == 'forget':
            agents.pop(aid, None)
            continue
        a = agents.setdefault(aid, {'name': clean(aid[-6:]), 'where': '', 'doing': '', 'ts': 0, 'known': False, 'turn': '',
                                    'wait': '', 'perms': set(), 'calls': set(), 'asks': set()})
        was = state(a).startswith('WAIT')
        if k == 'agent':
            a.update(known=True, name=clean(e.get('name') or aid[-6:], 20), where=clean(os.path.basename(e.get('cwd') or ''), 16))
            if one and ITERM:
                sys.stdout.write('\x1b]1;%s\a' % clean(a['name'] + ' · ' + a['where'], 40))      # the tab's title
        track(a, e)
        if live and TTY and not was and state(a).startswith('WAIT'):
            sys.stdout.write('\a')
        a['doing'] = doing(e) or a['doing']
        a['ts'] = max(a['ts'], e.get('ts') or 0)
        line = doing(e, 400 if one else 80)
        if line and (one or (tail and live)):       # an agent's own view shows its kept history too
            when = time.strftime('%H:%M:%S', time.localtime(e.get('ts') or time.time()))
            print('%s %s' % (when, line) if one else '%s %-20s %s' % (when, a['name'], line), flush=True)
    raise SystemExit('the server closed the stream; run again')


if __name__ == '__main__':
    sys.stdout.reconfigure(errors='replace')        # a character the terminal cannot encode is shown as ?, never a crash
    try:
        for tries in range(20):     # the server frees a closed window's connection within seconds: wait for one, a minute at most
            try:
                main()
                break
            except urllib.error.HTTPError as e:
                if e.code != 503 or tries == 19 or '--once' in sys.argv:
                    raise
                print('the server has no free connection yet (it takes 12 windows at a time); trying again…', flush=True)
                time.sleep(3)
    except KeyboardInterrupt:
        pass
    except urllib.error.HTTPError as e:
        raise SystemExit('W.A.T.C.H. on port %d said %d %s' % (PORT, e.code, clean(e.read().decode(errors='replace'))))
    except OSError as e:            # no server, no key, or the stream dropped
        raise SystemExit('W.A.T.C.H. not reachable on port %d (%s); start src/server.py first' % (PORT, e))
    finally:
        if 'amber' in TOUCHED:      # leave the tab as it was found, and only undo what this process did
            sys.stdout.write(amber(False) + ('' if '--agent' in sys.argv else badge('')))
        if 'tint' in TOUCHED:
            sys.stdout.write(tint(False))
        sys.stdout.flush()
