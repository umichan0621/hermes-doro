#!/usr/bin/env bash
# update.sh — daily update SOP: clean-worktree check → pull → (optional) image rebuild → run.sh → verify
# Usage: ./update.sh [--rebuild-image]
set -euo pipefail
cd "$(dirname "$0")"

echo "==> Checking worktree"
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "!! Uncommitted changes in the worktree; resolve them (git stash or commit) before updating" >&2
  git status --short --untracked-files=no
  exit 1
fi

echo "==> Pulling latest code"
git pull --ff-only

if [ "${1:-}" = "--rebuild-image" ]; then
  echo "==> Rebuilding image (refresh base image)"
  ./build.sh --pull
fi

echo "==> Rebuilding container (mount list follows the current skills/)"
./run.sh

echo "==> Verifying"
docker ps --filter name=hermes-doro --format 'container status: {{.Status}}'
docker logs hermes-doro --tail 20
