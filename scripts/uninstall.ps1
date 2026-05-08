#Requires -Version 5.1

<#
.SYNOPSIS
Uninstall the cursor-chats-bridge: removes scheduled task, MCP registration, and (optionally) the data directory.

.PARAMETER KeepData
If set, leaves ~/.claude/mcp/cursor-chats/ in place (snapshot, journal, scripts).
Default behavior is to delete everything.
#>

[CmdletBinding()]
param(
    [switch]$KeepData
)

$ErrorActionPreference = 'Continue'

Write-Host "==> Removing scheduled task 'ClaudeCursorChatPoller'" -ForegroundColor Cyan
schtasks /Delete /TN 'ClaudeCursorChatPoller' /F 2>&1 | Out-Null

Write-Host "==> Unregistering 'cursor-chats' MCP" -ForegroundColor Cyan
& claude mcp remove cursor-chats 2>&1 | Out-Null

if (-not $KeepData) {
    $dir = Join-Path $HOME '.claude\mcp\cursor-chats'
    if (Test-Path $dir) {
        Write-Host "==> Removing $dir" -ForegroundColor Cyan
        Remove-Item -Recurse -Force $dir
    }
} else {
    Write-Host "==> Keeping ~/.claude/mcp/cursor-chats/ (--KeepData)" -ForegroundColor Yellow
}

Write-Host "Done. Restart Claude Desktop to drop the MCP from the active session." -ForegroundColor Green
