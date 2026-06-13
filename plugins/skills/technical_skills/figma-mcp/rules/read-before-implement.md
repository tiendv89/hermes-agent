---
title: Read Figma context before implementing
impact: CRITICAL
impactDescription: Prevents guessing at design intent and reduces rework from mismatched implementations.
tags: [figma, workflow, frontend]
---

# Read Figma Context Before Implementing

Always call the Figma MCP to retrieve design context before writing UI code. Do not implement from verbal descriptions or screenshots alone.

## What to read

1. **Frame structure** — understand the layout hierarchy before choosing component structure.
2. **Component properties** — check variants, boolean props, and instance swap slots.
3. **Variables/tokens** — extract color, spacing, and typography tokens rather than hardcoding.
4. **States** — confirm all interactive states (default, hover, focus, disabled, error, loading) are designed.

## Correct workflow

```
1. Get Figma file context → identify the target frame/component
2. Read component properties and variants
3. Extract design tokens
4. Implement with token references, not raw values
5. Verify visually against the Figma frame
```

## Wrong

Implementing from a screenshot or design brief without reading the actual Figma node structure.
