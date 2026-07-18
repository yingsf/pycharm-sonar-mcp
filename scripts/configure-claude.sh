#!/usr/bin/env bash
# configure-claude.sh — register pycharm-sonar-mcp with Claude Code on macOS/Linux.
#
# Uses the executable's absolute path (no PATH dependency). Idempotent; --force updates.
# If `claude` is not installed, prints a warning and exits 0.
# Compatible with system Bash 3.2.

set -euo pipefail

MCP_NAME="pycharm-sonar"
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

# Resolve executable.
EXE=""
if command -v pycharm-sonar-mcp >/dev/null 2>&1; then
  EXE="$(command -v pycharm-sonar-mcp)"
elif [ -x "$HOME/.local/bin/pycharm-sonar-mcp" ]; then
  EXE="$HOME/.local/bin/pycharm-sonar-mcp"
fi

if [ -z "$EXE" ]; then
  echo "error: pycharm-sonar-mcp executable not found." >&2
  exit 1
fi

case "$EXE" in
  /*) : ;;
  *)  EXE="$(cd "$(dirname "$EXE")" && pwd)/$(basename "$EXE")" ;;
esac

if ! command -v claude >/dev/null 2>&1; then
  echo "warn: claude (Claude Code) not found; skipping Claude registration." >&2
  echo "To register manually once Claude Code is installed:" >&2
  echo "  claude mcp add --transport stdio --scope user $MCP_NAME -- \"$EXE\"" >&2
  exit 0
fi

# Check existing registration.
EXISTING=0
if claude mcp list >/dev/null 2>&1; then
  if claude mcp list 2>/dev/null | grep -q "$MCP_NAME"; then
    EXISTING=1
  fi
fi

if [ "$EXISTING" = "1" ] && [ "$FORCE" != "1" ]; then
  echo "Claude Code MCP '$MCP_NAME' already registered. Re-run with --force to update." >&2
  exit 0
fi

if [ "$EXISTING" = "1" ]; then
  echo "Updating Claude Code MCP '$MCP_NAME'..."
  claude mcp remove "$MCP_NAME" >/dev/null 2>&1 || true
else
  echo "Registering Claude Code MCP '$MCP_NAME'..."
fi

claude mcp add \
  --transport stdio \
  --scope user \
  "$MCP_NAME" \
  -- "$EXE"

echo ""
echo "Claude Code MCP entries:"
claude mcp list || true
