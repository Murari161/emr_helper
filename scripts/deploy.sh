#!/usr/bin/env bash
# Update the online server (askeafya.chat) to the latest `main`, safely.
#
# Run by GitHub Actions after every push or merge to main (.github/workflows/
# deploy.yml), through an SSH key that may run nothing but this script. Can
# also be run by hand on the server:   bash scripts/deploy.sh
#
# The new version is built and tested first in a separate copy, in a container
# without the server's settings. So the live site never shows a half-finished
# update, and test output can never contain the server's secrets (a failed test
# once printed part of the login secret into the GitHub log). Only when the
# tests pass does the live copy switch to the new version and the app restart.
# Manuals, accounts and the question log live outside the code (data/, the
# database volume) and are not touched.
#
# Everything is inside main(), so bash reads the whole script before running
# it: the script replaces itself when the live copy switches.

main() {
  set -euo pipefail
  local live
  live="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
  local candidate="${live}-next"
  local image="askeafya-candidate"
  cd "$live"

  # The tests that need no database or manuals.
  local unit_tests=(
    tests/test_auth.py tests/test_access.py tests/test_admin.py tests/test_llm.py
    tests/test_fallback.py tests/test_figures.py tests/test_attribution.py
    tests/test_canonical.py tests/test_topics.py tests/test_callouts.py
    tests/test_welcome.py tests/test_config.py
  )

  local previous target
  previous="$(git rev-parse HEAD)"
  git fetch --quiet origin main
  target="$(git rev-parse origin/main)"
  echo "Updating ${previous:0:7} -> ${target:0:7}: $(git log -1 --format=%s "$target")"

  # 1. Build and test the new version in a separate copy. The live site keeps
  #    running the current version, untouched.
  git worktree remove --force "$candidate" >/dev/null 2>&1 || true
  rm -rf "$candidate"
  git worktree prune
  git worktree add --quiet --detach "$candidate" "$target"
  local ok=1
  if ! docker build -q -t "$image" "$candidate" >/dev/null; then
    echo "Build failed: the server stays on ${previous:0:7}."
    ok=0
  elif ! docker run --rm --network none "$image" \
      python -m pytest -p no:cacheprovider -q "${unit_tests[@]}"; then
    echo "Tests failed: the server stays on ${previous:0:7}."
    ok=0
  fi
  git worktree remove --force "$candidate" >/dev/null 2>&1 || rm -rf "$candidate"
  git worktree prune
  if [ "$ok" -ne 1 ]; then
    docker image rm -f "$image" >/dev/null 2>&1 || true
    exit 1
  fi

  # 2. Switch the live copy and restart with the new version. The build reuses
  #    what step 1 built, so it takes seconds.
  git reset --quiet --hard "$target"
  docker compose build app
  docker compose up -d --remove-orphans
  docker image rm -f "$image" >/dev/null 2>&1 || true
  docker image prune -f >/dev/null
  echo "Done: the server runs ${target:0:7}."
}

main "$@"
