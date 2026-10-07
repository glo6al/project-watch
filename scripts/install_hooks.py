#!/usr/bin/env python3
"""Add (or with --remove, take out) W.A.T.C.H.'s Claude Code hooks in ~/.claude/settings.json.

  Notification       tells the room a session wants attention. Observes only.
  Stop, SubagentStop tell the room Claude, or a sub-agent, is about to stop. That is provisional, not a finished
                     turn: another Stop hook can tell Claude to continue, and the log's own records of the turn's
                     end may arrive later; the room labels it so. Observe only: they print nothing, so they cannot
                     keep Claude going or change what it does.
  PermissionRequest  asks the room for your Allow / Deny and passes the answer back to Claude.
                     This one DOES decide prompts on your behalf when you press a button in the room.
                     With the room closed, or no answer, it prints nothing. Claude is then expected to ask in the app
                     as usual; that has not yet been confirmed against a real prompt (see README).

All send the key from ~/.live-room/header, which the server writes when it starts. Only entries carrying this script's
marker are ever touched; other hooks in the same group are left alone. The file is replaced atomically and a
uniquely named backup is written first. PORT is honoured (default 8793).

The hooks trust whatever process is listening on that port: they send it the key, and the PermissionRequest hook
passes on its Allow or Deny. Nothing here proves that process is W.A.T.C.H. While the server is not running, another
local program able to bind the port could collect the key and answer prompts. Stopping other local programs from
binding it is outside what this tool protects against; remove the hooks (--remove) if that matters on this machine.
"""
import json, os, shutil, sys, tempfile, time

P = os.path.expanduser('~/.claude/settings.json')
PORT = os.environ.get('PORT', '8793')
if not PORT.isdigit():
    sys.exit('PORT must be a number')
MARK = '#live-room-hook'
# The key is read by curl from a private file, so it never appears in a process's arguments. -f makes curl print
# nothing when the server refuses or errors, so Claude never receives a non-decision as if it were one.
HEAD = "-H 'Content-Type: application/json' -H @\"$HOME/.live-room/header\""
URL = 'http://127.0.0.1:%s' % PORT
# -q ignores any ~/.curlrc and --noproxy '*' ignores proxy settings, so the key is only ever sent to this machine.
CURL = "curl -q -sf --noproxy '*'"
TELL = "%s -m 1 -X POST %s --data-binary @- %s/hook >/dev/null 2>&1 || true %s" % (CURL, HEAD, URL, MARK)
# The reply is collected first and printed only if the transfer succeeded and it is, byte for byte, one of the two
# decisions the server sends (see permission() in src/server.py). Anything else, such as a reply cut part-way, an
# error page or some other JSON, prints nothing, which leaves the prompt to Claude.
REPLIES = [json.dumps({'hookSpecificOutput': {'hookEventName': 'PermissionRequest', 'decision': d}}) for d in
           ({'behavior': 'allow'}, {'behavior': 'deny', 'message': 'Denied by the user from W.A.T.C.H.'})]
ASK = ("r=$(%s -m 320 -X POST %s --data-binary @- %s/permission 2>/dev/null) && case \"$r\" in "
       "'%s'|'%s') printf '%%s' \"$r\";; esac; true %s" % (CURL, HEAD, URL, REPLIES[0], REPLIES[1], MARK))

remove = '--remove' in sys.argv
before = open(P).read() if os.path.exists(P) else ''
d = json.loads(before) if before.strip() else {}
if before:
    shutil.copy(P, '%s.live-room-backup-%s-%d' % (P, time.strftime('%Y%m%d-%H%M%S'), os.getpid()))
hooks = d.setdefault('hooks', {})
for ev, cmd, extra in (('Notification', TELL, {}), ('Stop', TELL, {}), ('SubagentStop', TELL, {}), ('PermissionRequest', ASK, {'timeout': 330})):
    groups = []
    for g in hooks.get(ev, []):
        mine = [h for h in g.get('hooks', []) if MARK in h.get('command', '')]
        rest = [h for h in g.get('hooks', []) if MARK not in h.get('command', '')]
        if rest or not mine:                       # keep the group, minus only our own entries
            groups.append(dict(g, hooks=rest) if mine else g)
    if not remove:
        groups.append({'hooks': [dict({'type': 'command', 'command': cmd}, **extra)]})
    if groups:
        hooks[ev] = groups
    else:
        hooks.pop(ev, None)
if not hooks:
    d.pop('hooks')
os.makedirs(os.path.dirname(P), exist_ok=True)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(P), prefix='.settings-', suffix='.tmp')
with os.fdopen(fd, 'w') as f:
    json.dump(d, f, indent=2)
# Compare again at the last moment. This narrows, but cannot fully close, the window in which another program
# could save settings.json; Claude Code offers no lock to coordinate on.
now = open(P).read() if os.path.exists(P) else ''
if now != before:
    os.unlink(tmp)
    sys.exit('settings.json changed while this was running; nothing written. Run it again.')
os.replace(tmp, P)
print('removed' if remove else 'installed', '->', P)
