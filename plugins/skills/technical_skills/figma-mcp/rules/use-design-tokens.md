---
title: Use design tokens from Figma variables
impact: HIGH
impactDescription: Keeps code and design in sync; prevents token drift when designers update values.
tags: [figma, tokens, frontend]
---

# Use Design Tokens from Figma Variables

Extract color, spacing, and typography values from Figma local variables rather than hardcoding hex or pixel values.

## How

1. Read the Figma variable collections via the MCP.
2. Map variable names to your codebase's token system (CSS custom properties, design-token JSON, Tailwind theme, etc.).
3. Reference tokens in code — never raw values.

## Example

**Wrong:**
```tsx
<div style={{ color: '#1A73E8', padding: '16px' }} />
```

**Correct:**
```tsx
<div className="text-brand-primary p-4" />
```

## Token naming convention

Use the Figma variable name directly as the basis for your token name. If the Figma variable is `color/brand/primary`, the CSS custom property should be `--color-brand-primary`.
