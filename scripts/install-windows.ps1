<#
.SYNOPSIS
  Download and install pycharm-code-quality-mcp on Windows (x64).

.DESCRIPTION
  Install location: $env:LOCALAPPDATA\pycharm-code-quality-mcp\pycharm-code-quality-mcp.exe
  - No administrator rights. No writes outside the user's profile.
  - SHA-256 verified. Atomic replacement. Failure leaves the old binary intact.
  - Supports paths with spaces and CJK user names.
  - Optionally registers with Codex and Claude Code (warnings only if absent).
  - Runs `doctor` at the end.

  Requires Windows PowerShell 5.1+ or PowerShell 7+.

.PARAMETER Force
  Re-download and overwrite an existing installation.

.PARAMETER Version
  Specific release tag to install. Defaults to the latest release.

.EXAMPLE
  pwsh -File install-windows.ps1
  pwsh -File install-windows.ps1 -Force
#>

[CmdletBinding()]
param(
  [switch]$Force,
  [string]$Version = ""
)

$ErrorActionPreference = "Stop"
$ProgName = "pycharm-code-quality-mcp"
$McpName = "pycharm-code-quality"
$InstallDir = Join-Path $env:LOCALAPPDATA "pycharm-code-quality-mcp"
$InstallPath = Join-Path $InstallDir "$ProgName.exe"
$BaseUrl = "https://github.com/yingsf/pycharm-code-quality-mcp/releases/download"
$ArchTag = "windows-x64"

function Write-Step($m) { Write-Host $m }
function Write-Warn2($m) { Write-Host "warn: $m" -ForegroundColor Yellow }
function Write-Err2($m)  { Write-Host "error: $m" -ForegroundColor Red }

# --- platform/arch check ---
if (-not $IsWindows -and ($PSVersionTable.Platform -ne $null)) {
  Write-Err2 "This installer is for Windows. Use install-macos.sh on macOS."
  exit 1
}

# --- resolve version ---
if (-not $Version) {
  try {
    $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/yingsf/pycharm-code-quality-mcp/releases/latest" -ErrorAction Stop
    $Version = $rel.tag_name
  } catch {
    Write-Err2 "Could not determine latest version. Pass -Version <tag>."
    exit 1
  }
}
Write-Step "Installing $ProgName $Version ($ArchTag)..."

$BinaryUrl = "$BaseUrl/$Version/$ProgName-$ArchTag.exe"
$SumsUrl   = "$BaseUrl/$Version/SHA256SUMS"

# --- temp dir + ensure install dir ---
$TmpDir = Join-Path ([System.IO.Path]::GetTempPath()) ("pcqm-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $TmpDir | Out-Null
try {
  New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

  $BinTmp = Join-Path $TmpDir "$ProgName-$ArchTag.exe"
  $SumsTmp = Join-Path $TmpDir "SHA256SUMS"

  Write-Step "Downloading $BinaryUrl"
  Invoke-WebRequest -Uri $BinaryUrl -OutFile $BinTmp -UseBasicParsing
  Invoke-WebRequest -Uri $SumsUrl -OutFile $SumsTmp -UseBasicParsing

  # --- verify SHA-256 ---
  $sumsContent = Get-Content -LiteralPath $SumsTmp -Encoding UTF8
  $expected = $null
  foreach ($line in $sumsContent) {
    # lines look like: <sha>  pycharm-code-quality-mcp-windows-x64.exe
    if ($line -match "^\s*([0-9A-Fa-f]{64})\s+\*?(.+)$") {
      $sha = $matches[1]; $name = $matches[2].Trim()
      if ($name -like "*$ArchTag*") { $expected = $sha.ToLower(); break }
    }
  }
  if (-not $expected) {
    Write-Err2 "No checksum entry for $ArchTag in SHA256SUMS."
    exit 1
  }
  $actual = (Get-FileHash -LiteralPath $BinTmp -Algorithm SHA256).Hash.ToLower()
  if ($expected -ne $actual) {
    Write-Err2 "SHA-256 mismatch:"
    Write-Err2 "  expected: $expected"
    Write-Err2 "  actual:   $actual"
    Write-Err2 "Temp files removed; existing installation (if any) is unchanged."
    exit 1
  }
  Write-Step "SHA-256 verified."

  # --- atomic replace: write to temp then move over the target ---
  $staging = Join-Path $TmpDir "staging.exe"
  Move-Item -LiteralPath $BinTmp -Destination $staging -Force
  # If the target is in use, the move will fail loudly rather than corrupting it.
  Move-Item -LiteralPath $staging -Destination $InstallPath -Force
  Write-Step "Installed to $InstallPath"

  # --- register with Codex / Claude (best-effort) ---
  $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
  $codexScript = Join-Path $scriptDir "configure-codex.ps1"
  $claudeScript = Join-Path $scriptDir "configure-claude.ps1"
  if (Test-Path $codexScript) {
    try { & powershell -NoProfile -ExecutionPolicy Bypass -File $codexScript -Force }
    catch { Write-Warn2 "Codex registration skipped." }
  }
  if (Test-Path $claudeScript) {
    try { & powershell -NoProfile -ExecutionPolicy Bypass -File $claudeScript -Force }
    catch { Write-Warn2 "Claude registration skipped." }
  }

  # --- doctor ---
  Write-Step ""
  Write-Step "Running doctor..."
  $DoctorOutput = ""
  try {
    $DoctorOutput = (& $InstallPath doctor 2>&1) | Out-String
    Write-Host $DoctorOutput
  } catch {
    $DoctorOutput = $_.Exception.Message
    Write-Warn2 "doctor reported issues."
  }

  # --- JetBrains 配置引导(若未配置,显式提示下一步) ---
  if ($DoctorOutput -match "JetBrains MCP: not configured") {
    Write-Step ""
    Write-Host "================================================================" -ForegroundColor Yellow
    Write-Host "  JetBrains backend is NOT configured yet." -ForegroundColor Yellow
    Write-Host "Without this step, the tool falls back to SonarQube for IDE only" -ForegroundColor Yellow
    Write-Host "(degraded mode). To enable PyCharm inspections:" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "1. PyCharm -> Settings -> Tools -> MCP Server -> Enable MCP Server"
    Write-Host "   In 'Exposed Tools', enable: get_file_problems (required)"
    Write-Host "2. Click 'Copy HTTP Stream Config' (copies a JSON snippet)."
    Write-Host "3. Run:"
    Write-Host "   $InstallPath jetbrains configure --json '<paste JSON here>'"
    Write-Host ""
    Write-Host "Sample JSON (PyCharm typically copies one of these 3 shapes):"
    Write-Host '   {"url":"http://127.0.0.1:64342/stream","headers":{}}'
    Write-Host '   {"transport":{"type":"streamable-http","url":"http://127.0.0.1:64342/stream","headers":{}}}'
    Write-Host '   {"mcpServers":{"pycharm":{"url":"http://127.0.0.1:64342/stream","headers":{}}}}'
    Write-Host ""
    Write-Host "The port (64342 here) is shown in PyCharm's MCP Server settings."
    Write-Host "================================================================" -ForegroundColor Yellow
  }

  Write-Step ""
  Write-Step "Done. Restart Codex App / reload Claude Code MCP to activate."
  exit 0
} finally {
  Remove-Item -LiteralPath $TmpDir -Recurse -Force -ErrorAction SilentlyContinue
}
