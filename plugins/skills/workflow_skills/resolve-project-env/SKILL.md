---
name: resolve-project-env
description: Resolve workflow-relevant environment values from the project `.env` file before any repo, git, or SSH operation.
---

## Environment

### Required
| Variable | Description |
|---|---|
| `WORKSPACE_ROOT` | Root that contains `workflow/` and all project folders |
| `GIT_AUTHOR_NAME` | Git commit identity; used as approval actor name |
| `GIT_AUTHOR_EMAIL` | Referenced directly in `workspace.yaml` as `actor_source` |
| `GITHUB_ACCOUNT` | GitHub username or org; required for PR creation and SSH remote URLs |

> **SSH key**: not a required env var. The runtime executor sets `GIT_SSH_COMMAND` via `SSH_PRIVATE_KEY` (raw PEM) before spawning agents — `SSH_KEY_PATH` (a file path) is never read by the executor or orchestrator. In interactive sessions git uses the local SSH agent / `~/.ssh/config`.

### Required per repo (from `workspace.yaml` repos list)
| Variable pattern | Description |
|---|---|
| `<REPO_ID_UPPER>_LOCAL_PATH` | Filesystem path to the local clone of each repo |

### Optional
| Variable | Default | Description |
|---|---|---|
| `SKIP_STAGING` | `false` | Set to `true` if project has no staging environment |

Base branches are declared per-repo in `workspace.yaml -> repos[].base_branch`, not in `.env`. There is no global default.

---

## Purpose
Provide one shared environment-resolution contract for all workflow skills.

## Inputs
Optional:
- `project_root`

## Path resolution
1. If `project_root` is provided, use it
2. Otherwise use the current directory
3. Validate project root contains:
   - `workspace.yaml`
   - `CLAUDE.md`

If project root cannot be validated, require the user to provide the project folder path.

## Environment resolution
Read `<project_root>/.env` if it exists.

Resolve workflow-relevant values from `.env`, especially:
- `WORKSPACE_ROOT`
- `GIT_AUTHOR_NAME`
- `GIT_AUTHOR_EMAIL`
- `GITHUB_ACCOUNT`

Also resolve any repo local path env references used by `workspace.yaml`, for example:
- `PROJECT_A_API_LOCAL_PATH`
- `PROJECT_A_WEB_LOCAL_PATH`
- `PROJECT_A_DATA_LOCAL_PATH`

## Rules
- Do not guess missing required values
- If a required value is missing, ask the user explicitly
- Prefer project `.env` over assumptions

## Output
Return a resolved environment summary suitable for downstream workflow skills:
- validated `project_root`
- resolved `WORKSPACE_ROOT`
- resolved git identity values
- resolved repo local path values
