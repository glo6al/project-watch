# WATCH Navigation for Antigravity

This user-level native extension lets WATCH request opening an existing
Antigravity desktop chat. It never sends a message, starts work, creates a chat,
changes permissions or calls a model. It uses a small adapter to the installed
native UI navigation contract; it does not replace the vendor SDK.

On macOS with Antigravity 2.31.2, choose **Connection → Install connector**
on an Antigravity desk in WATCH. This installs the reviewed package once as the global `watch-navigation`
sidecar and explicitly authorizes navigation to existing desktop chats. There
are no per-chat installations, target lists or pairing codes. Enable **WATCH
Navigation** once in Antigravity's **Customizations → Installed**. In any existing
chat, open **Extensions → WATCH Navigation** once. You can hide the
panel after it connects. WATCH offers Open only while a live connection exists.
If it becomes unavailable, reopen that panel. Cold startup and retention after
many chat switches are not guaranteed. No hidden flags or app restart are used.

The command-line equivalent is `python3 scripts/install_antigravity_open.py install`;
use `status`, `disconnect` or `remove` for the other lifecycle actions.
Disconnect revokes navigation immediately. A loaded panel can reconnect when you
connect again in WATCH; the idle extension gains no navigation authority. An
in-flight native navigation cannot be recalled. To remove or update installed
files, first turn WATCH Navigation off in Antigravity. Remove waits for the
sidecar to stop and deletes only verified owned files; changed or foreign files
are preserved. WATCH never writes Antigravity's shared configuration, so native
settings and concurrent native edits remain untouched. A private ownership stamp
is retained to recognize a later reinstall.

## Local protection

Owner authorization and ephemeral connection data live under
`~/.live-room/antigravity-open/` in owner-only directories and files. The owner
capability is distinct from both WATCH's API key and the native UI capability.
It never appears in URLs, the extension page, public status, logs or model
context. The native UI uses Antigravity's own token bootstrap. Its proxy can
forward a canonical `localhost:<port>` Host and omit Origin; UI requests always
require the separate native capability and reject any other supplied Origin.
Owner routes retain exact broker Host checks and reject browser Origin headers.

Every request checks the local authorization, existing primary desktop identity
and current process binding. CLI, IDE, duplicate identities and unsupported
platforms or versions do not qualify. Metadata checks read only the exact UUID's
primary database row, never conversation text or summaries. Runtime verification
uses public process, application and listener metadata, never arguments or
credentials. The standard macOS foreground request addresses the verified
running app without a URL, prompt or new-instance flag.

## Delivery limits

Commands have an immutable five-second expiry, one pending command and bounded
terminal retention. Claiming consumes a command before responding; uncertain
requests never replay. Repeated request IDs retain their originally accepted
expiry. Old requests remain expired after retention ends. Multiple loaded panels
share one active lease; other panels wait passively and cannot dispatch. Revoking
or losing a lease prevents further admission and claim.

The native contract has no selection acknowledgment. A successful response means
**Opening requested**, not independently confirmed selection or foreground.
WATCH never interprets that response as completed work. Process-exit races remain
possible. Reconnect when the current generation or its native process changes.
