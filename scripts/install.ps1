#Requires -Version 5.1

<#
.SYNOPSIS
Install or update the cursor-chats-bridge - Claude to Cursor read-only chat bridge.

.DESCRIPTION
Idempotent setup that:
- copies the MCP server (server.py) and poller (poller.py) to ~/.claude/mcp/cursor-chats/
- installs the 'mcp' Python package via pip if missing
- registers the MCP server with the `claude` CLI (removes any prior registration first)
- creates/updates the Windows scheduled task that runs the poller every 5 minutes
- runs the poller once to seed the snapshot/journal

Safe to re-run: every step uses force-overwrite semantics.

.PARAMETER Quiet
Suppress progress output (useful for automation).

.PARAMETER NoTask
Skip creating the scheduled task (for debugging or one-shot use).

.PARAMETER Scope
MCP registration scope: 'local' (default, this project only), 'user' (global), or
'project' (versioned via .mcp.json - DO NOT use, would commit no secrets but
preferences vary by project).

.NOTES
Windows-only. Requires Python 3.10+, the `claude` CLI on PATH, and Cursor installed.
#>

[CmdletBinding()]
param(
    [switch]$Quiet,
    [switch]$NoTask,
    [ValidateSet('local','user','project')]
    [string]$Scope = 'local'
)

$ErrorActionPreference = 'Stop'

function Write-Step($msg) {
    if (-not $Quiet) { Write-Host "==> $msg" -ForegroundColor Cyan }
}
function Write-Ok($msg) {
    if (-not $Quiet) { Write-Host "    $msg" -ForegroundColor Green }
}
function Write-Warn2($msg) {
    Write-Host "    $msg" -ForegroundColor Yellow
}

# ---- 1. Prerequisites ----
Write-Step "Checking prerequisites"

if (-not $IsWindows -and $PSVersionTable.Platform -and $PSVersionTable.Platform -ne 'Win32NT') {
    throw "Windows-only: this skill uses schtasks and Cursor's %APPDATA% path."
}

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    throw "Python not found on PATH. Install Python 3.10+ from python.org and re-run."
}
$pyVer = (& python --version 2>&1).ToString().Trim()
Write-Ok "Python: $pyVer at $($python.Source)"

# Locate pythonw.exe (silent runner for Task Scheduler)
$pythonw = Get-Command pythonw -ErrorAction SilentlyContinue
if ($pythonw) {
    $pythonwPath = $pythonw.Source
} else {
    $pythonDir = Split-Path -Parent $python.Source
    $candidate = Join-Path $pythonDir 'pythonw.exe'
    if (Test-Path $candidate) {
        $pythonwPath = $candidate
    } else {
        throw "pythonw.exe not found near $($python.Source) - needed for silent Task Scheduler execution."
    }
}
Write-Ok "pythonw: $pythonwPath"

$claude = Get-Command claude -ErrorAction SilentlyContinue
if (-not $claude) {
    throw "Claude CLI not found on PATH. Install Claude Code (https://claude.com/claude-code) and re-run."
}
Write-Ok "claude: $($claude.Source)"

$cursorDb = Join-Path $env:APPDATA 'Cursor\User\globalStorage\state.vscdb'
if (-not (Test-Path $cursorDb)) {
    Write-Warn2 "Cursor SQLite not found at $cursorDb - install Cursor and open it once before relying on the bridge."
} else {
    Write-Ok "Cursor DB: $cursorDb"
}

# ---- 2. Copy bundled scripts ----
Write-Step "Copying server.py and poller.py to ~/.claude/mcp/cursor-chats/"
$dest = Join-Path $HOME '.claude\mcp\cursor-chats'
New-Item -ItemType Directory -Path $dest -Force | Out-Null

$scriptDir = $PSScriptRoot
foreach ($file in @('server.py', 'poller.py')) {
    $src = Join-Path $scriptDir $file
    if (-not (Test-Path $src)) {
        throw "Bundled script not found: $src"
    }
    Copy-Item -Path $src -Destination $dest -Force
    Write-Ok "$file -> $dest"
}

# ---- 3. Install Python mcp package ----
Write-Step "Ensuring Python 'mcp' package is installed"
& python -c "import mcp" 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    & python -m pip install --quiet mcp
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install 'mcp' Python package."
    }
    Write-Ok "Installed mcp package via pip"
} else {
    Write-Ok "mcp package already present"
}

# ---- 4. Register MCP server with claude CLI ----
Write-Step "Registering 'cursor-chats' MCP (scope: $Scope)"
& claude mcp remove cursor-chats 2>&1 | Out-Null  # idempotent: ignore "not found"
$serverScript = Join-Path $dest 'server.py'
$mcpAddArgs = @('mcp','add','--scope', $Scope, 'cursor-chats','--','python', $serverScript)
& claude @mcpAddArgs 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Failed to register MCP server with claude CLI (scope=$Scope)."
}
Write-Ok "Registered cursor-chats MCP"

# ---- 5. Scheduled task ----
if (-not $NoTask) {
    Write-Step "Creating scheduled task 'ClaudeCursorChatPoller' (every 5 min, silent)"
    $pollerScript = Join-Path $dest 'poller.py'
    $taskCmd = "`"$pythonwPath`" `"$pollerScript`""
    $null = schtasks /Create /TN 'ClaudeCursorChatPoller' /TR $taskCmd /SC MINUTE /MO 5 /F 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "schtasks /Create failed."
    }
    Write-Ok "Task created (or updated)"
} else {
    Write-Warn2 "Skipping scheduled task (NoTask flag)"
}

# ---- 6. Initial poller run (seed snapshot/journal) ----
Write-Step "Running poller once to seed snapshot/journal"
$pollerScript = Join-Path $dest 'poller.py'
& python $pollerScript 2>&1 | Out-Null
$snapshot = Join-Path $dest 'active_snapshot.json'
if (Test-Path $snapshot) {
    Write-Ok "Snapshot created: $snapshot"
} else {
    Write-Warn2 "Snapshot not created (Cursor may not be running yet - that's fine)."
}

# ---- Done ----
Write-Step "Done."
if (-not $Quiet) {
    Write-Host ""
    Write-Host "Next steps:" -ForegroundColor Yellow
    Write-Host "  1. Restart Claude Desktop (full quit, not just close-window)" -ForegroundColor Yellow
    Write-Host "  2. After restart, the tools 'mcp__cursor-chats__*' will be available" -ForegroundColor Yellow
    Write-Host "  3. The poller will keep running every 5 min via Task Scheduler" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Verify with:" -ForegroundColor Yellow
    Write-Host "  claude mcp list" -ForegroundColor DarkGray
    Write-Host "  schtasks /Query /TN ClaudeCursorChatPoller /FO LIST" -ForegroundColor DarkGray
}
