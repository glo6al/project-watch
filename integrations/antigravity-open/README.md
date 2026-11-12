# Antigravity existing-chat navigation prototype

This is a disposable connection proof, not an enabled WATCH feature. It does not
change WATCH's Open button, install anything, send prompts, start conversations,
call models, interrupt work, enable remote debugging, or expose arbitrary RPC.

The native sidecar manifest uses the installed app's `has_web_ui` and
`ui_config.views` fields with `SIDECAR_UI_ENTRYPOINT_AUX_PANE`. The reviewed files
must be loaded through Antigravity's legitimate global sidecar mechanism under
the exact sidecar ID `watch-open-prototype`. Enabling a background process alone
does not prove that its UI has loaded. This temporary proof uses the native
conversation-scoped Extensions panel; it does not establish a global UI
connection. The native global sidebar surface is separately gated and has not
been demonstrated.

The frontend is **our small adapter to the native sidecar navigation contract**,
not a vendored or replacement SDK. The installed SDK uses the same
`toggle-conversation` message with a `conversationId`; Antigravity accepts it only
from a sidecar iframe it owns. Our message uses the pinned parent origin and
contains no credentials, arbitrary URLs, method names, or options.

## Private test preparation

After independent review, the operator verifies the exact native Hub origin and
selects one already existing, visible, unarchived, idle App conversation for this
temporary AUX-pane test.
Their UUIDs and that origin go in an owner-only `config.json` beneath the private
`~/.live-room/antigravity-open-prototype/` directory. The directory must have mode
700 and the configuration mode 600. This file must never enter the repository.

The private configuration has exactly `allowed_ids` (one or two canonical UUIDs)
and `parent_origin` (the observed `https://127.0.0.1:<port>` origin). The native Hub
HTTPS port is verified directly; it is not guessed from a language-server port.
The operator's unarchived check is a test prerequisite, not a continuous archive
state guarantee. Each dispatch rechecks the exact UUID's primary metadata row in
the App's own database, without reading conversation bodies or other providers.

Antigravity supplies the sidecar web port and native UI capability. The native UI
must load with its normal authenticated token bootstrap. Missing or incompatible
bootstrap, parent identity, or plugin origin leaves it disconnected; there is no
injected-frame or permissive browser fallback. The initial page accepts only the
native `token` query and optional `conversationId`; a supplied conversation ID
must be canonical and registered in the private allowlist. Duplicate or unknown
query fields are rejected.

Native UI routes accept the desktop proxy's canonical `localhost:<port>` Host;
owner routes still require the broker's exact listening address and port.
The native proxy can omit Origin. UI requests require their separate capability
in all cases and reject any supplied Origin other than the exact plugin origin.

At startup, the broker creates a separate ephemeral owner capability in a mode
600 `connection.json` beside that configuration. Only a private diagnostic test
client reads it. It never reaches HTML, JavaScript, URLs, native messages, or logs.
The native UI has its own capability and cannot submit owner navigation commands.

## What the proof must demonstrate

1. Open the legitimate Extensions panel in the registered conversation and
   verify that the lease is connected.
2. Navigate to Home, then submit one explicit owner request for the registered
   existing conversation while the loaded connector remains available.
3. Observe that exact conversation in the native desktop window, without a new
   conversation, prompt, send, or model run.
4. Hide the connector view, navigate normally, and repeat. Observe whether this
   loaded conversation-scoped frame stays connected.
5. Verify background/foreground behavior. Global availability, cold startup, and
   eviction after visiting many conversations remain unproven by this bounded
   test. Do not restart the app or disturb active work to broaden the proof.
6. Disconnect and remove the disposable sidecar and private test files through
   the operator's reviewed procedure. No command survives a broker restart.

The broker retains at most 64 request identities in memory and permits one
unresolved navigation at a time. A UI lease lasts eight seconds; a command lasts
five. A poll consumes a command before writing the response. Loss of a claimed
command produces `unknown`, never a replay. Same-request retries return the old
state. A fresh owner action requires a fresh request ID.

`called` means that the native navigation message was issued. The native contract
has no selection acknowledgment, so it **does not confirm which chat is visible**.
The live test must verify that separately. This temporary AUX-pane proof does not
authorize restoring the production WATCH Open button or claiming a global
connection; those require a separate reviewed lifecycle result.
