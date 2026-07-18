<#
.SYNOPSIS
  Remove pycharm-sonar-mcp from Windows.

.DESCRIPTION
  Removes the installed binary. Optionally removes Codex/Claude registrations.
  Does NOT remove PyCharm, the SonarQube for IDE plugin, or other MCP servers.

.PARAMETER RemoveCodex
  Also run `codex mcp remove pycharm-sonar`.

.PARAMETER RemoveClaude
  Also run `claude mcp remove pycharm-sonar`.

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
$ProgName = "pycharm-sonar-mcp"
$InstallDir = Join-Path $env:LOCALAPPDATA "pycharm-sonar-mcp"
$InstallPath = Join-Path $InstallDir "$ProgName.exe"
$McpName = "pycharm-sonar"

if ($Purge) { $RemoveCodex = $true; $RemoveClaude = $true }

function Write-Step($m) { Write-Host $m }
function Write-Warn2($m) { Write-Host "warn: $m" -ForegroundColor Yellow }

if (Test-Path $InstallPath) {
  Remove-Item -LiteralPath $InstallPath -Force
  Write-Step "Removed $InstallPath"
} else {
  Write-Step "$InstallPath not present; nothing to remove."
}

if ($RemoveCodex -and (Get-Command codex -ErrorAction SilentlyContinue)) {
  try {
    & codex mcp remove $McpName 2>$null | Out-Null
    Write-Step "Removed Codex MCP '$McpName'."
  } catch {
    Write-Step "Codex MCP '$McpName' was not registered."
  }
}

if ($RemoveClaude -and (Get-Command claude -ErrorAction SilentlyContinue)) {
  try {
    & claude mcp remove $McpName 2>$null | Out-Null
    Write-Step "Removed Claude Code MCP '$McpName'."
  } catch {
    Write-Step "Claude Code MCP '$McpName' was not registered."
  }
}

Write-Step "Uninstall complete. PyCharm and the SonarQube for IDE plugin were not touched."
exit 0
