# Roadmap

W.A.T.C.H. is a working study. This lists what exists, what has not been proven, where it is going, and what comes next. Nothing here is a commitment.

## Built

- A room of monitors, one per Claude Code, Codex or Cursor agent, grouped by family: an agent, the agents it is recorded or inferred to have started, and runs that are only related to it by working in the same folder. Each kind is labelled.
- One curved row, however many agents there are. The camera stands where it would for five, so the monitors in front of you keep about that size however many there are (measured in a browser for up to twelve, not beyond); the rest of the row curves round out of frame, and you turn to it (two-finger scroll, drag, or the arrow keys). The fleet strip at the top is the map: one chip per project, one lit cell per agent.
- Per agent: what it has open, edits as diffs, commands and their output, what it said, your messages, and a rendered preview of HTML it writes.
- A desk view for one agent: its log grouped under each of your messages, your messages (click one to scroll the log to that point), chain of command (marked where it is inferred), three screens (Doing / Seeing / Changes), and recorded facts such as model, branch and approval setting.
- Notices when an agent is waiting on you, alerts when a command looks like a deploy, push, delete or send, and a five-minute activity strip coloured by kind of action.
- Optional: answering Claude Code permission prompts from the desk (off by default), and opening a session in its desktop app.
- Trackpad gestures in Chrome; a desk layout for tall, narrow screens.
- The room in a terminal (`src/watch_tty.py`): a read-only board and event tail from the same stream the page reads, with a bell when an agent starts waiting and a one-line count for a status bar. In iTerm2, `--tab` opens the board in a new tab with its own profile, and the board opens one tab per agent with that agent's log (or, with `--split`, one pane per agent in the board's tab); a tab or pane turns amber while its agent waits on you. Not yet reviewed (see below).

Added on 2026-10-05 and reviewed since (reviews six to sixteen; see the README for what is still open):

- **Attention outside the page**: a count of waiting agents in the browser tab title, and an optional system notification when an agent starts waiting on you.
- **Context gauge**: tokens in use per agent, from the counts both tools log. A bar is drawn only against a reference the log gives: the model's window (Codex), or, for Claude, the size at which that session last summarised itself, which is history, not a limit. A count the log does not state is shown as not recorded.
- **Summarisation marker**: where a session was summarised and continued, with the sizes when recorded.
- **Step timing**: how long the current step has run, flagged when far beyond what is usual for that agent.
- **Stop and SubagentStop hooks**: Claude Code tells the room when it is about to stop. That is provisional, so the room marks the end as "reported by its hook" until the log agrees.

## Not yet proven

- Allow and Deny have each been seen working once on a real Claude Code permission prompt (held in the room, the click reached Claude; Allow ran the command, Deny stopped it). What the Claude app shows while the room holds a prompt has not been recorded. While answering is on, every Claude session's permission requests wait in the room first.
- Trackpad gestures have only been tested with synthetic events, not on a real trackpad.
- The "Open in Claude / Codex" links have not been tested end to end, or clicked by the author.
- The terminal view (`src/watch_tty.py`) reads the key and prints log text. It has had one independent review (one HIGH: the key could reach an HTTP proxy; three MEDIUM; ten LOW), and every finding was fixed; the fixes themselves have not been reviewed again. The amber tab and badge have been checked as the codes it writes, not yet seen on a real waiting agent.
- The sixteenth review, of the files as first published, returned GO without exercising a real server, unlock, hook install or live permission decision. Anything changed after it has not been independently reviewed. Previews are known not to render inside the Claude desktop app's built-in browser pane, which blocks that frame; they render in Chrome.
- Rendering performance with many agents and large previews has not been measured.

## Known limits

- Granularity is whatever the logs record: tool calls, their results, messages and turn boundaries, never keystrokes. For Codex, a step in flight is read from script text and marked unconfirmed until its finished record arrives. Hidden reasoning is not shown, because it is not in the logs.
- "No log entry for N" means the log is silent; it does not prove an agent is idle.
- Which agent started which is recorded for some cases and inferred for others; inferred links are labelled.
- Alerts and "probably needs you" notices are heuristics and can be wrong in either direction.
- Codex: messages between agents are encrypted in its log, and its permission prompts and queued messages are not visible.
- Another program running as the same user that can read `~/.live-room/` or drive the browser can do what you can do here. Closing that needs an operating-system boundary this tool does not have.
- macOS only. Phone-width and very short windows are unsupported.
- Known and left as they are: only the last ~200 KB of a log is replayed on first sight (the log panel says "earlier history not loaded" when steps are missing); the row re-spaces when agents come and go; interrupting a camera move jumps to its target; content shifts when a notice bar appears; a window opened late does not learn of calls, questions, queues or plans already in flight; timeouts are for inactivity, not total time; the installer cannot fully rule out another program saving `settings.json` in the instant it writes; the review history is kept in the project's private archive, not here.

## Direction

W.A.T.C.H. is becoming a command post: one room where a person can both watch and act. Today it is the watching half, plus one act: a window that has been switched on for it can answer a Claude Code permission prompt. The room reads the logs Claude Code and Codex write and shows every agent's work as it happens, labelled by how it was learned. The acting half comes next, and the room's rules carry over to it: the person stays in charge, every action is opt-in, nothing is done on an agent's say-so, and the page never claims more than the log and the tool confirm.

Three stages, in order:

1. **Watch.** One monitor per agent, across tools. This stage exists. It keeps improving, but it is the base the others stand on.
2. **Act.** From the room: start an agent with a prompt and files, answer what it is waiting on, stop it, and, where a tool offers a way in, speak to one that is already running. Each tool allows a different amount of this; the room shows what is possible for each and never pretends the rest.
3. **Coordinate.** Many agents, many providers, one interface: Claude, Codex, and whichever others expose a readable log or a way to be driven (Perplexity, Qwen and the like are the obvious next candidates). Hand work from one to another, see one's verdict land on another's monitor, and keep the whole fleet in view.

Each action that reaches an agent widens the trust boundary, so each gets what Allow and Deny got before it: off by default, switched on per window, key-protected, and independently reviewed before it is relied on.

## Next

Not started, in this order:

1. **Git state per agent**: branch, uncommitted files, commits made this session; also the honest basis for "two agents are editing the same file".
2. **Start an agent from the room**: a prompt, files, a folder and a provider; the new session appears on its own monitor with its parent recorded, not inferred. The first action beyond answering a prompt.
3. **A held hand on deploy, push and delete**: a hook that holds such a command until you confirm. Deliberately held back until the approval interface has passed review, because it widens that surface.
4. **Saved history**: "what did I miss", rewind the room and play it forward, an end-of-day summary per agent, search across agents.
5. **More providers**, one at a time, each only as far as its logs and interfaces honestly allow.

## Ideas

- Corrections landing: show a correction of yours reaching (or not reaching) each agent, and what each agent did before and after it.
- Review verdicts: an independent reviewer's pass or blockers appearing on the author's monitor.
- Rendered Markdown, the way HTML is rendered now.
- A rough running cost per agent (needs your pricing; the logs give tokens only).
- A room overview that uses the height of portrait screens.
- More automated tests for the log readers: the command parser and a Cursor transcript are covered, Claude and Codex logs are not.

Unknown: whether Codex exposes its permission prompts; what each further provider records.
