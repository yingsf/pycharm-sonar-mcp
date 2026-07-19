<#
.SYNOPSIS
  Register pycharm-code-quality-mcp with the Codex CLI on Windows.

.DESCRIPTION
  Uses the .exe's absolute path (no PATH dependency). Idempotent. Supports -Force.
  If `codex` is not installed, prints a warning and instructions, and exits 0 so the
  overall installer does not fail.

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
  Write-Err "Install it first."
  exit 1
}

$Exe = (Resolve-Path $Exe).Path

# Check codex presence.
$codex = Get-Command codex -ErrorAction SilentlyContinue
if (-not $codex) {
  Write-Warn "codex CLI not found; skipping Codex registration."
  Write-Warn "To register manually once codex is installed:"
  Write-Warn "  codex mcp add $McpName -- `"$Exe`""
  exit 0
}

# Detect existing registration.
$existing = $false
try {
  $list = & codex mcp list 2>$null
  if ($list -match [regex]::Escape($McpName)) {
    $existing = $true
  }
} catch {
  $existing = $false
}

if ($existing -and -not $Force) {
  Write-Host "Codex MCP '$McpName' already registered. Re-run with -Force to update."
  exit 0
}

if ($existing) {
  Write-Info "Updating Codex MCP '$McpName'..."
  try { & codex mcp remove $McpName 2>$null | Out-Null } catch {}
} else {
  Write-Info "Registering Codex MCP '$McpName'..."
}

& codex mcp add $McpName -- $Exe
if ($LASTEXITCODE -ne 0) {
  Write-Err "codex mcp add failed (exit $LASTEXITCODE)."
  exit $LASTEXITCODE
}

Write-Info ""
Write-Info "Codex MCP entries:"
& codex mcp list
exit 0
