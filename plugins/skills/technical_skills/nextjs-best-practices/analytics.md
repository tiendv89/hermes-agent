# Analytics Conventions

Apply these rules whenever instrumenting analytics events in a Next.js project.

## Event name constants

All event names must be defined in a dedicated constants file as an exported enum.

```
src/lib/analytics/events.ts   (or equivalent project path)
```

```typescript
export enum AnalyticsEvent {
  SessionStart = "session_start",
  SignUp = "sign_up",
  // ... all events
}
```

**Rules:**
- Never pass a raw string literal to `trackEvent`. Always use the enum.
- The `trackEvent` function signature must accept `AnalyticsEvent`, not `string`.
- When adding a new event, add it to the enum file first, then instrument the call site.
- If no enum file exists, create one before instrumenting any events.

## Bad

```typescript
// ❌ raw string — hard to find, easy to typo, no autocomplete
trackEvent("submit_trade", { asset, surface: "dashboard" });
```

## Good

```typescript
// ✅ enum member — discoverable, type-checked, refactor-safe
trackEvent(AnalyticsEvent.SubmitTrade, { asset, surface: "dashboard" });
```

## Grouping

Organise enum members by functional area with comments:

```typescript
export enum AnalyticsEvent {
  // Session
  SessionStart = "session_start",

  // Onboarding
  StartOnboarding = "start_onboarding",
  CompleteOnboarding = "complete_onboarding",
  SignUp = "sign_up",

  // Trade
  SubmitTrade = "submit_trade",
  ExecuteTrade = "execute_trade",
  // ...
}
```

## Review checklist

- [ ] No raw string literals passed to `trackEvent`
- [ ] New events added to the enum file before instrumentation
- [ ] `trackEvent` signature typed to `AnalyticsEvent`, not `string`
