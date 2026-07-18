#!/usr/bin/env bash
# configure-codex.sh — register pycharm-code-quality-mcp with Codex CLI on macOS/Linux.
#
# Uses the executable's absolute path (no PATH dependency). Idempotent: re-running
# updates the registration. Supports --force to overwrite an existing same-named entry.
# If `codex` is not installed, prints a warning and instructions, and exits 0 so the
# overall installer does not fail.
#
# Compatible with system Bash 3.2 (macOS default). No lsof/grep/sed/awk used.

set -euo pipefail

MCP_NAME="pycharm-code-quality"
LEGACY_MCP_NAME="pycharm-sonar"
FORCE=0

for arg in "$@"; do
  case "$arg" in
    --force|-f) FORCE=1 ;;
    -h|--help)
      echo "Usage: $0 [--force]"
      exit 0
      ;;
    *)
      echo "error: unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

# Resolve the absolute path to the executable.
# Prefer pycharm-code-quality-mcp on PATH / ~/.local/bin; fall back to legacy name.
EXE=""
if command -v pycharm-code-quality-mcp >/dev/null 2>&1; then
  EXE="$(command -v pycharm-code-quality-mcp)"
elif [ -x "$HOME/.local/bin/pycharm-code-quality-mcp" ]; then
  EXE="$HOME/.local/bin/pycharm-code-quality-mcp"
elif command -v pycharm-sonar-mcp >/dev/null 2>&1; then
  EXE="$(command -v pycharm-sonar-mcp)"
elif [ -x "$HOME/.local/bin/pycharm-sonar-mcp" ]; then
  EXE="$HOME/.local/bin/pycharm-sonar-mcp"
fi

if [ -z "$EXE" ]; then
  echo "error: pycharm-code-quality-mcp executable not found." >&2
  echo "Install it first, or pass its absolute path via PYCHARM_CODE_QUALITY_MCP_EXE." >&2
  exit 1
fi

# Make sure it's absolute and normalized.
case "$EXE" in
  /*) : ;;
  *)  EXE="$(cd "$(dirname "$EXE")" && pwd)/$(basename "$EXE")" ;;
esac

if ! command -v codex >/dev/null 2>&1; then
  echo "warn: codex CLI not found; skipping Codex registration." >&2
  echo "To register manually once codex is installed:" >&2
  echo "  codex mcp add $MCP_NAME -- \"$EXE\"" >&2
  exit 0
fi

# Check for an existing same-named entry (current or legacy name).
EXISTING=0
if codex mcp list >/dev/null 2>&1; then
  if codex mcp list 2>/dev/null | tr ',' '\n' | grep -q "^${MCP_NAME}\$"; then
    EXISTING=1
  elif codex mcp list 2>/dev/null | tr ',' '\n' | grep -q "^${LEGACY_MCP_NAME}\$"; then
    EXISTING=1
    # Remove the legacy-name entry so we can re-register under the new name.
    codex mcp remove "$LEGACY_MCP_NAME" >/dev/null 2>&1 || true
  fi
fi

if [ "$EXISTING" = "1" ] && [ "$FORCE" != "1" ]; then
  echo "Codex MCP '$MCP_NAME' already registered. Re-run with --force to update." >&2
  exit 0
fi

# Register (idempotent via remove-then-add when --force).
if [ "$EXISTING" = "1" ]; then
  echo "Updating Codex MCP '$MCP_NAME'..."
  codex mcp remove "$MCP_NAME" >/dev/null 2>&1 || true
else
  echo "Registering Codex MCP '$MCP_NAME'..."
fi

codex mcp add "$MCP_NAME" -- "$EXE"

echo ""
echo "Codex MCP entries:"
codex mcp list || true
