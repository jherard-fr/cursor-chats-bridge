---
name: cursor-chats-bridge
description: Install or update the Claude-Cursor chat bridge on a Windows PC — a Python MCP server that exposes Cursor's local chat database (read-only) to Claude, plus a background poller that journals new messages every 5 min via Task Scheduler. Use this skill whenever the user wants to set up the Cursor connection on a new machine, re-install / upgrade the bridge after changes, share the setup with a colleague, or troubleshoot the cursor-chats MCP. Triggers include "configure cursor chat bridge", "install cursor-chats", "set up Claude Cursor connection", "I want Claude to see my Cursor agent conversations", "reinstall the cursor MCP", or any time the user moves to a new PC and wants to restore visibility into their Cursor chats. Windows-only (uses schtasks + Cursor's %APPDATA% layout).
---

# cursor-chats-bridge

Installs (or updates) a read-only bridge that lets Claude observe what the user is doing with Cursor on the same machine. Two artifacts are deployed under `~/.claude/mcp/cursor-chats/`:

- **`server.py`** — an MCP server exposing 7 tools (`list_workspaces`, `list_chats`, `get_chat`, `get_active_chat`, `search_chats`, `get_journal`, `get_journal_summary`). It reads Cursor's SQLite KV store (`%APPDATA%\Cursor\User\globalStorage\state.vscdb`) in `mode=ro,immutable=1`, never modifying it.
- **`poller.py`** — a small script triggered every 5 minutes by Windows Task Scheduler. It detects each Cursor workspace's currently-active chat (the one that has its bubbles stored locally), diff'es against a snapshot, and appends new messages to a JSON-lines journal — so Claude can answer "what did Cursor do this morning?" even if Claude Desktop wasn't open at the time.

The bundled `install.ps1` does the deployment idempotently. Re-running is safe: it overwrites scripts with the bundled versions, re-registers the MCP, and re-creates the scheduled task with `/F`.

## Why this exists

Without the bridge, Claude has no visibility into the user's parallel work in Cursor. With the bridge:
- The user can ask "what's Cursor working on right now?" → Claude calls `get_active_chat`.
- The user can switch projects/PCs without losing trace of what was discussed → the journal is per-workspace and tagged.
- Claude can cross-check Cursor's plan against its own → useful for real-money / production-sensitive code.

The poller is needed in addition to live MCP queries because Cursor only stores message bubbles locally for the *currently-open* chat — older chats are cloud-only. The journal captures messages as they happen, so they outlive the active-chat window.

## What to do when this skill triggers

1. **Verify the user is on Windows.** If not, explain the skill is Windows-only (uses `schtasks` and the Windows Cursor path), and offer to help adapt the scripts for macOS/Linux as a separate task.

2. **Run the installer.** From the skill's `scripts/` directory:

   ```powershell
   powershell -ExecutionPolicy Bypass -File install.ps1
   ```

   Or from anywhere with the absolute path:

   ```powershell
   powershell -ExecutionPolicy Bypass -File "<skill-dir>\scripts\install.ps1"
   ```

   The installer goes through six steps and prints `==> ...` headers for each. If a step fails it raises a clear error — fix the underlying issue (missing Python, missing `claude` CLI) and re-run; it picks up from a clean state because every step is idempotent.

3. **Optional flags** for the installer:
   - `-Quiet` — suppress progress output
   - `-NoTask` — skip the scheduled task (for debugging or one-off testing)
   - `-Scope local|user|project` — MCP registration scope (default: `local`, current project only). Use `user` if the user wants the bridge active across all their Claude projects on this PC. **Don't** use `project` — that would put the registration into a `.mcp.json` checked into git.

4. **After successful install, tell the user**:
   - **Restart Claude Desktop** (full quit from the system tray, not just close-window) so the new MCP server is loaded.
   - The tools `mcp__cursor-chats__*` will be available after restart.
   - The poller has run once; the next scheduled run is within 5 min.
   - Cursor should be open for the poller to find an active chat. If Cursor is closed, the poller still runs cleanly but writes no journal entries.

5. **Verify** with:

   ```powershell
   claude mcp list
   schtasks /Query /TN ClaudeCursorChatPoller /FO LIST
   ```

   Both should show the new entries. The MCP listing should report `✓ Connected` for `cursor-chats`.

## Updating an existing install

Same procedure — just re-run `install.ps1`. It overwrites `server.py` / `poller.py` with the bundled versions, re-registers the MCP (so the latest `server.py` is live), and re-creates the scheduled task with `/F`. The `active_snapshot.json` and `journal.ndjson` are preserved (the installer doesn't touch state files), so journal history survives upgrades.

## Uninstalling

The bundled `uninstall.ps1` removes the scheduled task, unregisters the MCP, and deletes `~/.claude/mcp/cursor-chats/` by default:

```powershell
powershell -ExecutionPolicy Bypass -File uninstall.ps1            # full removal
powershell -ExecutionPolicy Bypass -File uninstall.ps1 -KeepData  # leave snapshot/journal
```

Always confirm with the user before running uninstall — the journal contains chat history they may want to keep.

## Sharing with a colleague

The whole skill folder (`SKILL.md` + `scripts/`) is self-contained. A colleague can drop it under their `~/.claude/skills/cursor-chats-bridge/` and trigger this skill on their machine. The installer is the same regardless of who runs it.

## Architecture details (read if troubleshooting)

For why we use SQLite key patterns `composerData:<uuid>` and `bubbleId:<composer>:<bubble>`, why the journal is needed in addition to live queries, how `workspaceIdentifier` enables per-project filtering, and edge cases (composers without `workspaceIdentifier`, archived chats, Cursor schema changes) — see `references/architecture.md`.
