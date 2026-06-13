---
name: figma-mcp
description: Local Figma MCP server setup and frontend-to-Figma reading conventions for frontend engineers.
---

# Figma MCP — Agent Rules

Local Figma MCP server for reading design context directly in Claude Code. Run `scripts/setup.sh` once, then `scripts/sync.sh <project>` per project.

---

## Setup

```bash
# One-time user-level setup
bash technical_skills/figma-mcp/scripts/setup.sh

# Per-project sync
bash technical_skills/figma-mcp/scripts/sync.sh <project-root>
```

Requires `FIGMA_PERSONAL_ACCESS_TOKEN` in environment.  
Get token: Figma → Account Settings → Security → Personal Access Tokens.

---

## Rules

### 1. Read Figma context before implementing (CRITICAL)

Always use the Figma MCP to read the target frame or component before writing UI code. Do not implement from descriptions or screenshots alone.

Read in this order:
1. Frame structure and layout hierarchy
2. Component properties and variants
3. Variables/design tokens
4. All interactive states (default, hover, focus, disabled, error, loading)

### 2. Use design tokens from Figma variables (HIGH)

Extract values from Figma local variables. Map variable names to codebase token system. Never hardcode hex or pixel values that exist as Figma variables.

Naming pattern: Figma variable `color/brand/primary` → CSS `--color-brand-primary` → Tailwind `text-brand-primary`.

### 3. Derive names from Figma (MEDIUM)

- Component name = Figma component name (PascalCase)
- Variant property names → prop names in code
- Variant values → union type values or enum members

This keeps designer and developer vocabulary in sync.

---

## MCP config reference

Added to `~/.claude/settings.json` (user) or `.claude/settings.local.json` (project):

```json
{
  "mcpServers": {
    "Framelink MCP for Figma": {
      "command": "npx",
      "args": ["-y", "figma-developer-mcp", "--stdio"],
      "env": {
        "FIGMA_API_KEY": "${FIGMA_PERSONAL_ACCESS_TOKEN}"
      }
    }
  }
}
```

Requires `FIGMA_PERSONAL_ACCESS_TOKEN` in environment.  
Get token: Figma → Account Settings → Security → Personal Access Tokens.
