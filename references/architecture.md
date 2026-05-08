# cursor-chats-bridge — architecture & rationale

## Cursor's local storage

Cursor stores chat data in two SQLite files (it's a VS Code fork, so the layout is VS Code-derived):

- `%APPDATA%\Cursor\User\globalStorage\state.vscdb` — global, ~30-50 MB, contains all composer (chat) data.
- `%APPDATA%\Cursor\User\workspaceStorage\<hash>\state.vscdb` — per-workspace, small, contains UI state.

The bridge only reads `globalStorage/state.vscdb`. We open it with the SQLite URI `file:...?mode=ro&immutable=1`. `immutable=1` tells SQLite the file is guaranteed not to change underneath it — which is a lie, but it makes SQLite skip locking entirely, so we never block Cursor's writes nor get blocked by them. The downside is that we may read a slightly stale view; for our purpose (sample every 5 min) that's fine.

## Key patterns in `cursorDiskKV`

The chat data lives in one table: `cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)`. Three families of keys are relevant:

| Key pattern | What it holds |
|---|---|
| `composerData:<uuid>` | One conversation's metadata: name, subtitle, list of message references (`fullConversationHeadersOnly`), files attached (`originalFileStates`), `workspaceIdentifier`, model config, dates, etc. The whole conversation summary, but **not** the actual message text. |
| `bubbleId:<composer-uuid>:<bubble-uuid>` | One message: `type` (1=user, 2=assistant), `text`, `richText`, attached code chunks, lints, tool results, etc. |
| `agentKv:blob:<sha256>` | Encrypted blobs (file content snapshots, attachments). Keys exist but values are encrypted with the composer's `blobEncryptionKey`. The bridge does NOT attempt decryption. |

## The "active chat" concept

We discovered empirically that **only one composer at a time per workspace** has its `bubbleId:*` rows stored locally. When the user switches conversations in Cursor, the previous one's bubbles get archived to Cursor's cloud, and the local rows disappear (only `composerData` remains, with the headers list).

This means:
- For an archived chat, the bridge can return metadata (name, subtitle, file list) but cannot return message text — that's cloud-only.
- For the active chat, all messages are accessible.
- Across multiple Cursor windows, each window has its own active chat (one per workspace).

## Workspace identification

Each `composerData` includes a `workspaceIdentifier` field:

```json
"workspaceIdentifier": {
  "id": "<32-hex-hash>",
  "configPath": {
    "fsPath": "c:\\projects\\my-project\\my-project.code-workspace",
    ...
  }
}
```

The `id` is a stable hash derived from the workspace path. It's the same for every chat opened in that workspace. Older chats (pre-feature) lack this field — for them we fall back to scanning `originalFileStates` URIs.

The same project on different PCs (or different paths) gets different workspace IDs. That's why the project's `CLAUDE.md` should list all known IDs for the project, so the MCP filter can match all of them.

## Why a poller in addition to live MCP queries

The MCP tools (`list_chats`, `get_active_chat`, etc.) read Cursor's SQLite at the moment of the call. That's enough for "what's Cursor doing right now?" — but it can't answer "what did Cursor say at 9 AM if I'm asking at 11 AM and the chat has since been archived" or "what happened while Claude Desktop was closed last night."

The poller fills this gap: every 5 min, it diffs against a snapshot and appends new messages to `journal.ndjson` (with `workspace_id`, `workspace_path`, `composer_id`, `role`, `text`). The journal:
- Survives chat archival (the bubble might disappear from Cursor's local store, but we've already captured its text).
- Works while Claude Desktop is closed (Task Scheduler runs `pythonw.exe` in user session, no UI needed).
- Is per-workspace tagged, so different Claude projects can filter cleanly.

## Edge cases

- **Cursor not installed** — installer warns; MCP server returns errors; poller skips silently.
- **Cursor closed** — poller still runs. If Cursor was open recently, the SQLite is still readable. New messages won't appear until Cursor is reopened.
- **Multiple Cursor windows** — each has its own workspace's active chat; the poller iterates them all and tags accordingly.
- **Composer switches mid-window** — `process_workspace` detects when `composer_id` changes and emits a `composer_switch` journal event.
- **First-ever sighting of a workspace** — emits a `workspace_first_seen` event but does NOT backfill historical messages (would flood the journal with thousands of entries). Only future ones are tracked.
- **Cursor schema change between versions** — the bridge depends on undocumented Cursor internals. If Cursor renames `cursorDiskKV` or restructures composer JSON, the scripts need updating. Symptoms: poller log entries, MCP tools returning errors.
- **Large messages** — text is capped at 5 KB per journal entry to keep the file manageable. Live MCP queries (`get_chat`, `get_active_chat`) don't apply this cap.

## Files written by the running bridge

Under `~/.claude/mcp/cursor-chats/`:

| File | Role |
|---|---|
| `server.py` | MCP server (deployed by installer) |
| `poller.py` | Background poller (deployed by installer) |
| `active_snapshot.json` | Per-workspace state from the last poller run (composer id, header count, last update timestamp) |
| `journal.ndjson` | Append-only log of message events. Grows unboundedly — manual rotation if needed (rare, ~few KB per active hour) |
| `poller.log` | Error log; only written on failure |

## Security & privacy notes

- The bridge is **read-only** by SQLite enforcement (URI mode=ro). Even a buggy script cannot modify Cursor's data.
- The journal contains chat text — including code snippets, paths, anything the user types in Cursor. The file is in `~/.claude/`, protected by Windows ACLs at the user-profile level.
- Credentials in chat (API keys, passwords) end up in the journal verbatim. If this is a concern, the user should be careful what they paste into Cursor, or the journal could be filtered (out of scope for this bridge).
- The MCP registration is `local` scope by default — no risk of leaking the bridge into other projects.
