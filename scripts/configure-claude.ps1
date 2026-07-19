<#
.SYNOPSIS
  Register pycharm-code-quality-mcp with Claude Code on Windows.

.DESCRIPTION
  Uses the .exe's absolute path (no PATH dependency). Idempotent. Supports -Force.
  If `claude` is not installed, prints a warning and exits 0.

  Requires Windows PowerShell 5.1+ or PowerShell 7+.
#>

[CmdletBinding()]
param(
  [switch]$Force
)

$ErrorActionPreference = "Stop"
$McpName = "pycharm-code-quality"

function Write-Info($msg) { Write-Host $msg }
function Write-Warn($msg) { Write-Host "warn: $msg" -ForegroundColor Yellow }
function Write-Err($msg)  { Write-Host "error: $msg" -ForegroundColor Red }

# Locate the executable.
$Exe = $null
$candidate = Get-Command pycharm-code-quality-mcp.exe -ErrorAction SilentlyContinue
if ($candidate) {
  $Exe = $candidate.Source
} elseif (Test-Path "$env:LOCALAPPDATA\pycharm-code-quality-mcp\pycharm-code-quality-mcp.exe") {
  $Exe = "$env:LOCALAPPDATA\pycharm-code-quality-mcp\pycharm-code-quality-mcp.exe"
}

if (-not $Exe) {
  Write-Err "pycharm-code-quality-mcp.exe not found."
  exit 1
}
$Exe = (Resolve-Path $Exe).Path

# Check claude presence.
$claude = Get-Command claude -ErrorAction SilentlyContinue
if (-not $claude) {
  Write-Warn "claude (Claude Code) not found; skipping Claude registration."
  Write-Warn "To register manually once Claude Code is installed:"
  Write-Warn "  claude mcp add --transport stdio --scope user $McpName -- `"$Exe`""
  exit 0
}

# Detect existing registration.
$existing = $false
try {
  $list = & claude mcp list 2>$null
  if ($list -match [regex]::Escape($McpName)) {
    $existing = $true
  }
} catch { $existing = $false }

if ($existing -and -not $Force) {
  Write-Host "Claude Code MCP '$McpName' already registered. Re-run with -Force to update."
  exit 0
}

if ($existing) {
  Write-Info "Updating Claude Code MCP '$McpName'..."
  try { & claude mcp remove $McpName 2>$null | Out-Null } catch {}
} else {
  Write-Info "Registering Claude Code MCP '$McpName'..."
}

& claude mcp add --transport stdio --scope user $McpName -- $Exe
if ($LASTEXITCODE -ne 0) {
  Write-Err "claude mcp add failed (exit $LASTEXITCODE)."
  exit $LASTEXITCODE
}

Write-Info ""
Write-Info "Claude Code MCP entries:"
& claude mcp list
exit 0
