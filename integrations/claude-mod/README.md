# WATCH owner messaging mod

This local plugin uses Claude Code's official mod APIs to connect one existing conversation to WATCH's owner composer and Officer. It receives a complete authenticated owner request from WATCH and calls `$.prompt.submit({text: envelope, asUser: false})`. Claude retains plugin attribution and uses the conversation's current model, sign-in, project context, tools and native permissions.

It is maintained by WATCH and is not an Anthropic product or endorsement. This is an owner-only adapter. It does not enable WATCH peer delivery, create conversations, call a separate model, intercept permission decisions, or read Claude account credentials.

## Install in the intended existing project

Keep this repository at a stable local path. In the intended existing Claude conversation, use the supported plugin manager with the absolute path to this directory:

```text
/plugin marketplace add /absolute/path/to/agent-watch/integrations/claude-mod
/plugin install watch-owner@watch-local --scope local
/reload-plugins
```

Confirm the intended conversation's native UUID with `/status` after reload. Select that exact chat in WATCH, choose **Connect Claude**, and enter the single-use `/watch-connect <port>:<pair-code>` command WATCH displays into that chat. The code expires after five minutes and is pinned to the observed conversation UUID and project. Preserve unrelated plugins and settings through native plugin management. Do not launch another Claude session as an installation substitute.

`/watch-status` shows connection state without the service token. `/watch-disconnect` stops polling and revokes readiness. `/watch-connect` without arguments explicitly reconnects the saved enrollment with a fresh connection generation. A plugin reload also reconnects an enrollment that was active, retaining prior receipts without resubmitting any prompt.

WATCH issues its own scoped service token. The mod stores it in its plugin store under a key specific to the native conversation UUID. It never places the token in model input, status output, URLs, command arguments or logs. The plugin store is shared by this plugin across sessions; it is not a security vault or a transactional lock. The same operating-system account and other installed mods remain trusted.

## Delivery and recovery

The WATCH server durably reserves each request before returning its full envelope. The mod then persists an attempted request record and envelope digest before invoking the native API once. A busy Claude conversation queues that ordinary input. Heartbeats continue while the submission promise waits for idle; the mod accepts no second claim during that attempt. Distinct owner requests can wait in WATCH's durable queue.

A native submission receipt means input was admitted or started, not that an answer exists. The mod binds only a main `turn.start` whose full text exactly equals the submitted envelope. It returns an answer only from that same main turn's nonaborted `turn.complete` with reason `answer` and exactly one matching `WATCH_REPLY` marker line. Child agents, tools, background notices, unrelated turns and missing or conflicting markers do not fulfill requests. An unrecognized prompt rewrite cannot be credited as the answer.

An uncertain submission is never automatically retried. A lost claim HTTP response stops polling until reconnect; the server's durable claim still prevents that request being submitted again. A reload marks unfinished local attempts unknown and can replay the same stored completion event after an acknowledgment loss. Replaying status or a retained answer never invokes `prompt.submit` again. If a submit promise is still pending, disconnect does not make its native queue empty and reconnect is held until that promise settles.

Avoid clearing, switching or resuming a conversation until queued WATCH work settles. Such changes after the final UUID check can affect where native queued input runs. Reconnect the intended chat afterward. This is a disclosed lifecycle limitation; no atomic destination or exactly-once provider execution guarantee is claimed.

Service requests use only fixed endpoint paths at the numeric-loopback origin `http://127.0.0.1:<port>`. Pairing supplies a bounded numeric port, not an arbitrary URL. WATCH never redirects. Official `$.http.fetch` itself follows up to five redirects and drops authorization headers on a different-origin redirect; this mod uses no unsupported redirect-control option or private transport.

## Offline checks

```sh
npm --prefix integrations/claude-mod test
claude plugin validate --json integrations/claude-mod
claude plugin test integrations/claude-mod
```

The plugin and native fixtures were validated with Claude Code **2.1.309**. Node functional fixtures mock HTTP, store, clock and prompt admission. The official native test loads this one plugin, exercises its detached `asUser:false` call, verifies plugin provenance and attempted persistence, then fires its own main turn observers through the official test kit. This host omits optional `origin.asUser` for false; the Node fixture additionally checks the actual call argument is explicitly false.

The native harness mocks Claude's core. It proves official API acceptance and observer routing with an exact full-envelope text fixture, not live REPL attribution preprocessing, actual busy queue timing, retained native transcript, or model behavior. A separately authorized check in an already-existing disposable conversation must verify the ordinary WATCH-to-Claude-to-WATCH flow before claiming that live delivery was tested. No live installation, enrollment, configuration changes or model calls are part of the offline checks.

## Disconnect, revoke and remove

Run `/watch-disconnect` and inspect WATCH and native receipts. Disconnect stops future claims; already admitted native input cannot be recalled. The token remains saved for explicit reconnect. To invalidate an old token, complete a fresh pairing from WATCH; successful re-pairing replaces the earlier enrollment. When queued work has settled, uninstall `watch-owner@watch-local`, remove the `watch-local` marketplace if no longer needed, and reload using the native plugin manager. Uninstalling stops the plugin but does not itself invalidate its server token; re-pair first if invalidation is needed. A consumed request or delivered answer is not undone by uninstalling.
