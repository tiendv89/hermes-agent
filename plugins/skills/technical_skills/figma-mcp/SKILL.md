---
name: figma-mcp
description: Local Figma MCP server setup for frontend engineers to read design context, inspect components, and implement designs from Figma files directly in Claude Code.
---

# Figma MCP — Local Setup

Provides a locally running Figma MCP (Model Context Protocol) server so frontend engineers can read Figma design context, inspect components, variables, and assets without leaving Claude Code.

## What it enables

- Read Figma file structure, pages, and frames
- Inspect component properties, variants, and design tokens
- Extract design context for implementation
- No cloud relay — runs locally via your Figma Personal Access Token

## Quick start

```bash
# 1. Install and configure the MCP server
bash technical_skills/figma-mcp/scripts/setup.sh

# 2. Set your Figma token (add to shell profile)
export FIGMA_PERSONAL_ACCESS_TOKEN=your_token_here

# 3. Sync MCP config into a project
bash technical_skills/figma-mcp/scripts/sync.sh <project-root>
```

## Getting a Figma Personal Access Token

1. Open Figma → Account Settings → Security
2. Generate new Personal Access Token
3. Add it to your project `.env` (copied from `.env.template`):
   ```env
   FIGMA_PERSONAL_ACCESS_TOKEN=your_token_here
   ```

## Rules

- Always read the Figma file context before implementing UI components.
- Extract design tokens (colors, spacing, typography) from Figma variables before hardcoding values.
- Use frame/component names from Figma as the basis for component and file names in code.
- Verify component states (default, hover, disabled, error) exist in Figma before implementing.
- Reference the exact Figma node IDs when discussing designs with the team.
