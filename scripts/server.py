"""Cursor Chats MCP server — read-only access to Cursor's local chat database.

Cursor stores its chats in a SQLite KV store at:
  %APPDATA%/Cursor/User/globalStorage/state.vscdb
  table: cursorDiskKV (key TEXT, value BLOB JSON)

Key patterns:
  composerData:<uuid>            → metadata of one conversation
  bubbleId:<composer>:<bubble>   → one message (only present for the *active* chat)
  agentKv:blob:<sha256>          → encrypted blobs (file content snapshots)

Each composerData has a `workspaceIdentifier` field (`{id, configPath.fsPath}`)
that pins the chat to a specific Cursor workspace. Older chats may lack it; we
fall back to scanning `originalFileStates` URIs.

Read-only via SQLite URI mode=ro,immutable=1 — never touches the file even if
Cursor is running.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

DB = Path.home() / "AppData" / "Roaming" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
BASE = Path.home() / ".claude" / "mcp" / "cursor-chats"
JOURNAL = BASE / "journal.ndjson"
SNAPSHOT = BASE / "active_snapshot.json"

mcp = FastMCP("cursor-chats")


# ---------- low-level helpers ----------

def _q(sql: str, params: tuple = ()) -> list[tuple]:
    if not DB.exists():
        raise FileNotFoundError(f"Cursor DB not found: {DB}")
    uri = f"file:{DB.as_posix()}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _ts(ms: int | None) -> str:
    if not ms:
        return ""
    try:
        return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError):
        return ""


def _bubble_count(cid: str) -> int:
    try:
        return _q(
            "SELECT COUNT(*) FROM cursorDiskKV WHERE key LIKE ?",
            (f"bubbleId:{cid}:%",),
        )[0][0]
    except Exception:
        return 0


def _composer_workspace(d: dict) -> tuple[str | None, str | None]:
    """Extract (workspace_id, workspace_fsPath) from a composer dict, or (None, None)."""
    wsi = d.get("workspaceIdentifier") or {}
    if not isinstance(wsi, dict):
        return (None, None)
    cp = wsi.get("configPath") or {}
    fs = cp.get("fsPath") if isinstance(cp, dict) else None
    return (wsi.get("id"), fs)


_HASH_RE = re.compile(r"^[a-f0-9]{32}$", re.IGNORECASE)


def _matches(d: dict, *, workspace_id: str = "", path_filter: str = "") -> bool:
    """True if the composer matches the workspace_id (exact) or path_filter (substring)
    against either workspace path OR file URIs. Empty strings = no filter."""
    if not workspace_id and not path_filter:
        return True
    ws_id, ws_path = _composer_workspace(d)
    if workspace_id:
        if ws_id and ws_id.lower() == workspace_id.lower():
            return True
    if path_filter:
        pf = path_filter.lower()
        if ws_path and pf in ws_path.lower():
            return True
        # Fallback: match against file URIs
        for u in (d.get("originalFileStates") or {}).keys():
            if pf in u.lower():
                return True
    return False


def _auto_resolve(workspace: str) -> tuple[str, str]:
    """Resolve `workspace` argument into (workspace_id, path_filter).

    Empty string → use os.getcwd() as path_filter.
    32-hex → workspace_id.
    Otherwise → path_filter (substring).
    """
    if not workspace:
        return ("", os.getcwd())
    if _HASH_RE.match(workspace):
        return (workspace, "")
    return ("", workspace)


# ---------- tools ----------

@mcp.tool()
def list_workspaces() -> str:
    """List all Cursor workspaces seen across composers, with their hash IDs and paths.

    Use this to find the workspace_id(s) corresponding to your project — then
    pass it to the other tools via the `workspace` parameter.
    """
    rows = _q("SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'")
    seen: dict[str, dict] = {}
    no_ws = 0
    total = 0
    for k, v in rows:
        if v is None:
            continue
        try:
            d = json.loads(v)
        except Exception:
            continue
        total += 1
        ws_id, ws_path = _composer_workspace(d)
        if not ws_id:
            no_ws += 1
            continue
        cid = k.split(":", 1)[1]
        info = seen.setdefault(ws_id, {
            "workspace_id": ws_id,
            "paths": set(),
            "chat_count": 0,
            "active_chats": 0,
            "last_activity": "",
            "sample_names": [],
        })
        if ws_path:
            info["paths"].add(ws_path)
        info["chat_count"] += 1
        if _bubble_count(cid) > 0:
            info["active_chats"] += 1
        ts = _ts(d.get("lastUpdatedAt", 0))
        if ts > info["last_activity"]:
            info["last_activity"] = ts
        if len(info["sample_names"]) < 3 and d.get("name"):
            info["sample_names"].append(d.get("name"))
    out = []
    for info in seen.values():
        info["paths"] = sorted(info["paths"])
        out.append(info)
    out.sort(key=lambda i: i["last_activity"], reverse=True)
    return json.dumps({
        "total_composers": total,
        "with_workspace_id": total - no_ws,
        "without_workspace_id": no_ws,
        "workspaces": out,
    }, indent=2, ensure_ascii=False)


@mcp.tool()
def list_chats(
    workspace: Annotated[
        str,
        Field(
            description=(
                "Filter: empty = auto (uses Claude's cwd as path_filter); "
                "32-hex = workspace_id (exact match); otherwise = path substring filter."
            ),
        ),
    ] = "",
    limit: Annotated[int, Field(description="Max chats to return", ge=1, le=200)] = 20,
) -> str:
    """List Cursor chats matching a workspace filter, sorted by last update.

    Note: only the currently-active chat keeps its messages stored locally
    (`local_bubbles>0`). Archived chats give metadata only.
    """
    workspace_id, path_filter = _auto_resolve(workspace)
    rows = _q("SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'")
    chats: list[dict] = []
    for k, v in rows:
        if v is None:
            continue
        try:
            d = json.loads(v)
        except Exception:
            continue
        if not _matches(d, workspace_id=workspace_id, path_filter=path_filter):
            continue
        cid = k.split(":", 1)[1]
        ws_id, ws_path = _composer_workspace(d)
        chats.append({
            "composerId": cid,
            "name": d.get("name") or "(untitled)",
            "subtitle": d.get("subtitle") or "",
            "lastUpdatedAt": _ts(d.get("lastUpdatedAt", 0)),
            "createdAt": _ts(d.get("createdAt", 0)),
            "status": d.get("status") or "",
            "mode": d.get("unifiedMode") or "",
            "model": (d.get("modelConfig") or {}).get("modelName", ""),
            "header_count": len(d.get("fullConversationHeadersOnly", [])),
            "local_bubbles": _bubble_count(cid),
            "workspace_id": ws_id,
            "workspace_path": ws_path,
        })
    chats.sort(key=lambda c: c["lastUpdatedAt"], reverse=True)
    return json.dumps({
        "filter_applied": {"workspace_id": workspace_id, "path_filter": path_filter},
        "chats": chats[:limit],
    }, indent=2, ensure_ascii=False)


def _fetch_chat(composer_id: str, max_messages: int, text_only: bool) -> str:
    rows = _q(
        "SELECT value FROM cursorDiskKV WHERE key = ?",
        (f"composerData:{composer_id}",),
    )
    if not rows or rows[0][0] is None:
        return json.dumps({"error": f"Chat {composer_id} not found"})
    composer = json.loads(rows[0][0])
    headers = composer.get("fullConversationHeadersOnly", [])
    ws_id, ws_path = _composer_workspace(composer)

    collected: list[dict] = []
    for h in reversed(headers):
        if len(collected) >= max_messages:
            break
        bid = h.get("bubbleId", "")
        if not bid:
            continue
        br = _q(
            "SELECT value FROM cursorDiskKV WHERE key = ?",
            (f"bubbleId:{composer_id}:{bid}",),
        )
        if not br or br[0][0] is None:
            continue
        try:
            b = json.loads(br[0][0])
        except Exception:
            continue
        bt = b.get("type")
        msg = {
            "bubbleId": bid,
            "role": "user" if bt == 1 else "assistant" if bt == 2 else f"type{bt}",
            "text": b.get("text") or "",
        }
        if not text_only:
            msg["attachedCodeChunks"] = b.get("attachedCodeChunks") or []
            msg["toolResults"] = b.get("toolResults") or []
            msg["suggestedCodeBlocks"] = b.get("suggestedCodeBlocks") or []
            msg["lints"] = b.get("lints") or []
        collected.append(msg)
    collected.reverse()

    return json.dumps({
        "composerId": composer_id,
        "name": composer.get("name"),
        "subtitle": composer.get("subtitle"),
        "lastUpdatedAt": _ts(composer.get("lastUpdatedAt", 0)),
        "workspace_id": ws_id,
        "workspace_path": ws_path,
        "header_count": len(headers),
        "returned": len(collected),
        "note": "" if collected else "No local bubbles — this chat is archived/cloud-only.",
        "messages": collected,
    }, indent=2, ensure_ascii=False)


@mcp.tool()
def get_chat(
    composer_id: Annotated[str, Field(description="UUID of the chat (from list_chats)")],
    max_messages: Annotated[
        int, Field(description="Max messages to return (most recent window)", ge=1, le=500)
    ] = 50,
    text_only: Annotated[
        bool,
        Field(description="If True, only return text content (skip code chunks, lints, tool results)"),
    ] = True,
) -> str:
    """Fetch messages from a Cursor chat.

    Messages returned in chronological order (oldest first within the requested window).
    role: 'user' (type=1) or 'assistant' (type=2).
    """
    return _fetch_chat(composer_id, max_messages, text_only)


@mcp.tool()
def get_active_chat(
    workspace: Annotated[
        str,
        Field(description="Workspace filter (id, path substring, or empty for auto via cwd)"),
    ] = "",
    max_messages: Annotated[int, Field(description="Max messages to return", ge=1, le=200)] = 30,
    text_only: Annotated[bool, Field(description="If True, skip code/lint/tool data")] = True,
) -> str:
    """Fetch live messages of the currently-active Cursor chat for the given workspace.

    Picks the most-recently-updated chat with locally-stored bubbles that matches
    the filter. Default = auto-detect workspace from Claude's cwd.
    """
    workspace_id, path_filter = _auto_resolve(workspace)
    rows = _q("SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'")
    candidates: list[tuple] = []
    for k, v in rows:
        if v is None:
            continue
        try:
            d = json.loads(v)
        except Exception:
            continue
        if not _matches(d, workspace_id=workspace_id, path_filter=path_filter):
            continue
        cid = k.split(":", 1)[1]
        if _bubble_count(cid) == 0:
            continue
        candidates.append((d.get("lastUpdatedAt", 0) or 0, cid))
    if not candidates:
        return json.dumps({
            "error": f"No active chat found for filter (workspace_id='{workspace_id}', path='{path_filter}').",
        })
    candidates.sort(reverse=True)
    _, cid = candidates[0]
    return _fetch_chat(cid, max_messages, text_only)


@mcp.tool()
def search_chats(
    query: Annotated[
        str,
        Field(description="Substring to search in chat names, subtitles, and message text (case-insensitive)"),
    ],
    workspace: Annotated[
        str, Field(description="Workspace filter (id / path / auto)")
    ] = "",
    limit: Annotated[int, Field(description="Max matches", ge=1, le=100)] = 20,
) -> str:
    """Search across Cursor chats. Searches chat names, subtitles, and message text
    (only chats with locally-stored bubbles for message-text search).
    """
    workspace_id, path_filter = _auto_resolve(workspace)
    q = query.lower()
    matches: list[dict] = []
    rows = _q("SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'")
    for k, v in rows:
        if v is None:
            continue
        try:
            d = json.loads(v)
        except Exception:
            continue
        if not _matches(d, workspace_id=workspace_id, path_filter=path_filter):
            continue
        cid = k.split(":", 1)[1]
        name = d.get("name") or ""
        subtitle = d.get("subtitle") or ""
        match_in_meta = q in name.lower() or q in subtitle.lower()
        bubble_hits: list[dict] = []
        if not match_in_meta:
            br = _q(
                "SELECT key, value FROM cursorDiskKV WHERE key LIKE ?",
                (f"bubbleId:{cid}:%",),
            )
            for bk, bv in br:
                if bv is None:
                    continue
                try:
                    b = json.loads(bv)
                except Exception:
                    continue
                t = b.get("text") or ""
                if q in t.lower():
                    idx = t.lower().find(q)
                    snippet = t[max(0, idx - 50): idx + len(q) + 50]
                    bubble_hits.append({
                        "bubbleId": bk.split(":")[-1],
                        "role": "user" if b.get("type") == 1 else "assistant",
                        "snippet": "..." + snippet + "...",
                    })
                    if len(bubble_hits) >= 3:
                        break
        if match_in_meta or bubble_hits:
            ws_id, ws_path = _composer_workspace(d)
            matches.append({
                "composerId": cid,
                "name": name,
                "subtitle": subtitle,
                "lastUpdatedAt": _ts(d.get("lastUpdatedAt", 0)),
                "workspace_id": ws_id,
                "workspace_path": ws_path,
                "matched_in_meta": match_in_meta,
                "bubble_matches": bubble_hits,
            })
        if len(matches) >= limit:
            break
    matches.sort(key=lambda m: m["lastUpdatedAt"], reverse=True)
    return json.dumps(matches, indent=2, ensure_ascii=False)


# ---------- journal tools (fed by background poller) ----------

def _read_journal(
    since: str = "",
    limit: int = 200,
    workspace_id: str = "",
    path_filter: str = "",
) -> list[dict]:
    if not JOURNAL.exists():
        return []
    out: list[dict] = []
    pf = path_filter.lower() if path_filter else ""
    with JOURNAL.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            if since and (e.get("ts") or "") <= since:
                continue
            if workspace_id and (e.get("workspace_id") or "").lower() != workspace_id.lower():
                continue
            if pf and pf not in (e.get("workspace_path") or "").lower():
                continue
            out.append(e)
    return out[-limit:]


@mcp.tool()
def get_journal(
    since: Annotated[
        str, Field(description="ISO 8601 UTC timestamp; only entries strictly after this. Empty = all.")
    ] = "",
    workspace: Annotated[
        str, Field(description="Workspace filter (id / path / auto). Empty = all workspaces.")
    ] = "",
    limit: Annotated[int, Field(description="Max entries (newest kept)", ge=1, le=2000)] = 200,
) -> str:
    """Return Cursor chat journal entries (events appended by the background poller every 5 min).

    Each entry has 'ts', 'event' ('message' | 'composer_switch' | 'poller_started'),
    'workspace_id', 'workspace_path', and event-specific fields.
    """
    workspace_id, path_filter = _auto_resolve(workspace) if workspace else ("", "")
    if not JOURNAL.exists():
        if SNAPSHOT.exists():
            return json.dumps({
                "warning": "Journal empty — poller has run but no message events yet.",
                "snapshot": json.loads(SNAPSHOT.read_text(encoding="utf-8")),
                "entries": [],
            }, indent=2, ensure_ascii=False)
        return json.dumps({
            "error": "Journal not initialized. The background poller hasn't run yet — check Task Scheduler.",
            "expected_path": str(JOURNAL),
        }, indent=2, ensure_ascii=False)
    entries = _read_journal(since=since, limit=limit, workspace_id=workspace_id, path_filter=path_filter)
    return json.dumps({
        "filter_applied": {"since": since, "workspace_id": workspace_id, "path_filter": path_filter},
        "entries": entries,
    }, indent=2, ensure_ascii=False)


@mcp.tool()
def get_journal_summary(
    window_minutes: Annotated[
        int, Field(description="Look back this many minutes from now (UTC)", ge=1, le=10080)
    ] = 60,
    workspace: Annotated[
        str, Field(description="Workspace filter (id / path / auto). Empty = workspace from cwd.")
    ] = "",
    include_recent_messages: Annotated[
        int, Field(description="Include this many most-recent message texts in the summary", ge=0, le=50)
    ] = 5,
) -> str:
    """Summary of Cursor activity in a time window (default: last hour).

    Counts by role, conversations touched, and (optionally) the last messages verbatim.
    Filters by workspace; default uses Claude's cwd to auto-pick the workspace.
    """
    workspace_id, path_filter = _auto_resolve(workspace)
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=window_minutes)).isoformat(timespec="seconds")
    entries = _read_journal(since=cutoff, limit=2000, workspace_id=workspace_id, path_filter=path_filter)

    by_role: dict[str, int] = {}
    by_event: dict[str, int] = {}
    composers: dict[str, dict] = {}
    msg_entries: list[dict] = []

    for e in entries:
        ev = e.get("event") or "unknown"
        by_event[ev] = by_event.get(ev, 0) + 1
        if ev == "message":
            r = e.get("role", "other")
            by_role[r] = by_role.get(r, 0) + 1
            cid = e.get("composer_id") or "?"
            c = composers.setdefault(cid, {
                "name": e.get("composer_name"),
                "first_ts": e.get("ts"),
                "last_ts": e.get("ts"),
                "message_count": 0,
            })
            c["last_ts"] = e.get("ts")
            c["message_count"] += 1
            msg_entries.append(e)

    snap = None
    if SNAPSHOT.exists():
        try:
            snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        except Exception:
            pass

    return json.dumps({
        "window_minutes": window_minutes,
        "cutoff_utc": cutoff,
        "filter_applied": {"workspace_id": workspace_id, "path_filter": path_filter},
        "total_events": len(entries),
        "by_event": by_event,
        "by_role": by_role,
        "composers_touched": composers,
        "recent_messages": [
            {
                "ts": m.get("ts"),
                "role": m.get("role"),
                "composer_name": m.get("composer_name"),
                "text": (m.get("text") or "")[:1500],
            }
            for m in msg_entries[-include_recent_messages:]
        ],
        "poller_snapshot": snap,
    }, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run()
