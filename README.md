# cursor-chats-bridge

**A read-only bridge that lets Claude Code see what you're doing in Cursor — in real time and across sessions.**

[![platform: windows](https://img.shields.io/badge/platform-Windows-blue)](#requirements)
[![license: MIT](https://img.shields.io/badge/license-MIT-green)](#license)

If you use **Claude Code** and **Cursor** in parallel — for example: agent-driven edits in Cursor while planning/auditing with Claude — this skill plugs Cursor's local chat database into Claude as an MCP server, plus journals new messages every 5 minutes so Claude can answer questions like *"what was Cursor doing this morning?"* even when Claude wasn't running.

Read-only by design. Never modifies Cursor's data.

---

## What you get

Once installed, Claude Code gains seven tools (all prefixed `mcp__cursor-chats__`):

| Tool | What it does |
|---|---|
| `list_workspaces` | List every Cursor workspace seen across your chat history, with hash IDs and paths. |
| `list_chats` | List Cursor chats, optionally filtered by workspace ID, path substring, or auto via Claude's `cwd`. |
| `get_chat` | Fetch the messages of a specific chat (by composer UUID). |
| `get_active_chat` | Convenience: fetch the live messages of the currently-open Cursor chat for a workspace. |
| `search_chats` | Substring search across chat names, subtitles, and message text. |
| `get_journal` | Read the append-only journal of new messages captured by the background poller. |
| `get_journal_summary` | Aggregate stats over a time window: messages by role, conversations touched, recent text snippets. |

Plus a Windows scheduled task `ClaudeCursorChatPoller` that runs every 5 minutes, detects new messages in each workspace's active chat, and appends them to a JSON-lines journal under `~/.claude/mcp/cursor-chats/journal.ndjson`.

## Why it exists

Cursor's chat data lives locally in a SQLite KV store (`%APPDATA%\Cursor\User\globalStorage\state.vscdb`), but with two annoying constraints:

1. Only the **currently-open chat per workspace** keeps its messages stored locally — older chats are archived to Cursor's cloud, leaving only metadata.
2. There's no public API.

So a live MCP query alone isn't enough: if you ask "what did Cursor do this morning?" at 3 PM, the relevant messages may already be archived. The poller solves that by capturing messages as they appear, with workspace tagging so different Claude projects can filter cleanly.

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  Windows Task Scheduler  >>  pythonw poller.py  >>  /5 min, 24/7│
└──────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼ (read mode=ro,immutable=1)
              ┌──────────────────────────────────┐
              │  Cursor SQLite globalStorage     │ ← live, written by Cursor
              │  state.vscdb / cursorDiskKV      │
              └──────────────────┬───────────────┘
                                 │
                                 ▼ (append-only)
              ┌──────────────────────────────────┐
              │  ~/.claude/mcp/cursor-chats/     │
              │   ├─ active_snapshot.json        │
              │   ├─ journal.ndjson              │
              │   └─ poller.log     (errors)     │
              └──────────────────┬───────────────┘
                                 │
                                 ▼ (on-demand)
              ┌──────────────────────────────────┐
              │   MCP server (server.py)         │
              │   exposes 7 tools                │
              └──────────────────┬───────────────┘
                                 │
                                 ▼
                            Claude Code
```

For deeper internals (SQLite key patterns, workspace identification, edge cases), see [`references/architecture.md`](references/architecture.md).

## Requirements

- **Windows 10 / 11** (Linux/macOS not yet supported — uses `schtasks` and the Windows Cursor path)
- **Python 3.10+** with `pythonw.exe` available (silent runner for the scheduled task)
- **Claude Code CLI** on `PATH` (`claude --version` should work)
- **Cursor** installed and opened at least once (the SQLite is created on first run)

The installer checks all of these and fails fast with actionable errors if something is missing.

## Install

### As a Claude Code skill (recommended)

1. Drop the folder into your Claude Code skills directory:
   ```
   ~/.claude/skills/cursor-chats-bridge/
   ```
   On Windows: `C:\Users\<you>\.claude\skills\cursor-chats-bridge\`.

2. Restart Claude Code (or just open a new session).

3. Ask Claude something like *"install the cursor-chats bridge"* or *"set up the Claude-Cursor connection"* — the skill description is tuned to trigger on those phrasings.

4. Claude reads `SKILL.md`, runs `scripts/install.ps1`, and reports the result.

5. **Restart Claude Desktop** (full quit from the system tray) to load the MCP server.

### Manual install (no Claude needed)

If you'd rather skip the agentic step:

```powershell
powershell -ExecutionPolicy Bypass -File "C:\Users\<you>\.claude\skills\cursor-chats-bridge\scripts\install.ps1"
```

The script:
1. Verifies prerequisites (Python, `pythonw.exe`, `claude` CLI, Cursor SQLite path)
2. Copies `server.py` and `poller.py` to `~/.claude/mcp/cursor-chats/`
3. Installs the Python `mcp` package via pip if missing
4. Removes any prior `cursor-chats` MCP registration, then adds it (default scope: `local`)
5. Creates / updates the `ClaudeCursorChatPoller` scheduled task (every 5 min, silent via `pythonw.exe`)
6. Runs the poller once to seed the snapshot/journal

Re-running is safe: every step uses force-overwrite semantics. State files (`active_snapshot.json`, `journal.ndjson`) are preserved.

### Installer flags

| Flag | Effect |
|---|---|
| `-Quiet` | Suppress progress output. |
| `-NoTask` | Skip creating the scheduled task (one-shot use / debugging). |
| `-Scope local\|user\|project` | MCP registration scope. Default `local` (current project only). Use `user` for global. **Don't use `project`** — that would write to `.mcp.json` which is intended to be committed. |

## Verify

After install + Claude restart:

```powershell
claude mcp list
schtasks /Query /TN ClaudeCursorChatPoller /FO LIST
```

Both should show entries; the `claude mcp list` line should report `✓ Connected` for `cursor-chats`.

In a Claude session, you can then ask:

> *"List my Cursor workspaces."*
> *"What's the latest message from my Cursor agent?"*
> *"Summarize what I did with Cursor this morning."*

## How Claude uses it (typical patterns)

The MCP doesn't poll on its own — Claude calls the tools when it makes sense. The background poller (separate process) handles continuous capture, so journal queries answer "what happened while you weren't looking" without keeping a live conversation open.

Examples:

- **Pickup** — *"continue what I was doing with Cursor"*
  → Claude calls `get_active_chat` to fetch the live conversation, summarizes, asks where to take over.

- **Recap** — *"recap of my Cursor activity since 9 AM"*
  → Claude calls `get_journal_summary(window_minutes=N)` and walks you through what changed.

- **Cross-check** — *"is what Cursor is suggesting consistent with our plan?"*
  → Claude reads the latest Cursor messages, compares to its own context, flags discrepancies.

- **Search** — *"where did I discuss the SQL backfill with Cursor?"*
  → Claude calls `search_chats("backfill")`, then drills into a hit with `get_chat`.

## Privacy & security

- **Read-only by SQLite enforcement** (`mode=ro,immutable=1`). Even a buggy script cannot modify Cursor's data.
- **All data stays local.** The journal and snapshots are under `~/.claude/`, protected by Windows ACLs at the user-profile level. Nothing is uploaded.
- **Credentials caveat.** Your Cursor chats may contain pasted API keys, passwords, etc. The journal stores message text verbatim. If that's a concern, be selective about what you paste into Cursor, or filter the journal post-hoc.
- **MCP scope.** Default `local` means the bridge is only active in the project where you ran the installer. Use `-Scope user` to make it global.

## Uninstall

```powershell
powershell -ExecutionPolicy Bypass -File "<skill-dir>\scripts\uninstall.ps1"
```

Removes the scheduled task, unregisters the MCP from Claude, and deletes `~/.claude/mcp/cursor-chats/` by default. Pass `-KeepData` to preserve `active_snapshot.json` and `journal.ndjson`.

## Limits & known issues

- **Windows only.** macOS/Linux variants are doable (cron instead of `schtasks`, `~/Library/Application Support/Cursor/...` path on macOS) but not implemented yet.
- **Cursor schema dependency.** The bridge reads undocumented Cursor internals. If Cursor renames `cursorDiskKV` or changes the composer JSON shape between versions, the scripts may need a one-line patch. Check `poller.log` if the journal stops growing.
- **Active chat only.** Older / archived chats give metadata only. Live messages exist only for the currently-open chat per workspace.
- **No backfill.** The poller skips historical messages on first sight of a workspace (would otherwise flood the journal). Only future messages are captured.
- **Cursor must be open** for new messages to land in the SQLite. If Cursor is closed, the poller still runs cleanly but writes no new entries.

## Project layout

```
cursor-chats-bridge/
├── SKILL.md               # YAML frontmatter + Claude-facing instructions
├── README.md              # this file
├── scripts/
│   ├── server.py          # MCP server (Python, ~300 lines)
│   ├── poller.py          # Background poller (Python, ~180 lines)
│   ├── install.ps1        # Idempotent installer
│   └── uninstall.ps1      # Clean removal
└── references/
    └── architecture.md    # Deep technical doc (SQLite layout, edge cases)
```

## Contributing

Pull requests welcome — especially for:
- macOS / Linux support (cron + Library/Application Support paths)
- Schema-resilience: helpers to detect Cursor version changes early
- Optional journal rotation / compression
- Better triggering of the skill on non-French queries

## License

MIT — do whatever you want, no warranty. See [LICENSE](LICENSE).

## Disclaimer

This is a third-party tool, not affiliated with Anthropic or Cursor. It reads Cursor's local data through unofficial means and may break with future Cursor versions. Use at your own discretion, especially on machines where you handle sensitive data.
