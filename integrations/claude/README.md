# Optional Claude Code channel

This package provides WATCH's local MCP channel for **sessions started with the channel enabled**. It does not attach to arbitrary existing Claude sessions. The repository launcher supplies the session UUID, instance UUID and private state directory; installing this package does not change Claude settings or permissions.

Install optional dependencies with Node.js 20 or later:

```sh
npm ci --prefix integrations/claude --ignore-scripts --no-audit --no-fund
npm test --prefix integrations/claude
```

The MCP SDK is pinned to 1.32.2. Tests simulate Claude through the official SDK and run local HTTP requests; no model, user session or global configuration is used. Busy-session behavior remains unproven in a live Claude session.

The launcher must provide `WATCH_CLAUDE_SESSION_ID`, `WATCH_CLAUDE_INSTANCE_ID` (UUIDs), and `WATCH_CLAUDE_STATE_DIR` (an absolute directory named for the session UUID). The state directory and its immediate parent must already exist, belong to this user, and have mode 0700; every path component must be a real directory, not a symlink. The normal location is `~/.live-room/claude-channels/<sessionUUID>`.

After the MCP initialized notification, the channel exclusively reserves a private `.connection-starting` file, restores durable requests, then writes and syncs a separate temporary payload and atomically renames it to `connection.json` containing `{version:1,session_id,instance_id,port,token}` with mode 0600. WATCH connects to `127.0.0.1:<port>` using that bearer token. The descriptor has exactly one hard link from first visibility. Credentials never go to stdout. An existing discovery file is never replaced. A crash may leave stale discovery or its `.connection-starting` reservation; the launcher must verify the old process is gone before its own explicit recovery/cleanup. Normal shutdown removes only this instance's discovery and keeps request files.

`POST /requests` accepts `{session_id,instance_id,request_id,content}`. `GET /requests/<requestUUID>` returns `{session_id,instance_id,request_id,state,reply,error}`. Host must exactly match the loopback listener, and browser Origin headers are rejected. Raw request bodies are limited to 16 KiB, content to 4,000 Unicode codepoints, and replies to 16,000 codepoints. No notification is sent until its exact request payload has been atomically saved and synced. Duplicate identical payloads return stored state; conflicting payloads are rejected.

`transport_written` means only that the notification was written to MCP, not that Claude received or acted on it. `answered` requires the matching reply tool call, persisted before acknowledgment. `unknown` is retained after uncertain writes or restart; it is never automatically re-notified. Records keep their original instance identity across restart. Requests are limited to one unresolved request per session, ten new requests per minute, and 256 records retained for one day. Unresolved requests are not evicted before retention expires.

Notifications preserve the owner's content exactly. Explicit work instructions authorize that work under existing permissions; status questions ask for information. The channel adds no tools beyond `reply`, performs no permission relay, and does not auto-allow tools.
