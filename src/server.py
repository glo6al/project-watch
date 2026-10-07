#!/usr/bin/env python3
"""W.A.T.C.H. — Window on Agents, Tools, Changes and Hand-offs.

A local observer for Claude Code and Codex sessions. (Working title during development: Live Room. Internal
identifiers such as ~/.live-room, the X-Live-Room-Key header and the hook marker keep that name so existing keys,
hooks and saved page settings continue to work.)

What it does
  * Reads the session logs both runtimes already write (~/.claude/projects, ~/.codex/sessions)
    and streams what it finds to the page.
  * Optionally answers Claude Code permission prompts from the page (Allow / Deny), through the
    PermissionRequest hook that scripts/install_hooks.py adds. That part is NOT read-only.
  * Can ask macOS to open a session in its desktop app.

Trust boundary
  The server binds to 127.0.0.1. Every data or action endpoint (/events, /check, /keep, /unlink,
  /presence, /open, /decide, /hook, /permission) needs the key stored in ~/.live-room/key (mode 0600).
  Unlock (/pair) makes the server open a private file in your default browser; that file carries a
  one-time code, which the page trades for the key once, over HTTP on this machine (/claim). Served
  without the key: /pair, /claim, the page itself, and its own icon files under /static/ (an exact
  allow-list of four names). Previews of files an agent wrote (/fs/...) use a separate per-run
  capability token, not the key, and are served in an opaque sandbox; they reach only files the
  server granted. A program that can read the key file, or drive your browser, has the same power
  you do here. That is an OS-level boundary this tool does not try to cross.

  The hooks do not authenticate the server. They send the key, and the permission hook accepts an
  Allow or Deny, from whatever process is listening on the port. While this server is not running,
  another local program able to bind that port could collect the key and answer prompts. Keeping
  other local programs off the port is likewise outside what this tool protects against.

Run:  python3 src/server.py      then open http://127.0.0.1:8793/ and press Unlock
Env:  PORT (8793), WINDOW_MIN (20: how recently a session must have been active to appear)
"""
import glob, hmac, json, mimetypes, os, re, secrets, select, socket, subprocess, threading, time, urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get('PORT', '8793'))
WINDOW = int(os.environ.get('WINDOW_MIN', '20')) * 60
HOME = os.path.expanduser('~')
HERE = os.path.dirname(os.path.abspath(__file__))
LIVE_CAP, OLD_CAP, BACKLOG = 60000, 6000, 200000
MAX_POLL, MAX_REST, MAX_PREVIEW, MAX_BODY = 8_000_000, 5_000_000, 8_000_000, 200_000
HOLD_SECONDS, APPROVER_GRACE, MAX_HOLDS, MAX_STREAMS = 60, 12, 6, 12      # a prompt nobody answers here costs a minute, then goes to the app
ID_RX = re.compile(r'[0-9a-fA-F-]{8,64}\Z')

BOOT = '%x' % int(time.time() * 1000)
PKEY = secrets.token_urlsafe(16)          # previews only; changes every run
LOG, BASE = [], 0
COND = threading.Condition()
LOCK = threading.Lock()
AGENTS = {}
CODES = {}                   # one-time unlock codes: code -> time issued
KEEP = {}                    # agents windows have pinned: aid -> {window: when it last said so}. Their logs are followed for a day, not 20 min
KEEP_DAYS, KEEP_LEASE = 1, 15 * 60      # a window re-sends its whole pin list every few minutes; a list not refreshed in 15 min lapses
AID_RX = re.compile(r'[cx]:[0-9a-f-]{8,64}\Z')


def count(v):
    """A token count as the log states it: a finite, non-negative number, or None. Never a string, never a stand-in zero."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v in (float('inf'), float('-inf')) or v < 0:
        return None
    return int(v)


def text_of(v, cap=2000):
    """A metadata string as the log states it (a folder, a name, a model, a branch), or '' when the log states
    something that is not a string. Nothing else is passed on to the page as if it were text."""
    return v[:cap] if isinstance(v, str) else ''


def shown(v, cap=2000):
    """A value the page will show as text (a command, a file's content, a tool's output): a string stays as it is;
    nothing becomes ''; anything else the log held in that place (a dict, a list, a number, a bool) becomes a short
    JSON rendering, so the page shows what the log held rather than failing on it or making something up."""
    if isinstance(v, str):
        return v
    if v is None:
        return ''
    try:
        return json.dumps(v, ensure_ascii=False)[:cap]
    except (TypeError, ValueError):
        return str(v)[:cap]


def as_dict(v):
    """The dict the log was expected to hold here, or an empty one when it holds something else."""
    return v if isinstance(v, dict) else {}


def tool_uses(d):
    """The tool_use blocks of a Claude record, if the record has the expected shape; otherwise none."""
    m = d.get('message') if isinstance(d, dict) else None
    c = m.get('content') if isinstance(m, dict) else None
    return [b for b in c if isinstance(b, dict) and b.get('type') == 'tool_use' and isinstance(b.get('input') or {}, dict)] if isinstance(c, list) else []


def aid_of(path):
    """The agent id a log file would get, without opening it (see ClaudeTail / CodexTail)."""
    stem = os.path.basename(path)[:-6]
    if '/.codex/' in path:
        m = re.search(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$', stem)
        return 'x:' + (m.group(1) if m else stem)
    return 'c:' + (stem.replace('agent-', '') if os.path.basename(os.path.dirname(path)) == 'subagents' else stem)


def kept(path, now):
    wins = KEEP.get(aid_of(path), {})
    return any(now - t < KEEP_LEASE for t in wins.values()) and now - os.path.getmtime(path) < KEEP_DAYS * 86400


def prune_keep(now):
    for aid, wins in list(KEEP.items()):
        for w in [w for w, t in wins.items() if now - t > KEEP_LEASE]:
            wins.pop(w, None)
        if not wins:
            KEEP.pop(aid, None)


HUNTED, WROTE = set(), {}    # orphans whose first message is too short to search for; orphan -> the agents found to have written its prompt
HUNT_DONE = {}               # (orphan, log) pairs already searched -> how far the log had been written when the search ended
                             # (the position of its last complete record)
HUNT_POS = {}                # (orphan, log) pairs whose search is part way through: the state the next slice resumes from (see Tail.hunt)
HUNT_OPEN = {}               # (orphan, log) pairs searched to the end -> the writes of the orphan's prompt found in that log whose
                             # results had not been seen yet, call id -> (time, file): a result arriving later resolves them (see Tail.hunt)
HUNT_FAIL = {}               # (orphan, log) pairs whose log could not be read -> (failures in a row, when to try again)
HUNT_GROWTH = 256            # a searched log that has grown by this much is searched again (the new part only)
HUNT_BACKOFF = (60, 300, 1800, 3600)     # a log that could not be read is tried again after this long: 1 min, 5 min, 30 min, then hourly


def hunt_done(pair, extent, now=None):
    """Is this (orphan, log) pair searched, as things stand? Yes if the log was searched and has not grown by
    HUNT_GROWTH since (`extent` is how much of it has been read by now), or could not be read and its next try is
    not due yet; otherwise no. An unreadable log is never recorded as searched: it is tried again, later."""
    f = HUNT_FAIL.get(pair)
    if f and (now or time.time()) < f[1]:
        return True
    done = HUNT_DONE.get(pair)
    return done is not None and extent - done < HUNT_GROWTH


def forget_hunt(aid):
    """Search state that mentions this agent, on either side: dropped with the agent, or with the log it was read from."""
    HUNTED.discard(aid)
    WROTE.pop(aid, None)
    for table in (HUNT_DONE, HUNT_POS, HUNT_OPEN, HUNT_FAIL):
        for k in [k for k in table if aid in k]:
            table.pop(k, None)
    for k, v in list(WROTE.items()):
        v.discard(aid)
        if not v:
            WROTE.pop(k, None)
STATIC = {'favicon.ico': 'image/x-icon', 'favicon-32.png': 'image/png', 'apple-touch-icon-180.png': 'image/png', 'icon.svg': 'image/svg+xml'}
HOOK_SEEN = set()            # which end-of-turn hooks have arrived at least once, for the start-up log
PENDING = {}                 # permission prompts being held: pid -> dict
APPROVER = 0.0               # last time a visible room page checked in
STREAMS = 0
ALLOWED_DIRS, ALLOWED_FILES = {}, {}     # insertion-ordered so they can be trimmed
LAUNCHES = []                # (agent, command, time) for commands that start another agent
SCRIPT_LAUNCHES = []         # (agent, command, time) for command-line agents merely NAMED in a Codex script's text: never a link, only a labelled guess
USED_GUESS = {}              # (agent, time, which invocation) -> the session it was guessed for, so one line is guessed for one session
FIRST_PROMPT = {}            # agent -> (first message, time)
SCAN = {'ts': 0.0, 'tails': 0, 'ready': False, 'err': '', 'failing': 0, 'scanned': 0.0}
LAST_PAIR = 0.0
LAST_PRUNE = [0.0]           # when stored links were last checked for children not seen in LINK_DAYS (see prune_links)


def load_key():
    """The key lives in a private directory, in a private regular file owned by you. Creation is serialised with a
    lock and each file is published by rename, so two servers starting together agree on one key and a hook never
    reads a half-written header. Unlock files old enough to be dead are removed; a younger one may belong to a
    server that is still running, and is left for it."""
    import fcntl, stat as _st, tempfile
    d = os.path.join(HOME, '.live-room')
    os.makedirs(d, mode=0o700, exist_ok=True)
    if os.path.islink(d) or os.stat(d).st_uid != os.getuid():
        raise SystemExit('~/.live-room is not a private directory owned by you; refusing to start')
    os.chmod(d, 0o700)

    def publish(name, text):
        fd, tmp = tempfile.mkstemp(dir=d, prefix='.' + name + '-')
        with os.fdopen(fd, 'w') as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, os.path.join(d, name))

    lock = os.open(os.path.join(d, '.lock'), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        p, k = os.path.join(d, 'key'), ''
        try:
            fd = os.open(p, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            try:
                st = os.fstat(fd)
                if _st.S_ISREG(st.st_mode) and st.st_uid == os.getuid():
                    os.fchmod(fd, 0o600)
                    k = os.read(fd, 400).decode('ascii', 'ignore').strip()
            finally:
                os.close(fd)
        except OSError:
            pass
        if len(k) < 24:
            k = secrets.token_urlsafe(32)
            publish('key', k)
        publish('header', 'X-Live-Room-Key: %s\n' % k)
        for stale in glob.glob(os.path.join(d, 'unlock-*.html')):
            try:
                if time.time() - os.lstat(stale).st_mtime > 180:
                    os.unlink(stale)
            except OSError:
                pass
    finally:
        os.close(lock)
    return k


KEY = load_key()


def clip(v, cap):
    if isinstance(v, str) and len(v) > cap:
        return v[:cap] + '\n… (truncated)'
    return v


# Every field the page shows as text. Whatever an adapter passes under one of these names reaches the page as a string
# (see shown); the fields the page reads as numbers or flags (ctx, pre, post, code, err, done, soft, …) are left as they are.
TEXT_KEYS = frozenset(('text', 'out', 'cmd', 'why', 'path', 'q', 'name', 'content', 'before', 'after', 'label', 'img', 'how', 'goal',
                       'doing', 'op', 'status', 'num', 'trigger', 'tool', 'via', 'task', 'title', 'description', 'input',
                       'cwd', 'branch', 'model', 'effort', 'perm', 'runtime', 'spawn', 'nick'))


def emit(agent, kind, old, ts=None, **kw):
    global BASE
    cap = OLD_CAP if old else LIVE_CAP
    kw = {k: clip(shown(v) if k in TEXT_KEYS else v, cap) for k, v in kw.items()}
    touch_link(agent)                                   # any event from an agent is that agent being seen (see prune_links)
    with COND:
        ev = dict(seq=BASE + len(LOG), boot=BOOT, agent=agent, kind=kind, old=old, ts=ts or time.time(), **kw)
        LOG.append(ev)
        if len(LOG) > 4000:
            del LOG[:1000]
            BASE += 1000
        COND.notify_all()


NAMES_PATH = os.path.join(HOME, '.live-room', 'projects.json')


def project_names():
    """Display names for project folders, from ~/.live-room/projects.json ({"folder name": "shown as"}), read each
    time a window connects so an edit to the file shows on the next reload. Private to this machine, never in the
    public files. Only short strings are accepted."""
    try:
        with open(NAMES_PATH, encoding='utf-8') as f:
            d = json.loads(f.read(65536))                      # a names file is small; anything past 64 KB is not one
        if not isinstance(d, dict):
            return {}
        out = {}
        for k, v in list(d.items())[:200]:
            if not (isinstance(k, str) and isinstance(v, str) and 0 < len(k) <= 200 and v.strip() and len(v.strip()) <= 60):
                continue                                      # an oversized key is dropped, never cut down to collide with another
            try:
                (k + v).encode('utf-8')                       # a string JSON accepts but UTF-8 cannot carry (a lone surrogate) must not break the stream
            except UnicodeEncodeError:
                continue
            out[k] = v.strip()
        return out
    except Exception:
        return {}                                             # naming is optional: nothing about it may stop a window connecting


LINKS_PATH = os.path.join(HOME, '.live-room', 'links.json')
LINKS = {}                   # child -> {parent, via, soft, sub, how, at, seen}: every association made, kept on disk for as long as the child is seen
LINKS_LOCK = threading.RLock()       # every change to LINKS, and every write of the file, happens under this one lock: the watcher
                                     # thread and the HTTP threads (/unlink, hook-made stations) all get here
LINK_DAYS = 30               # a link whose child has not been seen in any log for this long is dropped (and not before: see prune_links)
SEEN_SAVED = {}              # child -> the `seen` time last written to disk for it (see touch_link)
SEEN_SAVE_EVERY = 300        # a child's refreshed `seen` reaches the file at most this often, and at every prune


def link_entry(parent, via, soft, sub):
    """One stored link, with its provenance: recorded (the runtime wrote it down), inferred (a launch matched) or
    related (work in common). An entry cannot be both soft and a sub-agent; soft wins."""
    soft, sub = bool(soft), bool(sub) and not soft
    how = 'recorded' if not soft else ('related' if str(via or '').startswith('related') else 'inferred')
    return {'parent': parent, 'via': str(via or ''), 'soft': soft, 'sub': sub, 'how': how, 'at': time.time(), 'seen': time.time()}


def load_links():
    """Only well-formed entries are kept: valid ids on both sides, boolean flags, no link to itself, no cycles."""
    try:
        with open(LINKS_PATH) as f:
            d = json.load(f)
    except Exception:
        return
    if not isinstance(d, dict):
        return
    ok = {}
    for k, v in d.items():
        if not (isinstance(k, str) and AID_RX.match(k) and isinstance(v, dict) and isinstance(v.get('parent'), str)
                and AID_RX.match(v['parent']) and v['parent'] != k
                and all(isinstance(v.get(f, False), bool) for f in ('soft', 'sub'))):
            continue
        e = link_entry(v['parent'], v.get('via') if isinstance(v.get('via'), str) else '', v.get('soft'), v.get('sub'))
        for f in ('at', 'seen'):             # when it was made and when its child was last seen; 0 when the file does not say
            e[f] = v[f] if isinstance(v.get(f), (int, float)) and not isinstance(v.get(f), bool) else 0
        if v.get('how') in ('recorded', 'inferred', 'related') and (v['how'] == 'recorded') == (not e['soft']):
            e['how'] = v['how']
        ok[k] = e
    for k in list(ok):                                  # a chain that comes back to its start is dropped from the start
        node, seen = k, set()
        while node in ok and node not in seen:
            seen.add(node)
            node = ok[node]['parent']
        if node in seen:
            ok.pop(k, None)
    with LINKS_LOCK:
        LINKS.update(ok)


def save_links():
    """The graph as it is at this moment, written whole to a file of its own and then renamed over links.json. Held
    under LINKS_LOCK by every caller, so two writers never share a temporary file or race each other's rename."""
    import tempfile
    with LINKS_LOCK:
        text = json.dumps({k: dict(v) for k, v in LINKS.items()})      # a snapshot: nothing shared is iterated outside the lock
        try:
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(LINKS_PATH), prefix='.links-')
            try:
                with os.fdopen(fd, 'w') as f:
                    f.write(text)
                os.chmod(tmp, 0o600)
                os.replace(tmp, LINKS_PATH)
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError:
            pass


def prune_links(now):
    """Links whose child has not been seen (any event from it, a description from a log or a hook) for LINK_DAYS are
    dropped; an entry that says neither when it was made nor when its child was last seen is treated as old. Saved
    when something went, and also when a refreshed `seen` is still waiting to reach the file (see touch_link). Not
    run at start-up: the watcher runs it after its first pass over the logs, so children with a log on disk have
    been seen again before anything is judged old."""
    with LINKS_LOCK:
        old = [k for k, v in LINKS.items() if now - max(v.get('at') or 0, v.get('seen') or 0) > LINK_DAYS * 86400]
        for k in old:
            LINKS.pop(k, None)
            SEEN_SAVED.pop(k, None)
        for k in [k for k in SEEN_SAVED if k not in LINKS]:
            SEEN_SAVED.pop(k, None)
        if old or any(v.get('seen', 0) > SEEN_SAVED.get(k, 0) for k, v in LINKS.items()):
            save_links()
    return len(old)


def touch_link(aid, now=None):
    """The child of a stored link has just been seen: in a log, by a hook, or described. Its `seen` is refreshed in
    memory at once and written to disk at most every SEEN_SAVE_EVERY seconds per child (every refresh would mean a
    file write per event), and otherwise at the hourly prune. Cheap when the agent has no stored link."""
    if aid not in LINKS:                                # a bare lookup; LINKS only ever gains or loses keys under the lock
        return
    now = now or time.time()
    with LINKS_LOCK:
        e = LINKS.get(aid)
        if not e:
            return
        e['seen'] = now
        if now - SEEN_SAVED.get(aid, 0) >= SEEN_SAVE_EVERY:
            for k, v in LINKS.items():                  # one write carries every refresh pending at this moment
                SEEN_SAVED[k] = v.get('seen', 0)
            save_links()


def would_cycle(aid, parent):
    """Would making `parent` the parent of `aid` close a loop? The chain above `parent` is followed through the live
    graph and the stored one; the same rule load_links applies to the file, applied before a live link is accepted.
    Called under LINKS_LOCK, which every change to either graph's parent edges is made under, so the chain it
    follows is the graph the new edge would be committed into."""
    node, seen = parent, set()
    while node and node not in seen:
        if node == aid:
            return True
        seen.add(node)
        node = AGENTS.get(node, {}).get('parent') or (LINKS.get(node) or {}).get('parent')
    return False


def describe(aid, old=False, **kw):
    """What is known about an agent, merged into its record and sent to the page when something changed. The whole
    of it, from the cycle check through the stored link, the live parent fields, the file and the event, is one
    transaction under LINKS_LOCK: /unlink and the watcher cannot interleave between the stored graph and the live
    one, and the events leave in the order the changes were made."""
    with LINKS_LOCK:
        a = AGENTS.setdefault(aid, {})
        if aid in LINKS:
            LINKS[aid]['seen'] = time.time()          # the child is still about: its link is worth keeping (see prune_links)
        if kw.get('parent') and kw['parent'] != aid and AID_RX.match(str(kw['parent'])) and not would_cycle(aid, kw['parent']):
            # A link, once made, is kept on disk and never dropped by a restart or by a launch falling out of a log's
            # search window. A recorded link (the runtime's own metadata) always wins, even over an older recorded one;
            # an inferred link never replaces anything. A link that would close a loop is refused outright, whatever
            # its provenance (so a recorded link arriving after an inferred one in the other direction is refused too).
            if kw.get('soft'):
                kw = dict(kw, sub=None)
            have = LINKS.get(aid)
            if not have or not kw.get('soft'):
                new = link_entry(kw['parent'], kw.get('via'), kw.get('soft'), kw.get('sub'))
                if not have or any(have.get(f) != new[f] for f in ('parent', 'soft', 'sub')):
                    LINKS[aid] = new
                    SEEN_SAVED[aid] = new['seen']
                    save_links()
            elif have.get('parent') != kw['parent'] or (kw.get('soft') and not have.get('soft')):
                kw = dict(kw, parent=have['parent'], via=have.get('via') or kw.get('via'), soft=have.get('soft'), sub=have.get('sub'))
        elif kw.get('parent'):
            kw = dict(kw, parent=None, via=None, soft=None, sub=None)
        elif aid in LINKS and not a.get('parent') and not would_cycle(aid, LINKS[aid]['parent']):
            have = LINKS[aid]            # an agent seen again after a restart gets its link back before anything else
            kw = dict(kw, parent=have['parent'], via=have.get('via') or None, soft=have.get('soft') or None, sub=have.get('sub') or None)
        if kw.get('parent') and not kw.get('soft') and a.get('parent') not in (None, kw['parent']):
            a.pop('parent', None); a.pop('via', None); a.pop('soft', None); a.pop('sub', None)     # newer recorded evidence names another parent
        if kw.get('parent') or a.get('parent'):
            a.pop('maybe', None); kw = {k: v for k, v in kw.items() if k != 'maybe'}      # a parent, however it was found, retires the guess
        if kw.get('parent') and not kw.get('soft') and (a.get('soft') or (a.get('via') and not kw.get('via'))):
            a.pop('soft', None); a.pop('via', None)      # a recorded link replaces an inferred one outright
            kw = dict(kw, _cleared=True)
        changed = any(v and a.get(k) != v for k, v in kw.items())
        a.update({k: v for k, v in kw.items() if v and k != '_cleared'})
        if changed:
            emit(aid, 'agent', old, **a)


def unlink(child):
    """Drop a stored association, whatever made it, from the file and from the live record, as one transaction
    under LINKS_LOCK (see describe). Returns the agent's record if it has one, after the change."""
    with LINKS_LOCK:
        LINKS.pop(child, None)
        SEEN_SAVED.pop(child, None)
        save_links()
        a = AGENTS.get(child)
        if a:
            for f in ('parent', 'via', 'soft', 'sub'):
                a.pop(f, None)
            emit(child, 'agent', False, **dict(a, parent='', via='', soft=False, sub=False))
        return a


load_links()


def ts_of(d):
    try:
        return datetime.fromisoformat(d['timestamp'].replace('Z', '+00:00')).timestamp()
    except Exception:
        return time.time()


def flat(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):               # a block's text may itself be something other than a string: shown as the log held it
        return '\n'.join((shown(b['text']) if 'text' in b else '[%s]' % b.get('type', '?')) if isinstance(b, dict) else shown(b) for b in content)
    return shown(content)


ENVELOPES = ('in-app-browser-context', 'environment_context', 'user_instructions', 'system-reminder', 'ide_selection',
             'ide_opened_file', 'turn_aborted', 'codex_delegation', 'task-notification', 'local-command-stdout',
             'local-command-stderr', 'local-command-caveat', 'command-name', 'command-message', 'command-args')
WRAPPER = re.compile(r'\s*<(%s)\b[^>]*>.*?</\1>\s*' % '|'.join(map(re.escape, ENVELOPES)), re.S)


def clean_user(txt):
    """Both desktop apps prepend machine-written blocks (ambient UI state and the like) to what you typed. Only
    blocks with known names are removed, from the front; anything else, including other XML-like text, is kept."""
    txt = txt or ''
    for _ in range(12):
        m = WRAPPER.match(txt)
        if not m:
            break
        txt = txt[m.end():]
    if txt.lstrip().startswith('# Files mentioned by the user') and '## My request:' in txt:   # Codex Desktop's wrapper
        txt = txt.split('## My request:', 1)[1]
    elif txt.lstrip().startswith('## My request:'):
        txt = txt.lstrip()[len('## My request:'):]
    txt = txt.strip()
    return '' if txt.startswith('[Request interrupted') else txt


def is_html(path):
    return bool(path) and path.lower().endswith(('.html', '.htm'))


# ---------------------------------------------------------------- outward-facing commands (pattern match only)
RISKS = [
    ('deploy', re.compile(r"^(?:npx\s+)?(vercel|netlify|wrangler|firebase|flyctl|fly|railway|amplify)\b.*(\bdeploy\b|--prod\b|\bpublish\b)|^(npm|pnpm|yarn)\s+(run\s+)?deploy\b|^terraform\s+apply\b|^kubectl\s+(apply|delete)\b|^(?:npx\s+)?supabase\s+(db\s+push|functions\s+deploy|migration\s+up)\b", re.I | re.S)),
    ('push', re.compile(r"^git\s+(?:-\S+\s+\S+\s+)*push\b|^gh\s+(pr\s+(create|merge)|release\s+create|repo\s+(create|delete))\b", re.I)),
    ('publish', re.compile(r"^(npm|pnpm|yarn)\s+publish\b", re.I)),
    ('delete', re.compile(r"^(?:sudo\s+)?rm\s+(?:-\w+\s+)*-\w*[rR]\w*\s|^(?:sudo\s+)?rm\s+(?:-\w+\s+)*--recursive|^git\s+(?:-\S+\s+\S+\s+)*(reset\s+--hard|clean\s+-\w*f)", re.S)),
    ('send', re.compile(r"^(sendmail|mailx|mail\s+-s)\b", re.I)),
]
MUTATE = re.compile(r"-X\s*(POST|PUT|PATCH|DELETE)\b|\s(--data\S*|-d|--form|-F|--json)\s", re.I)
TOOL_RISK = re.compile(r"send|publish|deploy|delete|trash|share|merge|post_|create_scheduled|RemoteTrigger", re.I)
LOCAL_HOSTS = ('127.0.0.1', 'localhost', '::1')


PREFIX = re.compile(r'^\s*(?:(?:sudo|env|time|nohup|command|exec)\s+|\w+=\S*\s+)*')
SHELL_C = re.compile(r'^(?:ba|z|da)?sh\s+(?:-\w+\s+)*-\w*c\s+(["\'])(.*)\1\s*$', re.S)


HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1(.*?)\n\2\b", re.S)


def sub_end(cmd, j):
    """Where the $( opened just before j closes. Quotes are respected, so a ) inside '...' or "..." does not end it."""
    dq, n = False, len(cmd)
    while j < n:
        ch = cmd[j]
        if ch == '\\':
            j += 2
            continue
        if ch == "'" and not dq:
            k = cmd.find("'", j + 1)
            j = n if k < 0 else k + 1
            continue
        if ch == '"':
            dq = not dq
        elif ch == '$' and cmd[j + 1:j + 2] == '(':
            j = sub_end(cmd, j + 2)
            continue
        elif not dq and ch == '(':
            j = sub_end(cmd, j + 1)
            continue
        elif not dq and ch == ')':
            return j + 1
        j += 1
    return n + 1


def segments(cmd, depth=0, mark=False, body=False):
    """The commands a shell line would run, as far as a small parser can tell: split on ; | & and newlines outside
    quotes; heredoc bodies and # comments removed; sudo/env wrappers stripped; `sh -c "..."`, $(...) and backtick
    substitutions followed, including those in a heredoc whose tag is unquoted (the shell runs them). Text that is
    only an argument (to echo, python -c, a commit message) is not a command.
    This is lexical: it says what a line asks for, not that it ran. With mark, a substitution leaves $() behind."""
    bodies = []

    def heredoc(m):
        if not m.group(1):
            bodies.append(m.group(3))
        return ' '
    cmd = cmd or ''
    if not body:
        cmd = HEREDOC.sub(heredoc, cmd)
    out, cur, q, esc, subs, i, n = [], '', '"' if body else '', False, [], 0, len(cmd)
    while i < n:
        ch = cmd[i]
        if esc:
            cur += ch; esc = False
        elif ch == '\\' and q != "'":
            cur += ch; esc = True
        elif q != "'" and ch == '$' and cmd[i + 1:i + 2] == '(':       # command substitution runs, even inside "..."
            j = sub_end(cmd, i + 2)
            subs.append(cmd[i + 2:j - 1]); cur += '$()' if mark else ' '; i = j; continue
        elif q != "'" and ch == '`':
            j = cmd.find('`', i + 1)
            j = n if j < 0 else j
            subs.append(cmd[i + 1:j]); cur += '$()' if mark else ' '; i = j + 1; continue
        elif q:
            cur += ch
            if ch == q and not body:
                q = ''
        elif ch in '\'"':
            q = ch; cur += ch
        elif ch == '#' and (not cur or cur[-1] in ' \t'):              # a comment runs to the end of the line
            j = cmd.find('\n', i)
            i = n if j < 0 else j
            continue
        elif ch in ';|&\n':
            out.append(cur); cur = ''
        else:
            cur += ch
        i += 1
    out.append(cur)
    res = []
    for seg in ([] if body else out):               # a heredoc body is text; only what it substitutes is run
        seg = PREFIX.sub('', seg).strip()
        if not seg:
            continue
        m = SHELL_C.match(seg)
        if m and depth < 3:
            res += segments(m.group(2), depth + 1, mark)
        else:
            res.append(seg)
    if depth < 3:
        for inner in subs:
            res += segments(inner, depth + 1)
        for inner in bodies:
            res += segments(inner, depth + 1, body=True)
    return res


SQL_RX = re.compile(r"\b(DROP|TRUNCATE)\s+(TABLE|DATABASE)\b|\bDELETE\s+FROM\b", re.I)
CHECK_RX = re.compile(r"^((npm|pnpm|yarn|bun|npx)\s+(run\s+)?(test|build|lint|typecheck)\b|(npx\s+)?(pytest|vitest|jest|tsc|eslint)\b|python[\d.]*\s+-m\s+pytest\b|(npx\s+)?playwright\s+test\b|cargo\s+(test|build)\b|go\s+test\b)", re.I)


def sql_text(seg):
    """A database command's arguments with the shell's quoting removed and SQL string literals emptied, so a
    statement is judged by what it does, not by words inside a value. If the line cannot be split, it is kept whole."""
    import shlex
    try:
        words = shlex.split(seg)
    except ValueError:
        return seg
    return ' '.join(re.sub(r"'(?:[^']|'')*'", "''", w) for w in words)


def risk(cmd):
    """A label when a command LOOKS outward-facing or destructive. Pattern match on the parsed commands."""
    for seg in segments(cmd):
        if re.match(r'(echo|printf)\b', seg):
            continue
        for label, rx in RISKS:
            if rx.search(seg):
                return label
        if re.match(r'(psql|mysql|sqlite3|duckdb|supabase|sqlcmd|mongosh)\b', seg) and SQL_RX.search(sql_text(seg)):
            return 'delete'
        if re.match(r'curl\b', seg) and MUTATE.search(' ' + seg + ' '):
            for url in re.findall(r'https?://[^\s\'"]+', seg):
                host = (urllib.parse.urlparse(url).hostname or '').lower()
                if host not in LOCAL_HOSTS:
                    return 'send'
    return ''


def is_check(cmd):
    """Does this line ask for a test, build, lint or typecheck? Decided here once, and sent to the page."""
    return any(CHECK_RX.match(seg) for seg in segments(cmd))


LAUNCH_RX = re.compile(r'(?:npx\s+)?(?:\S*/)?(codex\s+exec|claude\s+(?:\S+\s+)*(-p|--print))\b')     # the tool may be given by path


def launch_calls(cmd):
    """Each command-line agent a shell line starts, as (tool, text, indirect): the text of that one invocation with
    any heredoc fed to it, and whether its prompt comes from a file, variable or stdin. Other commands on the same
    line, and comments, are not part of it."""
    bodies = []

    def stash(m):
        head, _, text = m.group(3).partition('\n')
        bodies.append(text)
        return ' \x00%d\x00 %s\n' % (len(bodies) - 1, head)
    out = []
    for g in segments(HEREDOC.sub(stash, (cmd or '').replace('\x00', '')), mark=True):
        if LAUNCH_RX.match(g):
            w = g.split()
            bare = re.sub(r"'[^']*'", "''", g)                   # a $ inside '...' is only text
            out.append(('codex' if 'codex' in w[1 if w[0] == 'npx' else 0] else 'claude',
                        re.sub(r'\x00(\d+)\x00', lambda m: bodies[int(m.group(1))], g),
                        bool(re.search(r'\x00|\$\(|\$\{?\w|<\s*\S|\s-\s*$', bare))))
    return out


def note_launch(aid, cmd, ts):
    """A command this agent is recorded as running (a Claude Bash call, or a Codex CommandExecution item) that starts
    another command-line agent. A command that only appears in the text of a Codex script step is not one, whether
    or not a result arrives for the step (see CodexTail.call). The same command seen again within a few minutes,
    say in the replayed tail and again further back in a search, is one launch."""
    if not launch_calls(cmd):
        return False
    if not any(p == aid and c == cmd and abs(t - ts) < 300 for p, c, t in LAUNCHES):
        LAUNCHES.append((aid, cmd, ts))
        del LAUNCHES[:-80]
        kept = {(p, t) for p, _, t in LAUNCHES}
        for k in [k for k in USED_LAUNCH if k[:2] not in kept]:
            USED_LAUNCH.pop(k, None)
        match_launch()
    return True


def note_script_launch(aid, cmd, ts):
    """A command-line agent named in the text of a Codex script step. The script may never run that line, so this is
    not a launch and never becomes a link (review 7, F113). It is kept, briefly, so a session that starts right after
    can be shown as PROBABLY started here: a labelled guess on its monitor, in memory only, gone with the session."""
    if not launch_calls(cmd):
        return False
    if not any(p == aid and c == cmd and abs(t - ts) < 300 for p, c, t in SCRIPT_LAUNCHES):
        SCRIPT_LAUNCHES.append((aid, cmd, ts))
        del SCRIPT_LAUNCHES[:-80]
        kept = {(p, t) for p, _, t in SCRIPT_LAUNCHES}
        for k in [k for k in USED_GUESS if k[:2] not in kept]:
            USED_GUESS.pop(k, None)
        match_guess()
    return True


def match_guess(aid=None):
    """The same two tests as match_launch, run over commands that only appear in script text. A match is a GUESS:
    describe(child, maybe={parent, via}) puts it on the monitor as 'possibly started by', nothing is written to the
    link store, 'Open in' never follows it, and any real parent (recorded or inferred from an execution record)
    replaces it. A child with a parent is never guessed about."""
    calls = [(p, t, i, tool, text, indirect) for p, c, t in SCRIPT_LAUNCHES for i, (tool, text, indirect) in enumerate(launch_calls(c))]
    for child in ([aid] if aid else list(FIRST_PROMPT)):
        text, t0 = FIRST_PROMPT.get(child, ('', 0))
        a = AGENTS.get(child, {})
        if a.get('parent') or not t0:
            continue
        held = a.get('maybe') or {}                     # an earlier guess: only a stronger one (the prompt text itself) may replace it
        start = a.get('started') or t0
        key = re.split(r"['\"`\n]", text.strip())[0][:80]
        tool = 'codex' if child.startswith('x:') else 'claude'
        mine = [c for c in calls if c[0] != child and c[3] == tool and not is_ancestor(child, c[0])
                and USED_GUESS.get(c[:3], child) == child]
        found = None
        if len(key) >= 30:
            for c in reversed(mine):
                if key in c[4] and -10 <= t0 - c[1] <= 600 and -10 <= start - c[1] <= 600:
                    found = (c, 'its first message is the prompt in that script; the script may not have run that line')
                    break
        if not found and child not in BACKFILLED:
            indirect = [c for c in mine if c[5] and -3 <= start - c[1] <= 30]
            if len({c[0] for c in indirect}) == 1:
                found = (indirect[0], 'it began seconds after that script named a command-line agent with a prompt from a file; the script may not have run that line')
        if found and held and not (found[1].startswith('its first message') and not held.get('via', '').startswith('its first message')):
            found = None                                # nothing better than what is already shown
        if found:
            USED_GUESS[found[0][:3]] = child
            describe(child, maybe={'parent': found[0][0], 'via': found[1] + ' (' + (found[0][4][:120] if found[0][4] else 'a command-line agent') + ')'})


def is_ancestor(maybe_ancestor, node):
    seen = set()
    while node and node not in seen:
        if node == maybe_ancestor:
            return True
        seen.add(node)
        node = AGENTS.get(node, {}).get('parent')
    return False


USED_LAUNCH = {}              # (agent, time, which invocation on that line) -> the session it was paired with: one launch, one session


def match_launch(aid=None):
    """An agent that runs a command-line agent (Claude running `codex exec`, Codex running `claude -p`, …) is treated
    as the parent of the session that starts, which is how the owner thinks of them. Neither tool records the link,
    so it is inferred and always labelled so:
      strong  the new session's first message is the prompt text in that one invocation (not merely somewhere on
              the same line), and the session began soon after;
      timing  the invocation took its prompt from a file, variable or stdin (so no text can match), this session
              began in the half minute after it, and every such unclaimed launch in that time is one agent's.
    Either way a launch is claimed by one session only. A first message recovered from further back in a log is
    not a reliable start time and is never matched by timing."""
    calls = [(p, t, i, tool, text, indirect) for p, c, t in LAUNCHES for i, (tool, text, indirect) in enumerate(launch_calls(c))]
    for child in ([aid] if aid else list(FIRST_PROMPT)):
        text, t0 = FIRST_PROMPT.get(child, ('', 0))
        a = AGENTS.get(child, {})
        if a.get('parent') or not t0:
            continue
        start = a.get('started') or t0
        key = re.split(r"['\"`\n]", text.strip())[0][:80]
        tool = 'codex' if child.startswith('x:') else 'claude'
        mine = [c for c in calls if c[0] != child and c[3] == tool and not is_ancestor(child, c[0])
                and USED_LAUNCH.get(c[:3], child) == child]
        found = None
        if len(key) >= 30:
            for c in reversed(mine):
                if key in c[4] and -10 <= t0 - c[1] <= 600 and -10 <= start - c[1] <= 600:
                    found = (c, 'inferred: its first message is the prompt that agent passed on a command line')
                    break
        if not found and child not in BACKFILLED:
            indirect = [c for c in mine if c[5] and -3 <= start - c[1] <= 30]
            if len({c[0] for c in indirect}) == 1:
                found = (indirect[0], 'inferred: it began seconds after that agent ran a command-line agent with a prompt from a file')
        if found:
            USED_LAUNCH[found[0][:3]] = child
            describe(child, parent=found[0][0], via=found[1], soft=True)


BACKFILLED = set()            # agents whose "first message" was recovered from further back, so its time is not a start time
EXEC_KIDS = {}               # script-launched runs still without a parent: agent -> None
WORKED = {}                  # folder -> [(agent, first seen there, last seen there), …]: where agents have been reading, writing, running
PATH_RX = re.compile(r"/Users/[^/\n\"'`]+(?:/[^/\n\"'`<>|;*$]+){2,8}")     # folder names may contain spaces


def folder_keys(path):
    """'Which piece of work is this', from specific to general: the FOLDER (a trailing file name is dropped) cut at
    nine, eight and seven levels."""
    parts = [x.strip() for x in path.split('/')]
    if parts and '.' in parts[-1]:
        parts = parts[:-1]
    return ['/'.join(parts[:d]) for d in (9, 8, 7, 6) if len(parts) >= d]


def note_work(k, aid, first, last):
    lst = WORKED.pop(k, [])
    for x in lst:
        if x[0] == aid:                              # later work never erases when it was first seen there
            first, last = min(first, x[1]), max(last, x[2])
    WORKED[k] = [x for x in lst if x[0] != aid][-3:] + [(aid, first, last)]
    while len(WORKED) > 900:
        WORKED.pop(next(iter(WORKED)))


def worked(aid, text, ts):
    for path in set(PATH_RX.findall(text or '')[:40]):
        for k in folder_keys(path):
            note_work(k, aid, ts, ts)


def match_work(child, text, ts):
    """A run started by a script records no parent, and nobody launched it from an agent's command line. If the files
    it was handed sit in a folder that exactly one agent was already working in BEFORE it began, it is shown with that
    agent, as related work. This is association, not proof of who started it, and the page says so. Two agents in the
    same folder means no answer."""
    if AGENTS.get(child, {}).get('parent'):
        return
    best = None                                  # (depth rank, agents)
    for path in set(PATH_RX.findall(text or '')[:60]):
        for rank, k in enumerate(folder_keys(path)):
            who = {a for a, first, last in WORKED.get(k, []) if a != child and first <= ts and ts - last < 6 * 3600 and not is_ancestor(child, a)}
            if who:
                if best is None or rank < best[0]:
                    best = (rank, who)
                elif rank == best[0]:
                    best = (rank, best[1] | who)
                break
    if best and len(best[1]) == 1:
        describe(child, parent=next(iter(best[1])), soft=True,
                 via='related, not launched by it: it was handed files from a folder that agent was already working in')


def set_goal(aid, text, ts=None, early=False):
    """The goal as the owner stated it: the earliest message of the session that has been read, one line of it, so the
    monitor can carry it in the room. When the log was not read from its start the page is told so (goalEarly), since
    the message may not be the first; a message with an earlier time replaces a later one. What the owner meant is
    theirs to remember; this is only what they typed."""
    a = AGENTS.get(aid)
    if a is None:
        return
    have = a.get('goal_ts')
    if a.get('goal') and (ts is None or have is None or ts >= have):
        return
    a['goal_ts'] = ts
    describe(aid, False, goal=' '.join(str(text).split())[:240], goalEarly=bool(early or aid in BACKFILLED or a.get('partial')))


def remember_prompt(aid, text, ts, backfilled=False):
    set_goal(aid, text, ts, backfilled)
    if aid not in FIRST_PROMPT:
        if backfilled:
            BACKFILLED.add(aid)
        FIRST_PROMPT[aid] = (text, ts)
        while len(FIRST_PROMPT) > 300:
            BACKFILLED.discard(next(iter(FIRST_PROMPT)))
            FIRST_PROMPT.pop(next(iter(FIRST_PROMPT)))
        match_launch(aid)
        match_guess(aid)


IMG_EXT = ('.png', '.jpg', '.jpeg', '.gif', '.webp')


def saw(aid, old, ts, path, label):
    """An image the agent was actually handed (a screenshot result, a picture it opened)."""
    path = urllib.parse.unquote(path[7:]) if path.startswith('file://') else path
    try:
        real = os.path.realpath(path)
        st = os.lstat(real)
        import stat as _st
        if not real.lower().endswith(IMG_EXT) or not _st.S_ISREG(st.st_mode) or real.startswith(tuple(p for p in PRIVATE_ROOTS if '.claude' not in p)):
            return
    except OSError:
        return
    ALLOWED_FILES.pop(real, None)
    ALLOWED_FILES[real] = (st.st_ino, st.st_dev)
    while len(ALLOWED_FILES) > 400:
        ALLOWED_FILES.pop(next(iter(ALLOWED_FILES)))
    emit(aid, 'see', old, ts, img=real, label=label)


PRIVATE_ROOTS = tuple(os.path.join(HOME, p) + os.sep for p in ('.live-room', '.ssh', '.claude', '.codex', '.aws', '.gnupg', '.config', 'Library'))


def dir_ids(paths):
    out = set()
    for p in paths:
        try:
            st = os.stat(p)
            out.add((st.st_ino, st.st_dev))
        except OSError:
            pass
    return out


def above(path):
    """A folder and every folder above it."""
    out = [path]
    while os.path.dirname(path) != path:
        path = os.path.dirname(path)
        out.append(path)
    return out


def in_private(real):
    """Is this path inside a private folder? Judged by name and also by the identity of each folder above it, since
    file names here may ignore case and the home folder may be reached under more than one name."""
    return (real + os.sep).startswith(PRIVATE_ROOTS) or bool(dir_ids(above(os.path.dirname(real))) & dir_ids(PRIVATE_ROOTS))


def allow_preview(aid, old, ts, cid, path):
    """Let the page render an HTML file an agent wrote. The path must be a real, regular .html file reached without
    any symlink, outside private configuration folders. Its identity is recorded, so a file swapped in later under
    the same name is not served. The folder it sits in is granted for its styles, scripts and images, unless that
    folder is your home folder or above it; private folders below a granted folder are never served (see preview)."""
    try:
        real = os.path.realpath(path)
        st = os.lstat(real)
        import stat as _st
        if (not is_html(path) or not is_html(real) or os.path.abspath(path) != real or not _st.S_ISREG(st.st_mode)
                or in_private(real)):
            return
    except OSError:
        return
    ALLOWED_FILES.pop(real, None)
    ALLOWED_FILES[real] = (st.st_ino, st.st_dev)
    while len(ALLOWED_FILES) > 400:
        ALLOWED_FILES.pop(next(iter(ALLOWED_FILES)))
    try:
        ds = os.stat(os.path.dirname(real))
        if (ds.st_ino, ds.st_dev) in dir_ids(above(HOME)) or (HOME + os.sep).startswith(os.path.dirname(real).rstrip(os.sep) + os.sep):
            raise OSError('too broad')                # a page saved straight into the home folder brings no folder with it
        ALLOWED_DIRS.pop(os.path.dirname(real), None)
        ALLOWED_DIRS[os.path.dirname(real)] = (ds.st_ino, ds.st_dev)     # styles, scripts and images beside it; never other pages
        while len(ALLOWED_DIRS) > 100:
            ALLOWED_DIRS.pop(next(iter(ALLOWED_DIRS)))
    except OSError:
        pass
    emit(aid, 'file', old, ts, id=cid, path=real)


# ---------------------------------------------------------------- log tailing
class Tail:
    def __init__(self, path):
        self.path, self.rest, self.calls, self.first = path, b'', {}, True
        st = os.stat(path)
        self.ino = st.st_ino
        self.off = max(0, st.st_size - BACKLOG)
        self.skip_partial = False
        self.partial = self.off > 0
        self.drop_long = False
        self.replaced = False
        self.backfilling = False
        if self.off:
            with open(path, 'rb') as f:
                f.seek(self.off - 1)
                self.skip_partial = f.read(1) != b'\n'

    def fuel(self, old, used, top=None, ref=None, partial=False):
        """Context in use, as the log records it. `top` is the model's window when the log states it (Codex);
        `ref` is the size at which this session was last summarised automatically (Claude, which states no window).
        Usage is sent on when it has moved by a noticeable amount, not on every reply; a changed window or reference
        is sent at once. `used` is None when the record states no usable count (absent, or present but not a number):
        the figure shown until now is then withdrawn, since the latest record does not support it, and the page shows
        nothing rather than a stale number. A valid zero is a count, and is sent as zero, never dropped: a move to or
        from zero is always sent, whatever the threshold. `partial` says the record stated only some of the parts
        that make up the count, so the figure is a lower bound, and it is sent with ctxpart=True so the page can say
        so; a change in that status is sent at once."""
        if not getattr(self, 'aid', None) or getattr(self, 'skip', False):
            return
        used, top, ref = count(used), count(top) or None, count(ref) or None
        partial = bool(partial) and used is not None
        was = getattr(self, 'fuel_sent', None)       # None: no figure has been sent (or the last one was withdrawn)
        if used is None or was is None:
            moved = (used is None) != (was is None)
        elif (used == 0) != (was == 0):
            moved = True                              # nothing in use, or something again: never swallowed by the threshold
        else:
            moved = abs(used - was) >= max(2000, was * .01)
        moved = moved or (used is not None and partial != getattr(self, 'part_sent', False))
        if not (moved or (top and top != getattr(self, 'top_sent', None)) or (ref and ref != getattr(self, 'ref_sent', None))):
            return
        if moved:
            self.fuel_sent, self.part_sent = used, partial
        else:                                         # a window or reference sent before any count: the count stays as it was (none)
            self.fuel_sent, self.part_sent = was, getattr(self, 'part_sent', False)
        if top:
            self.top_sent = top
        if ref:
            self.ref_sent = ref
        a = AGENTS.setdefault(self.aid, {})
        if self.fuel_sent is None:
            a.pop('ctx', None)
            a.pop('ctxpart', None)
        else:
            a['ctx'] = self.fuel_sent
            if self.part_sent:
                a['ctxpart'] = True
            else:
                a.pop('ctxpart', None)
        if top:
            a['ctxmax'] = top
        if ref:
            a['ctxref'] = ref
        # ctx is always present here: a number, or null to withdraw one; ctxpart likewise, true or false
        emit(self.aid, 'agent', old, **dict(a, ctx=a.get('ctx'), ctxpart=bool(a.get('ctxpart'))))

    def backfill(self, needle, exclude=b'\x00', n=30, span=12_000_000):
        """Only the tail of a long log is replayed, which can start after your last message. Look further back
        for your recent messages so the desk can show what the agent was asked. Many candidate records turn out
        to be machine-written, so a generous number are examined; only real messages produce anything."""
        if not self.off:
            return
        try:
            start = max(0, self.off - span)
            with open(self.path, 'rb') as f:
                f.seek(start)
                chunk = f.read(self.off - start)
            hits = [l for l in chunk.split(b'\n')[1 if start else 0:] if needle in l and exclude not in l]
            self.backfilling = True
            for raw in hits[-n:]:
                try:
                    self.handle(json.loads(raw), True)
                except Exception:
                    pass
        except OSError:
            pass
        finally:
            self.backfilling = False

    def launch_lines(self, raw_lines):
        """Note any agent launches in these records (without showing them), as backfill_launches does. Only a record
        of a command being run counts: a Claude Bash call, or a Codex CommandExecution item. A command that merely
        appears in the text of a Codex script step is not one; the script may never have run that line, and a result
        arriving for the step says nothing about that line. A record of an unexpected shape is skipped, never fatal.
        Returns how many launches these records held, already known or not."""
        n = 0
        for raw in raw_lines:
            try:
                d = json.loads(raw)
            except Exception:
                continue                            # a fragment at a slice edge, or not a record
            try:
                if not isinstance(d, dict):
                    continue
                ts = ts_of(d)
                for b in tool_uses(d):
                    if b.get('name') == 'Bash':
                        n += note_launch(self.aid, shown(as_dict(b.get('input')).get('command')), ts)
                pl = d.get('payload') if isinstance(d.get('payload'), dict) else {}
                it = pl.get('item') if isinstance(pl.get('item'), dict) else {}
                if it.get('type') == 'CommandExecution':
                    cmd = it.get('command') or []
                    dur = it.get('duration') if isinstance(it.get('duration'), dict) else {}
                    secs = dur.get('secs') if isinstance(dur.get('secs'), (int, float)) else 0
                    n += note_launch(self.aid, str(cmd[-1]) if isinstance(cmd, list) and cmd else str(cmd), ts - secs)
            except Exception as e:
                print('skip search record:', type(e).__name__, e)
        return n

    @staticmethod
    def index_result(raw, state):
        """A Claude tool_result record, parsed: each tool call it answers is filed as succeeded or failed, by call id.
        Only the record's own blocks count; the same text quoted inside some other record's content is not a result.
        Newest records are filed first, so the id lists are trimmed from their oldest-filed (newest-in-log) end when
        they grow too large: a result dropped that way means a write is not known to have succeeded, nothing more."""
        try:
            r = json.loads(raw)
        except Exception:
            return
        if not isinstance(r, dict) or r.get('type') != 'user':
            return
        m = r.get('message')
        c = m.get('content') if isinstance(m, dict) else None
        for rb in c if isinstance(c, list) else []:
            if isinstance(rb, dict) and rb.get('type') == 'tool_result' and isinstance(rb.get('tool_use_id'), str):
                state['bad' if rb.get('is_error') else 'ok'][rb['tool_use_id']] = 1
        for k in ('ok', 'bad'):
            while len(state[k]) > 20_000:
                state[k].pop(next(iter(state[k])))

    def hunt(self, key, before, state=None, budget=32_000_000, parse=6_000_000, limit=512_000_000):
        """Look back through this log, newest first, for a launch whose command carries `key` (a child's first
        message, which began at `before`), or failing that for a record that wrote `key` into a file (see wrote).
        Big logs are read in slices: each call reads at most `budget` bytes and parses at most `parse` bytes of
        records (the tool results it indexes and the lines that carry `key`; every other line costs substring
        scans only), so polling is never held up for long. Records are walked newest first, so by the time a call
        is reached every result newer than it has been filed in `state`, which is what the next call resumes from:
          pos    where to resume: a record boundary, the start of the oldest record already walked
          floor  how far back to go (0: the start of the log; otherwise the boundary an earlier search had reached)
          top    the end of the last complete record when the search began (a record still being written is left
                 for a later search); the watcher keeps it as how much of the log this search covered
          ok/bad the ids of tool calls whose results have been seen, succeeded and failed, carried across slices
          open   writes of `key` found before `before` whose result had not been seen (call id -> (time, file)):
                 carried across slices and, by the watcher, across a completed search, so that a result appended
                 after the search ended still resolves the write it answers (a result is indexed the moment it is
                 read; the write it answers may have been walked in an earlier search)
          done   whether the search has ended: at the first hit, at the floor, or `limit` bytes back
        The answer is (what was found, the state). The log being unreadable raises OSError: that is a failure to
        report and retry, not a search that found nothing. A record longer than MAX_REST is dropped rather than
        carried along."""
        needle = key.encode()
        size = os.path.getsize(self.path)
        if not isinstance(state, dict):
            state = {'pos': size, 'floor': 0, 'top': size, 'ok': {}, 'bad': {}, 'open': {}, 'done': False}
        state.setdefault('open', {})
        pos, floor = min(state['pos'], size), max(0, min(state.get('floor', 0), size))
        read, parsed, found, carry, fresh = 0, 0, None, b'', state['pos'] >= size
        with open(self.path, 'rb') as f:
            while pos > floor and size - pos < limit and read < budget and parsed < parse and not found:
                start = max(floor, pos - min(8_000_000, budget))
                f.seek(start)
                chunk = f.read(pos - start)
                read += len(chunk)
                lines = chunk.split(b'\n')
                if fresh:                             # the first slice of a search from the end of the log: whatever follows the
                    fresh = False                     # last newline is a record still being written, and is left alone
                    pos -= len(lines[-1])
                    lines[-1] = b''
                    state['top'] = pos
                end = pos                             # the boundary after the newest record in this slice
                if carry:                             # the newer slice began inside a record; this slice's last piece completes it
                    lines[-1] += carry
                    lines.append(b'')
                    end += len(carry) + 1
                carry = lines[0] if start > floor else b''     # at the floor (a boundary) or the start of the log, no record straddles
                body = lines[1:] if start > floor else lines
                if len(carry) > MAX_REST:
                    carry = b''
                # walk this slice newest first, tracking each record's start so a budget that runs out can resume exactly there;
                # the last piece of `body` is always empty (see above) and is the only one not followed by a newline
                stopped, off = False, end
                for i in range(len(body) - 1, -1, -1):
                    l = body[i]
                    end = off
                    off = end - len(l) - (1 if i < len(body) - 1 else 0)
                    if parsed >= parse:               # out of parsing budget: resume at this record next call
                        pos, carry, stopped = end, b'', True
                        break
                    if b'"tool_result"' in l:
                        parsed += len(l)
                        self.index_result(l, state)
                        if any(c in state['ok'] and c not in state['bad'] for c in state['open']):
                            found = 'wrote'           # the result answers a matching write walked earlier (this search or a completed one)
                            break
                    if needle not in l:
                        continue
                    parsed += len(l)
                    if (b'codex exec' in l or b'claude -p' in l or b'claude --print' in l) and self.launch_lines([l]):
                        found = 'launch'
                        break
                    # no launch, but this agent WROTE the text into a file before the run began: weaker evidence that
                    # the run was started on its behalf by something else, such as a scheduled script
                    if self.wrote(l, key, before, state):
                        found = 'wrote'
                        break
                if not stopped:
                    pos = start                       # the whole slice walked; a straddling record is finished by the next slice
            if carry and not found:                   # the call ended with a record half read: resume just after it instead
                pos += len(carry) + 1
        state['pos'] = pos
        state['done'] = bool(found or pos <= floor or size - pos >= limit)
        return found, state

    def wrote(self, raw, key, before, state):
        """Did this record write `key` into a file (Write/Edit/MultiEdit, in the NEW text), before `before`, with a
        result seen saying the write succeeded (a tool_result for that call without is_error, filed in `state` by
        hunt from the records newer than this one)? A write whose result was never seen, or failed, does not count;
        nor does printing the text, or having it in the text being replaced. A matching write whose result has not
        been seen is remembered in state['open'] (newest 200), so a result read later can still resolve it."""
        try:
            d = json.loads(raw)
            if not isinstance(d, dict) or ts_of(d) >= before:
                return False
            for b in tool_uses(d):
                i = b.get('input') or {}
                if b.get('name') == 'Write':
                    new = [i.get('content')]
                elif b.get('name') in ('Edit', 'MultiEdit'):
                    new = [e.get('new_string') for e in (i.get('edits') if isinstance(i.get('edits'), list) else [i]) if isinstance(e, dict)]
                else:
                    continue
                if not any(isinstance(s, str) and key in s for s in new):
                    continue
                cid = b.get('id')
                if not isinstance(cid, str) or cid in state['bad']:
                    continue
                if cid in state['ok']:
                    return True
                if cid not in state['open'] and len(state['open']) < 200:
                    state['open'][cid] = (ts_of(d), text_of(i.get('file_path'), 400))
        except Exception as e:
            print('skip search record:', type(e).__name__, e)
        return False

    def backfill_launches(self):
        """Commands that started other agents may be further back than the replayed tail. Note them (without
        showing them) so an agent launched from a command line keeps its parent after this server restarts."""
        if not self.off:
            return
        try:
            start = max(0, self.off - 12_000_000)
            with open(self.path, 'rb') as f:
                f.seek(start)
                chunk = f.read(self.off - start)
            self.launch_lines([l for l in chunk.split(b'\n')[1 if start else 0:] if b'codex exec' in l or b'claude -p' in l or b'claude --print' in l][-40:])
        except OSError:
            pass

    def poll(self):
        st = os.stat(self.path)             # a vanished or unreadable log is reported by the watcher, not hidden
        if st.st_ino != self.ino or st.st_size < self.off:   # a different file, or cut short in place: start a clean reader
            self.replaced = True
            return
        if st.st_size == self.off:
            self.first = False
            return
        with open(self.path, 'rb') as f:
            f.seek(self.off)
            data = f.read(min(st.st_size - self.off, MAX_POLL))
        self.off += len(data)                                   # advance by what was really read
        if self.drop_long:                                      # still inside one absurdly long record: skip to its end
            cut = data.find(b'\n')
            if cut < 0:
                return
            data, self.drop_long, self.skip_partial = data[cut + 1:], False, False   # the fragment being skipped ended here
        lines = (self.rest + data).split(b'\n')
        self.rest = lines.pop()
        if self.skip_partial and lines:
            lines, self.skip_partial = lines[1:], False
        if len(self.rest) > MAX_REST:                           # the complete records before it are kept
            self.rest, self.drop_long = b'', True
        if self.partial and getattr(self, 'aid', None) and not getattr(self, 'skip', False):
            self.partial = False
            describe(self.aid, True, partial=True)
        for raw in lines:
            if not raw.strip():
                continue
            try:
                d = json.loads(raw)
            except Exception:
                continue
            try:
                self.handle(d, self.first)
            except Exception as e:
                print('skip record:', type(e).__name__, e)
        if self.off >= st.st_size:
            self.first = False


SHOT_TOOL = re.compile(r'Browser|chrome|screenshot|computer|Simulator', re.I)


class ClaudeTail(Tail):
    def __init__(self, path):
        stem = os.path.basename(path)[:-6]
        self.sub = os.path.basename(os.path.dirname(path)) == 'subagents'
        if self.sub:
            self.aid = 'c:' + stem.replace('agent-', '')
            parent = 'c:' + os.path.basename(os.path.dirname(os.path.dirname(path)))
            meta = {}
            try:
                meta = as_dict(json.load(open(path[:-6] + '.meta.json')))
            except Exception:
                pass
            describe(self.aid, True, runtime='Claude', parent=parent, sub=True,
                     name=text_of(meta.get('description')) or 'Sub-agent', task=text_of(meta.get('agentType')),
                     spawn=text_of(meta.get('toolUseId')))
        else:
            self.aid = 'c:' + stem
            describe(self.aid, True, runtime='Claude', name='Claude ' + stem[:4])
        self.seen_prompt = False
        super().__init__(path)
        if not self.sub:
            self.backfill(b'"subtype":"compact_boundary"', n=1)       # the size it last summarised itself at, for the context gauge
            self.backfill(b'"type":"user"', b'tool_result')
            self.backfill_work()
        self.backfill_launches()

    def backfill_work(self):
        """Which folders this session has been working in, from a few megabytes before the replayed tail. Only the
        folders it mentions most are kept, so a path that merely appears once in some output does not count. Each
        mention is dated by its own record; a folder counts from its fifth mention, never from an unrelated record."""
        import collections
        try:
            start = max(0, self.off - 4_000_000)
            with open(self.path, 'rb') as f:
                f.seek(start)
                text = f.read(self.off - start).decode('utf-8', 'replace')
            seen = collections.defaultdict(list)
            for line in text.split('\n'):
                m = re.search(r'"timestamp":"([^"]+)"', line)
                try:
                    ts = datetime.fromisoformat(m.group(1).replace('Z', '+00:00')).timestamp()
                except Exception:
                    continue                                    # an undated record is not evidence of when
                for path in PATH_RX.findall(line):
                    for k in folder_keys(path.replace('\\/', '/')):
                        seen[k].append(ts)
            for k, v in sorted(seen.items(), key=lambda kv: -len(kv[1]))[:25]:
                if len(v) >= 5:
                    v.sort()
                    note_work(k, self.aid, v[4], v[-1])
        except OSError:
            pass

    def handle(self, d, old):
        t, ts, aid, msg = d.get('type'), ts_of(d), self.aid, as_dict(d.get('message'))
        if text_of(d.get('cwd')):                     # metadata reaches the page only as strings; anything else is dropped here
            describe(aid, old, cwd=text_of(d.get('cwd')), branch=text_of(d.get('gitBranch')))
        if t == 'assistant' and text_of(msg.get('model')):
            describe(aid, old, model=text_of(msg.get('model')), effort=text_of(d.get('effort')))
        if t == 'custom-title' and not self.sub:
            describe(aid, old, name=text_of(d.get('customTitle')))
        elif t == 'queue-operation':
            op = d.get('operation')
            if op == 'enqueue':
                emit(aid, 'queued', old, ts, text=flat(d.get('content')))
            elif op in ('dequeue', 'remove', 'popAll'):
                emit(aid, 'dequeued', old, ts, op=op, text=flat(d.get('content')))
        elif t == 'system' and d.get('subtype') == 'turn_duration':
            emit(aid, 'turn_end', old, ts)
        elif t == 'system' and d.get('subtype') == 'stop_hook_summary' and not d.get('preventedContinuation'):
            emit(aid, 'turn_end', old, ts)
        elif t == 'system' and d.get('subtype') == 'compact_boundary':
            # the session was summarised and continues with a shorter context; the log records the sizes
            m = d.get('compactMetadata') if isinstance(d.get('compactMetadata'), dict) else {}
            pre, post = count(m.get('preTokens')), count(m.get('postTokens'))      # None when not stated: never a stand-in zero
            if not self.backfilling:                # found further back only to learn the size; its place in the log is not shown
                emit(aid, 'compact', old, ts, pre=pre, post=post, trigger=m.get('trigger'))
            self.fuel(old, post, ref=pre if m.get('trigger') == 'auto' else None)
        elif t == 'user' and d.get('isCompactSummary'):
            pass                                    # the summary Claude wrote for itself; not something you said
        elif t == 'user' and not d.get('isMeta'):
            c = msg.get('content')
            blocks = c if isinstance(c, list) else [{'type': 'text', 'text': c or ''}]
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                if b.get('type') == 'tool_result':
                    self.result(d, b, old, ts)
                elif b.get('type') == 'text' and shown(b.get('text')).strip():
                    if self.sub and not self.seen_prompt:
                        self.seen_prompt = True
                        emit(aid, 'assign', old, ts, text=shown(b.get('text')).strip())
                        set_goal(aid, shown(b.get('text')).strip(), ts, self.backfilling)
                        continue
                    txt = clean_user(shown(b.get('text')))
                    if txt:
                        if not self.sub:
                            remember_prompt(aid, txt, ts, self.backfilling)
                        emit(aid, 'user', old, ts, text=txt)
            if d.get('toolEndsTurn') is True:       # the log marks this tool result as the end of the turn (a sub-agent handing back its report)
                emit(aid, 'turn_end', old, ts, how='handed back its report' if self.sub else '')
        elif t == 'assistant':
            u = msg.get('usage')
            if isinstance(u, dict):                 # what was sent to the model for this reply, plus the reply: the context in use
                names = ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens', 'output_tokens')
                parts = [count(u[k]) for k in names if k in u]
                # a component the record states but not as a number makes the whole figure unknown, as does a usage
                # block that states none of them; a missing component is not a zero, so the ones stated add up to a
                # figure that is marked partial: a lower bound on the context, not the context
                self.fuel(old, None if not parts or any(x is None for x in parts) else sum(parts), partial=len(parts) < len(names))
            for b in msg.get('content') if isinstance(msg.get('content'), list) else []:
                if not isinstance(b, dict):
                    continue
                if b.get('type') == 'text' and shown(b.get('text')).strip():
                    emit(aid, 'say', old, ts, text=shown(b.get('text')).strip())
                elif b.get('type') == 'tool_use':
                    self.tool(b, old, ts)

    def tool(self, b, old, ts):
        # what the log holds in each text-shaped place is taken as text whatever its shape (see shown), so a value
        # that is not a string neither stops this record nor reaches the page as something the page cannot show
        n, i, cid, aid = shown(b.get('name')), as_dict(b.get('input')), b.get('id'), self.aid
        path = shown(i.get('file_path') or i.get('notebook_path'))
        self.calls[cid] = (n, path, i)
        if not self.sub:
            cds = ' '.join(m.group(2) for g in segments(shown(i.get('command'))) for m in [re.match(r'cd\s+(["\']?)(/Users/[^"\']+)\1\s*$', g)] if m)
            worked(aid, path + ' ' + shown(i.get('path')) + ' ' + cds, ts)
        if len(self.calls) > 300:
            self.calls.pop(next(iter(self.calls)))
        if n == 'Read':
            emit(aid, 'read', old, ts, id=cid, path=path)
        elif n == 'Write':
            emit(aid, 'write', old, ts, id=cid, path=path, content=i.get('content'))
        elif n in ('Edit', 'MultiEdit', 'NotebookEdit'):
            edits = [e for e in (i.get('edits') if isinstance(i.get('edits'), list) else [i]) if isinstance(e, dict)]
            emit(aid, 'edit', old, ts, id=cid, path=path,
                 before='\n⋯\n'.join(shown(e.get('old_string')) for e in edits),
                 after='\n⋯\n'.join(shown(e.get('new_string', e.get('new_source'))) for e in edits))
        elif n == 'Bash':
            cmd = shown(i.get('command'))
            emit(aid, 'run', old, ts, id=cid, cmd=cmd, why=i.get('description'), alert=risk(cmd), check=is_check(cmd))
            note_launch(aid, cmd, ts)
        elif n in ('Grep', 'Glob'):
            emit(aid, 'search', old, ts, id=cid, q=i.get('pattern'), path=i.get('path'))
        elif n in ('WebSearch', 'WebFetch'):
            emit(aid, 'web', old, ts, id=cid, q=i.get('query') or i.get('url'))
        elif n in ('Agent', 'Task'):
            emit(aid, 'spawn', old, ts, id=cid, name=i.get('description'), text=i.get('prompt'))
        elif n in ('TaskCreate', 'TaskUpdate'):
            pass                                    # shown once the result says it worked
        elif n == 'AskUserQuestion':
            qs = i.get('questions') if isinstance(i.get('questions'), list) else [{}]
            emit(aid, 'ask', old, ts, id=cid, text='\n'.join(shown(as_dict(q).get('question')) for q in qs))
        elif n == 'ExitPlanMode':
            emit(aid, 'ask', old, ts, id=cid, text='Wants you to approve its plan before it continues.')
        else:
            label = ''
            if n.startswith('mcp__') and TOOL_RISK.search(n.split('__')[-1]):
                label = 'send'
            if n == 'Artifact' and i.get('action', 'publish') in ('publish', 'delete'):
                label = 'publish'
            emit(aid, 'tool', old, ts, id=cid, name=n, text=json.dumps(i, ensure_ascii=False)[:1500], alert=label)

    def result(self, d, b, old, ts):
        cid, failed = b.get('tool_use_id'), bool(b.get('is_error'))
        n, path, i = self.calls.pop(cid, ('', '', {}))
        tur = d.get('toolUseResult')
        text = flat(b.get('content'))
        if n == 'Read' and isinstance(tur, dict) and isinstance(tur.get('file'), dict):
            f = tur['file']
            text = shown(f.get('content', text)) if tur.get('type') == 'text' else '[%s file]' % (shown(tur.get('type')) or 'binary')
        if n == 'TaskCreate':
            if not failed:
                m = re.search(r'#(\d+)', text)
                emit(self.aid, 'task', old, ts, num=m.group(1) if m else shown(cid), text=i.get('subject'))
            return
        if n == 'TaskUpdate':
            if not failed:
                emit(self.aid, 'task_upd', old, ts, num=shown(i.get('taskId')), status=i.get('status'), text=i.get('subject'))
            return
        emit(self.aid, 'result', old, ts, id=cid, text=text, error=failed)
        if failed:
            return
        if SHOT_TOOL.search(n):                     # only tools that return pictures; a path in ordinary output proves nothing
            for m in re.finditer(r'\[Image: source: ([^\]]+)\]', text):
                saw(self.aid, old, ts, m.group(1).strip(), 'Image returned by its ' + ('browser' if re.search('Browser|chrome', n, re.I) else 'screen') + ' tool')
        if n == 'Read' and path.lower().endswith(IMG_EXT):
            saw(self.aid, old, ts, path, os.path.basename(path))
        if n in ('Write', 'Edit', 'MultiEdit'):
            allow_preview(self.aid, old, ts, cid, path)


TITLES, TITLES_MTIME = {}, 0


def load_titles():
    """Codex keeps session titles in ~/.codex/session_index.jsonl, not in the session log. Latest entry wins."""
    global TITLES_MTIME
    p = HOME + '/.codex/session_index.jsonl'
    try:
        m = os.path.getmtime(p)
        if m == TITLES_MTIME:
            return
        TITLES_MTIME = m
        for line in open(p):
            try:
                d = json.loads(line)
                if text_of(d.get('id')) and text_of(d.get('thread_name')):
                    TITLES[d['id']] = text_of(d['thread_name'])
            except Exception:
                pass
    except OSError:
        return
    for tid, title in TITLES.items():
        a = AGENTS.get('x:' + tid)
        if a and not a.get('nick'):
            describe('x:' + tid, name=title)


def desktop_files():
    return glob.glob(HOME + '/Library/Application Support/Claude/claude-code-sessions/*/*/local_*.json')


def desktop_scan():
    """Claude Desktop's own record of a session: its permission mode, when it was created, when you last focused it.
    The model is NOT taken from here; the log's own record of the model is newer and wins."""
    for p in desktop_files():
        try:
            if time.time() - os.path.getmtime(p) > WINDOW * 12:
                continue
            d = json.load(open(p))
            aid = 'c:' + str(d.get('cliSessionId'))
            if aid in AGENTS:
                describe(aid, perm=('Claude mode: ' + text_of(d.get('permissionMode'))) if text_of(d.get('permissionMode')) else '',
                         started=int(d.get('createdAt') or 0) / 1000 or None, looked=int(d.get('lastFocusedAt') or 0) / 1000 or None)
        except Exception:
            pass


def claude_desktop_id(cli_id):
    for p in desktop_files():
        try:
            with open(p) as f:
                head = f.read(600)
            if cli_id in head:
                return json.loads(open(p).read()).get('sessionId') or os.path.basename(p)[:-5]
        except Exception:
            pass
    return ''


CMD_ANY = re.compile(r'''["']?cmd["']?\s*:\s*(["'`])((?:\\.|(?!\1).)*)\1''', re.S)


class CodexTail(Tail):
    def __init__(self, path):
        m = re.search(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$', path)
        self.aid = 'x:' + (m.group(1) if m else os.path.basename(path)[:-6])
        self.skip, self.agent_path, self.parent, self.sticky, self.meta_done, self.beat = False, '', '', set(), False, 0
        self.cells, self.waits = {}, {}     # a step still running: its cell -> the call that began it; a wait -> the cell it watches
        try:
            with open(path, 'rb') as f:
                first = f.readline()
            if first.endswith(b'\n'):
                d = json.loads(first)
                if d.get('type') == 'session_meta':
                    self.meta(as_dict(d.get('payload')))
        except Exception:
            pass
        super().__init__(path)
        if self.off and not self.skip:          # model and approval settings are written once, near the start
            try:
                with open(path, 'rb') as f:
                    head = f.read(400_000)
                for raw in [l for l in head.split(b'\n')[:-1] if b'thread_settings_applied' in l][-1:]:
                    self.handle(json.loads(raw), True)
            except Exception:
                pass
        if not self.skip:
            self.backfill(b'"UserMessage"')
            self.backfill_launches()

    def meta(self, m):
        """Identity comes from the session_meta record. It is applied whenever that record is seen, so a file
        discovered before its first line was complete is still named and placed correctly afterwards."""
        if self.meta_done:
            return
        self.meta_done = True
        if text_of(m.get('id')):                    # identity fields are taken only as strings (see text_of)
            self.aid = 'x:' + text_of(m.get('id'))
        self.skip = m.get('thread_source') == 'guardian_review'
        self.agent_path = text_of(m.get('agent_path'))
        p0 = text_of(m.get('parent_thread_id'))
        self.parent = ('x:' + p0) if p0 and p0 != text_of(m.get('id')) else ''
        if self.skip:
            return
        self.exec = m.get('originator') == 'codex_exec' or m.get('source') == 'exec'
        git = m.get('git') if isinstance(m.get('git'), dict) else {}
        try:
            started = datetime.fromisoformat(m.get('timestamp', '').replace('Z', '+00:00')).timestamp()
        except Exception:
            started = None
        describe(self.aid, True, runtime='Codex', cwd=text_of(m.get('cwd')), branch=text_of(git.get('branch')), started=started,
                 name=text_of(m.get('agent_nickname')) or TITLES.get(text_of(m.get('id'))) or 'Codex ' + self.aid[-4:],
                 nick=text_of(m.get('agent_nickname')),
                 task=text_of(m.get('agent_path')).split('/')[-1].replace('_', ' '), parent=self.parent)

    def call(self, p, old, ts):
        """A step Codex has just issued. What it contains is read out of the script text, so it is marked as a guess;
        the structured records that follow are the authority, and alerts are raised from those, not from this."""
        aid, cid, name = self.aid, p.get('call_id'), shown(p.get('name'))
        src = shown(p.get('input') or p.get('arguments'))     # the script or argument text; a structured value is taken as its JSON
        try:
            args = json.loads(src) if p.get('type') == 'function_call' else {}
        except Exception:
            args = {}
        if not isinstance(args, dict):
            args = {}
        secs = lambda k: int((count(args.get(k)) or 0) / 1000)      # a stated number of milliseconds, else 0
        if name == 'send_message':
            return
        if name == 'request_user_input_async':
            qs = args.get('questions') if isinstance(args.get('questions'), list) else [{}]
            text = '\n'.join(shown(q.get('title')) + ('  [' + ' / '.join(map(shown, q.get('options'))) + ']' if isinstance(q.get('options'), list) and q.get('options') else '') for q in qs if isinstance(q, dict))
            self.sticky.add(cid)
            emit(aid, 'waiting', old, ts, why='question', sticky=True, qid=cid, text=text or 'Asked you a question')
        elif name == 'spawn_agent':
            emit(aid, 'spawn', old, ts, id=cid, name=(shown(args.get('task_name')) or 'a new agent').replace('_', ' '),
                 text='(Codex encrypts the assignment text in its log, so it cannot be shown here.)')
        elif name == 'followup_task':
            emit(aid, 'spawn', old, ts, id=cid, name='follow-up for ' + shown(args.get('target')).split('/')[-1].replace('_', ' '),
                 text='(Codex encrypts the message text in its log, so it cannot be shown here.)')
        elif name == 'wait' and args.get('cell_id') is not None:      # waiting on one of its own running steps, not on agents
            self.waits[cid] = shown(args['cell_id'])
            while len(self.waits) > 50:
                self.waits.pop(next(iter(self.waits)))
            emit(aid, 'tool', old, ts, id=cid, name='Waiting for a running step', doing='waiting for a running step',
                 text='Will wait up to %ss for step %s to finish.' % (secs('yield_time_ms'), self.waits[cid]))
        elif name in ('wait_agent', 'wait'):
            emit(aid, 'tool', old, ts, id=cid, name='Waiting for its agents', doing='waiting for its agents',
                 text='Will wait up to %ss for a report.' % secs('timeout_ms'))
        elif name == 'sleep':
            emit(aid, 'tool', old, ts, id=cid, name='Pausing', doing='pausing', text='Pausing for %ss.' % secs('duration_ms'))
        elif name == 'interrupt_agent':
            emit(aid, 'tool', old, ts, id=cid, name='Interrupted ' + shown(args.get('target')).replace('_', ' '), text='')
        elif name == 'exec':
            cmds = [m.group(2).replace('\\n', '\n').replace('\\"', '"').replace("\\'", "'").replace('\\\\', '\\') for m in CMD_ANY.finditer(src)] if 'exec_command' in src else []
            if re.search(r'tools\.apply_patch\s*\(', src) and '*** Begin Patch' in src:
                emit(aid, 'tool', old, ts, id=cid, guess=True, name='Codex issued a step that edits files', doing='editing', text='The exact change appears when it completes.')
            elif cmds:
                # Shown as a guess, and nothing more: a command-line agent named in a script's text is not a launch, not
                # even once the step's result arrives (the script may never run that line). Only a CommandExecution
                # item, Codex's own record of a command being run, can establish one (see handle, launch_lines).
                emit(aid, 'run', old, ts, id=cid, guess=True, cmd='\n'.join(cmds), why='')
                for c in cmds:
                    note_script_launch(aid, c, ts)       # a labelled guess for a session that starts right after, never a link
            elif 'web__run' in src:
                emit(aid, 'tool', old, ts, id=cid, guess=True, name='Codex issued a web step', doing='on the web', text='Search terms appear when it finishes.')
            elif 'view_image' in src:
                emit(aid, 'tool', old, ts, id=cid, guess=True, name='Codex issued a step that opens an image', doing='looking at an image', text='')
            else:
                emit(aid, 'tool', old, ts, id=cid, guess=True, name='Codex is running a step', text='')
        elif name == 'js':
            emit(aid, 'tool', old, ts, id=cid, name=shown(args.get('title')) or 'Browser step', doing='using the browser', text=args.get('code'))
        else:
            emit(aid, 'tool', old, ts, id=cid, name=name, text=src[:1500], alert='send' if TOOL_RISK.search(name) else '')

    def handle(self, d, old):
        t, p, ts = d.get('type'), as_dict(d.get('payload')), ts_of(d)
        if t == 'session_meta':
            self.meta(p)
            return
        if self.skip:
            return
        aid, pt = self.aid, p.get('type')
        if t == 'response_item' and pt in ('custom_tool_call', 'function_call'):
            self.call(p, old, ts)
        elif t == 'response_item' and pt in ('custom_tool_call_output', 'function_call_output'):
            cid = p.get('call_id')
            text = re.sub(r'^Script completed\nWall time [^\n]*\nOutput:\n', '', flat(p.get('output')))
            if cid in self.sticky:
                self.sticky.discard(cid)
                if '"accepted":true' not in text.replace(' ', ''):      # the question was never put to you
                    emit(aid, 'unask', old, ts, qid=cid)
                return
            cell = self.waits.pop(cid, '')
            m = re.match(r'\s*Script running with cell ID (\d+)', flat(p.get('output')))
            if m:                                        # Codex's own envelope for "still running": the step is not finished
                if cell:                                 # but a wait that came back has; the step it watched stays open
                    emit(aid, 'result', old, ts, id=cid, text='The step is still running.', unknown=True)
                else:
                    self.cells[m.group(1)] = cid
                    while len(self.cells) > 50:
                        self.cells.pop(next(iter(self.cells)))
                return
            # Codex does not say here whether the step succeeded; the page shows it as "returned", not "done".
            emit(aid, 'result', old, ts, id=cid, text=text, unknown=True)
            if cell in self.cells:                       # the wait saw the step finish: close the call that began it
                emit(aid, 'result', old, ts, id=self.cells.pop(cell), text=text, unknown=True)
        elif t == 'event_msg' and pt == 'item_completed':
            it = as_dict(p.get('item'))
            kind = it.get('type')
            texts = lambda: flat([{'text': c.get('text', '')} for c in (it.get('content') if isinstance(it.get('content'), list) else []) if isinstance(c, dict)])
            if kind == 'AgentMessage':
                txt = texts().strip()
                if txt:
                    emit(aid, 'say', old, ts, text=txt)
            elif kind == 'ImageView':
                saw(aid, old, ts, shown(it.get('path')), 'Image it opened')
            elif kind == 'UserMessage':
                txt = clean_user(texts())
                if txt:
                    first = aid not in FIRST_PROMPT
                    remember_prompt(aid, txt, ts, self.backfilling) if getattr(self, 'exec', False) or self.parent else (FIRST_PROMPT.setdefault(aid, (txt, ts)), set_goal(aid, txt, ts, self.backfilling))
                    if first and getattr(self, 'exec', False) and not AGENTS.get(aid, {}).get('parent'):
                        EXEC_KIDS[aid] = None          # matched now if possible, and retried as other logs are read
                        match_work(aid, txt, ts)
                    emit(aid, 'user', old, ts, text=txt)
            elif kind == 'CommandExecution':
                cmd = it.get('command') or []
                cmd = shown(cmd[-1] if isinstance(cmd, list) and cmd else cmd)
                parsed = it.get('parsed_cmd') if isinstance(it.get('parsed_cmd'), list) else []
                out = it.get('aggregated_output') or it.get('stdout')
                raw_code = it.get('exit_code')               # log data: an integer, or nothing. Anything else is not a code and is not passed on
                code = raw_code if isinstance(raw_code, int) and not isinstance(raw_code, bool) else None
                err = (code != 0) if code is not None else None          # None: the log did not record an outcome, which is not success
                if parsed and all(isinstance(x, dict) and x.get('type') == 'read' for x in parsed):
                    emit(aid, 'read', old, ts, id=it.get('id'), path=', '.join(shown(x.get('path') or x.get('name')) for x in parsed),
                         done=True, out=out, err=err, unknown=code is None)
                else:
                    emit(aid, 'run', old, ts, id=it.get('id'), cmd=cmd, why='', done=True, out=out, err=err, unknown=code is None,
                         code=code, alert=risk(cmd), check=is_check(cmd))
                    dur = it.get('duration') if isinstance(it.get('duration'), dict) else {}
                    note_launch(aid, cmd, ts - float(count(dur.get('secs')) or 0))
            elif kind == 'FileChange':
                for path, ch in as_dict(it.get('changes')).items():
                    ch = as_dict(ch)
                    op = shown(ch.get('type'))
                    body = ch.get('unified_diff') if op == 'update' else ''.join('%s%s\n' % ('-' if op == 'delete' else '+', l) for l in shown(ch.get('content')).split('\n'))
                    ok = it.get('status') == 'completed'
                    emit(aid, 'patch', old, ts, id=it.get('id'), path=path, op=op, text=body, done=True, err=not ok)
                    if ok and op != 'delete':
                        allow_preview(aid, old, ts, it.get('id'), path)
            elif kind == 'McpToolCall':
                res = as_dict(it.get('result'))
                err = it.get('error') or {}
                failed = it.get('status') != 'completed' or bool(res.get('isError')) or bool(err)
                out = flat(res.get('content')) or (shown(err.get('message')) if isinstance(err, dict) else shown(err))
                emit(aid, 'tool', old, ts, id=it.get('id'), name='%s · %s' % (shown(it.get('server')), shown(it.get('tool'))),
                     text=json.dumps(it.get('arguments'), ensure_ascii=False)[:1500], done=True, out=out, err=failed,
                     alert='send' if TOOL_RISK.search(shown(it.get('tool'))) else '')
            elif kind == 'Extension' and 'search' in str(it.get('kind')):
                emit(aid, 'web', old, ts, id=it.get('id'), q=it.get('query'), done=True,
                     out='\n'.join('%s — %s' % (shown(r.get('domain')), shown(r.get('snippet'))) for r in (it.get('results') if isinstance(it.get('results'), list) else [])[:8] if isinstance(r, dict)))
            elif kind == 'Reasoning':
                self.heartbeat(old, ts)
        elif t == 'event_msg' and pt == 'token_count':
            self.heartbeat(old, ts)
            info = p.get('info')
            if isinstance(info, dict):              # Codex sometimes writes info: null here, which states nothing and changes nothing;
                last = info.get('last_token_usage')  # a block that is present but does not state a numeric total withdraws the figure
                self.fuel(old, last.get('total_tokens') if isinstance(last, dict) else None, top=info.get('model_context_window'))
        elif t == 'event_msg' and pt == 'context_compacted':
            emit(aid, 'compact', old, ts, trigger='')       # Codex records that it happened, not the sizes
        elif t == 'event_msg' and pt == 'thread_settings_applied':
            s = as_dict(p.get('thread_settings'))
            bits = [b for b in ('approval policy: %s' % text_of(s.get('approval_policy')) if text_of(s.get('approval_policy')) else '',
                                'reviewer: %s' % text_of(s.get('approvals_reviewer')) if text_of(s.get('approvals_reviewer')) else '') if b]
            describe(aid, old, model=text_of(s.get('model')), perm='Codex ' + ', '.join(bits) if bits else '')
        elif t == 'event_msg' and pt == 'turn_aborted':
            emit(aid, 'turn_end', old, ts, how='turn stopped (%s)' % (shown(p.get('reason')) or 'aborted'))
        elif t == 'event_msg' and pt == 'task_started':
            emit(aid, 'turn_start', old, ts)
        elif t == 'event_msg' and pt == 'task_complete':
            emit(aid, 'turn_end', old, ts)
            if self.parent:
                emit(aid, 'link', old, ts, src=aid, dst=self.parent, label='finished a turn')
        elif t == 'response_item' and pt == 'agent_message':
            parent_path = self.agent_path.rsplit('/', 1)[0] if '/' in self.agent_path else ''
            if self.parent and p.get('recipient') == self.agent_path and p.get('author') == parent_path:
                emit(aid, 'link', old, ts, src=self.parent, dst=aid, label='message from its parent')

    def heartbeat(self, old, ts):
        """Reasoning and token records carry nothing to show, but they prove the agent is alive between steps."""
        if not old and ts - self.beat > 20:
            self.beat = ts
            emit(self.aid, 'beat', False, ts)


def drop_agent(aid, tails):
    if aid and not any(getattr(o, 'aid', None) == aid for o in tails.values()):
        AGENTS.pop(aid, None)
        FIRST_PROMPT.pop(aid, None)
        BACKFILLED.discard(aid)
        HOOKED.pop(aid, None)
        EXEC_KIDS.pop(aid, None)
        forget_hunt(aid)
        emit(aid, 'forget', False)


HOOKED = {}                  # agents known only from a hook, with no log being followed: agent -> when last heard from


def expire_hooked(tails, now):
    """A station made for a hook whose session log was never found has no log to go quiet, so it is dropped after
    the same silence that drops any other agent, unless one of its prompts is still being held."""
    followed = {getattr(o, 'aid', None) for o in tails.values()}
    for aid, t in list(HOOKED.items()):
        if aid in followed:
            HOOKED.pop(aid, None)                   # its log turned up: from here on it lives and dies with the log
        elif now - t > WINDOW * 6:
            with LOCK:
                held = any(p['aid'] == aid for p in PENDING.values())
            if held:
                HOOKED[aid] = now
            else:
                drop_agent(aid, tails)


def watch():
    tails, last_scan = {}, 0
    while True:
        now = time.time()
        if now - last_scan > 3:
            last_scan = now
            try:
                load_titles()
                desktop_scan()
                found = [(p, ClaudeTail) for p in glob.glob(HOME + '/.claude/projects/*/*.jsonl')
                         + glob.glob(HOME + '/.claude/projects/*/*/subagents/*.jsonl')]
                found += [(p, CodexTail) for p in glob.glob(HOME + '/.codex/sessions/*/*/*/*.jsonl')]
                bad = 0
                prune_keep(now)
                for p, cls in found:
                    try:
                        if p not in tails and (now - os.path.getmtime(p) < WINDOW or kept(p, now)):
                            tails[p] = cls(p)
                            HUNTED.clear()              # a new log is new ground: orphans may be searched for again
                    except Exception as e:
                        bad += 1
                        print('cannot follow', p, e)
                SCAN.update(ready=True, scanned=time.time(), err='%d log(s) could not be opened' % bad if bad else '')
            except Exception as e:
                SCAN.update(err='log discovery failed: %s' % type(e).__name__)
                print('scan failed', type(e).__name__, e)
        failed = 0
        for p, t in list(tails.items()):
            try:
                t.poll()
                if t.replaced:                              # same path, different file: start a clean reader
                    old_aid = getattr(t, 'aid', None)
                    tails[p] = type(t)(p)
                    if old_aid:                             # what was searched in the old file says nothing about the new one
                        forget_hunt(old_aid)
                    if getattr(tails[p], 'aid', None) != old_aid:
                        drop_agent(old_aid, tails)
                    continue
            except Exception as e:
                failed += 1
                print('poll failed', p, type(e).__name__, e)
            try:
                gone = now - os.path.getmtime(p) > WINDOW * 6 and not kept(p, now)
            except OSError:
                gone = True                                 # the log was deleted
            if gone:
                tails.pop(p, None)
                drop_agent(getattr(t, 'aid', None), tails)
        expire_hooked(tails, now)
        if SCAN['ready'] and now - LAST_PRUNE[0] > 3600:     # not before the logs have been found once: their agents are then seen again first
            LAST_PRUNE[0] = now
            prune_links(now)
        # An agent with no parent whose first message is long enough to be distinctive: look further back in the
        # other logs for the command that launched it. One log, one slice of it, per pass. What a slice costs is
        # bounded by Tail.hunt's budgets: at most 32 MB read and 6 MB of records parsed per call (the rest is
        # substring scanning), so the pause is bounded by that work, not by how many matching records the log
        # holds; it resumes where it stopped. A log that could not be read is tried again after a pause that grows
        # with each failure (HUNT_BACKOFF), is never recorded as searched, and counts as failing in health for as
        # long as that lasts. A log already searched is searched again, in its new part only, once it has grown;
        # matching writes found earlier whose results were still to come are carried into that search (HUNT_OPEN).
        try:
            for kid, (text, t0) in list(FIRST_PROMPT.items()):
                if kid in HUNTED or AGENTS.get(kid, {}).get('parent') or kid not in AGENTS:
                    continue
                key = re.split(r"['\"`\n]", text.strip())[0][:80]
                if len(key) < 30:
                    HUNTED.add(kid)
                    continue
                left = [t for t in tails.values() if getattr(t, 'aid', None) and t.aid != kid and not getattr(t, 'skip', False)
                        and not hunt_done((kid, t.aid), t.off, now)]
                if not left:                          # every log searched as it stands; looked at again as logs grow or appear
                    continue
                t = left[0]
                pair = (kid, t.aid)
                state = HUNT_POS.get(pair)
                if state is None and pair in HUNT_DONE:    # searched before and grown since: only the new part is read, with the
                    state = {'pos': t.off, 'floor': HUNT_DONE[pair], 'top': t.off, 'ok': {}, 'bad': {},       # writes still unanswered
                             'open': dict(HUNT_OPEN.get(pair, {})), 'done': False}
                try:
                    r, state = t.hunt(key, AGENTS.get(kid, {}).get('started') or t0, state)
                except OSError:
                    fails = HUNT_FAIL.get(pair, (0, 0))[0] + 1
                    HUNT_FAIL[pair] = (fails, now + HUNT_BACKOFF[min(fails, len(HUNT_BACKOFF)) - 1])
                    if isinstance(state, dict):
                        HUNT_POS[pair] = state       # resumed from the same place when the time comes
                    raise
                HUNT_FAIL.pop(pair, None)
                if state['done']:
                    HUNT_DONE[pair] = state['top']
                    HUNT_POS.pop(pair, None)
                    if state['open'] and r != 'wrote':
                        HUNT_OPEN[pair] = state['open']
                    else:
                        HUNT_OPEN.pop(pair, None)
                else:
                    HUNT_POS[pair] = state
                if r == 'launch':
                    match_launch(kid)
                    match_guess(kid)
                elif r == 'wrote':
                    WROTE.setdefault(kid, set()).add(t.aid)
                if (state['done'] and len(left) == 1 and not AGENTS.get(kid, {}).get('parent') and len(WROTE.get(kid, ())) == 1
                        and not any(k[0] == kid for k in HUNT_FAIL)):     # every log read: an unread one could hold another writer
                    w = next(iter(WROTE[kid]))        # exactly one agent wrote this run's prompt: shown as related, never as its launcher
                    if w in AGENTS and not is_ancestor(kid, w):
                        describe(kid, parent=w, soft=True, via='related, not launched by it: that agent wrote the prompt this run was given')
                break
        except Exception as e:
            failed += 1
            print('search failed', type(e).__name__, e)
        for kid in list(EXEC_KIDS):                 # the folder an agent works in may only become known after the run is seen
            if AGENTS.get(kid, {}).get('parent') or kid not in AGENTS:
                EXEC_KIDS.pop(kid, None)
            elif kid in FIRST_PROMPT:
                match_work(kid, *FIRST_PROMPT[kid])
        # logs that could not be read for a search stay counted as failing until a later try reads them
        SCAN.update(ts=time.time(), tails=len(tails), failing=failed + len({k[1] for k in HUNT_FAIL}))
        time.sleep(0.4)


# ---------------------------------------------------------------- HTTP
# The page's one script runs under a per-request nonce, so nothing that reaches the page as data (an inline handler in
# log text, say) can run even if some escape is missed. Styles stay inline: the page sets them from its own code.
UI_CSP = ("default-src 'self'; script-src 'nonce-%s'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
          "font-src 'self' data:; connect-src 'self'; frame-src 'self' about: blob: data:; object-src 'none'; base-uri 'self'; "
          "form-action 'none'; frame-ancestors 'none'")


def same_request(what, msg):
    """Is this notice about exactly the held request `what` (its command, file or URL)? The notice reads
    "Claude wants to run: <command>", so the request portion is what follows the first short "label:" prefix; a
    notice with no such prefix is compared whole. Both sides are compared whole and exactly (only the ends trimmed:
    a quoted argument's inner spaces are part of the request): equal, or not the same request. Containment is never
    enough."""
    norm = lambda s: str(s).strip()
    m = re.match(r'[^:\n]{1,80}:\s*(.*)\Z', msg, re.S)
    return bool(norm(what)) and norm(m.group(1) if m else msg) == norm(what)


def page_with_nonce():
    """index.html as on disk, its script tag carrying a fresh nonce, and the policy that admits only that nonce."""
    nonce = secrets.token_urlsafe(18)
    body = open(os.path.join(HERE, 'web', 'index.html'), 'rb').read()
    if body.count(b'<script>') != 1:
        raise RuntimeError('index.html must hold exactly one <script> tag')
    return body.replace(b'<script>', b'<script nonce="%s">' % nonce.encode(), 1), UI_CSP % nonce
ASSET_EXT = ('.css', '.js', '.mjs', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.ico', '.woff', '.woff2', '.ttf')
# A preview runs in an opaque-origin sandbox; only the page at this server's own origin may frame it (see H.preview).
PREVIEW_CSP = "sandbox allow-scripts; default-src 'self' data: blob: 'unsafe-inline'; frame-ancestors http://127.0.0.1:%d http://localhost:%d" % (PORT, PORT)


class H(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    timeout = 15              # applies from the moment a connection is accepted, so stalled headers cannot hold a thread

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype='text/plain; charset=utf-8', extra=()):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def host_ok(self):
        if self.headers.get('Host') not in ('127.0.0.1:%d' % PORT, 'localhost:%d' % PORT):
            return False
        origin = self.headers.get('Origin')
        return origin in (None, 'http://127.0.0.1:%d' % PORT, 'http://localhost:%d' % PORT)

    def keyed(self, given):
        return isinstance(given, str) and hmac.compare_digest(given.encode(), KEY.encode())

    def do_GET(self):
        if not self.host_ok():
            return self.send(403, b'forbidden')
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == '/':
            body, csp = page_with_nonce()
            return self.send(200, body, 'text/html; charset=utf-8',
                             (('Content-Security-Policy', csp), ('X-Frame-Options', 'DENY')))
        if u.path.startswith('/static/'):          # the page's own icon files, by exact name, nothing else
            name = u.path[len('/static/'):]
            if name in STATIC:
                try:
                    with open(os.path.join(HERE, 'web', 'static', name), 'rb') as f:
                        return self.send(200, f.read(), STATIC[name], (('Cache-Control', 'max-age=86400'),))
                except OSError:
                    pass
            return self.send(404, b'not found')
        if u.path == '/check':
            return self.send(200 if self.keyed((q.get('k') or [''])[0]) else 403, b'')
        if u.path == '/events':
            if not self.keyed((q.get('k') or [''])[0]):
                return self.send(403, b'forbidden')
            return self.events()
        if u.path.startswith('/fs/' + PKEY + '/'):
            return self.preview('/' + urllib.parse.unquote(u.path[len('/fs/' + PKEY + '/'):]))
        self.send(404, b'not found')

    def preview(self, want):
        """Serve one previewable file. A page must be a file an agent wrote and still the very same file. Anything
        else must be a style, script, image or font inside a granted folder, and is opened by walking down from that
        folder one name at a time with symlinks refused at every step, so what is served is always inside the grant.
        Nothing in or under a private folder is served that way, whatever folder was granted: the granted folder and
        every folder walked through is compared, by identity, with the private ones."""
        import stat as _st
        want = os.path.normpath(want)
        flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0)
        fd = -1
        try:
            granted = ALLOWED_FILES.get(want)
            if granted:
                fd = os.open(want, flags)
                st = os.fstat(fd)
                if (st.st_ino, st.st_dev) != granted:
                    return self.send(404, b'not found')
            else:
                root = next((d for d in list(ALLOWED_DIRS) if want.startswith(d + os.sep)), None)
                if not root or not want.lower().endswith(ASSET_EXT) or in_private(want):
                    return self.send(404, b'not found')
                private = dir_ids(PRIVATE_ROOTS)
                cur = os.open(root, os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0))
                try:
                    ident = (os.fstat(cur).st_ino, os.fstat(cur).st_dev)
                    if ident != ALLOWED_DIRS[root] or ident in private:
                        return self.send(404, b'not found')
                    parts = [x for x in want[len(root) + 1:].split(os.sep) if x]
                    if any(x in ('.', '..') for x in parts) or not parts:
                        return self.send(404, b'not found')
                    for name in parts[:-1]:
                        nxt = os.open(name, os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0), dir_fd=cur)
                        os.close(cur)
                        cur = nxt
                        if (os.fstat(cur).st_ino, os.fstat(cur).st_dev) in private:
                            return self.send(404, b'not found')
                    fd = os.open(parts[-1], flags, dir_fd=cur)
                finally:
                    os.close(cur)
                st = os.fstat(fd)
            if not _st.S_ISREG(st.st_mode) or st.st_size > MAX_PREVIEW:
                return self.send(404, b'not found')
            body = os.read(fd, MAX_PREVIEW)
        except (OSError, KeyError):
            return self.send(404, b'not found')
        finally:
            if fd >= 0:
                os.close(fd)
        # The policy sandboxes the document, which makes its origin opaque: 'self' would then match nothing, not even
        # the page that embeds it, so the embedding page's origin is named outright. This response is the whole
        # policy for a preview (the page loads it by iframe src), and no X-Frame-Options header is sent with it.
        self.send(200, body, mimetypes.guess_type(want)[0] or 'application/octet-stream',
                  (('Content-Security-Policy', PREVIEW_CSP),))

    def body(self):
        try:
            n = int(self.headers.get('Content-Length', ''))
        except ValueError:
            return None
        if n < 0 or n > MAX_BODY:
            self.close_connection = True
            return None
        try:
            d = json.loads(self.rfile.read(n) or b'{}')
        except Exception:
            return None
        return d if isinstance(d, dict) else None

    def do_POST(self):
        global LAST_PAIR, APPROVER
        if not self.host_ok():
            return self.send(403, b'forbidden')
        route = urllib.parse.urlparse(self.path).path
        d = self.body()
        if d is None:
            return self.send(400, b'bad request')
        if route == '/pair':
            # An unauthenticated action: open a tab in the default browser. What that browser is handed is a
            # one-time code, not the key, and neither is sent back to whoever asked.
            with LOCK:
                if time.time() - LAST_PAIR < 60:
                    return self.send(429, b'wait a moment')
                LAST_PAIR = time.time()
            # The browser is asked to open a private file holding a ONE-TIME code, good for two minutes. The page
            # trades that code for the key (see /claim). Neither the key nor the code appears in any process's
            # arguments. A window opened this way only watches; answering prompts must be switched on in it.
            code = secrets.token_urlsafe(24)
            steady = d.get('steady') is True          # a page in steady mode asks for the new tab to be in it too
            with LOCK:                               # registered before the file exists, so the fastest claim finds it;
                for c in [c for c, t in CODES.items() if time.time() - t > 120]:    # each code lapses by its own age
                    CODES.pop(c, None)
                while len(CODES) >= 8:
                    CODES.pop(next(iter(CODES)))
                CODES[code] = time.time()
            d0 = os.path.join(HOME, '.live-room')
            path = os.path.join(d0, 'unlock-%s.html' % secrets.token_hex(6))
            # This document carries an inline script of the server's own writing, and no nonce: it is a private file
            # opened from disk by the browser (a file: page), never served by this server, so the page's nonce policy
            # (UI_CSP, sent only with /) does not reach it; nothing from a log or a request goes into it, only the
            # code and the port, both JSON-encoded.
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'w') as f:
                    f.write('<!doctype html><meta charset="utf-8"><title>Unlocking W.A.T.C.H.</title><script>location.replace(%s)</script>'
                            % json.dumps('http://127.0.0.1:%d/%s#p=%s' % (PORT, '?steady' if steady else '', code)))
                subprocess.Popen(['/usr/bin/open', path])
            except OSError:
                with LOCK:
                    CODES.pop(code, None)
                try:
                    os.unlink(path)
                except OSError:
                    pass
                return self.send(500, b'could not open a browser tab')

            def tidy():
                try:
                    os.unlink(path)
                except OSError:
                    pass
            threading.Timer(120, tidy).start()
            return self.send(200, b'ok')
        if route == '/claim':                        # the page hands back the one-time code and receives the key, once
            with LOCK:
                t = CODES.pop(str(d.get('code', '')), None)
            if t is None or time.time() - t > 120:
                return self.send(403, b'that unlock link has expired; press Unlock again')
            return self.send(200, KEY.encode())
        if not self.keyed(self.headers.get('X-Live-Room-Key', '')):
            return self.send(403, b'forbidden')
        if route == '/keep':                         # this window's whole pin list, re-sent every few minutes; an empty list clears it
            ids = d.get('ids')
            if not isinstance(ids, list):
                return self.send(400, b'ids must be a list')
            win = d.get('win') if isinstance(d.get('win'), str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', d.get('win')) else ''
            with LOCK:
                now = time.time()
                for wins in KEEP.values():
                    wins.pop(win, None)
                for x in ids[:50]:
                    if isinstance(x, str) and AID_RX.match(x):
                        KEEP.setdefault(x, {})[win] = now
                prune_keep(now)
            return self.send(200, b'ok')
        if route == '/unlink':                       # drop a stored association, whatever made it
            child = str(d.get('child', ''))
            if not AID_RX.match(child):
                return self.send(400, b'bad id')
            unlink(child)                            # file, live record and event together, under the link lock (see unlink)
            return self.send(200, b'ok')
        if route == '/presence':
            if d.get('visible') is True:
                APPROVER = time.time()
            return self.send(200, b'ok')
        if route == '/open':
            return self.open_session(str(d.get('agent', '')))
        if route == '/decide':
            if not isinstance(d.get('allow'), bool):
                return self.send(400, b'allow must be true or false')
            with LOCK:
                p = PENDING.get(str(d.get('pid')))
                if not p:
                    return self.send(404, b'no longer waiting')
                if p['allow'] is not None:
                    return self.send(409, b'already decided')
                p['allow'] = d['allow']
            p['event'].set()
            return self.send(200, b'ok')
        if route in ('/hook', '/permission'):
            sid, sub = d.get('session_id'), d.get('agent_id')
            if not (isinstance(sid, str) and ID_RX.match(sid)) or (sub is not None and not (isinstance(sub, str) and ID_RX.match(sub))):
                return self.send(400, b'bad id')
            aid = 'c:' + (sub or sid)
            hev = str(d.get('hook_event_name', ''))[:40]
            if hev in ('Stop', 'SubagentStop'):      # Claude Code saying a turn, or a sub-agent, has just finished
                if hev not in HOOK_SEEN:
                    HOOK_SEEN.add(hev)
                    print('first %s hook received (names the sub-agent: %s)' % (hev, 'yes' if sub else 'no'))
                # A sub-agent's stop that does not name the sub-agent cannot be attributed, and must never end its parent's turn.
                # A hook is provisional: another Stop hook may still keep the turn going, and the log's own records may
                # arrive later; it is marked so the page can order it against what the log says. The wording sent with
                # it says only what the hook reported, never that the report was handed back: the log's own
                # toolEndsTurn record is what establishes that (see ClaudeTail.handle).
                if aid in AGENTS and (hev == 'Stop' or sub):
                    emit(aid, 'turn_end', False, how='reported stopped by its hook, not yet in its log' if sub else '', hook=True)
                return self.send(200, b'ok')
            if aid not in AGENTS:                    # heard from before its log was found: give it a described station
                describe(aid, False, runtime='Claude', name='Claude ' + aid[-4:])
                HOOKED[aid] = time.time()
            elif aid in HOOKED:
                HOOKED[aid] = time.time()
            if route == '/permission':
                return self.permission(aid, d)
            nt = str(d.get('notification_type', ''))[:40]
            msg = str(d.get('message', ''))
            # Some permission prompts (sandbox network access, for one) only ever arrive as a notification, so they
            # are shown unless this agent already has a held request on screen for the very same request. The notice
            # reads "Claude wants to run: <command>" (or "… to edit/read/open: <file or URL>"), so the part after the
            # colon is compared with the held request's own text, whole and exactly (only the ends trimmed): equal or not.
            # Containment is not identity (`pwd` held must not hide `pwd && rm -rf …`). When the notice names the
            # tool, that must match too. A notice that cannot be matched that way is shown.
            with LOCK:
                held = any(p['aid'] == aid and p['allow'] is None and time.time() - p['ts'] < 4 and p['what']
                           and same_request(p['what'], msg) and (not d.get('tool_name') or str(d.get('tool_name')) == p['tool'])
                           for p in PENDING.values())
            if d.get('hook_event_name') == 'Notification' and nt != 'idle_prompt' and not (nt == 'permission_prompt' and held):
                emit(aid, 'waiting', False, why='permission' if nt == 'permission_prompt' else nt,
                     text=str(d.get('message', 'Needs your attention'))[:2000] + (' — answer it in the app.' if nt == 'permission_prompt' else ''))
            return self.send(200, b'ok')
        self.send(404, b'not found')

    def open_session(self, aid):
        seen = set()
        a = AGENTS.get(aid)
        while a and a.get('sub') and not a.get('soft') and a.get('parent') in AGENTS and aid not in seen:     # only a recorded Claude sub-agent lives inside its parent; inferred relations are never followed
            seen.add(aid)
            aid = a['parent']
            a = AGENTS[aid]
        if not a or not re.fullmatch(r'[cx]:[0-9a-f-]{8,64}', aid):
            return self.send(404, b'unknown agent')
        if aid[0] == 'x':
            url = 'codex://threads/' + aid[2:]
        else:
            local = claude_desktop_id(aid[2:])
            if not re.fullmatch(r'local_[A-Za-z0-9-]{1,64}', local):
                return self.send(404, b'This session was not started in Claude Desktop, so there is no window to open.')
            url = 'claude://code/continue?session=' + local
        subprocess.Popen(['/usr/bin/open', url])
        self.send(200, b'ok')

    def permission(self, aid, d):
        """Hold Claude Code's PermissionRequest hook until you press Allow or Deny at that agent's desk.
        An empty reply means "no decision", after which Claude is expected to show its own prompt (not yet confirmed
        against a real prompt; see README). An empty reply is what is sent at once when
        no visible room window has checked in recently, when too many prompts are already held, on timeout, and
        when the last visible window goes away while holding."""
        ti = d.get('tool_input') if isinstance(d.get('tool_input'), dict) else {}
        tool = str(d.get('tool_name', 'this action'))[:80]
        if tool == 'AskUserQuestion':               # a question for you, not permission to act: Allow or Deny cannot answer it,
            return self.send(200, b'')              # so it is never held and goes straight to the app (the room still shows it is asking)
        what = str(ti.get('command') or ti.get('file_path') or ti.get('url') or '').strip()     # the request itself, for matching a notice to it
        full = (what + '\n\n' if what else '') + json.dumps(ti, ensure_ascii=False, indent=1)
        pid = secrets.token_hex(8)
        p = {'event': threading.Event(), 'allow': None, 'aid': aid, 'tool': tool, 'what': what, 'text': full, 'ts': time.time()}
        fits = len(full) <= 20000 and full.count('\n') <= 600      # only requests that can be shown whole may be answered here
        with LOCK:
            holdable = fits and time.time() - APPROVER < APPROVER_GRACE and len(PENDING) < MAX_HOLDS
            if holdable:
                PENDING[pid] = p                       # registered before anyone can be told about it
        if not holdable:
            emit(aid, 'waiting', False, why='permission', text='Claude is asking permission for %s. Answer it in the app%s.' % (tool, '' if fits else ' (the request is too long to show in full here)'))
            return self.send(200, b'')
        emit(aid, 'perm', False, pid=pid, tool=tool, text=p['text'])
        self.connection.settimeout(None)
        deadline, how = time.time() + HOLD_SECONDS, 'timed out here, so no answer was sent; Claude normally asks in the app'
        while not p['event'].wait(1):
            if time.time() > deadline:
                break
            if time.time() - APPROVER > APPROVER_GRACE:
                how = 'no room window is open any more, so no answer was sent; Claude normally asks in the app'
                break
            try:
                r, _, _ = select.select([self.connection], [], [], 0)
                if r and self.connection.recv(1, socket.MSG_PEEK) == b'':
                    how = 'the request went away (answered in the app, cancelled, or the session ended)'
                    break
            except OSError:
                how = 'the request went away'
                break
        with LOCK:
            PENDING.pop(pid, None)
            allow = p['allow']
        if allow is None:
            emit(aid, 'perm_end', False, pid=pid, how=how)
            if how.startswith('the request went away'):
                self.close_connection = True
                return None
            return self.send(200, b'')
        emit(aid, 'perm_end', False, pid=pid, how='allowed from the room' if allow else 'denied from the room')
        # The installed hook passes on only these two replies, compared byte for byte (scripts/install_hooks.py).
        decision = {'behavior': 'allow'} if allow else {'behavior': 'deny', 'message': 'Denied by the user from W.A.T.C.H.'}
        self.send(200, json.dumps({'hookSpecificOutput': {'hookEventName': 'PermissionRequest', 'decision': decision}}).encode(), 'application/json')

    def events(self):
        global STREAMS
        with LOCK:
            if STREAMS >= MAX_STREAMS:
                full_up = True
            else:
                full_up = False
                STREAMS += 1
        if full_up:
            return self.send(503, b'too many windows')

        def line(d):
            return 'data: %s\n\n' % json.dumps(dict(d, snap=True, boot=BOOT, seq=-1))      # ASCII-escaped: a lone surrogate in log text travels as \udXXX and cannot break the stream
        try:                                        # everything after the slot is taken sits inside the release
            self.connection.settimeout(25)          # a window that stops reading is dropped, not waited on forever
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.end_headers()
            # A page that has just loaded sends no cursor: it starts at the oldest event still kept. Only a page
            # that is RESUMING, and whose cursor has fallen off the end of what is kept, is told there is a gap;
            # it then reloads and comes back as a fresh page.
            raw = self.headers.get('Last-Event-ID')
            try:
                nxt = int(raw) + 1 if raw is not None else None
            except ValueError:
                nxt = None
            with COND:
                expired = nxt is not None and nxt < BASE
                if nxt is None:
                    nxt = BASE
            out = line({'kind': 'hello', 'agent': '', 'pkey': PKEY, 'window': WINDOW, 'names': project_names()})
            if expired:
                self.wfile.write((out + line({'kind': 'gap', 'agent': ''})).encode())
                self.wfile.flush()
                return
            out += ''.join(line(dict(a, kind='agent', agent=k, old=True, ts=0)) for k, a in list(AGENTS.items()))
            with LOCK:                               # prompts still being held, so a reload never loses one
                held = [(pid, p) for pid, p in PENDING.items() if p['allow'] is None]
            out += ''.join(line({'kind': 'perm', 'agent': p['aid'], 'pid': pid, 'tool': p['tool'], 'text': p['text'], 'ts': p['ts'], 'old': False}) for pid, p in held)
            self.wfile.write(out.encode())
            while True:
                with COND:
                    if nxt >= BASE + len(LOG):
                        COND.wait(5)
                    lost = nxt < BASE                # this window stalled and fell behind what is still kept
                    batch = LOG[max(0, nxt - BASE):]
                    nxt = BASE + len(LOG)
                if lost:
                    self.wfile.write(line({'kind': 'gap', 'agent': ''}).encode())
                    self.wfile.flush()
                    return
                out = ''.join('id: %d\ndata: %s\n\n' % (e['seq'], json.dumps(e)) for e in batch)      # ASCII-escaped, see the snapshot
                out += line({'kind': 'health', 'agent': '', 'scan': SCAN['ts'], 'tails': SCAN['tails'], 'ready': SCAN['ready'], 'err': SCAN['err'], 'failing': SCAN['failing'], 'now': time.time()})
                self.wfile.write(out.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with LOCK:
                STREAMS -= 1
            self.close_connection = True


class Server(ThreadingHTTPServer):
    daemon_threads = True
    active = 0

    def process_request(self, request, client_address):
        with LOCK:
            if Server.active >= 64:             # refuse rather than spawn without bound
                self.shutdown_request(request)
                return
            Server.active += 1
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with LOCK:
                Server.active -= 1


if __name__ == '__main__':
    threading.Thread(target=watch, daemon=True).start()
    print('W.A.T.C.H. on http://127.0.0.1:%d/  — open it and press Unlock (sessions active in the last %d min)' % (PORT, WINDOW // 60))
    Server(('127.0.0.1', PORT), H).serve_forever()
