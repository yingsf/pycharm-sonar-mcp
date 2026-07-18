<#
.SYNOPSIS
  Remove pycharm-code-quality-mcp from Windows.

.DESCRIPTION
  Removes the installed binary. Optionally removes Codex/Claude registrations.
  Also cleans up the legacy name `pycharm-sonar-mcp` (binary + registrations) if present.
  Does NOT remove PyCharm, the SonarQube for IDE plugin, or other MCP servers.

.PARAMETER RemoveCodex
  Also run `codex mcp remove pycharm-code-quality`
  (and pycharm-sonar if registered under the legacy name).

.PARAMETER RemoveClaude
  Also run `claude mcp remove pycharm-code-quality`
  (and pycharm-sonar if registered under the legacy name).

.PARAMETER Purge
  Shorthand for -RemoveCodex -RemoveClaude.
#>

[CmdletBinding()]
param(
  [switch]$RemoveCodex,
  [switch]$RemoveClaude,
  [switch]$Purge
)

$ErrorActionPreference = "Stop"
$ProgName = "pycharm-code-quality-mcp"
$InstallDir = Join-Path $env:LOCALAPPDATA "pycharm-code-quality-mcp"
$InstallPath = Join-Path $InstallDir "$ProgName.exe"
$McpName = "pycharm-code-quality"

# Legacy names (for migration cleanup).
$LegacyProgName = "pycharm-sonar-mcp"
$LegacyInstallDir = Join-Path $env:LOCALAPPDATA "pycharm-sonar-mcp"
$LegacyMcpName = "pycharm-sonar"

if ($Purge) { $RemoveCodex = $true; $RemoveClaude = $true }

function Write-Step($m) { Write-Host $m }
function Write-Warn2($m) { Write-Host "warn: $m" -ForegroundColor Yellow }

# --- remove current-name binary ---
if (Test-Path $InstallPath) {
  Remove-Item -LiteralPath $InstallPath -Force
  Write-Step "Removed $InstallPath"
} else {
  Write-Step "$InstallPath not present; nothing to remove."
}

# --- remove legacy binary (migration cleanup) ---
$legacyExe = Join-Path $LegacyInstallDir "$LegacyProgName.exe"
if (Test-Path $legacyExe) {
  Remove-Item -LiteralPath $legacyExe -Force -ErrorAction SilentlyContinue
  Write-Step "Removed legacy $legacyExe"
}
if ((Test-Path $LegacyInstallDir) -and ($LegacyInstallDir -ne $InstallDir)) {
  $remaining = Get-ChildItem -LiteralPath $LegacyInstallDir -Force -ErrorAction SilentlyContinue
  if (-not $remaining) {
    Remove-Item -LiteralPath $LegacyInstallDir -Recurse -Force -ErrorAction SilentlyContinue
  }
}

if ($RemoveCodex -and (Get-Command codex -ErrorAction SilentlyContinue)) {
  try {
    & codex mcp remove $McpName 2>$null | Out-Null
    Write-Step "Removed Codex MCP '$McpName'."
  } catch {
    Write-Step "Codex MCP '$McpName' was not registered."
  }
  # Also remove a legacy-name registration if present.
  try {
    & codex mcp remove $LegacyMcpName 2>$null | Out-Null
    Write-Step "Removed legacy Codex MCP '$LegacyMcpName'."
  } catch {}
}

if ($RemoveClaude -and (Get-Command claude -ErrorAction SilentlyContinue)) {
  try {
    & claude mcp remove $McpName 2>$null | Out-Null
    Write-Step "Removed Claude Code MCP '$McpName'."
  } catch {
    Write-Step "Claude Code MCP '$McpName' was not registered."
  }
  # Also remove a legacy-name registration if present.
  try {
    & claude mcp remove $LegacyMcpName 2>$null | Out-Null
    Write-Step "Removed legacy Claude Code MCP '$LegacyMcpName'."
  } catch {}
}

Write-Step "Uninstall complete. PyCharm and the SonarQube for IDE plugin were not touched."
exit 0
