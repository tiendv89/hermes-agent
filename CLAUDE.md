# Agent guidance for hermes-agent

## Slug generation for `workflow_init_feature`

When calling `workflow_init_feature` in chat and the user has not provided an explicit feature name/slug, derive one from their description:

- Extract 3–5 meaningful domain words (nouns, key verbs) from the description
- Lowercase, hyphen-join, strip special characters, keep under 100 characters
- Prefer specificity over brevity: `memory-leak-auth` over `fix-bug`
- Never include filler words (the, a, we, need, should, fix, etc.)
- If the generated name collides (backend returns a duplicate/unique-constraint error), retry once with a `-2` suffix (e.g. `thread-context-isolation-2`); if that also fails, ask the user to pick a different name

### Examples

| User description | Generated slug |
|---|---|
| "Fix thread context isolation bugs" | `thread-context-isolation-bugs` |
| "Add OAuth2 support for GitHub" | `oauth2-support-github` |
| "We need a dashboard for feature status" | `dashboard-feature-status` |
