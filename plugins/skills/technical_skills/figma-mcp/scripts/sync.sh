#!/usr/bin/env bash
set -euo pipefail

# Sync Figma MCP config into a project's .claude/settings.local.json.
#
# Usage:
#   bash technical_skills/figma-mcp/scripts/sync.sh <project-root>
#
# Effect:
#   - Creates/updates <project-root>/.claude/settings.local.json
#   - Adds the figma-local MCP server entry
#   - Does NOT overwrite existing non-MCP settings

SCRIPT_NAME="$(basename "$0")"
PROJECT_ROOT="${1:-}"

if [[ -z "$PROJECT_ROOT" ]]; then
  echo "Usage: $SCRIPT_NAME <project-root>" >&2
  exit 1
fi

if [[ ! -d "$PROJECT_ROOT" ]]; then
  echo "ERROR: project root does not exist: $PROJECT_ROOT" >&2
  exit 1
fi

SETTINGS_FILE="$PROJECT_ROOT/.claude/settings.local.json"
MCP_KEY="Framelink MCP for Figma"

echo "==> Syncing Figma MCP into: $PROJECT_ROOT"
mkdir -p "$(dirname "$SETTINGS_FILE")"

# Ensure file exists and is valid JSON
if [[ ! -f "$SETTINGS_FILE" ]] || ! python3 -c "import json; json.load(open('$SETTINGS_FILE'))" &>/dev/null 2>&1; then
  echo "{}" > "$SETTINGS_FILE"
fi

python3 - <<PYEOF
import json

path = "$SETTINGS_FILE"
key = "$MCP_KEY"

with open(path) as f:
    settings = json.load(f)

settings.setdefault("mcpServers", {})

if key in settings["mcpServers"]:
    print(f"  '{key}' already present in {path} — skipping.")
else:
    settings["mcpServers"][key] = {
        "command": "npx",
        "args": ["-y", "figma-developer-mcp", "--stdio"],
        "env": {
            "FIGMA_API_KEY": "\${FIGMA_PERSONAL_ACCESS_TOKEN}"
        }
    }
    print(f"  Added '{key}' to {path}.")

with open(path, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
PYEOF

# Add settings.local.json to .gitignore if not already there
GITIGNORE="$PROJECT_ROOT/.gitignore"
if [[ -f "$GITIGNORE" ]] && ! grep -qF ".claude/settings.local.json" "$GITIGNORE"; then
  echo ".claude/settings.local.json" >> "$GITIGNORE"
  echo "  Added .claude/settings.local.json to .gitignore"
fi

echo ""
echo "Done. Restart Claude Code in this project to activate the Figma MCP."
echo ""
echo "Verify the server is available by asking Claude:"
echo "  \"List available MCP tools\" or use figma-local tools directly."
