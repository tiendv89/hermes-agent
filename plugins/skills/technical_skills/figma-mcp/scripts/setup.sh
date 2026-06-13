#!/usr/bin/env bash
set -euo pipefail

# Setup script for the local Figma MCP server.
#
# Run once to install figma-developer-mcp and wire up the user-level
# Claude Code MCP config (~/.claude/settings.json).
#
# Usage:
#   bash technical_skills/figma-mcp/scripts/setup.sh

SETTINGS_FILE="$HOME/.claude/settings.json"
MCP_KEY="Framelink MCP for Figma"

echo "==> Checking node/npm..."
if ! command -v node &>/dev/null; then
  echo "ERROR: node is required. Install via https://nodejs.org" >&2
  exit 1
fi

echo "==> Checking figma-developer-mcp availability..."
if npx --yes figma-developer-mcp --version &>/dev/null; then
  echo "OK: figma-developer-mcp is available via npx"
else
  echo "WARN: Could not verify figma-developer-mcp via npx --version; it may still work at runtime."
fi

echo "==> Checking FIGMA_PERSONAL_ACCESS_TOKEN..."
if [[ -z "${FIGMA_PERSONAL_ACCESS_TOKEN:-}" ]]; then
  echo ""
  echo "  FIGMA_PERSONAL_ACCESS_TOKEN is not set."
  echo "  To get a token: Figma → Account Settings → Security → Personal Access Tokens"
  echo "  Then add to your shell profile:"
  echo "    export FIGMA_PERSONAL_ACCESS_TOKEN=<your-token>"
  echo ""
fi

echo "==> Updating $SETTINGS_FILE..."
mkdir -p "$(dirname "$SETTINGS_FILE")"

# Ensure settings file exists and is valid JSON
if [[ ! -f "$SETTINGS_FILE" ]] || ! python3 -c "import json,sys; json.load(open('$SETTINGS_FILE'))" &>/dev/null 2>&1; then
  echo "{}" > "$SETTINGS_FILE"
fi

# Merge MCP server config using Python (available on macOS by default)
python3 - <<PYEOF
import json, sys

path = "$SETTINGS_FILE"
key = "$MCP_KEY"

with open(path) as f:
    settings = json.load(f)

settings.setdefault("mcpServers", {})

if key in settings["mcpServers"]:
    print(f"  MCP server '{key}' already configured — skipping.")
else:
    settings["mcpServers"][key] = {
        "command": "npx",
        "args": ["-y", "figma-developer-mcp", "--stdio"],
        "env": {
            "FIGMA_API_KEY": "\${FIGMA_PERSONAL_ACCESS_TOKEN}"
        }
    }
    print(f"  Added MCP server '{key}'.")

with open(path, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
PYEOF

echo ""
echo "Done. Restart Claude Code to pick up the new MCP server."
echo ""
echo "Next steps:"
echo "  1. Set FIGMA_PERSONAL_ACCESS_TOKEN in your shell profile if not done."
echo "  2. Run sync.sh to configure a specific project:"
echo "       bash technical_skills/figma-mcp/scripts/sync.sh <project-root>"
