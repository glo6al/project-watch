# Roadmap

**One workspace for your AI conversations—across Claude, Codex, Cursor, and more.**

WATCH brings supported sessions, their context, and their available capabilities into one interface. The room provides an overview; the desk brings a conversation and its work together. Multi-agent supervision is one use of that workspace. The broader goal is to help you find, understand, and continue a conversation without having to remember which tool holds it.

This is a working study. The sections below distinguish shipped behavior, investigated gaps, proposed priorities, and longer-term ideas. Priorities are a direction for planning, not release dates or promises of access to a provider's private interfaces. The [README capability table](../README.md#current-provider-capabilities) is the reference for what users can do today.

## Available today

- **Supported local sessions:** observation of Claude Code and Codex session logs and Cursor transcripts. Recently active sessions appear by default; this is not an import of every chat or full account history. Codex cloud work on this computer has command observation only.
- **A shared conversation interface:** recorded messages with formatted responses, provider labels, project context, and parent–child relationships. The conversation is central at the desk; the composer remains plain text.
- **Work beside the conversation:** a sliding, resizable panel with Preview, Changes, and Activity. Evidence is limited to what the provider records; Changes shows recorded edits, not the Git working tree. Narrow layouts switch between conversation and work.
- **Overview and attention:** the room, project filters, compact agent navigation, fixed Alerts/Activity controls, possible-request notices, and optional browser notifications. Relationships and requests are labelled as recorded or inferred.
- **Original and Neutral themes:** the same interaction model with different palettes. Original remains the default.
- **Direct messaging where verified:** messages queue to eligible existing local Codex chats. Room and desk share recipient-specific drafts, histories, and delivery records. New messages to named Claude chats are disabled because exact conversation targeting is unverified; recovery of existing requests remains available.
- **The Officer of the Watch:** questions about recorded context, cited evidence, and explicit supported message/control requests. Local Codex steering and interruption are available through WATCH on the verified macOS desktop build. This sends a new instruction or stops a turn; it does not promote an existing queued message.
- **Questions and permissions:** optional Claude Code permission hooks support Allow/Deny when Answer here is enabled. Supported native Codex questions and selected model/effort can be observed on the verified build; answering those questions or changing those settings is not enabled.
- **Optional peer messaging:** the shared MCP bridge allows connected clients to address reachable Codex chats. Claude clients can send through it, but Claude chats cannot currently receive bridge messages. Peer messages confer no new authority and cannot invoke controls.
- **Recorded session facts:** model, branch, context counts, summarisation markers, step timing, and stop hooks where available. Missing facts are shown as not recorded.
- **A read-only terminal view:** a board and event tail from the same stream, plus optional iTerm2 tabs or panes. Its validation limitations are listed below.

## Priorities

### 1. Make supported conversations easy to find and return to

The first product priority is dependable access to existing sessions across tools. The current activity window is useful for watching work, but is not a complete conversation library.

- Improve session discovery and navigation across providers and projects, with a clear distinction between a session, its child agents, and other sessions sharing a folder.
- Make recorded-history coverage and unavailable content understandable before someone mistakes a partial transcript for the full conversation.
- Design persistent history and search across sessions as an extension of the workspace. WATCH can answer from retained context today; durable searchable history and full-account imports are not shipped.

**Completion criterion:** a user can identify the intended supported session, see where it comes from and how much history is available, and return to it without confusing it with another conversation. Any new storage needs explicit retention and privacy rules.

### 2. Make actions reliable within the conversation

Provider integration work is already under investigation; these are unresolved capabilities, not completed features waiting for a button.

- **Claude messaging:** establish a delivery route bound to the exact selected conversation. A connected process that can switch chats is insufficient. The same targeting proof must apply to owner messages and the peer bridge.
- **Queued-message steering:** send normally, then offer **Steer** on the queued message when supported. WATCH uses that term across providers. Steering must consume that exact queued message once, without also leaving it to run later. If the run has finished, deliver normally. No provider-specific wording or substitute stop-only action should stand in for this flow.
- **Native questions and settings:** evaluate supported ways to answer ordinary questions and change selected model/effort, preserving the distinction between a selection for the next turn and the model recorded for past work.
- **Capability clarity:** keep observation, messaging, steering, interruption, question answering, and permission approval separate. An unavailable action needs an explanation and an appropriate route back to the original tool when available.

**Completion criterion:** each enabled action targets the intended session, exposes pending/confirmed/uncertain outcomes, survives retries without duplicate effects, and passes independent review and provider-specific verification. Direct actions should be available where the user is conversing, without requiring an Ask WATCH detour.

### 3. Broaden provider and conversation coverage

Depth of support matters as much as adding a provider name. Candidates must expose a usable, verifiable source of conversation data or an action interface. No unverified integration is promised.

| Connection | Current boundary | Next support question |
| --- | --- | --- |
| Local Codex | Logs, eligible queued messages, verified active-turn controls; native reads only | Can WATCH safely promote the exact queued message and support native replies/settings? |
| Claude Code | Logs and optional permission hooks; new named-chat messages disabled | Can delivery remain bound to the selected chat across clear, resume, and fork? |
| Cursor | Transcript observation without tool results or direct messaging | What verified routes exist for session targeting, results, and actions? |
| Codex cloud on this computer | Local commands only, no conversation | Is there an authorized interface for conversation history and actions? |
| Ordinary web chats and additional providers | Not imported | What access, permissions, and session identity guarantees are available? |

**Completion criterion:** every added connection has an explicit support matrix, visible limitations, a defined privacy boundary, and tests against real supported behavior. A provider badge must never imply all of its chats or capabilities are available.

### 4. Deepen work context and coordination

Once users can reliably find and interact with existing sessions, extend what they can do across them:

- Git state per coding session: branch, uncommitted files, and attributable changes as a basis for possible editing conflicts.
- Start a session with a prompt, files, a folder, and a provider where supported.
- Explicit handoffs and independent review verdicts linked to the relevant conversation and work. The existing peer bridge is a transport foundation, not a complete coordination workflow.
- Optional holds on deploy, push, and delete, only after the approval interface and provider hook behavior are verified. This expands the permission boundary and remains a later investigation.

**Completion criterion:** coordination preserves the selected session, owner intent, evidence, and delivery state. It must not create implicit authority between agents or silently move work to another provider.

## Validation and platform work

These run alongside the product priorities; broader messaging support does not remove them.

- **Windows:** automated tests run in CI, but the application has not been validated on a Windows desktop with real logs or a browser. Hooks and Officer requests are not available there. Preview assets need a directory-handle-based folder walk; until then previews serve the page alone, with Python 3.12 or later required. File IDs can be reused after deletion; 8.3 names, `subst` drives, and restart/port behavior still need testing. See the [README](../README.md#windows) and [Windows guide](WINDOWS.md) for setup and current restrictions.
- **Real devices and scale:** measure large session collections and previews; test actual trackpad gestures, physical phones, and short windows. Browser emulation and synthetic gesture events do not establish those results.
- **Provider operations:** the recorded live checks cover specific Codex steering/interruption cases and one Allow and one Deny on real Claude prompts. Expand coverage of provider updates, unavailable or stale targets, lost replies, and the native app's behavior while a request is held. Open-in-app links still need end-to-end validation.
- **Preview compatibility:** previews are known not to render in Claude desktop's built-in browser pane; they render in Chrome. This needs continued compatibility testing, not a claim of universal browser support.
- **Terminal view:** its first independent review findings were repaired, but the repairs have not had a second review. Amber tabs and badges have been checked as output codes, not against a real waiting agent.
- **Publication:** changes require independent review. Public examples and recordings use fictional data; live conversation text and command output can contain personal paths. Private investigation evidence stays local and ignored.

## Known limits

- Granularity is whatever the logs record: tool calls, their results, messages and turn boundaries, never keystrokes. For Codex, a step in flight is read from script text and marked unconfirmed until its finished record arrives. Hidden reasoning is not shown, because it is not in the logs.
- "No log entry for N" means the log is silent; it does not prove an agent is idle.
- Touching a log whose last timed record is outside the window does not make it active: for Claude Code and Codex the last timed record decides, by the log's own clock, so a log written by a clock more than the window behind this machine's stays out.
- Which agent started which is recorded for some cases and inferred for others; inferred links are labelled.
- Alerts and "probably needs you" notices are heuristics and can be wrong in either direction.
- Codex: messages between agents can be encrypted in its log, and permission prompts are not visible. WATCH tracks questions it queues itself; this does not expose every queue in Codex.
- The Officer of the Watch answers only from what the room keeps (the last few thousand steps), so a question about earlier work gets "not in the logs"; its answers are a model's reading of those steps and can be wrong, which is why the entries it consulted are shown beside each answer. Voice is not available. Not offered on Windows yet (the key file's owner and permissions are not checked there).
- Codex cloud agents keep no transcript on this computer. Their monitor is read from the process list (the ChatGPT app's `codex exec-server`, which OpenAI marks experimental): commands and folders as they run, never their words, what they read, what they changed, or whether a command succeeded. The list is sampled about once a second, so a shorter command can be missed; of several new commands at once the oldest four are examined first, so a short-lived one among the rest can be missed; a process is told from another by pid and start time to within a few seconds; the monitor says when the list cannot be read. Not on Windows. A Changes view for such a session would need an OS file watcher with a trustworthy record of who wrote what; three reviews showed a folder listing cannot give that honestly, so it is not attempted.
- Another program running as the same user that can read `~/.live-room/` or drive the browser can do what you can do here. Closing that needs an operating-system boundary this tool does not have.
- macOS; Windows only in CI's automated tests: the application has not been run on a Windows desktop, and has no hooks (so no Allow / Deny) there yet. Phone-width layouts have been checked in browser emulation; physical phones and very short windows still have limited coverage.
- Known and left as they are: only the last ~200 KB of a log is replayed on first sight (the log panel says "earlier history not loaded" when steps are missing); the row re-spaces when agents come and go; interrupting a camera move jumps to its target; content shifts when a notice bar appears; partial log replay cannot reconstruct every earlier in-flight call or plan; the installer cannot fully rule out another program saving `settings.json` in the instant it writes; the review history is kept in the project's private archive, not here. WATCH-owned message and control records have separate persisted status recovery.


## Longer-term ideas

- A visible record of a correction reaching a session and the work that followed it.
- A rough running cost estimate where usage and pricing inputs are available.
- A room overview that uses portrait-screen height more effectively.
- Broader transcript fixtures and provider compatibility checks.

## Rules that carry forward

The person stays in charge. A conversation, log entry, or peer message cannot authorize new actions. Permission decisions remain opt-in per window; messaging and controls require an explicit owner request through an authenticated route. Recorded, inferred, accepted, delivered, answered, and completed are different states. WATCH must preserve those differences as support grows.
