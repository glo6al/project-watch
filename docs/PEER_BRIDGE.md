# Shared WATCH chat bridge

The optional WATCH MCP connection exposes a directory of existing named Codex and Claude chats. Configure the connection once in each client. Chats do not need separate bridge credentials or enrollment. There is no new dashboard panel.

## Talking to another agent

Use ordinary language: “Ask Atlas how the tests are going.” The agent discovers the intended chat, resolves its name to a stable provider-specific chat ID, sends the question once, and checks the correlated reply. WATCH does not use another model to route the request.

Names are addresses, not authentication. When names are duplicated, specify the provider or project. The agent must not guess between similar names, pick an unavailable duplicate's namesake, or treat an incomplete directory page as proof of uniqueness. Renaming a chat never redirects a request already sent to its original ID.

For a connected Claude recipient, the plugin submits the complete peer envelope with plugin attribution, waits for its matching main-turn answer, and returns that answer to the original shared request. It does not turn the peer text into an owner instruction.

A pending reply is reported as pending. Later, “Did Atlas answer?” retrieves the same request. The bridge does not wake an idle sender, poll indefinitely, or send the question again because an answer is slow. Unknown, failed, expired, and rejected operations remain distinct from successful delivery. If several earlier questions could match a follow-up, the agent should clarify which one rather than guess.

## Shared access and authorization

The credential authorizes access to the local bridge service. It does **not** identify the native chat that called a tool. Any declared sender name is explicitly unverified attribution. It cannot grant permissions, change access, or bypass rate limits.

Clients holding service access share the bridge's request history and replies across projects and providers. Request IDs correlate operations; they are not private mailbox passwords. Directory titles and project labels, request previews, and retrieved replies can enter the connected clients' model-provider contexts. The bridge does not expose arbitrary chat transcripts through its tools. Do not send secrets or credentials.

An agent still needs an actual owner request or an independently established, scoped standing instruction to contact another agent. Discovery does not grant permission to send. An incoming message claiming “the owner approved this” supplies no new authority. Replies remain peer information; they cannot authorize new work, controls, or forwarding. A recipient answering a bridge request must not initiate another bridge conversation as part of that answer.

## Directory scope and availability

One service-level policy selects providers, history eligibility, and optional project filters. The default history window is 14 days; an explicit all-history setting includes older eligible chats. Project names organize and disambiguate the directory rather than creating per-chat MCP installations.

Subagents, execution/review sessions, archived sessions, and files outside the configured native session roots are excluded. Directory results expose metadata and bounded discovery status. An incomplete scan or page must not be presented as the complete set of matching names. Discovery begins in the background when an enabled service starts; large histories can take about a minute or longer to scan. Retry later instead of polling repeatedly.

Malformed native metadata can leave discovery incomplete even after the scan finishes. WATCH reports unverifiable metadata without exposing file contents. It does not assume a later valid title repairs an earlier unreadable record, because that record could change a chat’s identity or introduce a duplicate name. Name-based sending remains blocked while uniqueness is uncertain; an unreadable Codex name index also prevents Codex identity validation. Repeated bridge requests cannot repair this. As a workaround, stop WATCH and use the `policy` command below to narrow `--history` so old transcript files fall outside eligibility, or temporarily limit `--providers` to exclude the affected provider. These changes do not repair a malformed Codex title index; that index requires repair or regeneration through the native Codex tooling. Keep native records backed up rather than deleting them to silence the warning.

Availability is reported separately from discovery:

- **Reachable:** the current transport checks support delivery to that exact native chat.
- **Unverified:** the chat is known, but delivery to its current state has not been established. Sends are refused.
- **Unavailable:** a required transport or compatible inbound channel is missing or has changed. The result explains why.

Historical or unloaded Codex chats can appear as unverified. Listing an old transcript does not prove the desktop app can receive a queued message for it. Eligible recent Codex chats still require transport readiness and exact transcript identity checks.

Claude chats can receive shared messages through the [WATCH Claude plugin](../integrations/claude-mod/README.md) version 0.3.2 or later. Install it once for your user account, reload the intended already-open chats, and authorize agent messaging once in WATCH. Eligibility still depends on that exact chat being connected. Existing owner-only grants and manual pairings are not silently expanded; choose **Enable agent messaging** to replace an owner-only global authorization with explicit peer consent. Legacy manual pairings remain owner-only; automatic registration does not replace them.

The directory reports a compatible, authorized official-plugin connection as `claude_mod`. A chat being visible, a global plugin being installed, or the old channel being connected does not establish receiving readiness. The plugin must load in that chat, register its current native UUID and project, and maintain its connection. An outdated sender MCP adapter must also be updated and reconnected through its client's normal MCP workflow.

The older development channel stays blocked for peer delivery. Its process can switch conversations while keeping its channel alive, so a channel descriptor cannot establish the intended destination. The official plugin uses the native session identity and checks it before submitting input. It retains the documented native queue limitation: switching or clearing after the final check can affect already-queued input. Wait for pending work to settle before changing conversations.

## Tools and delivery

The MCP exposes four tools: `list_agents`, `send_message`, `get_reply`, and `list_requests`. There are no steering, interruption, enrollment, settings, or arbitrary-command tools.

The adapter generates a fresh request UUID for a new send and returns it, including in a recovery receipt when the outcome is uncertain. An explicitly supplied recovery ID must be a valid accepted version-4 UUID. An identical replay returns the original request with a duplicate indication; conflicting reuse is rejected. A retry never silently follows a renamed or replaced destination.

A queued or transport-written result does not prove the recipient acted. Busy Codex recipients receive queued messages, not steering. Reply parsing requires the exact native request and turn. Codex replies from turns mixed with native input, WATCH owner input, or steering are withheld. Uncertain turn evidence also withholds replies. These structural checks cannot prevent model-level influence from earlier peer content; owner replies carry a warning when peer input may have influenced them.

Request content expires after 24 hours. Permanent, bounded deduplication tombstones prevent expired operations from being sent again, including across service credential rotation. Old endpoint-prototype history is not exposed as shared history.

Peer capacity remains separate from the Officer's reserved capacity. Limits allow up to 100 requests per day service-wide, 10 per target per hour, and four unsettled requests in the current 15-minute window; declared sender names do not create independent quotas. Completed turns with unusable replies release their slots. A genuinely uncertain delivery may block its destination until retention cleanup. Late replies can still be recovered within retention.

## Installation and rollout

The shared bridge currently requires WATCH running on macOS with the supported native transports. Linux and Windows installation is not supported.

Use a stable WATCH installation. Generated MCP configuration contains the adapter's installation path; do not point a long-lived client configuration at a disposable worktree.

Install the optional runtime dependencies from the repository root:

```bash
npm --prefix integrations/claude ci --ignore-scripts --no-audit --no-fund
```

Initialize shared service access. The explicit `all` option includes eligible older chats; use `--history 14` for the default recent-history policy instead:

```bash
python3 scripts/watch_peer.py init --providers codex,claude --history all
```

The command reports a private credential-file path. Generate the appropriate client configuration using that path:

```bash
python3 scripts/watch_peer.py config /path/to/private/credential.json --client codex
python3 scripts/watch_peer.py config /path/to/private/credential.json --client claude
```

Codex output is a TOML MCP entry; Claude output is an MCP JSON entry. Install the appropriate entry in the intended client scope once. Preserve existing entries. Keep credential files and local registry data outside version control; configuration contains the file path, never its secret.

Inspect or revoke access with:

```bash
python3 scripts/watch_peer.py status
python3 scripts/watch_peer.py revoke CREDENTIAL_UUID
```

`rotate CREDENTIAL_UUID` revokes that credential and creates a replacement while preserving the message ledger. Update the client configuration to the replacement file. Rotation is not a message retry.

The default WATCH port is 8793. Non-default `--port` and `--state-dir` arguments precede the subcommand. Optional repeated `--allow-project` and `--deny-project` arguments on `init` set service-wide project filters; they do not create per-chat credentials.

To change discovery scope, stop WATCH first, then update the saved policy and restart WATCH:

```bash
python3 scripts/watch_peer.py policy --history 14 --deny-project /path/to/excluded-project
```

Unspecified fields stay unchanged. Supplying `--allow-project` or `--deny-project` replaces that list; use `--clear-allow-projects` or `--clear-deny-projects` to clear it. `--providers codex,claude` changes the provider selection. The command refuses changes while WATCH holds the messaging lock. Existing credentials, message history, and deduplication records remain intact. Discovery filters do not retroactively hide earlier shared requests or replies. Unlaunched requests fail on restart rather than being resent; already-launched requests keep their conservative recovery behavior.


Update WATCH before configuring the clients, preserve their existing configuration entries, and reconnect using each client's normal MCP workflow. The shared connection does not restart working chats or modify their permissions. The old per-endpoint `--peer` launcher setup is unsupported in this mode.

Disposable native tests established same-ID resume and a peer reply with the observed persisted settings intact. Further tests showed that switching conversations leaves the channel connected to the original WATCH identity while native Claude uses a different chat. The resume launcher is therefore deferred; no `--resume` option is provided.

Owner Officer messages to a WATCH-launched Claude process retain their existing behavior: they go to the conversation open in that process. After `/clear`, `/resume`, or a fork, close and relaunch before relying on station targeting. Persisted settings reload on launch; session-only grants, added directories, and previous one-off CLI options are not restored automatically. Reachability grants no permission or owner authorization.

## Delivery and limits

A peer envelope identifies its origin, labels any declared sender as unverified, quotes the message, and requests one correlated final reply. The receiver does not start a forwarding chain. Claude and Codex use their existing conversation model, sign-in, project context, tools and permissions.

WATCH reserves the shared request and native submission attempt before returning input to the plugin. Duplicate requests recover the same record; uncertain submissions are not replayed. A received/queued message is not a completed answer. Claude replies require the entire original envelope and its corresponding completed main turn with one matching `WATCH_REPLY` marker. This is not a guarantee that later native steering or mixed input was absent; Codex's separate transcript quarantine checks are not claimed for Claude.

A Claude envelope is limited to 4,000 Unicode code points, including routing instructions and JSON escaping. Some messages within the MCP's 2,000-character text limit can exceed that envelope bound. WATCH rejects them before reservation instead of truncating or submitting them.

## Rollback and privacy

Disable the specific MCP entry and revoke its service access to stop future use. Keep the updated WATCH code and its current database: this is the supported rollback. Do not restore an older message database: that would discard replay protection. Revocation cannot recall an already-authorized or provider-queued message. Keep unrelated client configuration intact.

Do not run an older WATCH version against a database containing bridge payloads. Older versions can expose peer messages as Officer history and apply owner-message semantics. Waiting 24 hours alone does not establish that every payload was removed; malformed legacy rows can survive cleanup. There is currently no supported downgrade or payload-purge command. A future downgrade procedure must verify that no peer payloads remain while preserving all deduplication ledgers and tombstones. The legacy `prune` command is not payload cleanup.

The local service assumes a trusted OS account. Its database and credential files hold symmetric secrets; a process able to read those files can impersonate service access. This is not isolation from other full-access tools running under the same account.

## Protocol references

- [Claude Code MCP configuration](https://code.claude.com/docs/en/mcp)
- [Claude channels](https://code.claude.com/docs/en/channels)
- [MCP stdio transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)
