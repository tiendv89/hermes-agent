---
title: Derive component names from Figma
impact: MEDIUM
impactDescription: Keeps developer and designer vocabulary aligned; reduces communication overhead.
tags: [figma, naming, frontend]
---

# Derive Component and File Names from Figma

Use the Figma component and frame names as the canonical source for component, file, and prop names in code.

## Rules

- Component name in code = Figma component name (PascalCase).
- Variant property names in Figma → prop names in code.
- Variant values in Figma → union type values or enum members in code.

## Example

Figma component: `Button / Primary / Large / Icon Left`
- Component: `Button`
- Props: `variant="primary"`, `size="large"`, `iconPosition="left"`

## Benefit

When a designer says "fix the Button / Destructive state", engineers can find the exact component immediately without translation.
