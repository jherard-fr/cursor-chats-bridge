"""Background poller for Cursor chats — workspace-agnostic.

Runs every 5 min via Windows Task Scheduler. Tracks the *active* chat (the one
with locally-stored bubbles) for *every* Cursor workspace, and journals new
messages with workspace tagging so consumers can filter per-project later.

Files written under ~/.claude/mcp/cursor-chats/:
- active_snapshot.json  → per-workspace state (composer id, header count, …)
- journal.ndjson        → append-only event log (each entry has workspace_id/path)
- poller.log            → error log
"""
from __future__ import annotations

import json
import sqlite3
import sys
import traceback
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

BASE = Path.home() / ".claude" / "mcp" / "cursor-chats"
DB = Path.home() / "AppData" / "Roaming" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
SNAPSHOT = BASE / "active_snapshot.json"
JOURNAL = BASE / "journal.ndjson"
LOG = BASE / "poller.log"

# Per-message text cap (avoid blowing up the journal on huge pasted blocks).
MAX_TEXT_BYTES = 5000

# Composers without workspaceIdentifier are bucketed under this synthetic ID.
NO_WS_ID = "_no_workspace"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log_error(exc: BaseException) -> None:
    BASE.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{now_iso()}] {type(exc).__name__}: {exc}\n")
        f.write(traceback.format_exc())
        f.write("\n")


def load_snapshot() -> dict:
    if SNAPSHOT.exists():
        try:
            return json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_snapshot(d: dict) -> None:
    BASE.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")


def journal_append(entry: dict) -> None:
    BASE.mkdir(parents=True, exist_ok=True)
    with JOURNAL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def workspace_of(d: dict) -> tuple[str, str | None]:
    """(workspace_id, fsPath) — falls back to NO_WS_ID."""
    wsi = d.get("workspaceIdentifier") or {}
    if not isinstance(wsi, dict):
        return (NO_WS_ID, None)
    cp = wsi.get("configPath") or {}
    fs = cp.get("fsPath") if isinstance(cp, dict) else None
    return (wsi.get("id") or NO_WS_ID, fs)


def fetch_bubble(conn: sqlite3.Connection, cid: str, bid: str) -> dict | None:
    row = conn.execute(
        "SELECT value FROM cursorDiskKV WHERE key = ?",
        (f"bubbleId:{cid}:{bid}",),
    ).fetchone()
    if not row or row[0] is None:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None


def truncate_text(s: str) -> str:
    if not s:
        return ""
    encoded = s.encode("utf-8")
    if len(encoded) <= MAX_TEXT_BYTES:
        return s
    return encoded[:MAX_TEXT_BYTES].decode("utf-8", errors="ignore") + "...[truncated]"


def find_active_per_workspace(conn: sqlite3.Connection) -> dict[str, tuple[str, dict, str | None]]:
    """For each workspace seen across composers, pick the most-recently-updated
    composer that has locally-stored bubbles.

    Returns dict[workspace_id] = (composer_id, composer_data, fsPath).
    """
    rows = conn.execute(
        "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
    ).fetchall()
    by_ws: dict[str, list[tuple[int, str, dict, str | None]]] = defaultdict(list)
    for k, v in rows:
        if v is None:
            continue
        try:
            d = json.loads(v)
        except Exception:
            continue
        cid = k.split(":", 1)[1]
        bcount = conn.execute(
            "SELECT COUNT(*) FROM cursorDiskKV WHERE key LIKE ?",
            (f"bubbleId:{cid}:%",),
        ).fetchone()
        if not bcount or bcount[0] == 0:
            continue
        ws_id, fs = workspace_of(d)
        by_ws[ws_id].append((d.get("lastUpdatedAt", 0) or 0, cid, d, fs))
    result: dict[str, tuple[str, dict, str | None]] = {}
    for ws_id, lst in by_ws.items():
        lst.sort(reverse=True)
        _, cid, d, fs = lst[0]
        result[ws_id] = (cid, d, fs)
    return result


def process_workspace(
    conn: sqlite3.Connection,
    ws_id: str,
    cid: str,
    composer: dict,
    fs_path: str | None,
    snapshot: dict,
) -> dict:
    """Emit journal entries for new messages in this workspace's active chat.

    Returns the new state dict for this workspace to store in the snapshot.
    """
    headers = composer.get("fullConversationHeadersOnly", []) or []
    prev_state = snapshot.get("workspaces", {}).get(ws_id, {})
    prev_cid = prev_state.get("composer_id")
    prev_count = int(prev_state.get("header_count", 0)) if prev_cid == cid else 0

    if prev_cid and prev_cid != cid:
        # Composer switched within this workspace
        journal_append({
            "ts": now_iso(),
            "event": "composer_switch",
            "workspace_id": ws_id,
            "workspace_path": fs_path,
            "from_composer_id": prev_cid,
            "to_composer_id": cid,
            "to_name": composer.get("name"),
            "to_subtitle": composer.get("subtitle"),
        })
        prev_count = 0
    elif not prev_cid:
        # First-ever sighting of this workspace — record but skip historical backfill
        journal_append({
            "ts": now_iso(),
            "event": "workspace_first_seen",
            "workspace_id": ws_id,
            "workspace_path": fs_path,
            "composer_id": cid,
            "composer_name": composer.get("name"),
            "header_count_at_start": len(headers),
            "note": "Historical messages skipped — only future ones tracked.",
        })
        prev_count = len(headers)

    # New messages (after prev_count)
    new_headers = headers[prev_count:]
    for i, h in enumerate(new_headers, start=prev_count):
        bid = h.get("bubbleId") or ""
        if not bid:
            continue
        b = fetch_bubble(conn, cid, bid)
        if not b:
            continue
        bt = b.get("type")
        role = "user" if bt == 1 else "assistant" if bt == 2 else f"type{bt}"
        journal_append({
            "ts": now_iso(),
            "event": "message",
            "workspace_id": ws_id,
            "workspace_path": fs_path,
            "composer_id": cid,
            "composer_name": composer.get("name"),
            "index": i,
            "bubbleId": bid,
            "role": role,
            "text": truncate_text(b.get("text") or ""),
        })

    return {
        "composer_id": cid,
        "composer_name": composer.get("name"),
        "composer_subtitle": composer.get("subtitle"),
        "workspace_path": fs_path,
        "header_count": len(headers),
        "last_updated_ms": composer.get("lastUpdatedAt", 0),
        "new_messages_this_run": len(new_headers) if (prev_cid == cid) else 0,
    }


def run() -> None:
    if not DB.exists():
        return

    BASE.mkdir(parents=True, exist_ok=True)
    snapshot = load_snapshot()

    uri = f"file:{DB.as_posix()}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        active_per_ws = find_active_per_workspace(conn)
        new_workspaces_state: dict[str, dict] = {}
        for ws_id, (cid, composer, fs) in active_per_ws.items():
            try:
                new_workspaces_state[ws_id] = process_workspace(
                    conn, ws_id, cid, composer, fs, snapshot
                )
            except Exception as e:
                log_error(e)

        save_snapshot({
            "last_run": now_iso(),
            "workspaces_seen": len(active_per_ws),
            "workspaces": new_workspaces_state,
        })
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        log_error(e)
        sys.exit(1)
