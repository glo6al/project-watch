# WATCH messaging plugin

This WATCH-maintained plugin connects existing local Claude Code conversations to WATCH's owner composer, Officer and optional shared agent messaging. Install it once for your user account, authorize WATCH once, and eligible chats register automatically after loading the plugin. Each chat keeps its own native UUID, project, service connection and request receipts. Chat names are display labels, never routing authority.

It uses Claude Code's official mod APIs, including `$.prompt.submit({text: envelope, asUser: false})`. Claude retains plugin attribution and uses the existing conversation's model, sign-in, tools, permissions and project context. This is not an Anthropic product or endorsement. Peer receiving requires explicit WATCH authorization and a compatible current connection. It does not interrupt Claude, create conversations, answer native permissions, or read Claude account credentials. It does not call a separate model.

## Install once for your user account

Use a Claude Code executable with mod support (the native compatibility target is **2.1.309**). Your terminal's `claude` may be older than Desktop's bundled executable; check `claude --version` and use a supported executable before installation. Keep this repository at a stable local path.

Run these commands in a terminal. They manage the plugin without starting another conversation:

```sh
claude plugin marketplace add /absolute/path/to/agent-watch/integrations/claude-mod --scope user
claude plugin install watch-owner@watch-local --scope user
```

**User scope enables the plugin across your local projects on this computer.** It is not a per-chat or per-project installation. Existing project/local overrides can take precedence; inspect the native plugin manager if an older local version remains enabled. Preserve unrelated plugins and settings.

In each already-open Claude chat you want available, run `/reload-plugins` once to load the installed plugin. New local chats load it at startup. Claude does not expose a supported command to force-load a new plugin into every already-running chat at once. Cloud sessions do not load your local user settings.

## Authorize WATCH once

1. Open an unlocked WATCH window and choose an eligible Claude chat as the recipient.
2. Select **Set up Claude**. WATCH creates a single-use code that expires after five minutes.
3. Run the displayed `/watch-setup <port>:<code>` command in **one** existing local Claude chat that has loaded the plugin.

The setup screen discloses authorization for your messages and agent messages from the shared WATCH MCP. Replies to agents are visible to every connected shared client across projects and providers; do not include secrets. This explicitly authorizes the plugin to register eligible local parent Claude chats with this WATCH installation. It applies across local projects, rather than only the chat where you enter the command. Other loaded chats discover the shared authorization automatically. No separate pairing command is needed for each chat. WATCH enables Send only after the intended chat itself has registered and maintains its connection.

Existing owner-only global authorizations show **Enable agent messaging**. A fresh setup code explicitly replaces the prior automatic authorization; it never silently expands it. Replacement cannot recall native input already queued. Legacy manual pairings remain owner-only and automatic registration does not replace them. Peer receiving also requires the MCP service to be configured separately in the sending client.

Registration uses the official host's current native UUID and canonical project, matched to an observed eligible chat or independently verified local parent-chat metadata. An idle chat does not need a tile in the WATCH room. Incomplete or ambiguous metadata waits; child, related-parent, cloud, wrong-runtime or mismatched-project observations veto registration. Two chats in the same project remain distinct. Native reloads, WATCH restarts and temporary service outages trigger registration recovery with bounded request rate; backoff can delay reconnection.

Automatic registration runs in interactive terminal sessions and sessions observed in Claude Desktop. Once the plugin observes a Desktop attachment for the current native UUID and project, it can keep that chat connected while you view another chat. This evidence stays in the current plugin runtime and is cleared on identity changes, resets, reloads and explicit disconnect. An unattended headless session that has never been observed in Desktop does not register automatically. A stopped or unloaded native host cannot maintain a connection. Metadata and installation alone never establish receiving readiness: the exact chat must keep a live authenticated plugin lease.

`/watch-status` reports setup/connection state without secrets. `/watch-disconnect` intentionally opts this chat out, including after reload; `/watch-connect` without arguments re-enables it. Temporary native lifecycle transitions do not count as an owner opt-out. Legacy per-chat `/watch-connect <port>:<pair-code>` enrollment remains compatible; an existing manual enrollment is retained rather than silently replaced by automatic registration.

## Authorization and local trust

WATCH issues a shared registration grant that is accepted directly only by the registration route. Registration returns service credentials for an eligible chat; those credentials can claim that chat's owner deliveries and, when the grant explicitly permits it and the connection negotiates peer protocol 1, shared peer deliveries. They report only matching replies. Possession of the registration grant therefore permits obtaining eligible-chat delivery credentials. It cannot originate owner messages, control an agent, or use peer/native routes. Each registered chat has separate service credentials and a per-runtime membership lease. Service credentials cannot register another chat or invoke owner actions. Owner requests still enter through WATCH's authenticated owner gateway.

WATCH persists hashes of grants and service tokens, not their recoverable plaintext. The plugin stores its registration grant under a shared plugin-store key and keeps delivery state in separate runtime receipt records. Tokens never enter model input, status output, URLs or public logs. This store is shared across sessions and is not a security vault. The same operating-system account and other installed mods remain trusted. Metadata matching does not cryptographically attest a native host's identity.

A second running host for the same native UUID cannot replace a live membership. After a lease expires it can recover that enrollment without resending an attempted prompt. Ordered connection episodes prevent delayed old registration requests from rolling a recovered connection backward. A grant replacement or revocation invalidates its derived service credentials. A lost one-use setup response requires a fresh owner setup code; setup is not claimed to be automatically retry-idempotent.

## Delivery and recovery

The delivery pipeline reserves each owner or authorized shared peer request durably before returning its envelope. The plugin persists the attempted receipt before calling the native prompt API once. A busy conversation queues that ordinary input, with heartbeats continuing independently. Different chats and their receipts remain separate.

A submission receipt means input was admitted or started, not that an answer exists. Only a corresponding main turn with the entire submitted envelope can bind a reply. Matching accepts the complete raw envelope or Claude Code 2.1.309's exact `watch-owner` attribution wrapper. Only that main turn's nonaborted answer with one exact `WATCH_REPLY` marker fulfills the request. Child agents, tools, unrelated turns, changed input and ambiguous markers do not fulfill it.

An uncertain claim or submission is never automatically retransmitted. Reconnecting does not prove that native input finished or disappeared: an uncertain peer execution continues to reserve its recipient until completion is verified or an explicit recovery procedure resolves it. Original request, attempt, origin and enrollment identities are retained. Peer messages use `WATCH_PEER_REQUEST`, explicitly identify untrusted agent content and never claim owner authority. Their answers return through the original shared request, without an additional MCP send or reply chain. Receipt recovery replays retained status or answers, never the prompt. Runtime receipt namespaces prevent a delayed former host from overwriting its successor's receipts. The current owner periodically recovers late receipts; recovery is bounded and fails closed if retained data exceeds its limit. Native hooks do not wait for WATCH network requests before forwarding native events.

Version 0.5.0 negotiates separate execution-completion reports with a compatible bridge. A reconnect in the same plugin runtime preserves the original bound turn. Completed execution releases its reservation even when the answer must be withheld because additional input entered that turn. Completion reports are saved before transmission and retried with stable identity; the original question is never retried. Older servers keep their existing protocol. Upgrade the bridge before the plugin, and check both unresolved executions and pending plugin receipts before rollback. See [completion and reconnects](../../docs/PEER_BRIDGE.md#completion-and-reconnects).

Version 0.4.0 adds negotiated, one-way reply-ready notices. With the separate [automatic reply hook setup](../../docs/PEER_BRIDGE.md#automatic-replies-to-the-asking-chat), WATCH can notify the original asking Claude chat after its peer answers. The plugin acknowledges native admission of the notice; it does not track another question or require a `WATCH_REPLY` answer. The asking model retrieves the original shared reply once. Notice receipts are separate from ordinary message receipts, and older servers or plugins retain ordinary messaging without receiving unsupported notices. Installing this plugin alone does not authorize outbound source capture or enable automatic returns.

Avoid clearing, switching or resuming a conversation until queued WATCH work settles. Such changes after the final UUID check can affect where native queued input runs. The plugin stops its old connection and discovers the current eligible identity, but no atomic destination or exactly-once provider-execution guarantee is claimed. Disconnecting cannot recall native input already admitted.

Service traffic uses fixed paths at `http://127.0.0.1:<port>`. Setup supplies a bounded numeric port, not an arbitrary host or URL. WATCH does not redirect. Official `$.http.fetch` can follow redirects; no unsupported redirect-control option or private transport is used.

## Verification

```sh
python3 tests/test_claude_mod.py
python3 tests/test_claude_global_integration.py
python3 tests/test_claude_peer_integration.py
npm --prefix integrations/claude-mod test
node tests/test_claude_mod_page.js
claude plugin validate --json integrations/claude-mod
claude plugin test integrations/claude-mod
```

Node fixtures mock native HTTP/store/clock/prompt APIs. The official native fixture harness mocks Claude's core; it verifies supported API/observer routing rather than live model behavior. A separate integration fixture runs two plugin runtimes sharing one store against the production WATCH HTTP handlers and outbox, verifying one authorization, automatic registration, isolated message/reply correlation and revocation. Native execution in that fixture is mocked. A separate peer integration fixture exercises the actual shared MCP adapter, signed HTTP, SQLite claim ledger and production plugin with native execution mocked. These checks do not establish actual busy queue timing, installation into every open chat, or live global registration.

Version 0.1.1's ordinary owner message and matching reply were separately verified through WATCH's direct composer using per-chat pairing in a disposable Desktop conversation.

Version 0.3.1's global setup and shared peer round trip were verified in an existing disposable local Desktop chat against an isolated WATCH instance. A Codex-side MCP client sent one fictional question; the Claude plugin submitted it with plugin attribution, observed its matching marked main-turn answer, and returned that answer through the original MCP request. The live host reported noninteractive SDK startup with a current Desktop attachment, confirming the distinction covered by the startup repair. Test access was revoked and the temporary plugin installation removed afterward. This proves that bounded message-and-reply path; it does not establish busy queue timing, loading into every already-open chat, or native cancellation.

Version 0.5.0 was verified with disposable live Codex and Claude Code 2.1.321 chats against an isolated bridge: two message-and-reply exchanges in each direction completed, and all four execution reservations cleared. A fifth test stopped the bridge during Claude's bound turn. Claude retained its completion while offline, and the original reply was recovered after reconnecting with the original attempt intact and exactly one native prompt. The sender's bounded wait expired during that outage; a later read of the same request recovered the answer without resending. These live tests retrieved replies through the original MCP requests. Automatic return notices are covered separately by the integration fixture with simulated native turns.

## Disconnect, revoke and remove

Use **Disconnect automatic Claude connections** in WATCH to revoke the registration grant and its derived service credentials. This does not revoke independent legacy manual connections or cancel native input already queued. A new global authorization requires a fresh setup code. Per-chat `/watch-disconnect` is an opt-out; it does not disconnect other chats.

When pending native work has settled, uninstall `watch-owner@watch-local` at the scope where it was installed, remove the `watch-local` marketplace if no longer needed, and reload through the native plugin manager. Revoke automatic connections first if you need backend credential invalidation: uninstalling alone stops polling but does not revoke saved server credentials. An answered request is not undone by removal.
