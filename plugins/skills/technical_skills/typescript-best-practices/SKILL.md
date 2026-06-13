---
name: typescript-best-practices
description: Implements advanced TypeScript type systems, creates custom type guards, utility types, and branded types, and configures tRPC for end-to-end type safety. Covers port/adapter pattern for swappable infrastructure, module-level singletons for env-derived values, and class-based lazy singletons. Use when building TypeScript applications requiring advanced generics, conditional or mapped types, discriminated unions, dependency injection, notification channels, monorepo setup, or full-stack type safety with tRPC.
---

# TypeScript

## Core Workflow

1. **Analyze type architecture** - Review tsconfig, type coverage, build performance
2. **Design type-first APIs** - Create branded types, generics, utility types
3. **Implement with type safety** - Write type guards, discriminated unions, conditional types; run `tsc --noEmit` to catch type errors before proceeding
4. **Optimize build** - Configure project references, incremental compilation, tree shaking; re-run `tsc --noEmit` to confirm zero errors after changes
5. **Test types** - Confirm type coverage with a tool like `type-coverage`; validate that all public APIs have explicit return types; iterate on steps 3–4 until all checks pass

## Reference Guide

Load detailed guidance based on context:

| Topic | Reference | Load When |
|-------|-----------|-----------|
| Advanced Types | `references/advanced-types.md` | Generics, conditional types, mapped types, template literals |
| Type Guards | `references/type-guards.md` | Type narrowing, discriminated unions, assertion functions |
| Utility Types | `references/utility-types.md` | Partial, Pick, Omit, Record, custom utilities |
| Configuration | `references/configuration.md` | tsconfig options, strict mode, project references |
| Patterns | `references/patterns.md` | Builder, factory, singleton (module-level + class), port/adapter, type-safe APIs |

## Code Examples

### Branded Types
```typescript
// Branded type for domain modeling
type Brand<T, B extends string> = T & { readonly __brand: B };
type UserId  = Brand<string, "UserId">;
type OrderId = Brand<number, "OrderId">;

const toUserId  = (id: string): UserId  => id as UserId;
const toOrderId = (id: number): OrderId => id as OrderId;

// Usage — prevents accidental id mix-ups at compile time
function getOrder(userId: UserId, orderId: OrderId) { /* ... */ }
```

### Discriminated Unions & Type Guards
```typescript
type LoadingState = { status: "loading" };
type SuccessState = { status: "success"; data: string[] };
type ErrorState   = { status: "error";   error: Error };
type RequestState = LoadingState | SuccessState | ErrorState;

// Type predicate guard
function isSuccess(state: RequestState): state is SuccessState {
  return state.status === "success";
}

// Exhaustive switch with discriminated union
function renderState(state: RequestState): string {
  switch (state.status) {
    case "loading": return "Loading…";
    case "success": return state.data.join(", ");
    case "error":   return state.error.message;
    default: {
      const _exhaustive: never = state;
      throw new Error(`Unhandled state: ${_exhaustive}`);
    }
  }
}
```

### Custom Utility Types
```typescript
// Deep readonly — immutable nested objects
type DeepReadonly<T> = {
  readonly [K in keyof T]: T[K] extends object ? DeepReadonly<T[K]> : T[K];
};

// Require exactly one of a set of keys
type RequireExactlyOne<T, Keys extends keyof T = keyof T> =
  Pick<T, Exclude<keyof T, Keys>> &
  { [K in Keys]-?: Required<Pick<T, K>> & Partial<Record<Exclude<Keys, K>, never>> }[Keys];
```

### Recommended tsconfig.json
```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "NodeNext",
    "moduleResolution": "NodeNext",
    "strict": true,
    "noUncheckedIndexedAccess": true,
    "noImplicitOverride": true,
    "exactOptionalPropertyTypes": true,
    "isolatedModules": true,
    "declaration": true,
    "declarationMap": true,
    "incremental": true,
    "skipLibCheck": false
  }
}
```

## Constraints

### MUST DO
- Enable strict mode with all compiler flags
- Use type-first API design
- Implement branded types for domain modeling
- Use `satisfies` operator for type validation
- Create discriminated unions for state machines
- Use `Annotated` pattern with type predicates
- Generate declaration files for libraries
- Optimize for type inference

### MUST NOT DO
- Use explicit `any` without justification
- Skip type coverage for public APIs
- Mix type-only and value imports
- Disable strict null checks
- Use `as` assertions without necessity
- Ignore compiler performance warnings
- Skip declaration file generation
- Use enums (prefer const objects with `as const`)
- Hardcode workflow artifact paths or filenames inline — define them in a central constants module

### No hardcoded workflow artifact paths

Any string that names a workflow-specific file, directory, or path segment (`"workspace.yaml"`, `"docs/features"`, `"tasks"`, `"tasks.md"`, `"logs"`, `"technical_skills"`, `"SKILL.md"`, etc.) **must not** appear as a literal anywhere in source code. Instead:

1. Define the string as a named export in a dedicated constants/paths module (e.g. `src/paths.ts`).
2. Import and use the named constant or path-builder helper everywhere.
3. If a new workflow artifact is introduced, add its name to `src/paths.ts` first, then use it.

**Why:** Inline literals create silent inconsistencies when the workspace layout changes — a single rename breaks multiple files with no compiler error. Named constants make the layout contract explicit and changes mechanical.

**How to apply:** Before writing any `join(root, "some-dir", "some-file")` expression, check whether `"some-dir"` or `"some-file"` is a workflow concept. If yes, look it up in `src/paths.ts` and use the appropriate constant or helper. If it isn't there yet, add it first.

## Output Templates

When implementing TypeScript features, provide:
1. Type definitions (interfaces, types, generics)
2. Implementation with type guards
3. tsconfig configuration if needed
4. Brief explanation of type design decisions

## Knowledge Reference

TypeScript 5.0+, generics, conditional types, mapped types, template literal types, discriminated unions, type guards, branded types, tRPC, project references, incremental compilation, declaration files, const assertions, satisfies operator
