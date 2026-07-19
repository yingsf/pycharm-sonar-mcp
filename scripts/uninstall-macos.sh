#!/usr/bin/env bash
# uninstall-macos.sh — remove pycharm-code-quality-mcp from macOS.
#
# Removes the installed binary. Optionally removes Codex/Claude registrations.
# Does NOT remove PyCharm, the SonarQube for IDE plugin, or other MCP servers.
#
# Flags:
#   --remove-codex    also run `codex mcp remove pycharm-code-quality`
#   --remove-claude   also run `claude mcp remove pycharm-code-quality`
#   --purge           shorthand for --remove-codex --remove-claude

set -euo pipefail

PROG_NAME="pycharm-code-quality-mcp"
INSTALL_DIR="$HOME/.local/bin"
INSTALL_PATH="$INSTALL_DIR/$PROG_NAME"
MCP_NAME="pycharm-code-quality"

REMOVE_CODEX=0
REMOVE_CLAUDE=0

for arg in "$@"; do
  case "$arg" in
    --remove-codex)  REMOVE_CODEX=1 ;;
    --remove-claude) REMOVE_CLAUDE=1 ;;
    --purge)         REMOVE_CODEX=1; REMOVE_CLAUDE=1 ;;
    -h|--help)
      echo "Usage: $0 [--remove-codex] [--remove-claude] [--purge]"
      exit 0
      ;;
    *) echo "warn: ignoring unknown argument: $arg" >&2 ;;
  esac
done

log() { printf '%s\n' "$*"; }

# --- remove binary ---
if [ -f "$INSTALL_PATH" ]; then
  rm -f "$INSTALL_PATH"
  log "Removed $INSTALL_PATH"
else
  log "$INSTALL_PATH not present; nothing to remove."
fi

if [ "$REMOVE_CODEX" = "1" ] && command -v codex >/dev/null 2>&1; then
  if codex mcp remove "$MCP_NAME" >/dev/null 2>&1; then
    log "Removed Codex MCP '$MCP_NAME'."
  else
    log "Codex MCP '$MCP_NAME' was not registered."
  fi
fi

if [ "$REMOVE_CLAUDE" = "1" ] && command -v claude >/dev/null 2>&1; then
  if claude mcp remove "$MCP_NAME" >/dev/null 2>&1; then
    log "Removed Claude Code MCP '$MCP_NAME'."
  else
    log "Claude Code MCP '$MCP_NAME' was not registered."
  fi
fi

log "Uninstall complete. PyCharm and the SonarQube for IDE plugin were not touched."
