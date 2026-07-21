#!/usr/bin/env bash
# install-macos.sh — download and install pycharm-code-quality-mcp on macOS (arm64 or x64).
#
# Install location: ~/.local/bin/pycharm-code-quality-mcp
# - No sudo. No writes outside the user's home.
# - SHA-256 verified. Atomic replacement. Failure leaves the old binary intact.
# - Supports paths with spaces and CJK user names.
# - Optionally registers with Codex and Claude Code (warnings only if absent).
# - Runs `doctor` at the end.
#
# Env overrides:
#   PYCHARM_CODE_QUALITY_MCP_VERSION   tag/version to install (default: latest)
#   PYCHARM_CODE_QUALITY_MCP_BASE_URL  download base (default: GitHub releases)
#
# Bash 3.2 compatible (macOS system Bash).

set -euo pipefail

PROG_NAME="pycharm-code-quality-mcp"
INSTALL_DIR="$HOME/.local/bin"
INSTALL_PATH="$INSTALL_DIR/$PROG_NAME"
TMP_DIR="$(mktemp -d 2>/dev/null || mktemp -d -t pcqm)"
trap 'rm -rf "$TMP_DIR"' EXIT

VERSION="${PYCHARM_CODE_QUALITY_MCP_VERSION:-}"
BASE_URL="${PYCHARM_CODE_QUALITY_MCP_BASE_URL:-https://github.com/yingsf/pycharm-code-quality-mcp/releases/download}"

log()  { printf '%s\n' "$*"; }
err()  { printf 'error: %s\n' "$*" >&2; }

# --- platform check ---
if [ "$(uname)" != "Darwin" ]; then
  err "This installer is for macOS. Use install-windows.ps1 on Windows."
  exit 1
fi

ARCH="$(uname -m)"
case "$ARCH" in
  arm64|aarch64) ARCH_TAG="macos-arm64" ;;
  x86_64)        ARCH_TAG="macos-x64"   ;;
  *)
    err "Unsupported architecture: $ARCH"
    exit 1
    ;;
esac

# --- resolve version ---
if [ -z "$VERSION" ]; then
  if command -v curl >/dev/null 2>&1; then
    VERSION="$(curl -fsSL https://api.github.com/repos/yingsf/pycharm-code-quality-mcp/releases/latest \
               2>/dev/null | grep -m1 '"tag_name"' | sed -E 's/.*"([^"]+)".*/\1/' || true)"
  fi
  if [ -z "$VERSION" ]; then
    err "Could not determine latest version. Set PYCHARM_CODE_QUALITY_MCP_VERSION manually."
    exit 1
  fi
fi
log "Installing $PROG_NAME $VERSION ($ARCH_TAG)..."

BINARY_URL="$BASE_URL/$VERSION/$PROG_NAME-$ARCH_TAG"
SUMS_URL="$BASE_URL/$VERSION/SHA256SUMS"

mkdir -p "$INSTALL_DIR"

# --- download to temp dir ---
BIN_TMP="$TMP_DIR/$PROG_NAME-$ARCH_TAG"
SUMS_TMP="$TMP_DIR/SHA256SUMS"

log "Downloading $BINARY_URL"
if command -v curl >/dev/null 2>&1; then
  curl -fsSL -o "$BIN_TMP" "$BINARY_URL" || { err "download failed"; exit 1; }
  curl -fsSL -o "$SUMS_TMP" "$SUMS_URL" || { err "checksums download failed"; exit 1; }
elif command -v wget >/dev/null 2>&1; then
  wget -q -O "$BIN_TMP" "$BINARY_URL" || { err "download failed"; exit 1; }
  wget -q -O "$SUMS_TMP" "$SUMS_URL" || { err "checksums download failed"; exit 1; }
else
  err "Neither curl nor wget is available."
  exit 1
fi

# --- verify SHA-256 ---
EXPECTED_LINE="$(grep -E "[[:space:]]+$PROG_NAME-$ARCH_TAG\$" "$SUMS_TMP" || true)"
if [ -z "$EXPECTED_LINE" ]; then
  err "No checksum entry for $PROG_NAME-$ARCH_TAG in SHA256SUMS."
  exit 1
fi
EXPECTED_SHA="$(printf '%s' "$EXPECTED_LINE" | awk '{print $1}')"

if command -v shasum >/dev/null 2>&1; then
  ACTUAL_SHA="$(shasum -a 256 "$BIN_TMP" | awk '{print $1}')"
elif command -v sha256sum >/dev/null 2>&1; then
  ACTUAL_SHA="$(sha256sum "$BIN_TMP" | awk '{print $1}')"
else
  err "Neither shasum nor sha256sum is available."
  exit 1
fi

if [ "$EXPECTED_SHA" != "$ACTUAL_SHA" ]; then
  err "SHA-256 mismatch:"
  err "  expected: $EXPECTED_SHA"
  err "  actual:   $ACTUAL_SHA"
  err "Temp files removed; existing installation (if any) is unchanged."
  exit 1
fi
log "SHA-256 verified."

# --- make executable + atomic replace ---
chmod 0755 "$BIN_TMP"

STAGING="$TMP_DIR/staging"
mv "$BIN_TMP" "$STAGING"
mv -f "$STAGING" "$INSTALL_PATH"
log "Installed to $INSTALL_PATH"

# --- register with Codex / Claude (best-effort, never fail the install) ---
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -x "$SCRIPT_DIR/configure-codex.sh" ]; then
  "$SCRIPT_DIR/configure-codex.sh" || log "warn: Codex registration skipped."
fi
if [ -x "$SCRIPT_DIR/configure-claude.sh" ]; then
  "$SCRIPT_DIR/configure-claude.sh" || log "warn: Claude registration skipped."
fi

# --- doctor ---
log ""
log "Running doctor..."
DOCTOR_LOG="$TMP_DIR/doctor.out"
if "$INSTALL_PATH" doctor 2>&1 | tee "$DOCTOR_LOG"; then
  :
else
  log "warn: doctor reported issues (exit $?)."
fi
DOCTOR_OUT="$(cat "$DOCTOR_LOG")"

# --- JetBrains 配置引导(若未配置,显式提示下一步) ---
if echo "$DOCTOR_OUT" | grep -q "JetBrains MCP: not configured"; then
  log ""
  log "================================================================"
  log "⚠  JetBrains backend is NOT configured yet."
  log "Without this step, the tool falls back to SonarQube for IDE only"
  log "(degraded mode). To enable PyCharm inspections:"
  log ""
  log "1. PyCharm → Settings → Tools → MCP Server → Enable MCP Server"
  log "   In 'Exposed Tools', enable: get_file_problems (required)"
  log "2. Click 'Copy HTTP Stream Config' (copies a JSON snippet)."
  log "3. Run:"
  log "   $INSTALL_PATH jetbrains configure --json '<paste JSON here>'"
  log ""
  log "Sample JSON (PyCharm typically copies one of these 3 shapes):"
  log '   {"url":"http://127.0.0.1:64342/stream","headers":{}}'
  log '   {"transport":{"type":"streamable-http","url":"http://127.0.0.1:64342/stream","headers":{}}}'
  log '   {"mcpServers":{"pycharm":{"url":"http://127.0.0.1:64342/stream","headers":{}}}}'
  log ""
  log "The port (64342 here) is shown in PyCharm's MCP Server settings."
  log "================================================================"
fi

log ""
log "Done. Restart Codex App / reload Claude Code MCP to activate."
