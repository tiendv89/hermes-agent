#!/usr/bin/env bash
#
# Sync git submodules (vendor/hermes-agent).
#
# Usage:
#   scripts/sync-submodules.sh            Init + check out the commits pinned in this repo.
#                                         Safe default — run after clone/pull/branch switch.
#   scripts/sync-submodules.sh --remote   Advance each submodule to its upstream's latest
#                                         and leave the new pointer staged for you to commit.
#
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

# Pick up any URL changes from .gitmodules into .git/config.
git submodule sync --recursive

if [[ "${1:-}" == "--remote" ]]; then
  echo "Advancing submodules to upstream latest..."
  git submodule update --init --remote --recursive
  echo
  echo "Submodules now point at:"
  git submodule status
  echo
  echo "Review the changes, then commit the bumped pointer(s), e.g.:"
  echo "  git add vendor/hermes-agent && git commit -m 'chore: bump hermes-agent submodule'"
else
  git submodule update --init --recursive
  echo "Submodules checked out at the pinned commits:"
  git submodule status
fi
